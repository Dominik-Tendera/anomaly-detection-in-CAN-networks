"""Hardware-independent tests for generator/device time alignment."""

from __future__ import annotations

from types import SimpleNamespace
import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.time_reference import (  # noqa: E402
    TimeReferenceError,
    compute_time_scale_reference,
    record_time_reference,
)
from can_detect.dbc import DbcDatabase, MessageDefinition  # noqa: E402
from can_generate.time_reference import (  # noqa: E402
    DEFAULT_MARKER_CAN_ID,
    MARKER_END,
    MARKER_START,
    decode_marker,
    encode_marker,
)
from can_generate.truth import GroundTruthRecorder  # noqa: E402
from can_generate.traffic import ReferenceMessage, ReferenceTrafficGenerator  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TimeReferenceTests(unittest.TestCase):
    def _record(self, kind: int, generator_us: int, device_us: int, seq: int):
        return SimpleNamespace(
            frame=SimpleNamespace(
                can_id=DEFAULT_MARKER_CAN_ID,
                data=encode_marker(kind, generator_us),
            ),
            ts64=device_us,
            seq=seq,
        )

    def test_marker_payload_round_trips_start_and_end(self) -> None:
        for kind in (MARKER_START, MARKER_END):
            marker = decode_marker(encode_marker(kind, 123456))
            self.assertIsNotNone(marker)
            self.assertEqual(marker.kind, kind)
            self.assertEqual(marker.generator_time_us, 123456)

    def test_evaluation_uses_both_markers_and_records_method(self) -> None:
        records = [
            self._record(MARKER_START, 1_000_000, 1_250_000, 10),
            self._record(MARKER_END, 4_000_000, 4_250_000, 20),
        ]
        reference = compute_time_scale_reference(records)
        self.assertEqual(reference.offset_us, 250_000)
        self.assertEqual(reference.generator_to_device_us(2_000_000), 2_250_000)
        truth = record_time_reference({"session_id": "s1", "episodes": []}, reference)
        self.assertEqual(
            truth["time_reference"]["method"],
            "can_marker_mean_device_minus_generator_us",
        )
        self.assertEqual(truth["time_reference"]["markers"][0]["kind"], "start")

    def test_generator_session_emits_dedicated_start_and_end_markers(self) -> None:
        database = DbcDatabase(
            path=Path("station.dbc"),
            messages_by_id={0x120: MessageDefinition(0x120, "Status", 1, ())},
        )
        generator = ReferenceTrafficGenerator(
            database, [ReferenceMessage(0x120, 0.1, payload=b"\x01")])
        clock = FakeClock()
        sent: list[tuple[int, bytes]] = []
        run = generator.run_session(
            0.2, lambda can_id, data: sent.append((can_id, data)),
            clock=clock.monotonic, sleeper=clock.sleep)
        self.assertEqual(sent[0][0], DEFAULT_MARKER_CAN_ID)
        self.assertEqual(decode_marker(sent[0][1]).kind, MARKER_START)
        self.assertEqual(sent[-1][0], DEFAULT_MARKER_CAN_ID)
        self.assertEqual(decode_marker(sent[-1][1]).kind, MARKER_END)
        self.assertEqual(len(run.frames), 4)

        with self.assertRaises(TimeReferenceError):
            compute_time_scale_reference(
                [self._record(MARKER_START, 1, 2, 1)])

    def test_ground_truth_recorder_persists_alignment_metadata(self) -> None:
        records = [
            self._record(MARKER_START, 10, 20, 1),
            self._record(MARKER_END, 30, 40, 2),
        ]
        reference = compute_time_scale_reference(records)
        recorder = GroundTruthRecorder("s1")
        recorder.record_time_scale_reference(reference)
        payload = recorder.as_dict()
        self.assertEqual(payload["time_scale_reference"]["marker_can_id"],
                         DEFAULT_MARKER_CAN_ID)
        self.assertEqual(payload["time_scale_reference"]["generator_to_device_offset_us"], 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
