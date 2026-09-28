"""Bounded performance metrics for streaming record processing.

The collector is deliberately independent of the detector and receiver so fake
clock and memory providers can be used in tests without changing production
code.  Memory is reported as resident bytes and queue backlog as a count of
records waiting for processing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import time
from typing import Callable, Optional


def resident_memory_bytes() -> int:
    """Return the process resident set size, or zero if the platform lacks it."""
    try:
        import resource
    except ImportError:
        return 0
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, while macOS reports bytes.  Raspberry Pi is Linux.
    if os.name == "posix" and usage < 1024 * 1024 * 1024:
        return int(usage) * 1024
    return int(usage)


@dataclass
class PerformanceMetrics:
    """Collect bounded timing, memory, and input-backlog measurements."""

    clock_ns: Callable[[], int] = time.perf_counter_ns
    memory_bytes: Callable[[], int] = resident_memory_bytes
    records_processed: int = 0
    total_processing_ns: int = 0
    max_processing_ns: int = 0
    peak_resident_memory_bytes: int = 0
    max_input_queue_backlog: int = 0
    _started_ns: Optional[int] = field(default=None, init=False, repr=False)

    def observe_queue_backlog(self, backlog: int) -> None:
        if isinstance(backlog, bool) or not isinstance(backlog, int) or backlog < 0:
            raise ValueError("queue backlog must be a non-negative integer")
        self.max_input_queue_backlog = max(self.max_input_queue_backlog, backlog)

    def start_record(self) -> None:
        if self._started_ns is not None:
            raise RuntimeError("a performance record is already running")
        self._started_ns = self.clock_ns()

    def finish_record(self) -> None:
        if self._started_ns is None:
            raise RuntimeError("no performance record is running")
        elapsed = max(0, self.clock_ns() - self._started_ns)
        self._started_ns = None
        self.records_processed += 1
        self.total_processing_ns += elapsed
        self.max_processing_ns = max(self.max_processing_ns, elapsed)
        self.peak_resident_memory_bytes = max(
            self.peak_resident_memory_bytes, int(self.memory_bytes()))

    def snapshot(self) -> dict[str, int | float]:
        mean_ns = (self.total_processing_ns / self.records_processed
                   if self.records_processed else 0.0)
        return {
            "records_processed": self.records_processed,
            "processing_time_per_record_ns": round(mean_ns, 3),
            "processing_time_per_record_us": round(mean_ns / 1_000, 3),
            "processing_time_total_ns": self.total_processing_ns,
            "processing_time_max_ns": self.max_processing_ns,
            "peak_resident_memory_bytes": self.peak_resident_memory_bytes,
            "max_input_queue_backlog": self.max_input_queue_backlog,
        }


__all__ = ["PerformanceMetrics", "resident_memory_bytes"]
