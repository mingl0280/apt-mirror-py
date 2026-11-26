"""Local HTTP tests for single and concurrent ranged transfers."""

from collections import defaultdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import base64
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import warnings

from apt_mirror.downloader import (
    CHUNK_SIZE, DownloadError, Downloader, categorize_downloads,
    threads_for_size,
)
from apt_mirror.planning import Download, DownloadPlan


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    data = {}
    hits = defaultdict(int)
    ranges = defaultdict(list)
    lock = threading.Lock()
    active_ranges = 0
    peak_ranges = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        if path == "/protected" and self.headers.get("Authorization") != (
                "Basic " + base64.b64encode(b"user:pass").decode()):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="mirror"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with self.lock:
            self.hits[path] += 1
            hit = self.hits[path]
        if path == "/retry" and hit == 1:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/small")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        content = self.data.get(path)
        if content is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        range_header = self.headers.get("Range")
        if range_header and path != "/no-range":
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            start, end = map(int, match.groups())
            body = content[start:end + 1]
            with self.lock:
                self.ranges[path].append((start, end))
                Handler.active_ranges += 1
                Handler.peak_ranges = max(Handler.peak_ranges,
                                          Handler.active_ranges)
            try:
                time.sleep(0.02)
                self.send_response(206)
                total = len(content) + (1 if path == "/bad-range" else 0)
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with self.lock:
                    Handler.active_ranges -= 1
            return
        if path == "/truncated":
            body = content[:len(content) // 2]
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class ProxyForTest(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        token = base64.b64encode(b"proxy:secret").decode()
        if self.headers.get("Proxy-Authorization") != f"Basic {token}":
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"through proxy"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.data = {
            "/small": b"small file",
            "/large": b"a" * (CHUNK_SIZE * 2 + 10),
            "/boundary": b"b" * CHUNK_SIZE,
            "/no-range": b"c" * (CHUNK_SIZE + 1),
            "/bad-hash": b"wrong",
            "/truncated": b"truncated content",
            "/retry": b"eventually works",
            "/bad-range": b"d" * CHUNK_SIZE,
            "/zero": b"",
            "/protected": b"private index",
            "/rate-a": b"a" * (128 * 1024),
            "/rate-b": b"b" * (128 * 1024),
        }
        Handler.hits = defaultdict(int)
        Handler.ranges = defaultdict(list)
        Handler.active_ranges = 0
        Handler.peak_ranges = 0
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = Downloader(self.root, retries=1, timeout=2,
                                 reserve_bytes=0)

    def item(self, name, kind="archive", sha256=None):
        data = Handler.data[name]
        digest = sha256 or hashlib.sha256(data).hexdigest()
        return Download(f"http://127.0.0.1:{self.server.server_port}{name}",
                        f"repo/{name.lstrip('/')}", len(data), digest, kind)

    def test_size_categories_and_boundaries(self):
        self.assertEqual(threads_for_size(CHUNK_SIZE - 1), 1)
        self.assertEqual(threads_for_size(CHUNK_SIZE), 2)
        self.assertEqual(threads_for_size(CHUNK_SIZE * 3), 3)
        self.assertEqual(threads_for_size(CHUNK_SIZE * 40), 32)
        plan = DownloadPlan((self.item("/small", "index"),
                             self.item("/boundary", "optional-index")),
                            (self.item("/large"),), (), ())
        groups = categorize_downloads(plan)
        self.assertEqual(set(groups), {
            ("index", "single"), ("optional-index", "ranged"),
            ("archive", "ranged")})

    def test_small_and_large_downloads(self):
        small = self.client.download_file(self.item("/small"))
        large = self.client.download_file(self.item("/large"))
        self.assertEqual((small.threads, large.threads), (1, 3))
        self.assertEqual(Handler.peak_ranges >= 2, True)
        self.assertEqual(len(Handler.ranges["/large"]), 4)
        self.assertEqual((self.root / "repo/large").read_bytes(),
                         Handler.data["/large"])
        current = self.client.download_file(self.item("/large"))
        self.assertEqual(current.status, "current")

    def test_exact_boundary_uses_two_ranges(self):
        result = self.client.download_file(self.item("/boundary"))
        self.assertEqual(result.threads, 2)
        self.assertEqual(len(Handler.ranges["/boundary"]), 3)

    def test_range_ignored_falls_back_to_one_request(self):
        result = self.client.download_file(self.item("/no-range"))
        self.assertEqual(result.threads, 1)
        self.assertEqual(Handler.hits["/no-range"], 1)

    def test_retry_truncation_and_hash_failure(self):
        result = self.client.download_file(self.item("/retry"))
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(Handler.hits["/retry"], 2)
        with self.assertRaises(DownloadError):
            self.client.download_file(self.item("/truncated"))
        self.assertFalse((self.root / "repo/truncated").exists())
        bad = self.item("/bad-hash", sha256="0" * 64)
        destination = self.root / bad.local_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"previous valid file")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.client.download_file(bad)
        self.assertTrue(any("SHA256 mismatch" in str(w.message)
                            for w in caught))
        self.assertEqual(destination.read_bytes(), b"wrong")
        self.assertEqual(list(destination.parent.glob("*.part")), [])

    def test_zero_redirect_and_invalid_range(self):
        zero = self.client.download_file(self.item("/zero"))
        self.assertEqual(zero.status, "downloaded")
        small = self.item("/small")
        redirected = Download(
            f"http://127.0.0.1:{self.server.server_port}/redirect",
            "repo/redirect", small.size, small.sha256, "archive")
        self.assertEqual(self.client.download_file(redirected).status,
                         "downloaded")
        with self.assertRaises(DownloadError):
            self.client.download_file(self.item("/bad-range"))

    def test_space_reserve_and_symlink_target(self):
        client = Downloader(self.root, reserve_bytes=10 ** 18)
        with self.assertRaises(DownloadError):
            client.download_file(self.item("/small"))
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks unavailable")
        item = self.item("/small")
        item = Download(item.url, "linked/escaped", item.size,
                        item.sha256, item.kind)
        with self.assertRaises(DownloadError):
            self.client.download_file(item)
        self.assertFalse((outside / "escaped").exists())

    def test_optional_404_and_required_404(self):
        missing = Download(
            f"http://127.0.0.1:{self.server.server_port}/missing",
            "repo/missing", 1, "a" * 64, "optional-index")
        plan = DownloadPlan((missing,), (), (), ())
        result = self.client.download_plan(plan)
        self.assertEqual(result[0].status, "optional-missing")
        required = Download(missing.url, missing.local_path,
                            missing.size, missing.sha256, "index")
        with self.assertRaises(DownloadError):
            self.client.download_file(required)

    def test_bounded_bootstrap_fetch(self):
        url = (f"http://127.0.0.1:{self.server.server_port}/small")
        self.assertEqual(self.client.fetch_bootstrap(url), b"small file")
        with self.assertRaises(DownloadError):
            self.client.fetch_bootstrap(url, max_bytes=2)

    def test_authentication_challenge_and_preemptive(self):
        url = (f"http://user:pass@127.0.0.1:"
               f"{self.server.server_port}/protected")
        self.assertEqual(self.client.fetch_bootstrap(url), b"private index")
        eager = Downloader(self.root, auth_no_challenge=True,
                           reserve_bytes=0)
        self.assertEqual(eager.fetch_bootstrap(url), b"private index")

    def test_rate_limit_is_shared_across_concurrent_files(self):
        limited = Downloader(self.root, limit_rate=128 * 1024,
                             max_connections=2, reserve_bytes=0)
        plan = DownloadPlan((),
                            (self.item("/rate-a"), self.item("/rate-b")),
                            (), ())
        start = time.monotonic()
        limited.download_plan(plan)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.3)

    def test_proxy_with_authentication(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyForTest)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(),
                                 thread.join()))
        client = Downloader(
            self.root, use_proxy=True,
            http_proxy=f"http://127.0.0.1:{server.server_port}",
            proxy_user="proxy", proxy_password="secret",
            reserve_bytes=0)
        self.assertEqual(client.fetch_bootstrap(
            "http://example.invalid/index"), b"through proxy")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required")
    def test_certificate_check_option(self):
        cert = self.root / "cert.pem"
        key = self.root / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-nodes", "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=localhost", "-days", "1"],
            check=True, capture_output=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(),
                                 thread.join()))
        url = f"https://127.0.0.1:{server.server_port}/small"
        with self.assertRaises(DownloadError):
            self.client.fetch_bootstrap(url)
        client = Downloader(self.root, no_check_certificate=True,
                            reserve_bytes=0)
        self.assertEqual(client.fetch_bootstrap(url), b"small file")


if __name__ == "__main__":
    unittest.main()
