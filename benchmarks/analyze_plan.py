"""Estimate archive transfer bytes from a JSONL mirror plan and local tree."""

import argparse
from collections import defaultdict
import json
from pathlib import Path


def analyze(plan_path, mirror_path):
    counts = defaultdict(int)
    sizes = defaultdict(int)
    by_host = defaultdict(lambda: defaultdict(int))
    mirror = Path(mirror_path)
    with Path(plan_path).open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item["kind"] != "archive":
                continue
            path = mirror / item["local_path"]
            size = item["size"]
            if not path.is_file():
                category = "missing"
            elif path.stat().st_size != size:
                category = "size_mismatch"
            else:
                category = "same_size"
            counts[category] += 1
            sizes[category] += size
            host = item["local_path"].split("/", 1)[0]
            by_host[host][category] += size
    return {
        "counts": dict(counts),
        "bytes": dict(sizes),
        "transfer_bytes_at_least": sizes["missing"] +
                                   sizes["size_mismatch"],
        "hosts": {host: dict(values) for host, values in
                  sorted(by_host.items())},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan")
    parser.add_argument("mirror")
    args = parser.parse_args()
    print(json.dumps(analyze(args.plan, args.mirror), indent=2))


if __name__ == "__main__":
    main()
