"""Bounded live HTTP smoke test for the isolated downloader deployment.

Release bytes are parsed but their signatures are not verified here. This
script tests transfer behavior only and must not publish mirror metadata.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter

from apt_mirror.downloader import Downloader
from apt_mirror.metadata import parse_release
from apt_mirror.planning import Download


REPOSITORIES = (
    ("ubuntu", "https://archive.ubuntu.com/ubuntu",
     "noble-backports", "multiverse/binary-amd64/Packages.xz",
     4 * 1024 * 1024),
    ("kali", "https://http.kali.org/kali",
     "kali-rolling", "main/binary-amd64/Packages.gz",
     32 * 1024 * 1024),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path,
                        default=Path("/tmp/apt-mirror-py-smoke"))
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--only", choices=("ubuntu", "kali"))
    args = parser.parse_args()
    client = Downloader(args.staging, timeout=20, retries=2,
                        max_connections=args.connections)
    for name, base, distribution, path, limit in REPOSITORIES:
        if args.only is not None and name != args.only:
            continue
        release_url = f"{base}/dists/{distribution}/Release"
        started = perf_counter()
        release = parse_release(
            client.fetch_bootstrap(release_url).decode("utf-8"))
        entry = release.files[path]
        if entry.size > limit:
            raise RuntimeError(
                f"{name} index grew beyond smoke limit: {entry.size}")
        item = Download(
            f"{base}/dists/{distribution}/{path}",
            f"{name}/dists/{distribution}/{path}",
            entry.size, entry.sha256, "index")
        result = client.download_file(item)
        print(json.dumps({"repository": name, "result": asdict(result),
                          "elapsed_s": round(perf_counter() - started, 3)}),
              flush=True)


if __name__ == "__main__":
    main()
