"""apt-mirror compatible workflow: acquire, scan, download, then copy."""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import bz2
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
from itertools import chain
import lzma
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import warnings
import shlex

from .config import parse_config
from .downloader import (CHUNK_SIZE, Downloader, DownloadError, MissingDownload,
                         _safe_target, _sha256, categorize_downloads,
                         threads_for_size)
from .errors import ParseError
from .metadata import Release, parse_release
from .package_index import iter_packages_file, iter_sources_file
from .planning import (Download, DownloadPlan, _download, _release_path,
                       _repository_prefix, plan_downloads)


class MirrorError(RuntimeError):
    """A run cannot complete without risking a partial published mirror."""


@dataclass(frozen=True)
class Paths:
    base: Path
    mirror: Path
    staging: Path
    state: Path


def host_architecture():
    try:
        result = subprocess.run(["dpkg", "--print-architecture"],
                                capture_output=True, text=True,
                                timeout=5, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "i386"


def _validate_settings(settings):
    supported = {"base_path", "mirror_path", "skel_path", "var_path",
                 "defaultarch", "nthreads", "run_postmirror",
                 "postmirror_script", "_contents", "_tilde",
                 "_autoclean", "cleanscript", "limit_rate",
                 "auth_no_challenge", "no_check_certificate", "unlink",
                 "use_proxy", "http_proxy", "https_proxy",
                 "proxy_user", "proxy_password", "include_dir"}
    unknown = sorted(set(settings) - supported)
    if unknown:
        raise MirrorError(f"unsupported mirror.list settings: {unknown}")
    for key in ("_contents", "run_postmirror", "_autoclean", "_tilde",
                "auth_no_challenge", "no_check_certificate", "unlink"):
        if settings.get(key, "0") not in ("0", "1"):
            raise MirrorError(f"{key} must be 0 or 1")
    if settings.get("_tilde", "0") == "1":
        raise MirrorError("_tilde 1 is not supported")


def _limit_rate(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmMgG]?)", value)
    if not match:
        raise MirrorError(f"invalid limit_rate: {value}")
    amount = float(match.group(1)) * 1024 ** (
        {"": 0, "k": 1, "m": 2, "g": 3}[match.group(2).lower()])
    return amount or None


def _expand(value, settings):
    pattern = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
    for _ in range(10):
        updated = pattern.sub(
            lambda match: settings.get(match.group(1), match.group(0)),
            value)
        if updated == value:
            break
        value = updated
    if "$" in value:
        raise MirrorError(f"unknown or recursive setting in path: {value}")
    return value


def resolve_paths(settings, *, base=None, mirror=None, staging=None,
                  state=None):
    values = dict(settings)
    values.setdefault("base_path", "/var/spool/apt-mirror")
    if base is not None:
        values["base_path"] = str(base)
    values.setdefault("mirror_path", "$base_path/mirror")
    values.setdefault("skel_path", "$base_path/skel")
    values.setdefault("var_path", "$base_path/var")
    if mirror is not None:
        values["mirror_path"] = str(mirror)
    if staging is not None:
        values["skel_path"] = str(staging)
    if state is not None:
        values["var_path"] = str(state)
    result = tuple(Path(_expand(values[key], values)).expanduser().absolute()
                   for key in ("base_path", "mirror_path", "skel_path",
                               "var_path"))
    if any(path.is_symlink() for path in result):
        raise MirrorError(
            "mirror, staging, and state roots cannot be symlinks")
    if result[1] == result[2] or result[1] == result[3]:
        raise MirrorError("mirror, staging, and state paths must differ")
    return Paths(*result)


@contextmanager
def mirror_lock(state):
    """Hold a nonblocking process lock for the entire run on Linux."""
    import fcntl

    state.mkdir(parents=True, exist_ok=True)
    with (state / "apt-mirror-py.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MirrorError("another mirror run holds the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _base_url(uri, distribution):
    base = uri.rstrip("/")
    if distribution == "./":
        return base + "/"
    if distribution.endswith("/"):
        return base + "/" + distribution
    return f"{base}/dists/{distribution}/"


def _fetch_releases(config, client, staging, perf=None):
    releases = {}
    metadata = {}
    for source in (*config.binary_sources, *config.source_sources):
        uri, distribution = source.uri, source.distribution
        if (uri, distribution) in releases:
            continue
        started = time.perf_counter_ns() if perf else 0
        base = _base_url(uri, distribution)
        files = {}
        for name in ("InRelease", "Release", "Release.gpg"):
            try:
                content = client.fetch_bootstrap(base + name, optional=True)
            except DownloadError as exc:
                if not isinstance(exc, MissingDownload):
                    warnings.warn(f"cannot fetch {base}{name}: {exc}")
                continue
            relative = _repository_prefix(uri) + "/" + _release_path(
                distribution, name)
            target = _safe_target(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".apt-mirror-release-", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            files[name] = relative
        parsed = Release({}, {})
        for name in ("Release", "InRelease"):
            if name in files:
                try:
                    parse_started = time.perf_counter_ns() if perf else 0
                    parsed = parse_release(
                        (staging / files[name]).read_text(encoding="utf-8"))
                    if perf:
                        perf.record("release_parse", parse_started,
                                    path=files[name])
                    break
                except (ParseError, UnicodeError) as exc:
                    if perf:
                        perf.record("release_parse", parse_started,
                                    path=files[name], status="error")
                    warnings.warn(f"cannot parse {base}{name}: {exc}")
        releases[(uri, distribution)] = parsed
        metadata[(uri, distribution)] = files
        if perf:
            perf.record("release_repository", started,
                        repository=_repository_prefix(uri),
                        distribution=distribution, files=len(files))
    return releases, metadata


def _manifest_connection(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE archives (path TEXT PRIMARY KEY, url TEXT, "
               "size INTEGER, sha256 TEXT)")
    return db


def _add_archive(db, item):
    row = db.execute("SELECT url,size,sha256 FROM archives WHERE path=?",
                     (item.local_path,)).fetchone()
    if row is not None:
        if row != (item.url, item.size, item.sha256):
            raise MirrorError(
                f"conflicting archive metadata for {item.local_path}")
        return
    db.execute("INSERT INTO archives VALUES (?,?,?,?)",
               (item.local_path, item.url, item.size, item.sha256))


def _index_bases(source, kind):
    components = source.components or ("",)
    for component in components:
        base = (f"{component}/binary-{source.architecture}/Packages"
                if kind == "binary" and component else
                "Packages" if kind == "binary" else
                f"{component}/source/Sources" if component else "Sources")
        yield base


def _index_candidates(source, kind):
    for base in _index_bases(source, kind):
        for extension in (".gz", ".bz2", ".xz"):
            yield base + extension


def _auxiliary_candidates(source, kind, contents):
    if kind == "source":
        for component in source.components:
            yield f"{component}/source/Release"
        return
    for component in source.components:
        yield f"{component}/binary-{source.architecture}/Release"
        yield f"{component}/i18n/Index"
        yield f"{component}/cnf/Commands-{source.architecture}.xz"
    if contents:
        for component in ("", *source.components):
            base = f"{component}/" if component else ""
            for extension in (".gz", ".bz2", ".xz"):
                yield f"{base}Contents-{source.architecture}{extension}"


def _stage_candidates(config, client, plan, include_optional=True):
    indexes = list(plan.indexes)
    seen = {item.local_path for item in indexes}
    pending = []
    entries = [(source, "binary") for source in config.binary_sources]
    entries += [(source, "source") for source in config.source_sources]
    for source, kind in entries:
        candidates = [(name, "index") for name in
                      _index_candidates(source, kind)]
        if include_optional:
            candidates += [(name, "optional-index") for name in
                           _auxiliary_candidates(
                               source, kind, config.settings.get(
                                   "_contents", "1") == "1")]
        for candidate, category in candidates:
            relative = _repository_prefix(source.uri) + "/" + _release_path(
                source.distribution, candidate)
            if relative in seen:
                continue
            seen.add(relative)
            url = _base_url(source.uri, source.distribution) + candidate
            pending.append((url, relative, category))

    def fetch(row):
        url, relative, category = row
        try:
            size = client.fetch_candidate(url, relative)
        except DownloadError as exc:
            warnings.warn(f"cannot fetch candidate {url}: {exc}")
            size = None
        return Download(url, relative, size or 0, None, category)

    with ThreadPoolExecutor(max_workers=client.max_connections) as pool:
        indexes.extend(pool.map(fetch, pending))
    # apt-mirror retains candidate index paths during cleaning even when
    # the upstream server does not currently serve them.
    return DownloadPlan(tuple(indexes), plan.archives,
                        plan.clean_prefixes, plan.skip_clean_prefixes)


def _scan_indexes(config, staging, db, perf=None):
    seen = set()
    entries = [(source, "binary") for source in config.binary_sources]
    entries += [(source, "source") for source in config.source_sources]
    for source, kind in entries:
        for base in _index_bases(source, kind):
            scanned = False
            for chosen in (base + ".gz", base + ".xz", base + ".bz2", base):
                local_path = (_repository_prefix(source.uri) + "/" +
                              _release_path(source.distribution, chosen))
                if local_path in seen:
                    scanned = True
                    break
                path = staging / local_path
                if not path.is_file():
                    continue
                records = (iter_packages_file(path) if kind == "binary" else
                           iter_sources_file(path))
                index_started = time.perf_counter_ns() if perf else 0
                record_count = 0
                file_count = 0
                db.execute("SAVEPOINT index_scan")
                try:
                    for record in records:
                        record_count += 1
                        files = (record,) if kind == "binary" else record.files
                        for file in files:
                            file_count += 1
                            relative = (file.filename if kind == "binary" else
                                        file.path)
                            item = _download(source.uri, relative, file,
                                             "archive")
                            if perf:
                                record_started = time.perf_counter_ns()
                                try:
                                    _add_archive(db, item)
                                except Exception:
                                    perf.record("manifest_record", record_started,
                                                path=item.local_path,
                                                status="error")
                                    raise
                                perf.record("manifest_record", record_started,
                                            path=item.local_path)
                            else:
                                _add_archive(db, item)
                except ParseError as exc:
                    db.execute("ROLLBACK TO index_scan")
                    if perf:
                        perf.record("index_parse", index_started,
                                    path=local_path, kind=kind,
                                    records=record_count, files=file_count,
                                    status="error")
                    warnings.warn(f"cannot parse {path}: {exc}")
                else:
                    seen.add(local_path)
                    scanned = True
                    db.execute("RELEASE index_scan")
                    if perf:
                        perf.record("index_parse", index_started,
                                    path=local_path, kind=kind,
                                    records=record_count, files=file_count)
                    break
                db.execute("RELEASE index_scan")
            if not scanned:
                warnings.warn(f"no usable index: {source.uri} "
                              f"{source.distribution} {base}")
        db.commit()
    count, size = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM archives").fetchone()
    return count, size


def _archive_rows(db):
    from .planning import Download

    cursor = db.execute(
        "SELECT url,path,size,sha256 FROM archives ORDER BY path")
    for url, path, size, sha256 in cursor:
        yield Download(url, path, size, sha256, "archive")


def _write_plan_output(path, plan, db):
    destination = Path(path).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".apt-mirror-plan-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for item in chain(plan.indexes, _archive_rows(db)):
                output.write(json.dumps({
                    "kind": item.kind,
                    "transfer": ("single" if item.size < CHUNK_SIZE else
                                 "ranged"),
                    "threads": threads_for_size(item.size),
                    "url": item.url,
                    "local_path": item.local_path,
                    "size": item.size,
                    "sha256": item.sha256,
                }) + "\n")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _install_archive(staged, target, expected_sha, unlink=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise MirrorError(f"archive destination is a symlink: {target}")
    if target.exists() and not target.is_file():
        raise MirrorError(f"archive destination is not a file: {target}")
    if target.exists() and target.stat().st_size == staged.stat().st_size:
        if not expected_sha or _sha256(target) == expected_sha:
            return False
        warnings.warn(f"SHA256 differs; overwriting {target}")
    try:
        if target.exists():
            raise OSError("replace existing archive")
        os.link(staged, target)
    except OSError:
        descriptor, name = tempfile.mkstemp(
            prefix=".apt-mirror-archive-", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copy2(staged, temporary)
            if expected_sha and _sha256(temporary) != expected_sha:
                warnings.warn(f"SHA256 mismatch; overwriting {target}")
            if unlink and target.exists():
                target.unlink()
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def _archive_downloads(db, client, mirror, perf=None):
    downloaded = 0
    already_present = 0
    failed = 0
    batch = []

    def flush():
        nonlocal downloaded, failed
        if not batch:
            return
        groups = {}
        for item in batch:
            workers = min(client.max_connections, threads_for_size(item.size))
            groups.setdefault(workers, []).append(item)

        def fetch(item):
            try:
                return client.download_file(item)
            except (DownloadError, OSError) as exc:
                warnings.warn(f"cannot fetch archive {item.url}: {exc}")
                return None

        for workers, items in groups.items():
            file_workers = max(1, client.max_connections // workers)
            with ThreadPoolExecutor(max_workers=file_workers) as pool:
                results = list(pool.map(fetch, items))
            for item, result in zip(items, results):
                if result is None:
                    failed += 1
                    continue
                staged = _safe_target(client.root, item.local_path)
                target = _safe_target(mirror, item.local_path)
                install_started = time.perf_counter_ns() if perf else 0
                installed = _install_archive(staged, target, item.sha256,
                                             client.unlink)
                if perf:
                    perf.record("archive_install", install_started,
                                path=item.local_path, bytes=item.size,
                                status="installed" if installed else "current")
                if installed:
                    downloaded += 1
        batch.clear()

    for item in _archive_rows(db):
        check_started = time.perf_counter_ns() if perf else 0
        target = _safe_target(mirror, item.local_path)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                if perf:
                    perf.record("archive_check", check_started,
                                path=item.local_path, status="unsafe")
                raise MirrorError(f"unsafe archive destination: {target}")
            if target.stat().st_size == item.size:
                # The Perl mirror uses size to decide whether an existing
                # archive needs downloading.
                already_present += 1
                if perf:
                    perf.record("archive_check", check_started,
                                path=item.local_path, status="current",
                                bytes=item.size)
                continue
        batch.append(item)
        if perf:
            perf.record("archive_check", check_started,
                        path=item.local_path, status="download_needed",
                        bytes=item.size)
        if len(batch) >= 64:
            flush()
    flush()
    return downloaded, already_present, failed


def _copy_metadata(mirror, staging, plan, metadata, perf=None):
    """Copy staged metadata into the legacy host/path tree."""

    def destination(relative):
        parent, _, filename = relative.rpartition("/")
        return _safe_target(mirror, parent) / filename

    names = {item.local_path for item in plan.indexes}
    for files in metadata.values():
        names.update(files.values())
    copied = 0
    plain_indexes = {}
    for relative in sorted(names):
        copy_started = time.perf_counter_ns() if perf else 0
        source = _safe_target(staging, relative)
        if not source.is_file():
            if perf:
                perf.record("metadata_copy", copy_started,
                            path=relative, status="missing")
            continue
        target = destination(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".apt-mirror-copy-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
                shutil.copyfileobj(stream, output)
            shutil.copystat(source, temporary)
            os.replace(temporary, target)
            copied += 1
        finally:
            temporary.unlink(missing_ok=True)
        if perf:
            perf.record("metadata_copy", copy_started,
                        path=relative, bytes=source.stat().st_size)
        for priority, suffix, opener in ((0, ".gz", gzip.open),
                                         (1, ".xz", lzma.open),
                                         (2, ".bz2", bz2.open)):
            if not relative.endswith(suffix):
                continue
            plain_name = relative[:-len(suffix)]
            if plain_name.rsplit("/", 1)[-1] not in ("Packages", "Sources"):
                break
            previous = plain_indexes.get(plain_name)
            if previous is None or priority < previous[0]:
                plain_indexes[plain_name] = (priority, source, opener)
            break
    for relative, (_, source, opener) in plain_indexes.items():
        decompress_started = time.perf_counter_ns() if perf else 0
        plain = destination(relative)
        descriptor, name = tempfile.mkstemp(
            prefix=".apt-mirror-copy-", dir=plain.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output, opener(
                    source, "rb") as stream:
                shutil.copyfileobj(stream, output)
            os.replace(temporary, plain)
            names.add(relative)
            if perf:
                perf.record("index_decompress", decompress_started,
                            path=relative, bytes=plain.stat().st_size)
        except (OSError, EOFError, lzma.LZMAError) as exc:
            if perf:
                perf.record("index_decompress", decompress_started,
                            path=relative, status="error")
            warnings.warn(f"cannot decompress {source}: {exc}")
        finally:
            temporary.unlink(missing_ok=True)
    return copied, names


def _clean_candidates(mirror, plan, db, retained=(), perf=None):
    """Find files absent from current indexes under explicit clean scopes."""
    retained = set(retained)
    for prefix in plan.clean_prefixes:
        root = _safe_target(mirror, prefix.rstrip("/"))
        if not root.is_dir() or root.is_symlink():
            continue
        for directory, subdirs, files in os.walk(root, followlinks=False):
            subdirs[:] = [name for name in subdirs
                          if not (Path(directory) / name).is_symlink()]
            for name in files:
                lookup_started = time.perf_counter_ns() if perf else 0
                path = Path(directory) / name
                if path.is_symlink():
                    if perf:
                        perf.record("cleanup_lookup", lookup_started,
                                    path=path.relative_to(mirror).as_posix(),
                                    status="symlink")
                    continue
                relative = path.relative_to(mirror).as_posix()
                if relative in retained:
                    status = "retained_metadata"
                elif any(relative.startswith(skip)
                         for skip in plan.skip_clean_prefixes):
                    status = "skip_scope"
                elif db.execute("SELECT 1 FROM archives WHERE path=?",
                                (relative,)).fetchone():
                    status = "archive_manifest"
                else:
                    status = "candidate"
                if perf:
                    perf.record("cleanup_lookup", lookup_started,
                                path=relative, status=status)
                if status != "candidate":
                    continue
                yield path


def _clean_script(paths, settings, candidates):
    value = settings.get("cleanscript", "$var_path/clean.sh")
    script = Path(_expand(value, {**settings,
                                  "base_path": str(paths.base),
                                  "var_path": str(paths.state)}))
    script.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh", "set -e", "cd " + shlex.quote(str(paths.mirror))]
    lines += ["rm -f -- " + shlex.quote(str(path.relative_to(paths.mirror)))
              for path in candidates]
    directories = {parent for path in candidates
                   for parent in path.parents if parent != paths.mirror and
                   parent.is_relative_to(paths.mirror)}
    lines += ["rmdir -- " + shlex.quote(str(path.relative_to(paths.mirror))) +
              " 2>/dev/null || true" for path in sorted(
                  directories, key=lambda item: len(item.parts), reverse=True)]
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | 0o111)
    return script


def _apply_clean(mirror, candidates, perf=None):
    for path in candidates:
        started = time.perf_counter_ns() if perf else 0
        path.unlink(missing_ok=True)
        if perf:
            perf.record("cleanup_delete", started,
                        path=path.relative_to(mirror).as_posix())
    for directory in sorted({parent for path in candidates
                             for parent in path.parents
                             if parent != mirror and parent.is_relative_to(mirror)},
                            key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return len(candidates)


def run_mirror(config_text, *, source_path=None, base=None, mirror=None,
               staging=None, state=None, workers=None, timeout=30,
               retries=4, dry_run=False, skip_postmirror=False,
               include_optional=True, plan_output=None, perf=None):
    """Run an apt-mirror compatible update and return a JSON-ready summary."""
    def stage(name):
        return perf.stage(name) if perf else nullcontext()

    with stage("parse_config"):
        preliminary = parse_config(config_text, default_arch=host_architecture(),
                                   source_path=source_path)
        default_arch = preliminary.settings.get("defaultarch", host_architecture())
        if default_arch == "<running host architecture>":
            default_arch = host_architecture()
        config = parse_config(config_text, default_arch=default_arch,
                              source_path=source_path)
        _validate_settings(config.settings)
    if workers is None:
        try:
            workers = min(32, int(config.settings.get("nthreads", "20")))
        except ValueError as exc:
            raise MirrorError("nthreads must be an integer") from exc
    with stage("prepare_paths"):
        paths = resolve_paths(config.settings, base=base, mirror=mirror,
                              staging=staging, state=state)
        paths.state.mkdir(parents=True, exist_ok=True)
        paths.staging.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            paths.mirror.mkdir(parents=True, exist_ok=True)
    with mirror_lock(paths.state):
        with tempfile.TemporaryDirectory(
                prefix=".apt-mirror-work-", dir=paths.state) as work:
            work = Path(work)
            download_root = work / "staging" if dry_run else paths.staging
            if dry_run and paths.staging.is_dir():
                with stage("copy_dry_run_staging"):
                    shutil.copytree(paths.staging, download_root,
                                    copy_function=os.link, symlinks=True)
            with stage("create_downloader"):
                client = Downloader(download_root, timeout=timeout,
                                    retries=retries, max_connections=workers,
                                    verify_existing_archives=True,
                                    limit_rate=_limit_rate(config.settings.get(
                                        "limit_rate", "100m")),
                                    use_proxy=config.settings.get(
                                        "use_proxy", "off") in ("on", "yes"),
                                    http_proxy=config.settings.get("http_proxy"),
                                    https_proxy=config.settings.get("https_proxy"),
                                    proxy_user=config.settings.get("proxy_user"),
                                    proxy_password=config.settings.get(
                                        "proxy_password"),
                                    no_check_certificate=config.settings.get(
                                        "no_check_certificate", "0") == "1",
                                    auth_no_challenge=config.settings.get(
                                        "auth_no_challenge", "0") == "1",
                                    unlink=config.settings.get("unlink", "0") == "1",
                                    perf=perf)
            with stage("fetch_releases"):
                releases, metadata = _fetch_releases(config, client,
                                                      download_root, perf=perf)
            with stage("plan_indexes"):
                plan = plan_downloads(config, releases)
            if not include_optional:
                plan = DownloadPlan(
                    tuple(item for item in plan.indexes
                          if item.kind == "index"), (),
                    plan.clean_prefixes, plan.skip_clean_prefixes)
            def fetch_index(item):
                try:
                    return client.download_file(item)
                except (OSError, ParseError, DownloadError) as exc:
                    warnings.warn(f"cannot fetch index {item.url}: {exc}")
                    return None

            with stage("fetch_indexes"):
                with ThreadPoolExecutor(max_workers=client.max_connections) as pool:
                    index_results = [result for result in
                                     pool.map(fetch_index, plan.indexes)
                                     if result is not None]
            with stage("fetch_candidates"):
                plan = _stage_candidates(config, client, plan, include_optional)
            with stage("open_manifest"):
                db = _manifest_connection(work / "archives.sqlite")
            try:
                with stage("scan_indexes"):
                    archive_count, archive_bytes = _scan_indexes(
                        config, download_root, db, perf=perf)
                summary_started = time.perf_counter_ns() if perf else 0
                summary = {
                    "repositories": len(releases),
                    "signature_verification": "not enforced",
                    "indexes": len(plan.indexes),
                    "archives": archive_count,
                    "archive_bytes": archive_bytes,
                    "index_results": {
                        status: sum(result.status == status
                                    for result in index_results)
                        for status in ("downloaded", "current",
                                       "optional-missing")},
                    "dry_run": dry_run,
                    "ignored_legacy_settings": [],
                    "categories": {
                        f"{kind}:{strategy}": len(items)
                        for (kind, strategy), items in
                        categorize_downloads(plan).items()},
                    "archive_size_categories": {
                        "single": db.execute(
                            "SELECT COUNT(*) FROM archives WHERE size < ?",
                            (CHUNK_SIZE,)).fetchone()[0],
                        "ranged": db.execute(
                            "SELECT COUNT(*) FROM archives WHERE size >= ?",
                            (CHUNK_SIZE,)).fetchone()[0]},
                }
                if perf:
                    perf.record("summarize_plan", summary_started)
                if plan_output is not None:
                    with stage("write_plan"):
                        _write_plan_output(plan_output, plan, db)
                    summary["plan_output"] = str(Path(plan_output).absolute())
                if dry_run:
                    with stage("scan_cleanup"):
                        summary["cleanup_candidates"] = sum(
                            1 for _ in _clean_candidates(paths.mirror, plan,
                                                         db, perf=perf))
                    return summary
                with stage("download_archives"):
                    downloaded, present, failed = _archive_downloads(
                        db, client, paths.mirror, perf=perf)
                with stage("publish_metadata"):
                    published, retained = _copy_metadata(
                        paths.mirror, download_root, plan, metadata, perf=perf)
                summary["archives_downloaded"] = downloaded
                summary["archives_current"] = present
                summary["archives_failed"] = failed
                summary["published_indexes"] = published
                with stage("scan_cleanup"):
                    candidates = list(_clean_candidates(
                        paths.mirror, plan, db, retained, perf=perf))
                summary["cleanup_candidates"] = len(candidates)
                with stage("write_clean_script"):
                    script = _clean_script(paths, config.settings, candidates)
                summary["cleanscript"] = str(script)
                if config.settings.get("_autoclean", "0") == "1":
                    with stage("autoclean"):
                        summary["cleaned_files"] = _apply_clean(
                            paths.mirror, candidates, perf=perf)
                if (not skip_postmirror and
                        config.settings.get("run_postmirror", "1") == "1"):
                    script = _expand(config.settings.get(
                        "postmirror_script", "$var_path/postmirror.sh"),
                        {**config.settings, "var_path": str(paths.state)})
                    command = ([script, ""] if os.access(script, os.X_OK)
                               else ["/bin/sh", script])
                    with stage("postmirror"):
                        result = subprocess.run(command, check=False)
                    summary["postmirror_exit_code"] = result.returncode
                    summary["postmirror_script"] = script
                return summary
            finally:
                with stage("close_manifest"):
                    db.close()


