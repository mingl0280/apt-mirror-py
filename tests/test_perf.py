"""Individual work timings are streamed only when requested."""

import json
from pathlib import Path
import tempfile
import unittest

from apt_mirror.perf import PerfTrace


class PerfTraceTests(unittest.TestCase):
    def test_individual_events_and_exclusive_file(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            path = Path(temporary) / "trace.jsonl"
            with PerfTrace(path) as trace:
                with trace.stage("scan_indexes"):
                    trace.record("index_parse", trace._started,
                                 path="repo/dists/stable/Packages.gz",
                                 records=3)
                    trace.record("index_parse", trace._started,
                                 path="repo/dists/testing/Packages.gz",
                                 records=5)
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([event["event"] for event in events],
                             ["trace_start", "operation", "operation",
                              "stage", "trace_end"])
            self.assertEqual([event["path"] for event in events[1:3]],
                             ["repo/dists/stable/Packages.gz",
                              "repo/dists/testing/Packages.gz"])
            self.assertGreaterEqual(events[1]["duration_ns"], 0)
            self.assertIn("thread", events[1])
            with self.assertRaises(FileExistsError):
                PerfTrace(path)


if __name__ == "__main__":
    unittest.main()
