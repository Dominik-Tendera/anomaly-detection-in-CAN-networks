"""Thread-safe handoff buffer between the stream receiver and detection.

The receiver must not wait for anomaly detection.  ``RecordBuffer`` therefore
uses an unbounded queue by default: a short detector pause accumulates records
instead of blocking serial ingestion or dropping data.  ``max_occupancy`` is
updated at enqueue time and can be included in the receiver report.
"""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class BufferReport:
    """Serializable buffer counters."""

    current_occupancy: int
    max_occupancy: int
    enqueued: int
    dequeued: int

    def as_dict(self) -> dict[str, int]:
        return {
            "current_occupancy": self.current_occupancy,
            "max_occupancy": self.max_occupancy,
            "enqueued": self.enqueued,
            "dequeued": self.dequeued,
        }


class RecordBuffer(Generic[T]):
    """A lossless, thread-safe producer/consumer buffer.

    ``capacity`` is optional.  The default is intentionally unbounded because
    the receiver must remain lossless during a temporary detector pause.  A
    finite capacity can be supplied by an integrator that wants an explicit
    memory limit; in that case ``put`` raises :class:`queue.Full` rather than
    silently losing a record.
    """

    def __init__(self, capacity: Optional[int] = None) -> None:
        if capacity is not None and capacity < 1:
            raise ValueError("capacity must be greater than zero")
        self._queue: queue.Queue[T] = queue.Queue(maxsize=capacity or 0)
        self._lock = threading.Lock()
        self._max_occupancy = 0
        self._enqueued = 0
        self._dequeued = 0
        self._closed = False

    def put(self, item: T) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot put into a closed record buffer")
            # Reserve the item before publishing it to the queue.  This makes
            # peak accounting correct even when a worker consumes immediately.
            previous_max = self._max_occupancy
            self._enqueued += 1
            self._max_occupancy = max(self._max_occupancy,
                                      self._enqueued - self._dequeued)
        try:
            if self._queue.maxsize:
                self._queue.put_nowait(item)
            else:
                self._queue.put(item)
        except BaseException:
            with self._lock:
                self._enqueued -= 1
                self._max_occupancy = max(previous_max,
                                          self._enqueued - self._dequeued)
            raise

    enqueue = put

    def get(self, timeout: Optional[float] = None) -> T:
        item = self._queue.get(timeout=timeout)
        with self._lock:
            self._dequeued += 1
        return item

    def get_nowait(self) -> T:
        return self.get(timeout=0)

    def task_done(self) -> None:
        self._queue.task_done()

    def join(self) -> None:
        self._queue.join()

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def max_occupancy(self) -> int:
        with self._lock:
            return self._max_occupancy

    def report(self) -> dict[str, int]:
        with self._lock:
            return BufferReport(
                current_occupancy=self._enqueued - self._dequeued,
                max_occupancy=self._max_occupancy,
                enqueued=self._enqueued,
                dequeued=self._dequeued,
            ).as_dict()


class BufferedConsumer(Generic[T]):
    """Run a detector callback behind a ``RecordBuffer`` worker."""

    def __init__(self, consumer: Callable[[T], None],
                 buffer: Optional[RecordBuffer[T]] = None) -> None:
        self.buffer = buffer or RecordBuffer[T]()
        self._consumer = consumer
        self._stop = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run,
                                        name="can-detector-consumer",
                                        daemon=True)
        self._thread.start()

    def __call__(self, item: T) -> None:
        self.buffer.put(item)

    def _run(self) -> None:
        while not self._stop.is_set() or self.buffer.qsize():
            try:
                item = self.buffer.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._consumer(item)
            except BaseException as error:  # surface on close, do not lose it
                self._error = error
                self._stop.set()
            finally:
                self.buffer.task_done()

    def close(self) -> None:
        self.buffer.join()
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._error is not None:
            raise self._error
        self.buffer.close()

    def report(self) -> dict[str, int]:
        return self.buffer.report()


__all__ = ["BufferReport", "BufferedConsumer", "RecordBuffer"]
