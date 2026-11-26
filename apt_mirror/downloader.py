"""Bounded HTTP downloads using Python's standard library."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
from http.client import IncompleteRead
import math
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import warnings
import base64
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

from ._validation import checksum, relative_path, repository_url
from .planning import Download, DownloadPlan


CHUNK_SIZE = 4 * 1024 * 1024
MAX_THREADS = 32
_BUFFER_SIZE = 1024 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class DownloadError(RuntimeError):
    """A transfer failed or did not match its expected metadata."""


class MissingDownload(DownloadError):
    """An optional file was not present at the server."""


class RetryableDownloadError(DownloadError):
    """A response was interrupted or did not have its promised length."""


@dataclass(frozen=True)
class DownloadResult:
    item: Download
    status: str  # downloaded, current, or optional-missing
    threads: int


def threads_for_size(size: int) -> int:
    """One worker below 4 MiB; otherwise 2–32 range workers."""
    if size < 0:
        raise ValueError("negative download size")
    if size < CHUNK_SIZE:
        return 1
    return min(MAX_THREADS, max(2, math.ceil(size / CHUNK_SIZE)))


def categorize_downloads(plan: DownloadPlan):
    """Group planned files by purpose and transfer strategy."""
    groups = {}
    for item in (*plan.indexes, *plan.archives):
        strategy = "single" if item.size < CHUNK_SIZE else "ranged"
        groups.setdefault((item.kind, strategy), []).append(item)
    return {key: tuple(items) for key, items in groups.items()}


def _validate_item(item):
    repository_url(item.url)
    relative_path(item.local_path, context="download target")
    if not isinstance(item.size, int) or item.size < 0:
        raise DownloadError(f"invalid download size: {item.size!r}")
    if item.sha256 is not None:
        checksum(item.sha256, 64, context="SHA256")
    if item.kind not in ("index", "optional-index", "archive"):
        raise DownloadError(f"invalid download kind: {item.kind!r}")


def _safe_target(root, relative):
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise DownloadError(f"symlink in download target: {path}")
    if not path.is_relative_to(root):
        raise DownloadError(f"download target outside root: {path}")
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_BUFFER_SIZE):
            digest.update(block)
    return digest.hexdigest()


class Downloader:
    """Download files into a staging root with at most 32 HTTP workers."""

    def __init__(self, root, *, timeout=30, retries=4,
                 max_connections=MAX_THREADS, reserve_bytes=256 * 1024 * 1024,
                 verify_existing_archives=False, limit_rate=None,
                 use_proxy=False, http_proxy=None, https_proxy=None,
                 proxy_user=None, proxy_password=None,
                 no_check_certificate=False, auth_no_challenge=False,
                 unlink=False, perf=None):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise DownloadError("staging root must not be a symlink")
        if timeout <= 0 or retries < 0:
            raise ValueError(
                "timeout must be positive and retries nonnegative")
        if not 1 <= max_connections <= MAX_THREADS:
            raise ValueError("max_connections must be between 1 and 32")
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be nonnegative")
        self.timeout = timeout
        self.retries = retries
        self.max_connections = max_connections
        self.reserve_bytes = reserve_bytes
        self.verify_existing_archives = verify_existing_archives
        self._budget_lock = threading.Lock()
        self._reserved_bytes = 0
        self.limit_rate = limit_rate
        self._rate_lock = threading.Lock()
        self._next_transfer = time.monotonic()
        self.auth_no_challenge = auth_no_challenge
        self.unlink = unlink
        self.perf = perf
        proxies = ({"http": http_proxy, "https": https_proxy}
                   if use_proxy else {})
        proxies = {key: value for key, value in proxies.items() if value}
        if proxy_user:
            for key, proxy in tuple(proxies.items()):
                parsed_proxy = urlsplit(proxy)
                credentials = (quote(proxy_user, safe="") + ":" +
                               quote(proxy_password or "", safe=""))
                proxies[key] = urlunsplit((parsed_proxy.scheme,
                    credentials + "@" + parsed_proxy.netloc,
                    parsed_proxy.path, parsed_proxy.query,
                    parsed_proxy.fragment))
        handlers = [ProxyHandler(proxies)]
        if no_check_certificate:
            handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
        self._opener = build_opener(*handlers)

    def _pace(self, count):
        if not self.limit_rate:
            return
        with self._rate_lock:
            now = time.monotonic()
            start = max(now, self._next_transfer)
            self._next_transfer = start + count / self.limit_rate
            delay = start - now
        if delay > 0:
            time.sleep(delay)

    def _read(self, response, count):
        count = min(count, 65536) if self.limit_rate else count
        self._pace(count)
        return response.read(count)

    def fetch_bootstrap(self, url, *, max_bytes=16 * 1024 * 1024,
                        optional=False):
        if self.perf is None:
            return self._fetch_bootstrap_impl(url, max_bytes=max_bytes,
                                              optional=optional)
        started = time.perf_counter_ns()
        status = "ok"
        try:
            return self._fetch_bootstrap_impl(url, max_bytes=max_bytes,
                                              optional=optional)
        except Exception:
            status = "error"
            raise
        finally:
            self.perf.record("bootstrap_download", started,
                             path=urlsplit(url).path, status=status)

    def _fetch_bootstrap_impl(self, url, *, max_bytes, optional):
        """Fetch bounded Release or signature bytes without validating them."""
        repository_url(url)
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        kind = "optional-index" if optional else "index"
        item = Download(url, "bootstrap", 0, "0" * 64, kind)

        def transfer():
            with self._request(item) as response:
                if response.status != 200:
                    raise DownloadError(
                        f"unexpected HTTP {response.status} for {url}")
                advertised = response.headers.get("Content-Length")
                if advertised is not None:
                    try:
                        advertised_size = int(advertised)
                    except ValueError as exc:
                        raise DownloadError(
                            "invalid bootstrap Content-Length") from exc
                    if advertised_size < 0 or advertised_size > max_bytes:
                        raise DownloadError(
                            "bootstrap metadata exceeds size limit")
                data = bytearray()
                while block := self._read(response, min(
                        _BUFFER_SIZE, max_bytes - len(data) + 1)):
                    data.extend(block)
                    if len(data) > max_bytes:
                        raise DownloadError(
                            "bootstrap metadata exceeds size limit")
                if (advertised is not None and
                        len(data) != advertised_size):
                    raise RetryableDownloadError(
                        "truncated bootstrap metadata")
                return bytes(data)

        return self._attempt(item, transfer)

    def _reserve(self, target, size):
        with self._budget_lock:
            free = shutil.disk_usage(target.parent).free
            if free - self._reserved_bytes - size < self.reserve_bytes:
                raise DownloadError(
                    f"insufficient staging space for {target}: "
                    f"need {size} bytes plus {self.reserve_bytes} reserve")
            self._reserved_bytes += size

    def _release(self, size):
        with self._budget_lock:
            self._reserved_bytes -= size

    def _request(self, item, range_header=None):
        if self.perf is None:
            return self._request_impl(item, range_header)
        started = time.perf_counter_ns()
        status = "ok"
        try:
            return self._request_impl(item, range_header)
        except Exception:
            status = "error"
            raise
        finally:
            self.perf.record("http_request", started,
                             path=item.local_path,
                             url_path=urlsplit(item.url).path,
                             kind=item.kind, range=range_header,
                             status=status)

    def _request_impl(self, item, range_header=None):
        headers = {"User-Agent": "apt-mirror-py/0.1",
                   "Accept-Encoding": "identity"}
        if range_header is not None:
            headers["Range"] = range_header
        parsed = urlsplit(item.url)
        if parsed.username and self.auth_no_challenge:
            credentials = f"{parsed.username}:{parsed.password or ''}"
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        response_url = item.url
        if parsed.username:
            response_url = urlunsplit((parsed.scheme,
                parsed.hostname + (f":{parsed.port}" if parsed.port else ""),
                parsed.path, parsed.query, parsed.fragment))
        request = Request(response_url, headers=headers)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            if (exc.code != 401 or not parsed.username or
                    self.auth_no_challenge):
                raise
            exc.close()
            credentials = f"{parsed.username}:{parsed.password or ''}"
            encoded = base64.b64encode(credentials.encode()).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
            response = self._opener.open(
                Request(response_url, headers=headers), timeout=self.timeout)
        try:
            repository_url(response.geturl())
            if (response.headers.get("Content-Encoding", "identity") !=
                    "identity"):
                raise DownloadError(
                    f"unexpected content encoding for {item.url}")
        except Exception:
            response.close()
            raise
        return response

    def _attempt(self, item, action):
        for attempt in range(self.retries + 1):
            try:
                return action()
            except HTTPError as exc:
                exc.close()
                if exc.code == 404 and item.kind == "optional-index":
                    raise MissingDownload(
                        f"optional file missing: {item.url}") from exc
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise DownloadError(
                        f"HTTP {exc.code} for {item.url}") from exc
                error = exc
            except (URLError, socket.timeout, TimeoutError,
                    ConnectionError, IncompleteRead,
                    RetryableDownloadError) as exc:
                error = exc
            if attempt == self.retries:
                raise DownloadError(
                    f"download failed after {attempt + 1} attempts: "
                    f"{item.url}: {error}") from error
            time.sleep(min(2.0, 0.25 * 2 ** attempt))

    def _copy_response(self, response, target, start, length):
        remaining = length
        with target.open("r+b", buffering=0) as output:
            output.seek(start)
            while remaining:
                block = self._read(response, min(_BUFFER_SIZE, remaining))
                if not block:
                    raise RetryableDownloadError("truncated HTTP response")
                output.write(block)
                remaining -= len(block)
            if self._read(response, 1):
                raise RetryableDownloadError(
                    "HTTP response longer than expected")

    @staticmethod
    def _check_range(response, start, end, total):
        if response.status != 206:
            raise DownloadError(
                f"range request returned HTTP {response.status}")
        match = _CONTENT_RANGE.fullmatch(
            response.headers.get("Content-Range", ""))
        if (match is None or tuple(map(int, match.groups())) !=
                (start, end, total)):
            raise DownloadError("invalid Content-Range response")

    def _single(self, item, target):
        if self.perf is None:
            return self._single_impl(item, target)
        started = time.perf_counter_ns()
        status = "ok"
        try:
            return self._single_impl(item, target)
        except Exception:
            status = "error"
            raise
        finally:
            self.perf.record("single_transfer", started,
                             path=item.local_path, bytes=item.size,
                             status=status)

    def _single_impl(self, item, target):
        def transfer():
            with self._request(item) as response:
                if response.status != 200:
                    raise DownloadError(
                        f"unexpected HTTP {response.status} for {item.url}")
                self._copy_response(response, target, 0, item.size)

        self._attempt(item, transfer)

    def _range(self, item, target, start, end):
        if self.perf is None:
            return self._range_impl(item, target, start, end)
        started = time.perf_counter_ns()
        status = "ok"
        try:
            return self._range_impl(item, target, start, end)
        except Exception:
            status = "error"
            raise
        finally:
            self.perf.record("range_transfer", started,
                             path=item.local_path, start=start, end=end,
                             bytes=end - start + 1, status=status)

    def _range_impl(self, item, target, start, end):
        def transfer():
            with self._request(item, f"bytes={start}-{end}") as response:
                self._check_range(response, start, end, item.size)
                self._copy_response(response, target, start, end - start + 1)

        self._attempt(item, transfer)

    def _large(self, item, target, workers):
        def probe():
            with self._request(item, "bytes=0-0") as response:
                if response.status == 200:
                    self._copy_response(response, target, 0, item.size)
                    return False
                self._check_range(response, 0, 0, item.size)
                self._copy_response(response, target, 0, 1)
                return True

        if not self._attempt(item, probe):
            return 1
        chunk_size = min(CHUNK_SIZE, math.ceil(item.size / workers))
        def ranges():
            for start in range(0, item.size, chunk_size):
                yield max(1, start), min(start + chunk_size, item.size) - 1

        remaining = iter(ranges())
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending = set()

            def submit_next():
                try:
                    start, end = next(remaining)
                except StopIteration:
                    return False
                pending.add(executor.submit(
                    self._range, item, target, start, end))
                return True

            for _ in range(workers * 2):
                if not submit_next():
                    break
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        future.result()
                    except Exception:
                        for other in pending:
                            other.cancel()
                        raise
                    submit_next()
        return min(workers, math.ceil(item.size / chunk_size))

    def download_file(self, item: Download) -> DownloadResult:
        if self.perf is None:
            return self._download_file_impl(item)
        started = time.perf_counter_ns()
        status = "error"
        try:
            result = self._download_file_impl(item)
            status = result.status
            return result
        finally:
            self.perf.record("file_download", started,
                             path=item.local_path, kind=item.kind,
                             bytes=item.size, status=status)

    def _download_file_impl(self, item: Download) -> DownloadResult:
        """Download and replace one staged file, warning on SHA mismatch."""
        _validate_item(item)
        self.root.mkdir(parents=True, exist_ok=True)
        target = _safe_target(self.root, item.local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _safe_target(self.root, item.local_path)
        if target.is_file() and target.stat().st_size == item.size:
            if (item.kind == "archive" and
                    not self.verify_existing_archives or
                    item.sha256 is None or
                    _sha256(target) == item.sha256):
                return DownloadResult(item, "current", 0)
            if item.sha256 is not None:
                warnings.warn(f"SHA256 differs; overwriting {target}")
        self._reserve(target, item.size)
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".apt-mirror-", suffix=".part", dir=target.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                output.truncate(item.size)
            workers = min(self.max_connections, threads_for_size(item.size))
            if item.size < CHUNK_SIZE:
                self._single(item, temporary)
                used = 1
            else:
                used = self._large(item, temporary, workers)
            if temporary.stat().st_size != item.size:
                raise DownloadError(f"download size mismatch for {item.url}")
            if item.sha256 and _sha256(temporary) != item.sha256:
                warnings.warn(f"SHA256 mismatch; overwriting {target}")
            os.replace(temporary, target)
            temporary = None
            return DownloadResult(item, "downloaded", used)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            self._release(item.size)

    def fetch_candidate(self, url, local_path):
        if self.perf is None:
            return self._fetch_candidate_impl(url, local_path)
        started = time.perf_counter_ns()
        status = "error"
        try:
            size = self._fetch_candidate_impl(url, local_path)
            status = "downloaded" if size is not None else "missing"
            return size
        finally:
            self.perf.record("candidate_download", started,
                             path=local_path, status=status)

    def _fetch_candidate_impl(self, url, local_path):
        """Try an index absent from Release, returning its staged size."""
        repository_url(url)
        relative_path(local_path)
        target = _safe_target(self.root, local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        item = Download(url, local_path, 0, None, "optional-index")

        def transfer():
            temporary = None
            try:
                descriptor, name = tempfile.mkstemp(
                    prefix=".apt-mirror-", suffix=".part", dir=target.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as output:
                    with self._request(item) as response:
                        if response.status != 200:
                            raise DownloadError(
                                f"unexpected HTTP {response.status} for {url}")
                        advertised = response.headers.get("Content-Length")
                        while block := self._read(response, _BUFFER_SIZE):
                            if (shutil.disk_usage(target.parent).free <
                                    self.reserve_bytes + len(block)):
                                raise DownloadError(
                                    f"insufficient staging space for {url}")
                            output.write(block)
                        if (advertised is not None and output.tell() !=
                                int(advertised)):
                            raise RetryableDownloadError(
                                f"truncated candidate index: {url}")
                os.replace(temporary, target)
                temporary = None
                return target.stat().st_size
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        try:
            return self._attempt(item, transfer)
        except MissingDownload:
            return None

    def download_plan(self, plan: DownloadPlan) -> tuple[DownloadResult, ...]:
        """Stage required indexes, optional indexes, then archives."""
        results = []
        groups = categorize_downloads(plan)
        for kind in ("index", "optional-index", "archive"):
            for strategy in ("single", "ranged"):
                items = groups.get((kind, strategy), ())
                buckets = {}
                for item in items:
                    workers = min(self.max_connections,
                                  threads_for_size(item.size))
                    buckets.setdefault(workers, []).append(item)
                for workers, batch in buckets.items():
                    file_workers = max(1, self.max_connections // workers)
                    with ThreadPoolExecutor(
                            max_workers=min(file_workers, len(batch))) as pool:
                        futures = [pool.submit(self.download_file, item)
                                   for item in batch]
                        for item, future in zip(batch, futures):
                            try:
                                results.append(future.result())
                            except MissingDownload:
                                if kind != "optional-index":
                                    raise
                                results.append(DownloadResult(
                                    item, "optional-missing", 0))
        return tuple(results)
