"""Read-only remote benchmark for an existing Packages and Packages.gz pair.

Run by piping this file to ``ssh host 'python3 - path.gz path mirror-root'``.
No files are created on the remote host. The Python parser below extracts only
Filename, so parser CPU timings do not represent the full project parser.
"""

from collections import OrderedDict
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter


PERL_SCAN = r'''
use strict;
use warnings;
my ($index, $root, $check) = @ARGV;
local $/ = "\n\n";
open my $stream, '<', $index or die "$index: $!";
my ($count, $found) = (0, 0);
while (my $package = <$stream>) {
    local $/ = "\n";
    chomp $package;
    my (undef, %lines) = split(/^([\w\-]+:)/m, $package);
    next unless exists $lines{'Filename:'};
    my $path = $lines{'Filename:'};
    $path =~ s/^\s+|\s+$//g;
    $found++ if $check && -f "$root/$path";
    $count++;
}
print "$count $found\n";
'''


def same_contents(compressed, plain):
    def digest(stream):
        sha = hashlib.sha256()
        count = 0
        while chunk := stream.read(1024 * 1024):
            sha.update(chunk)
            count += len(chunk)
        return count, sha.hexdigest()

    with gzip.open(compressed, "rb") as gz:
        gz_result = digest(gz)
    with plain.open("rb") as file:
        plain_result = digest(file)
    return gz_result == plain_result, gz_result[0]


def perl_scan(plain, root, check):
    started = perf_counter()
    process = subprocess.run(
        ["perl", "-e", PERL_SCAN, str(plain), str(root), str(int(check))],
        check=True, capture_output=True, text=True)
    count, found = (int(value) for value in process.stdout.split())
    return {"elapsed_s": round(perf_counter() - started, 3),
            "records": count, "existing": found}


def python_scan(compressed, root, directory_cache):
    started = perf_counter()
    check_s = 0.0
    count = 0
    found = 0
    cache = OrderedDict()

    def check(filename):
        target = root / filename
        if not directory_cache:
            return target.is_file()
        parent = target.parent
        if parent not in cache:
            try:
                with os.scandir(parent) as entries:
                    names = {entry.name for entry in entries
                             if entry.is_file(follow_symlinks=False)}
            except FileNotFoundError:
                names = set()
            cache[parent] = names
            if len(cache) > 256:
                cache.popitem(last=False)
        else:
            cache.move_to_end(parent)
        return target.name in cache[parent]

    with gzip.open(compressed, "rt", encoding="utf-8") as stream:
        filename = None
        for line in stream:
            if line.startswith("Filename:"):
                filename = line.partition(":")[2].strip()
            elif not line.strip() and filename is not None:
                begin = perf_counter()
                found += bool(check(filename))
                check_s += perf_counter() - begin
                count += 1
                filename = None
        if filename is not None:
            begin = perf_counter()
            found += bool(check(filename))
            check_s += perf_counter() - begin
            count += 1
    total = perf_counter() - started
    return {"elapsed_s": round(total, 3),
            "archive_checks_s": round(check_s, 3),
            "read_parse_s": round(total - check_s, 3),
            "records": count, "existing": found}


def main():
    compressed, plain, root = (Path(arg) for arg in sys.argv[1:4])
    same, uncompressed_bytes = same_contents(compressed, plain)
    if not same:
        raise SystemExit("plain and compressed indexes differ; aborting")
    output = {"compressed_bytes": compressed.stat().st_size,
              "plain_bytes": uncompressed_bytes,
              "record_limit": "full index",
              "cache_note": "first pass only; OS cache was not cleared"}
    for label in ("first", "warm"):
        output[label] = {
            "perl_plain_parse_only": perl_scan(plain, root, False),
            "perl_plain_with_checks": perl_scan(plain, root, True),
            "python_gzip_with_checks": python_scan(compressed, root, False),
            "python_gzip_directory_cache": python_scan(compressed, root, True),
        }
        print(json.dumps({label: output[label]}), flush=True)
    print(json.dumps({"summary": {
        key: value for key, value in output.items()
        if key not in ("first", "warm")}}), flush=True)


if __name__ == "__main__":
    main()
