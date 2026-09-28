"""Tests for live receiver persistence and detector handoff."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
RECEIVER_DIR = TOOLS_DIR.parent / "raspberry_pi/rpi_receiver"
for entry in (str(TOOLS_DIR), str(RECEIVER_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import can_stream_protocol as proto  # noqa: E402
from rpi_receiver import can_stream_rx  # noqa: E402
from rpi_receiver.record_buffer import RecordBuffer  # noqa: E402
from tests import synthetic  # noqa: E402


class FailingTraceFile(io.BytesIO):
    """File-like trace sink that fails on the selected write."""

    def __init__(self, fail_on_write: int) -> None:
        super().__init__()
        self.fail_on_write = fail_on_write
        self.write_count = 0

    def write(self, data: bytes) -> int:
        self.write_count += 1
        if self.write_count == self.fail_on_write:
            raise OSError("simulated disk full")
        return super().write(data)


class ReceiverPersistence(unittest.TestCase):
    def test_trace_is_written_before_each_record_is_handed_to_consumer(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(can_id=0x123, data=b"\x01", ts64=1_000_100)
        chunk = builder.bytes()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            observed: list[tuple[int, bytes]] = []

            def consume(record: proto.Record) -> None:
                # The callback must see the complete raw input already written.
                observed.append((record.type,
                                 (output / "live.canbin").read_bytes()))

            receiver = can_stream_rx.Receiver(
                "live", output, write_text_log=False,
                record_consumer=consume)
            try:
                receiver.feed(chunk)
            finally:
                receiver.close()

            self.assertEqual([record_type for record_type, _ in observed],
                             [proto.REC_TIME_SYNC, proto.REC_FRAME])
            self.assertTrue(all(raw == chunk for _, raw in observed))
            self.assertEqual((output / "live.canbin").read_bytes(), chunk)

    def test_trace_write_error_preserves_prior_data_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receiver = can_stream_rx.Receiver(
                "write-failure", Path(tmp), write_text_log=False)
            receiver.raw.close()
            failing = FailingTraceFile(fail_on_write=2)
            receiver.raw = failing  # type: ignore[assignment]
            try:
                self.assertTrue(receiver.feed(b"records before failure"))
                self.assertFalse(receiver.feed(b"record that cannot be saved"))
                self.assertFalse(receiver.feed(b"must not be processed"))

                report = receiver.report()
                self.assertEqual(failing.getvalue(), b"records before failure")
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["termination_reason"],
                                 "trace_write_error")
                self.assertEqual(report["trace_write_error"]["error"],
                                 "simulated disk full")
                self.assertEqual(
                    report["trace_write_error"]["bytes_received_before_error"],
                    len(b"records before failure"))
            finally:
                receiver.close()

    def test_partial_session_writes_interruption_report_with_prior_results(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(can_id=0x123, data=b"\x01", ts64=1_000_100)
        chunk = builder.bytes()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            receiver = can_stream_rx.Receiver(
                "partial", output, write_text_log=False)
            try:
                self.assertTrue(receiver.feed(chunk))
                report_path = receiver.interrupt()
            finally:
                receiver.close()

            report = __import__("json").loads(
                report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(report["termination_reason"], "user_interrupt")
            self.assertEqual(report["frames"], 1)
            self.assertEqual(report["bytes_received"], len(chunk))
            self.assertTrue((output / "partial.canbin").read_bytes() == chunk)
            self.assertFalse(receiver.feed(b"more data after interruption"))

        import itertools

        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(can_id=0x123, data=b"\x01", ts64=1_000_100)
        buffer = RecordBuffer[proto.Record]()

        with tempfile.TemporaryDirectory() as tmp:
            receiver = can_stream_rx.Receiver(
                "metrics", Path(tmp), write_text_log=False,
                record_buffer=buffer)
            receiver.performance.clock_ns = itertools.count(0, 25).__next__
            receiver.performance.memory_bytes = lambda: 123456
            try:
                receiver.feed(builder.bytes())
                report = receiver.report()
            finally:
                receiver.close()

        metrics = report["performance"]
        self.assertEqual(metrics["records_processed"], 2)
        self.assertEqual(metrics["processing_time_per_record_ns"], 25.0)
        self.assertEqual(metrics["peak_resident_memory_bytes"], 123456)
        self.assertEqual(metrics["max_input_queue_backlog"], 2)
        self.assertEqual(report["detector_buffer"]["max_occupancy"], 2)

        builder = synthetic.TraceBuilder()
        unknown_type = 0x21
        builder.raw(bytes([unknown_type]) + b"\x07\x00\xAA\xBB")
        chunk = builder.bytes()
        delivered: list[proto.Record] = []

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            receiver = can_stream_rx.Receiver(
                "unknown", output, write_text_log=False,
                record_consumer=delivered.append)
            try:
                receiver.feed(chunk)
                report = receiver.report()
            finally:
                receiver.close()

            self.assertEqual((output / "unknown.canbin").read_bytes(), chunk)
            self.assertEqual(len(delivered), 1)
            self.assertEqual(delivered[0].type, unknown_type)
            self.assertFalse(delivered[0].known)
            self.assertEqual(report["decoder"]["unknown_types"], 1)
            self.assertEqual(report["unknown_record_types"], {"0x21": 1})
            self.assertTrue(report["lossless"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
