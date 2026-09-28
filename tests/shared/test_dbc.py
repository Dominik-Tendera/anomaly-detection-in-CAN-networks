"""Tests for the DBC metadata adapter and signal decoder."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.dbc import (DbcDatabase, DbcError, MessageDefinition,
                             SignalDefinition, load_dbc)  # noqa: E402

DBC_PATH = TOOLS_DIR.parent / "generator" / "dbc" / "RTE_3.5_CAN1_CAR.dbc"

# These values are measured from the checked-in station DBC.  The specification
# documents 125 messages and 367 signals, but the repository file currently has
# 124 messages and 364 signals.  Keep both values explicit so a DBC update or a
# specification correction cannot silently change the tested fixture.
STATION_DBC_MESSAGE_COUNT = 124
STATION_DBC_SIGNAL_COUNT = 364
STATION_DBC_SIGNALS_WITHOUT_REFERENCE_RANGE = 52
SPEC_MESSAGE_COUNT = 125
SPEC_SIGNAL_COUNT = 367


class DbcMetadata(unittest.TestCase):
    def test_station_dbc_counts_record_spec_discrepancy(self) -> None:
        database = load_dbc(DBC_PATH)

        actual_counts = (len(database.messages_by_id), database.signal_count)
        self.assertEqual(
            actual_counts,
            (STATION_DBC_MESSAGE_COUNT, STATION_DBC_SIGNAL_COUNT),
            "The checked-in station DBC changed; update this test from the real file, "
            "not by fabricating messages or signals.",
        )
        self.assertNotEqual(
            actual_counts,
            (SPEC_MESSAGE_COUNT, SPEC_SIGNAL_COUNT),
            "The specification currently documents 125 messages and 367 signals, "
            "but this assertion must be updated if the specification is corrected.",
        )

        message = database.message_for_id(0x201)
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.can_id, 0x201)
        self.assertGreaterEqual(message.data_length, 0)
        self.assertGreaterEqual(len(message.signals), 1)

        signal = message.signals[0]
        self.assertIsInstance(signal.name, str)
        self.assertGreater(signal.bit_length, 0)
        self.assertIsInstance(signal.unit, (str, type(None)))
        if signal.has_reference_range:
            self.assertLess(signal.minimum, signal.maximum)

    def test_signals_without_dbc_limits_are_omitted_from_range_checks(self) -> None:
        database = load_dbc(DBC_PATH)

        signals_without_reference_range = [
            signal
            for message in database.messages_by_id.values()
            for signal in message.signals
            if not signal.has_reference_range
        ]
        signals_with_reference_range = [
            signal
            for message in database.messages_by_id.values()
            for signal in message.signals
            if signal.has_reference_range
        ]

        self.assertEqual(
            len(signals_without_reference_range),
            STATION_DBC_SIGNALS_WITHOUT_REFERENCE_RANGE,
            "The checked-in station DBC changed; recalculate this count from the "
            "file rather than assigning reference limits to signals.",
        )
        self.assertEqual(
            len(signals_with_reference_range),
            STATION_DBC_SIGNAL_COUNT - STATION_DBC_SIGNALS_WITHOUT_REFERENCE_RANGE,
        )
        self.assertTrue(signals_without_reference_range)
        self.assertTrue(
            all(
                signal.minimum is None and signal.maximum is None
                for signal in signals_without_reference_range
            )
        )
        self.assertTrue(
            all(
                signal.minimum is not None
                and signal.maximum is not None
                and signal.minimum < signal.maximum
                for signal in signals_with_reference_range
            )
        )

    def test_equal_dbc_limits_are_not_a_reference_range(self) -> None:
        signal = SignalDefinition(
            name="constant",
            start_bit=0,
            bit_length=8,
            minimum=5.0,
            maximum=5.0,
            unit=None,
        )

        self.assertFalse(signal.has_reference_range)

    def test_unknown_can_id_has_no_definition(self) -> None:
        database = load_dbc(DBC_PATH)
        self.assertIsNone(database.message_for_id(0x7FF))

    def test_missing_dbc_is_reported_as_dbc_error(self) -> None:
        with self.assertRaises(DbcError):
            load_dbc(DBC_PATH.with_name("does-not-exist.dbc"))

    def test_decodes_present_signals_with_dbc_scaling(self) -> None:
        signal = SignalDefinition("speed", 0, 8, 0.0, 100.0, "km/h",
                                  scale=0.5, offset=1.0)
        database = DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={1: MessageDefinition(1, "Status", 2, (signal,))},
        )

        self.assertEqual(database.decode_signals(1, bytes([10, 0])), {"speed": 6.0})
        self.assertEqual(database.skipped_signal_count, 0)

    def test_skips_signal_beyond_dlc_and_does_not_create_alarm(self) -> None:
        present = SignalDefinition("present", 0, 8, None, None, None)
        missing = SignalDefinition("missing", 8, 8, None, None, None)
        database = DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={1: MessageDefinition(1, "Status", 2,
                                                  (present, missing))},
        )

        decoded = database.decode_signals(1, bytes([0xA5]))

        self.assertEqual(decoded, {"present": 0xA5})
        self.assertEqual(database.skipped_signal_count, 1)
        # Decoding returns values and skip accounting only.  No anomaly event
        # is produced for a signal unavailable because of the received DLC.
        self.assertNotIn("alarm", decoded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
