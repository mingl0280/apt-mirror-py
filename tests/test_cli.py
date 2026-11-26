"""CLI config selection stays compatible with apt-mirror."""

from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from apt_mirror.__main__ import main


class CliTests(unittest.TestCase):
    def test_perf_path_is_opt_in_and_written(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            trace_path = Path(temporary) / "perf.jsonl"
            with patch.object(Path, "read_text", autospec=True,
                              return_value=""), \
                    patch("apt_mirror.__main__.run_mirror",
                          return_value={"ok": True}) as run, \
                    patch("sys.stdout", new_callable=StringIO) as output:
                self.assertEqual(main(["--perf", str(trace_path)]), 0)
            self.assertIsNotNone(run.call_args.kwargs["perf"])
            self.assertEqual(json.loads(output.getvalue())
                             ["performance_trace"], str(trace_path))
            events = [json.loads(line)
                      for line in trace_path.read_text().splitlines()]
            self.assertEqual(events[0]["event"], "trace_start")
            self.assertEqual(events[-1]["event"], "trace_end")
            self.assertIn("read_config", [event.get("name") for event in events])

    def test_default_config_path(self):
        with patch.object(Path, "read_text", autospec=True,
                          return_value="") as read, \
                patch("apt_mirror.__main__.run_mirror",
                      return_value={"ok": True}), \
                patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(main([]), 0)
        self.assertEqual(read.call_args.args, (Path("/etc/apt/mirror.list"),))
        self.assertEqual(read.call_args.kwargs, {"encoding": "utf-8"})

    def test_configs_alias(self):
        with patch.object(Path, "read_text", autospec=True,
                          return_value="") as read, \
                patch("apt_mirror.__main__.run_mirror",
                      return_value={"ok": True}), \
                patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(main(["--configs", "custom.list"]), 0)
        self.assertEqual(read.call_args.args,
                         (Path("custom.list"),))


if __name__ == "__main__":
    unittest.main()
