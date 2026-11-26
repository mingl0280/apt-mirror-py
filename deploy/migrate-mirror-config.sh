#!/bin/sh
set -eu

# Preview by default; pass --apply after reviewing the reported counts.
PYTHONPATH="/opt/apt-mirror-py${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m apt_mirror.migrate_config "$@"
