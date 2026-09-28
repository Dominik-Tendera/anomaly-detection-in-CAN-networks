"""Hardware-free tests for reference CAN traffic scheduling."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.dbc import DbcDatabase, MessageDefinition, SignalDefinition  # noqa: E402
from can_generate.traffic import (  # noqa: E402
    ReferenceMessage,
    ReferenceTrafficGenerator,
    TrafficConfigurationError,
)
from can_generate.episodes import AnomalyEpisode, AnomalyType  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingSender:
    def __init__(self, clock: FakeClock, send_time: float = 0.0) -> None:
        self.clock = clock
        self.send_time = send_time
        self.frames: list[tuple[float, int, bytes]] = []

    def send(self, can_id: int, data: bytes) -> None:
        self.frames.append((self.clock.now, can_id, data))
        self.clock.now += self.send_time


class ReferenceTrafficTests(unittest.TestCase):
    def setUp(self) -> None:
        signal = SignalDefinition("speed", 0, 8, 0.0, 100.0, "km/h")
        self.database = DbcDatabase(
            path=Path("station.dbc"),
            messages_by_id={
                0x120: MessageDefinition(0x120, "Status", 2, (signal,)),
                0x121: MessageDefinition(0x121, "Other", 1, ()),
            },
        )

    def test_signal_values_are_encoded_and_remain_in_dbc_range(self) -> None:
        generator = ReferenceTrafficGenerator(
            self.database,
            [ReferenceMessage(0x120, 0.1, signal_values={"speed": 42.0})],
        )
        sender = RecordingSender(FakeClock())
        result = generator.run(0.01, sender, clock=sender.clock.monotonic,
                               sleeper=sender.clock.sleep)

        self.assertEqual(result.frames[0].data, bytes([42, 0]))
        self.assertEqual(self.database.decode_signals(0x120, result.frames[0].data),
                         {"speed": 42.0})

    def test_payload_and_identifier_must_match_selected_dbc(self) -> None:
        with self.assertRaises(TrafficConfigurationError):
            ReferenceTrafficGenerator(
                self.database, [ReferenceMessage(0x120, 0.1, payload=b"\x00")]
            )
        with self.assertRaises(TrafficConfigurationError):
            ReferenceTrafficGenerator(
                self.database, [ReferenceMessage(0x7FF, 0.1, payload=b"\x00")]
            )
        with self.assertRaises(TrafficConfigurationError):
            ReferenceTrafficGenerator(
                self.database,
                [ReferenceMessage(0x120, 0.1, signal_values={"speed": 101.0})],
            )

    def test_schedule_uses_absolute_deadlines_without_send_time_drift(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(clock, send_time=0.03)
        generator = ReferenceTrafficGenerator(
            self.database, [ReferenceMessage(0x121, 0.1, payload=b"\xAA")]
        )

        result = generator.run(0.36, sender, clock=clock.monotonic,
                               sleeper=clock.sleep)

        self.assertEqual([round(frame.elapsed, 6) for frame in result.frames],
                         [0.0, 0.1, 0.2, 0.3])
        self.assertEqual([frame[1] for frame in sender.frames], [0x121] * 4)
        self.assertEqual([round(wait, 6) for wait in clock.sleeps],
                         [0.07, 0.07, 0.07])

    def test_mapping_configuration_accepts_hex_payload(self) -> None:
        generator = ReferenceTrafficGenerator.from_config(
            self.database,
            [{"can_id": 0x121, "period": 0.5, "payload": "aa"}],
        )
        sender = RecordingSender(FakeClock())
        run = generator.run(0.01, sender, clock=sender.clock.monotonic,
                            sleeper=sender.clock.sleep)
        self.assertEqual(run.frames[0].data, b"\xAA")

    def test_report_counts_frames_missed_when_sender_falls_behind(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(clock, send_time=0.15)
        generator = ReferenceTrafficGenerator(
            self.database, [ReferenceMessage(0x121, 0.1, payload=b"\xAA")]
        )

        result = generator.run(0.36, sender, clock=clock.monotonic,
                               sleeper=clock.sleep)

        self.assertEqual(result.frames_requested, 4)
        self.assertEqual(result.frames_sent, 3)
        self.assertEqual(result.frames_not_sent, 1)
        self.assertEqual(result.requested_rate, 4 / 0.36)
        self.assertEqual(result.achieved_rate, 3 / 0.36)
        self.assertEqual(result.warnings, ())

    def test_warns_when_requested_dlc8_rate_exceeds_host_link_capacity(self) -> None:
        clock = FakeClock()
        sender = RecordingSender(clock)
        capacity_database = DbcDatabase(
            path=Path("station.dbc"),
            messages_by_id={0x122: MessageDefinition(0x122, "Payload", 8, ())},
        )
        generator = ReferenceTrafficGenerator(
            capacity_database,
            [ReferenceMessage(0x122, 0.001, payload=b"\x00" * 8)],
        )

        result = generator.run(1.0, sender, clock=clock.monotonic,
                               sleeper=clock.sleep)

        self.assertGreater(result.requested_rate, 520)
        self.assertEqual(result.frames_requested, 1000)
        self.assertEqual(result.frames_not_sent, 0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("520 frames/s", result.warnings[0])

    def _run_anomaly(self, episode: AnomalyEpisode, duration: float = 1.0):
        generator = ReferenceTrafficGenerator(
            self.database, [ReferenceMessage(0x120, 0.2, signal_values={"speed": 42})]
        )
        sender = RecordingSender(FakeClock())
        return generator.run(duration, sender, anomalies=[episode],
                             clock=sender.clock.monotonic, sleeper=sender.clock.sleep)

    def test_frequency_episodes_change_only_selected_message_rate(self) -> None:
        increased = self._run_anomaly(AnomalyEpisode(
            AnomalyType.FREQUENCY_INCREASE, 0.2, 0.6, 0x120,
            {"frequency_factor": 2},
        ))
        decreased = self._run_anomaly(AnomalyEpisode(
            AnomalyType.FREQUENCY_DECREASE, 0.2, 0.6, 0x120,
            {"frequency_factor": 0.5},
        ))
        self.assertEqual(increased.counts_by_id[0x120], 8)
        self.assertEqual(decreased.counts_by_id[0x120], 4)

    def test_disappearance_removes_frames_inside_episode(self) -> None:
        run = self._run_anomaly(AnomalyEpisode(
            AnomalyType.DISAPPEARANCE, 0.2, 0.6, 0x120,
        ))
        self.assertEqual([round(frame.elapsed, 3) for frame in run.frames], [0.0, 0.8])

    def test_burst_is_parameterized_and_uses_reference_payload(self) -> None:
        run = self._run_anomaly(AnomalyEpisode(
            AnomalyType.BURST, 0.2, 0.3, 0x120,
            {"count": 3, "interval": 0.05},
        ))
        burst = [frame for frame in run.frames if 0.2 <= frame.elapsed < 0.35]
        self.assertEqual(len(burst), 4)  # normal frame plus three injected frames
        self.assertTrue(all(frame.data == bytes([42, 0]) for frame in burst))

    def test_special_episodes_create_unknown_dlc_and_out_of_range_frames(self) -> None:
        unknown = self._run_anomaly(AnomalyEpisode(
            AnomalyType.UNKNOWN_DBC_ID, 0.2, 0.1, None,
            {"can_id": 0x7FE, "payload": "0102"},
        ))
        self.assertEqual([(frame.can_id, frame.data) for frame in unknown.frames
                          if frame.elapsed == 0.2 and frame.can_id == 0x7FE],
                         [(0x7FE, b"\x01\x02")])

        mismatch = self._run_anomaly(AnomalyEpisode(
            AnomalyType.DLC_MISMATCH, 0.2, 0.1, 0x120, {"dlc": 1},
        ))
        self.assertEqual([len(frame.data) for frame in mismatch.frames
                          if frame.elapsed == 0.2 and len(frame.data) == 1], [1])

        out_of_range = self._run_anomaly(AnomalyEpisode(
            AnomalyType.OUT_OF_RANGE_SIGNAL, 0.2, 0.1, 0x120,
            {"signal": "speed", "value": 101},
        ))
        anomalous = [frame for frame in out_of_range.frames if frame.elapsed == 0.2]
        self.assertEqual(self.database.decode_signals(0x120, anomalous[-1].data),
                         {"speed": 101.0})

    def test_anomaly_episode_configuration_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            AnomalyEpisode("not-a-scenario", 0, 1)
        with self.assertRaises(TrafficConfigurationError):
            self._run_anomaly(AnomalyEpisode(
                AnomalyType.FREQUENCY_INCREASE, 0.2, 0.2, 0x120,
                {"frequency_factor": 1},
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
