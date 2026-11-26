"""Opt-in, streaming per-operation timing trace."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time


class PerfTrace:
    """Write one JSON object per timed work item without retaining events."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
        self._stream = os.fdopen(descriptor, "w", encoding="utf-8",
                                 buffering=1024 * 1024)
        self._lock = threading.Lock()
        self._started = time.perf_counter_ns()
        self._write({"event": "trace_start", "schema": 1,
                     "time_utc": datetime.now(timezone.utc).isoformat()})

    def _write(self, data):
        with self._lock:
            self._stream.write(json.dumps(data, separators=(",", ":"),
                                          sort_keys=True) + "\n")

    def record(self, name, start_ns, *, status="ok", **fields):
        end_ns = time.perf_counter_ns()
        self._write({"event": "operation", "name": name,
                     "started_ns": start_ns - self._started,
                     "duration_ns": end_ns - start_ns,
                     "status": status,
                     "thread": threading.get_ident(), **fields})

    @contextmanager
    def stage(self, name, **fields):
        start_ns = time.perf_counter_ns()
        cpu_start_ns = time.process_time_ns()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            end_ns = time.perf_counter_ns()
            self._write({"event": "stage", "name": name,
                         "started_ns": start_ns - self._started,
                         "duration_ns": end_ns - start_ns,
                         "process_cpu_ns": time.process_time_ns() - cpu_start_ns,
                         "status": status,
                         "thread": threading.get_ident(), **fields})

    def close(self, *, status="ok"):
        if self._stream.closed:
            return
        self._write({"event": "trace_end", "status": status,
                     "duration_ns": time.perf_counter_ns() - self._started,
                     "time_utc": datetime.now(timezone.utc).isoformat()})
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close(status="error" if exc_type else "ok")
