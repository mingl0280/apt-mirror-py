"""Command-line entry point for apt-mirror compatible updates."""

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys

from .downloader import DownloadError
from .errors import ParseError
from .runner import MirrorError, run_mirror
from .perf import PerfTrace


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="apt-mirror-py",
        description="Download and publish selected APT repositories")
    parser.add_argument("config_file", nargs="?", type=Path,
                        help="apt-mirror style mirror.list")
    parser.add_argument("--config", "--configs", dest="config", type=Path,
                        help="mirror.list (default: /etc/apt/mirror.list)")
    parser.add_argument("--base-path", type=Path)
    parser.add_argument("--mirror-path", type=Path)
    parser.add_argument("--staging-path", type=Path)
    parser.add_argument("--state-path", type=Path)
    parser.add_argument("--workers", type=int,
                        help="maximum simultaneous HTTP requests (1-32)")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true",
                        help="scan candidate indexes without publishing")
    parser.add_argument("--plan-output", type=Path,
                        help="write categorized planned files as JSON Lines")
    parser.add_argument("--perf", type=Path, metavar="TRACE.jsonl",
                        help="write individual work-item timings as JSON Lines")
    parser.add_argument("--skip-postmirror", action="store_true")
    parser.add_argument("--skip-optional-indexes", action="store_true")
    args = parser.parse_args(argv)
    try:
        config_path = args.config or args.config_file or Path(
            "/etc/apt/mirror.list")
        with (PerfTrace(args.perf) if args.perf else nullcontext(None)) as perf:
            with (perf.stage("read_config") if perf else nullcontext()):
                config_text = config_path.read_text(encoding="utf-8")
            result = run_mirror(
                config_text, source_path=config_path,
                base=args.base_path,
                mirror=args.mirror_path, staging=args.staging_path,
                state=args.state_path, workers=args.workers,
                timeout=args.timeout, retries=args.retries,
                dry_run=args.dry_run,
                skip_postmirror=args.skip_postmirror,
                include_optional=not args.skip_optional_indexes,
                plan_output=args.plan_output, perf=perf)
            if perf:
                result["performance_trace"] = str(perf.path)
    except (OSError, ValueError, ParseError, MirrorError, DownloadError,
            subprocess.CalledProcessError) as exc:
        print(f"apt-mirror-py: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
