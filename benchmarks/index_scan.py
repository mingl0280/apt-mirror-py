"""Time compressed-index scanning and archive existence checks.

This isolates index processing. It does not benchmark network downloads,
signature verification, or publication. First pass is not a guaranteed
OS-cache-cold run.
"""

import argparse
from collections import OrderedDict
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

from apt_mirror.package_index import iter_packages_file


def make_index(path, count):
    """Generate deterministic Packages data with distinct archive paths."""
    started = perf_counter()
    with gzip.open(path, "wb", compresslevel=6) as output:
        for number in range(count):
            digest = hashlib.sha256(str(number).encode("ascii")).hexdigest()
            record = (f"Package: synthetic-{number}\n"
                      f"Filename: pool/{number % 256:02x}/"
                      f"synthetic-{number}_{digest[:8]}_amd64.deb\n"
                      f"Size: {number % 100000}\n"
                      f"SHA256: {digest}\n\n")
            output.write(record.encode("ascii"))
    return perf_counter() - started


def scan(path, mirror_root, *, directory_cache=False):
    started = perf_counter()
    records = iter_packages_file(path)
    count = 0
    found = 0
    parse_elapsed = 0.0
    stat_elapsed = 0.0
    cache = OrderedDict()
    while True:
        begin = perf_counter()
        try:
            record = next(records)
        except StopIteration:
            break
        parse_elapsed += perf_counter() - begin
        begin = perf_counter()
        target = mirror_root / record.filename
        if directory_cache:
            parent = target.parent
            if parent not in cache:
                try:
                    with os.scandir(parent) as listing:
                        names = {entry.name: entry.is_file(
                            follow_symlinks=False) for entry in listing}
                except FileNotFoundError:
                    names = {}
                cache[parent] = names
                if len(cache) > 256:
                    cache.popitem(last=False)
            else:
                cache.move_to_end(parent)
            exists = cache[parent].get(target.name, False)
        else:
            exists = target.is_file()
        if exists:
            found += 1
        stat_elapsed += perf_counter() - begin
        count += 1
    return {"total_s": round(perf_counter() - started, 3),
            "parse_s": round(parse_elapsed, 3),
            "archive_checks_s": round(stat_elapsed, 3),
            "records": count, "existing_archives": found}


def decompress(source, target, max_bytes):
    started = perf_counter()
    with gzip.open(source, "rb") as compressed, target.open("wb") as plain:
        total = 0
        while chunk := compressed.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError("decompressed index exceeds disk budget")
            plain.write(chunk)
    return perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path,
                        help="existing Packages.gz to scan")
    parser.add_argument("--mirror-root", type=Path,
                        help="archive root for read-only existence checks")
    parser.add_argument("--records", type=int, default=120000,
                        help="synthetic package count when --index is absent")
    args = parser.parse_args()
    if args.records < 1:
        parser.error("--records must be positive")
    if args.index and not args.index.is_file():
        parser.error("--index must be an existing file")
    if args.index and args.index.suffix != ".gz":
        parser.error("this benchmark currently expects Packages.gz")
    if args.mirror_root and not args.mirror_root.is_dir():
        parser.error("--mirror-root must be a directory")

    # Keep generated and decompressed data in the workspace so the benchmark
    # runs under the same sandbox permissions as the project tests.
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
        work = Path(temporary)
        source = args.index or work / "Packages.gz"
        generation_s = None
        if args.index is None:
            generation_s = round(make_index(source, args.records), 3)
        mirror_root = args.mirror_root or work / "mirror"
        if args.mirror_root is None:
            mirror_root.mkdir()
            if args.index is None:
                for number in range(256):
                    (mirror_root / "pool" / f"{number:02x}").mkdir(
                        parents=True)
        available = shutil.disk_usage(work).free
        max_plain_bytes = min(1024 ** 3, available // 10)
        result = {"source": str(source), "compressed_bytes":
                  source.stat().st_size, "generation_s": generation_s,
                  "mirror_root": str(mirror_root),
                  "cache_note": "first pass only; OS cache was not cleared"}
        for label in ("first", "warm"):
            direct = scan(source, mirror_root)
            cached = scan(source, mirror_root, directory_cache=True)
            plain = work / "Packages"
            unpack_s = decompress(source, plain, max_plain_bytes)
            materialized = scan(plain, mirror_root)
            result[label] = {
                "stream": direct,
                "stream_directory_cache": cached,
                "materialized": {
                    "decompress_to_disk_s": round(unpack_s, 3),
                    "plain_bytes": plain.stat().st_size,
                    "scan": materialized,
                    "total_s": round(unpack_s + materialized["total_s"], 3),
                },
            }
            plain.unlink()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
