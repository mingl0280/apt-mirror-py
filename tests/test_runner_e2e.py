"""End-to-end compatibility checks against a local unsigned repository."""

import gzip
import hashlib
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import warnings

from apt_mirror.runner import run_mirror
from apt_mirror.perf import PerfTrace


@unittest.skipUnless(sys.platform == "linux", "Linux flock and shell required")
class RunnerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()

        class QuietHandler(SimpleHTTPRequestHandler):
            def __init__(handler, *args, **kwargs):
                super().__init__(*args, directory=str(self.root), **kwargs)

            def log_message(handler, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}/repo"
        self.config = (f"set base_path {self.root / 'work'}\n"
                       f"deb {self.base} test main\n")
        self.prefix = self.root / "work/mirror/127.0.0.1/repo"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _write_package(self, name="tiny_1_all.deb", content=b"archive",
                       *, release=True, sha=False, flat="dists/test/"):
        archive = self.repository / "pool" / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(content)
        binary = self.repository / flat / "main/binary-amd64"
        binary.mkdir(parents=True, exist_ok=True)
        rows = (f"Package: tiny\nVersion: 1\nFilename: pool/{name}\n"
                f"Size: {len(content)}\n")
        if sha:
            rows += f"SHA256: {hashlib.sha256(content).hexdigest()}\n"
        data = gzip.compress((rows + "\n").encode())
        (binary / "Packages.gz").write_bytes(data)
        if release:
            (self.repository / flat / "Release").write_text(
                "Suite: test\nValid-Until: Mon, 01 Jan 2001 00:00:00 UTC\n"
                "MD5Sum:\n " + hashlib.md5(data).hexdigest() +
                f" {len(data)} main/binary-amd64/Packages.gz\n")

    def test_perf_trace_records_individual_work(self):
        self._write_package()
        trace_path = self.root / "perf.jsonl"
        with PerfTrace(trace_path) as trace:
            run_mirror(self.config + f"clean {self.base}\n",
                       include_optional=False, skip_postmirror=True,
                       perf=trace)
        events = [json.loads(line)
                  for line in trace_path.read_text().splitlines()]
        names = [event.get("name") for event in events]
        for name in ("fetch_releases", "fetch_indexes", "scan_indexes",
                     "download_archives", "publish_metadata", "scan_cleanup",
                     "http_request", "index_parse", "manifest_record",
                     "archive_check", "file_download", "metadata_copy",
                     "cleanup_lookup"):
            self.assertIn(name, names)
        self.assertTrue(any(event.get("name") == "file_download" and
                            event.get("kind") == "archive" and
                            event.get("status") == "downloaded"
                            for event in events))

    def test_unsigned_expired_and_missing_release(self):
        self._write_package()
        first = run_mirror(self.config, include_optional=False,
                           skip_postmirror=True)
        self.assertEqual(first["archives_downloaded"], 1)
        self.assertEqual(first["signature_verification"], "not enforced")
        self.assertEqual((self.prefix / "pool/tiny_1_all.deb").read_bytes(),
                         b"archive")
        self.assertTrue((self.prefix /
                         "dists/test/main/binary-amd64/Packages").is_file())
        self.assertTrue((self.prefix / "dists/test/Release").is_file())
        again = run_mirror(self.config, include_optional=False,
                           skip_postmirror=True)
        self.assertEqual(again["archives_current"], 1)
        (self.repository / "dists/test/Release").unlink()
        self._write_package("tiny_2_all.deb", b"new", release=False)
        fallback = run_mirror(self.config, include_optional=False,
                              skip_postmirror=True)
        self.assertEqual(fallback["archives_downloaded"], 1)
        self.assertTrue((self.prefix / "pool/tiny_2_all.deb").is_file())

    def test_flat_nested_path_and_cleanup_defaults(self):
        flat = "flat/path/"
        self._write_package(flat=flat)
        config = (f"set base_path {self.root / 'flat-work'}\n"
                  "set _autoclean 1\nset run_postmirror 0\n"
                  f"deb {self.base} {flat} main\n"
                  f"clean {self.base}/\n")
        mirror = self.root / "flat-work/mirror/127.0.0.1/repo"
        preview = run_mirror(config, dry_run=True, include_optional=False)
        self.assertEqual(preview["archives"], 1)
        self.assertFalse(mirror.exists())
        result = run_mirror(config, include_optional=False)
        self.assertEqual(result["archives_downloaded"], 1)
        self.assertTrue((mirror / "flat/path/main/binary-amd64/Packages.gz")
                        .is_file())
        stale = mirror / "pool/stale.deb"
        stale.write_bytes(b"stale")
        cleaned = run_mirror(config, include_optional=False)
        self.assertGreaterEqual(cleaned["cleaned_files"], 1)
        self.assertFalse(stale.exists())
        self.assertTrue((self.root / "flat-work/var/clean.sh").is_file())

    def test_flat_dot_without_release_or_components(self):
        archive = self.repository / "pool/flat.deb"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"flat")
        (self.repository / "Packages.gz").write_bytes(gzip.compress(
            b"Package: flat\nFilename: pool/flat.deb\nSize: 4\n\n"))
        config = (f"set base_path {self.root / 'dot-work'}\n"
                  f"deb {self.base} ./\n")
        summary = run_mirror(config, skip_postmirror=True)
        prefix = self.root / "dot-work/mirror/127.0.0.1/repo"
        self.assertEqual(summary["archives_downloaded"], 1)
        self.assertEqual((prefix / "pool/flat.deb").read_bytes(), b"flat")
        self.assertTrue((prefix / "Packages.gz").is_file())

    def test_default_postmirror_runs_generated_clean_script(self):
        self._write_package()
        script = self.root / "work/var/postmirror.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n\"$(dirname \"$0\")/clean.sh\"\n",
                          encoding="utf-8")
        config = self.config + f"clean {self.base}/\n"
        run_mirror(config, include_optional=False)
        stale = self.prefix / "pool/stale.deb"
        stale.write_bytes(b"stale")
        summary = run_mirror(config, include_optional=False)
        self.assertEqual(summary["postmirror_exit_code"], 0)
        self.assertFalse(stale.exists())

    def test_same_size_reuse_and_download_sha_warning(self):
        self._write_package(content=b"correct", sha=True)
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        target = self.prefix / "pool/tiny_1_all.deb"
        target.write_bytes(b"corrupt")
        reused = run_mirror(self.config, include_optional=False,
                            skip_postmirror=True)
        self.assertEqual(reused["archives_current"], 1)
        self.assertEqual(target.read_bytes(), b"corrupt")
        target.write_bytes(b"x")
        (self.repository / "pool/tiny_1_all.deb").write_bytes(b"wrong!!")
        (self.root / "work/skel/127.0.0.1/repo/pool/tiny_1_all.deb").unlink()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            summary = run_mirror(self.config, include_optional=False,
                                 skip_postmirror=True)
        self.assertEqual(summary["archives_downloaded"], 1)
        self.assertEqual(target.read_bytes(), b"wrong!!")
        self.assertTrue(any("SHA256 mismatch" in str(w.message)
                            for w in caught))

    def test_release_refresh_does_not_modify_hardlinked_source(self):
        self._write_package()
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        staged = (self.root / "work/skel/127.0.0.1/repo/dists/test/Release")
        original = staged.read_bytes()
        sentinel = self.root / "original-release"
        os.link(staged, sentinel)
        upstream = self.repository / "dists/test/Release"
        upstream.write_bytes(upstream.read_bytes().replace(
            b"Suite: test", b"Suite: changed"))
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        self.assertEqual(sentinel.read_bytes(), original)
        self.assertIn(b"Suite: changed", staged.read_bytes())

    def test_missing_archive_warns_and_continues(self):
        self._write_package()
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        self._write_package("tiny_2_all.deb", b"new archive")
        (self.repository / "pool/tiny_2_all.deb").unlink()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            summary = run_mirror(self.config, include_optional=False,
                                 skip_postmirror=True, retries=0)
        self.assertEqual(summary["archives_failed"], 1)
        self.assertTrue(any("cannot fetch archive" in str(w.message)
                            for w in caught))
        self.assertTrue((self.prefix /
                         "dists/test/main/binary-amd64/Packages.gz").is_file())

    def test_contents_stays_compressed_like_perl_mirror(self):
        self._write_package()
        contents = gzip.compress(b"large contents index\n")
        path = self.repository / "dists/test/Contents-amd64.gz"
        path.write_bytes(contents)
        release = self.repository / "dists/test/Release"
        with release.open("a", encoding="utf-8") as output:
            output.write(f" {hashlib.md5(contents).hexdigest()} "
                         f"{len(contents)} Contents-amd64.gz\n")
        run_mirror(self.config, skip_postmirror=True)
        published = self.prefix / "dists/test"
        self.assertEqual((published / "Contents-amd64.gz").read_bytes(),
                         contents)
        self.assertFalse((published / "Contents-amd64").exists())

    def test_published_file_replaces_final_symlink_without_following(self):
        self._write_package()
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        target = (self.prefix /
                  "dists/test/main/binary-amd64/Packages.gz")
        outside = self.root / "outside-index"
        outside.write_bytes(b"keep outside")
        target.unlink()
        target.symlink_to(outside)
        run_mirror(self.config, include_optional=False,
                   skip_postmirror=True)
        self.assertFalse(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b"keep outside")


if __name__ == "__main__":
    unittest.main()
