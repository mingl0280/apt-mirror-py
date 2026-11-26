"""Print staging paths selected by the sample mirror.list (one per line)."""

from pathlib import Path
from urllib.parse import urlsplit

from apt_mirror.config import parse_config


config = parse_config(Path("sample-source/mirror.list").read_text(),
                      default_arch="amd64")
paths = set()
for source, kind in (
        [(source, "Packages") for source in config.binary_sources] +
        [(source, "Sources") for source in config.source_sources]):
    parsed = urlsplit(source.uri)
    base = f"/sources/skel/{parsed.netloc}{parsed.path.rstrip('/')}"
    if source.components:
        for component in source.components:
            relative = (f"{component}/binary-{source.architecture}/Packages"
                        if kind == "Packages" else
                        f"{component}/source/Sources")
            paths.add(f"{base}/dists/{source.distribution}/{relative}")
    else:
        paths.add(f"{base}/{source.distribution}/{kind}")
for path in sorted(paths):
    print(path)
