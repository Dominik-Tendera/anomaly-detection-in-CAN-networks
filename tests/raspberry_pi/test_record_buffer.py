"""Deterministic tests for the receiver-to-detector handoff buffer."""

from __future__ import annotations

import queue
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
RECEIVER_DIR = TOOLS_DIR.parent / "raspberry_pi/rpi_receiver"
for entry in (str(TOOLS_DIR), str(RECEIVER_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from rpi_receiver.record_buffer import RecordBuffer  # noqa: E402
from rpi_receiver import can_stream_rx  # noqa: E402
from tests import synthetic  # noqa: E402


class RecordBufferTests(unittest.TestCase):
    def test_pause_accumulates_records_and_reports_peak(self) -> None:
        buffer = RecordBuffer[int]()
        for value in range(5):
            buffer.put(value)

        self.assertEqual(buffer.qsize(), 5)
        self.assertEqual(buffer.max_occupancy, 5)
        self.assertEqual([buffer.get_nowait() for _ in range(5)], list(range(5)))
        self.assertEqual(buffer.report(), {
            "current_occupancy": 0,
            "max_occupancy": 5,
            "enqueued": 5,
            "dequeued": 5,
        })

    def test_finite_capacity_fails_explicitly_instead_of_dropping(self) -> None:
        buffer = RecordBuffer[int](capacity=2)
        buffer.put(1)
        buffer.put(2)
        with self.assertRaises(queue.Full):
            buffer.put(3)
        self.assertEqual([buffer.get_nowait(), buffer.get_nowait()], [1, 2])
        self.assertEqual(buffer.max_occupancy, 2)

    def test_receiver_reports_buffer_peak(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        for index in range(3):
            builder.frame(can_id=0x100 + index, data=b"\x01",
                          ts64=1_000_100 + index)

        with tempfile.TemporaryDirectory() as tmp:
            buffer = RecordBuffer()
            receiver = can_stream_rx.Receiver(
                "buffered", Path(tmp), write_text_log=False,
                record_buffer=buffer)
            try:
                receiver.feed(builder.bytes())
                report = receiver.report()
            finally:
                receiver.close()

        self.assertEqual(report["detector_buffer"]["max_occupancy"], 4)
        self.assertEqual(report["detector_buffer"]["current_occupancy"], 4)
        self.assertEqual(report["detector_buffer"]["enqueued"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
