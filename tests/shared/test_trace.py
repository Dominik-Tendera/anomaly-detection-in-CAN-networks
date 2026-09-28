"""Checks time reconstruction and the absolute time verdict of can_detect.trace.

The traces are assembled with the framing and the CRC of
rpi_receiver/can_stream_protocol.py, see tests/synthetic.py, so what is tested is
the reader and not a second guess at the wire format.

Four properties carry this task. A frame record holds only the low 32 bits of the
device time, so the reader has to place it on the full time line against the
newest synchronisation record. The difference of the two fields is signed, which
is what dates a frame stamped shortly before the synchronisation record correctly
instead of throwing it almost 4295 s into the future, and what makes the wrap of
the 32-bit field a non event. Absolute time is decided once per session and the
unset RTC of the bench, reading 2000-01-01, has to be reported as an absence
rather than as a date. And the same bytes read as a file and fed as a live stream
in arbitrary pieces have to give the same records, because the detector consumes
one iterator in both modes.

Task 2.6 adds the remaining cases of this group: a missing synchronisation
record, a restart in the middle, a counter wrap and a gap in the numbering.

Usage, from the repository root:
    powershell -ExecutionPolicy Bypass -File tests/run_tests.ps1
    python3 tests/test_trace.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import trace as tr  # noqa: E402
from tests import synthetic  # noqa: E402

import can_stream_protocol as proto  # noqa: E402

SECOND = 1_000_000
WRAP = 1 << 32


def frames(records: list[tr.TraceRecord]) -> list[tr.TraceRecord]:
    return [item for item in records if item.frame is not None]


class TimeReconstruction(unittest.TestCase):
    def test_frame_after_a_sync_gets_the_full_device_time(self) -> None:
        sync_at = 7 * SECOND
        builder = synthetic.TraceBuilder()
        builder.time_sync(sync_at)
        builder.frame(can_id=0x123, data=b"\x01\x02", ts64=sync_at + 1500)

        records = frames(list(tr.iter_trace_bytes(builder.bytes())))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].ts64, sync_at + 1500)
        self.assertEqual(records[0].time_source, tr.TIME_RECONSTRUCTED)
        self.assertEqual(records[0].time_state, tr.TIME_STATE_AVAILABLE)
        self.assertTrue(records[0].timing_eligible)
        self.assertTrue(records[0].time_usable_for_timing)
        self.assertEqual(records[0].frame.can_id, 0x123)
        self.assertEqual(records[0].frame.dlc, 2)

    def test_frame_stamped_before_the_sync_is_dated_earlier_not_later(self) -> None:
        # The firmware stamps a frame in the receive interrupt and emits the
        # synchronisation record from the main loop, so this ordering is the
        # normal one, not a corner case. Read unsigned, the frame would land
        # about 4295 s ahead of the session.
        sync_at = 7 * SECOND
        stamped_at = sync_at - 900
        builder = synthetic.TraceBuilder()
        builder.time_sync(sync_at)
        builder.frame(can_id=0x200, data=b"", ts64=stamped_at)

        record = frames(list(tr.iter_trace_bytes(builder.bytes())))[0]

        self.assertEqual(record.ts64, stamped_at)
        self.assertLess(record.ts64, sync_at)

    def test_wrap_of_the_32_bit_field_continues_the_time_line(self) -> None:
        # The 32-bit field wraps after about 4295 s. The synchronisation record
        # sits just below the boundary, the frame just above it.
        sync_at = WRAP - 500
        stamped_at = WRAP + 300
        builder = synthetic.TraceBuilder()
        builder.time_sync(sync_at)
        builder.frame(can_id=0x321, data=b"\xAA", ts64=stamped_at)

        record = frames(list(tr.iter_trace_bytes(builder.bytes())))[0]

        self.assertEqual(record.frame.ts32, 300)  # the wire really did wrap
        self.assertEqual(record.ts64, stamped_at)
        self.assertGreater(record.ts64, sync_at)

    def test_wrap_and_a_frame_before_the_sync_at_the_same_boundary(self) -> None:
        # Both effects at once: the synchronisation record is past the boundary,
        # the frame is stamped just before it, so the 32-bit field is near its
        # maximum while the full value is below the sync.
        sync_at = WRAP + 400
        stamped_at = WRAP - 600
        builder = synthetic.TraceBuilder()
        builder.time_sync(sync_at)
        builder.frame(can_id=0x400, data=b"\x00" * 8, ts64=stamped_at)

        record = frames(list(tr.iter_trace_bytes(builder.bytes())))[0]

        self.assertEqual(record.frame.ts32, (WRAP - 600) & 0xFFFFFFFF)
        self.assertEqual(record.ts64, stamped_at)

    def test_a_later_sync_dates_the_frames_that_follow_it(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(SECOND)
        builder.frame(can_id=0x10, data=b"\x01", ts64=SECOND + 100)
        builder.time_sync(2 * SECOND)
        builder.frame(can_id=0x10, data=b"\x02", ts64=2 * SECOND + 100)

        times = [item.ts64 for item in frames(
            list(tr.iter_trace_bytes(builder.bytes())))]

        self.assertEqual(times, [SECOND + 100, 2 * SECOND + 100])

    def test_a_frame_before_any_sync_is_unusable_by_timing_rules(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.frame(can_id=0x55, data=b"\x07", ts64=123_456)

        reader = tr.TraceReader()
        records = frames(list(tr.iter_trace_bytes(builder.bytes(),
                                                  reader=reader)))

        self.assertIsNone(records[0].ts64)
        self.assertFalse(records[0].has_time)
        self.assertEqual(records[0].time_state, tr.TIME_STATE_UNAVAILABLE)
        self.assertEqual(records[0].time_unavailable_reason,
                         tr.TIME_UNAVAILABLE_NO_SYNC)
        self.assertFalse(records[0].timing_eligible)
        self.assertFalse(records[0].time_usable_for_timing)
        self.assertEqual(reader.counters.frames_without_time, 1)

    def test_frame_at_half_timestamp_range_is_marked_ambiguous(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(0)
        builder.frame(can_id=0x56, data=b"", ts64=1 << 31)

        reader = tr.TraceReader()
        records = frames(list(tr.iter_trace_bytes(builder.bytes(),
                                                  reader=reader)))

        self.assertIsNone(records[0].ts64)
        self.assertEqual(records[0].time_state, tr.TIME_STATE_UNAVAILABLE)
        self.assertEqual(records[0].time_unavailable_reason,
                         tr.TIME_UNAVAILABLE_AMBIGUOUS)
        self.assertFalse(records[0].timing_eligible)
        self.assertEqual(reader.counters.frames_without_time, 1)

    def test_timed_frame_filter_excludes_unavailable_frames(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.frame(can_id=0x57, data=b"\x01", ts64=100)
        builder.time_sync(SECOND)
        builder.frame(can_id=0x57, data=b"\x02", ts64=SECOND + 100)
        builder.frame(can_id=0x57, data=b"\x03", ts64=SECOND + 200)

        records = list(tr.iter_trace_bytes(builder.bytes()))
        timed = list(tr.iter_timed_frames(records))

        self.assertEqual(len(timed), 2)
        self.assertEqual(timed[0].frame.data, b"\x02")
        self.assertTrue(timed[0].timing_eligible)
        self.assertEqual(tr.frame_interval_us(timed[0], timed[1]), 100)
        self.assertIsNone(tr.frame_interval_us(records[0], timed[0]))

    def test_records_carrying_a_full_timestamp_keep_it(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.session(device_us=250)
        builder.time_sync(SECOND)

        records = list(tr.iter_trace_bytes(builder.bytes()))

        self.assertEqual(records[0].type, proto.REC_SESSION)
        self.assertEqual(records[0].ts64, 250)
        self.assertEqual(records[0].time_source, tr.TIME_FROM_RECORD)
        self.assertEqual(records[1].type, proto.REC_TIME_SYNC)
        self.assertEqual(records[1].ts64, SECOND)


class PartialSessionRestart(unittest.TestCase):
    """A restart creates a new sequence/time epoch inside one trace."""

    @staticmethod
    def restarted_trace() -> bytes:
        first = synthetic.TraceBuilder()
        first.session(device_us=123)
        first.time_sync(5 * SECOND)
        first.frame(can_id=0x321, data=b"\x01", ts64=5 * SECOND + 100)
        first.frame(can_id=0x321, data=b"\x02", ts64=5 * SECOND + 200)

        # A fresh builder models the logger reboot: the session record and all
        # following records start their sequence numbering from zero again.
        second = synthetic.TraceBuilder()
        second.session(device_us=0)
        second.time_sync(2 * SECOND)
        second.frame(can_id=0x321, data=b"\x03", ts64=2 * SECOND + 100)
        return first.bytes() + second.bytes()

    def test_restart_records_boundary_and_resets_sequence_and_time_epoch(self) -> None:
        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(self.restarted_trace(),
                                            reader=reader))
        session_records = [record for record in records
                           if record.type == proto.REC_SESSION]
        frame_records = frames(records)

        self.assertEqual(len(session_records), 2)
        self.assertEqual(len(reader.session_boundaries), 1)
        boundary = reader.session_boundaries[0]
        self.assertEqual(boundary.reason, tr.SESSION_RESTART_REASON)
        self.assertEqual(boundary.previous_session_index, 0)
        self.assertEqual(boundary.session_index, 1)
        self.assertEqual(boundary.previous_seq, 3)
        self.assertEqual(boundary.restart_seq, 0)
        self.assertIs(session_records[1].session_boundary, boundary)
        self.assertEqual([record.session_index for record in records],
                         [0, 0, 0, 0, 1, 1, 1])
        self.assertEqual([record.seq for record in records[4:]], [0, 1, 2])
        self.assertEqual([record.ts64 for record in frame_records],
                         [5 * SECOND + 100, 5 * SECOND + 200,
                          2 * SECOND + 100])
        # The sequence reset belongs to a device restart, not to a loss on the
        # serial link. The restart boundary must therefore remain separate
        # from the gap accounting used for incomplete trace intervals.
        self.assertEqual(reader.sequence_gaps, [])
        self.assertEqual(reader.records_missing_on_link, 0)

        report = tr.summary(reader)
        self.assertEqual(report["session_boundaries"], [boundary.as_dict()])

    def test_frame_interval_is_not_created_across_restart_boundary(self) -> None:
        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(self.restarted_trace(),
                                            reader=reader))
        frame_records = frames(records)

        self.assertEqual(tr.frame_interval_us(frame_records[0],
                                              frame_records[1]), 100)
        self.assertIsNone(tr.frame_interval_us(frame_records[1],
                                               frame_records[2]))


class AbsoluteTime(unittest.TestCase):
    def test_unset_rtc_leaves_the_session_without_absolute_time(self) -> None:
        # The bench logger reports 2000-01-01, see the session record of every
        # capture taken so far.
        builder = synthetic.TraceBuilder()
        builder.session(device_us=1_000, rtc_valid=True,
                        rtc=synthetic.RTC_UNSET)
        builder.time_sync(SECOND)
        builder.frame(can_id=0x11, data=b"", ts64=SECOND)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertFalse(reader.absolute_time_trustworthy)
        note = reader.absolute_time_note()
        self.assertEqual(note["reason"], tr.RTC_NEVER_SET)
        clock = reader.session_clock
        self.assertIsNone(clock.absolute(records[-1].ts64))

    def test_rtc_marked_invalid_is_reported_as_such(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.session(device_us=1_000, rtc_valid=False,
                        rtc=(25, 3, 7, 14, 9, 2))

        reader = tr.TraceReader()
        list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertFalse(reader.absolute_time_trustworthy)
        self.assertEqual(reader.absolute_time_note()["reason"], tr.NO_RTC_FLAG)

    def test_a_set_rtc_gives_absolute_time_from_the_device_time(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.session(device_us=2 * SECOND, rtc_valid=True,
                        rtc=(25, 3, 7, 14, 9, 2))
        builder.time_sync(5 * SECOND)
        builder.frame(can_id=0x11, data=b"\x01", ts64=5 * SECOND + 250_000)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))
        clock = reader.session_clock

        self.assertTrue(clock.trustworthy)
        self.assertIsNone(clock.reason)
        self.assertEqual(clock.rtc_datetime, datetime(2025, 3, 7, 14, 9, 2))
        # 2 s of device time at the RTC read, the frame at 5.25 s, so 3.25 s
        # of wall clock time later.
        self.assertEqual(clock.absolute(records[-1].ts64),
                         datetime(2025, 3, 7, 14, 9, 5, 250_000))

    def test_reader_exposes_absolute_time_mapping(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.session(device_us=1_000, rtc_valid=True,
                        rtc=(25, 3, 7, 14, 9, 2))
        builder.time_sync(5 * SECOND)
        builder.frame(can_id=0x11, data=b"", ts64=5 * SECOND)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertEqual(reader.absolute_time(records[-1].ts64),
                         datetime(2025, 3, 7, 14, 9, 6, 999_000))

    def test_a_trace_without_a_session_record_has_no_absolute_time(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(SECOND)
        builder.frame(can_id=0x11, data=b"", ts64=SECOND)

        reader = tr.TraceReader()
        list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertIsNone(reader.session_clock)
        self.assertFalse(reader.absolute_time_trustworthy)
        self.assertEqual(reader.absolute_time_note()["reason"],
                         tr.NO_SESSION_RECORD)


class OneIteratorForBothModes(unittest.TestCase):
    """The live mode and the replay mode have to be the same iterator.

    Criterion 9.2 asks a replay to reproduce the live result exactly, so the
    records the reader emits must not depend on where the bytes came from or on
    how they were cut into chunks by the serial port.
    """

    def setUp(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.session(device_us=100, rtc_valid=True,
                        rtc=synthetic.RTC_UNSET)
        builder.time_sync(SECOND)
        for step in range(20):
            builder.frame(can_id=0x100 + step % 4,
                          data=bytes([step]) * (step % 9),
                          ts64=SECOND + step * 10_000)
        builder.time_sync(2 * SECOND)
        builder.frame(can_id=0x100, data=b"\xFF", ts64=2 * SECOND + 5)
        self.trace = builder.bytes()

    @staticmethod
    def fingerprint(records: list[tr.TraceRecord]) -> list[tuple]:
        return [(item.type, item.seq, item.ts64, item.time_source,
                 None if item.frame is None
                 else (item.frame.can_id, item.frame.dlc, item.frame.data))
                for item in records]

    def test_file_and_stream_give_identical_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "20250307_140902.canbin"
            path.write_bytes(self.trace)
            from_file, file_reader = tr.read_trace(path)

        # Live mode: bytes arrive in whatever pieces the port returns, including
        # pieces that cut a record in half.
        stream_reader = tr.TraceReader()
        chunks = [self.trace[i:i + 7] for i in range(0, len(self.trace), 7)]
        from_stream = list(stream_reader.iter_chunks(chunks))

        self.assertEqual(self.fingerprint(from_file),
                         self.fingerprint(from_stream))
        self.assertEqual(file_reader.counters.as_dict(),
                         stream_reader.counters.as_dict())
        self.assertEqual(file_reader.absolute_time_note(),
                         stream_reader.absolute_time_note())

    def test_chunk_size_does_not_change_the_records(self) -> None:
        reference = self.fingerprint(list(tr.iter_trace_bytes(self.trace)))
        for size in (1, 2, 13, 64, len(self.trace), len(self.trace) + 100):
            with self.subTest(chunk_bytes=size):
                produced = self.fingerprint(
                    list(tr.iter_trace_bytes(self.trace, chunk_bytes=size)))
                self.assertEqual(produced, reference)

    def test_every_record_was_decoded_and_the_link_was_clean(self) -> None:
        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(self.trace, reader=reader))

        self.assertEqual(len(records), reader.counters.records)
        self.assertEqual(reader.counters.frames, 21)
        self.assertEqual(reader.counters.frames_without_time, 0)
        self.assertEqual(reader.counters.time_syncs, 2)
        self.assertEqual(reader.counters.session_records, 1)
        self.assertEqual(reader.counters.undecodable_payloads, 0)
        self.assertEqual(reader.decoder.counters.crc_errors, 0)
        self.assertEqual(reader.decoder.counters.cobs_errors, 0)
        self.assertEqual(reader.decoder.counters.sync_losses, 0)

    def test_summary_reports_the_device_time_span(self) -> None:
        reader = tr.TraceReader()
        list(tr.iter_trace_bytes(self.trace, reader=reader))

        report = tr.summary(reader)
        self.assertEqual(report["device_time"]["first_frame_us"], SECOND)
        self.assertEqual(report["device_time"]["last_frame_us"],
                         2 * SECOND + 5)
        self.assertEqual(report["device_time"]["span_us"], SECOND + 5)
        self.assertFalse(report["absolute_time"]["absolute_time_trustworthy"])


class IdentifierProjection(unittest.TestCase):
    def test_nonzero_extended_counter_adds_projection_annotation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND, extended_frames=3)

        reader = tr.TraceReader()
        list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        annotation = tr.summary(reader)["identifier_projection"]
        self.assertIsNotNone(annotation)
        self.assertTrue(annotation["detected"])
        self.assertEqual(annotation["source_identifier_bits"], 29)
        self.assertEqual(annotation["stored_identifier_bits"], 11)
        self.assertEqual(annotation["extended_frames_by_channel"], {"1": 3})
        self.assertTrue(annotation["indistinguishable_with_standard_frames"])
        self.assertNotIn("can_id", annotation)
        self.assertNotIn("ecu", annotation["note"].lower())

    def test_zero_extended_counter_does_not_add_projection_annotation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND, extended_frames=0)

        reader = tr.TraceReader()
        list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertIsNone(reader.identifier_projection_note)
        self.assertIsNone(tr.summary(reader)["identifier_projection"])


class CompletenessAndCounterIncrements(unittest.TestCase):
    def test_unchanged_loss_counters_mark_the_interval_complete(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        builder.bus_stats(channel=1, ts64=2 * SECOND)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        stats = [item for item in records if item.type == proto.REC_BUS_STATS]
        self.assertEqual(len(stats), 2)
        interval = stats[-1].decoded["interval"]
        self.assertTrue(interval["complete"])
        self.assertEqual(interval["reasons"], [])
        self.assertEqual(interval["loss_increments"], {})
        self.assertEqual(reader.counters.records_missing_on_link, 0)
        self.assertEqual(len(reader.complete_intervals), 1)
        self.assertEqual(reader.incomplete_intervals, [])

    def test_growth_of_loss_counters_marks_the_interval_incomplete(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND, ring_dropped=4,
                          fifo_overrun=2)
        builder.bus_stats(channel=1, ts64=2 * SECOND, ring_dropped=7,
                          fifo_overrun=3)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        interval = records[-1].decoded["interval"]
        self.assertFalse(interval["complete"])
        self.assertEqual(interval["loss_increments"], {
            "fifo_overrun": 1,
            "ring_dropped": 3,
        })
        self.assertEqual(interval["counter_increments"]["ring_dropped"], 3)
        self.assertEqual(interval["counter_increments"]["fifo_overrun"], 1)
        self.assertEqual(interval["reasons"], ["device_loss"])
        self.assertEqual(len(reader.incomplete_intervals), 1)

    def test_counter_decrease_is_a_modulo_32_bit_wrap(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND,
                          ring_dropped=(1 << 32) - 2)
        builder.bus_stats(channel=1, ts64=2 * SECOND, ring_dropped=1)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        interval = records[-1].decoded["interval"]
        self.assertEqual(interval["counter_increments"]["ring_dropped"], 3)
        self.assertIn("ring_dropped", interval["counter_wraps"])
        self.assertFalse(interval["complete"])
        self.assertEqual(reader.counters.counter_wraps, 1)

    def test_sequence_gap_marks_the_bounded_interval_incomplete(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        builder.skip_seq(2)
        builder.bus_stats(channel=1, ts64=2 * SECOND)

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        interval = records[-1].decoded["interval"]
        self.assertFalse(interval["complete"])
        self.assertEqual(interval["reasons"], ["sequence_gap"])
        self.assertEqual(reader.records_missing_on_link, 2)
        self.assertEqual(reader.counters.sequence_gaps, 1)
        self.assertEqual(interval["sequence_gaps"][0]["missing"], 2)
        self.assertEqual(interval["sequence_gaps"][0]["start_ts64"], SECOND)
        self.assertEqual(interval["sequence_gaps"][0]["end_ts64"],
                         2 * SECOND)


class UnknownRecordType(unittest.TestCase):
    def test_reserved_analog_record_is_passed_on_and_counted(self) -> None:
        # Type 0x20 is reserved for CANH and CANL aggregates. A reader that
        # stumbled over it would have to be changed the day that record appears,
        # so it is carried through as an unknown record instead.
        builder = synthetic.TraceBuilder()
        builder.time_sync(SECOND)
        builder.raw(bytes([proto.REC_ANALOG_AGG]) + b"\x05\x00" + b"\x01\x02")

        reader = tr.TraceReader()
        records = list(tr.iter_trace_bytes(builder.bytes(), reader=reader))

        self.assertEqual(records[-1].type, proto.REC_ANALOG_AGG)
        self.assertFalse(records[-1].known)
        self.assertIsNone(records[-1].decoded)
        self.assertEqual(reader.counters.unknown_types, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
