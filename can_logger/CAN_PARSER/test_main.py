import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import main


class FrameFormatTests(unittest.TestCase):
    def test_legacy_eight_byte_frame(self):
        frame = main.parse_frame_record(
            "86116165210 0024 0000000000030201 1"
        )

        self.assertIsNotNone(frame)
        self.assertEqual(frame["dlc"], 8)
        self.assertEqual(frame["data"], bytes((1, 2, 3, 0, 0, 0, 0, 0)))

    def test_v2_frame_preserves_dlc(self):
        line = "86116165210 0024 030201 1"
        frame = main.parse_frame_record(line)

        self.assertIsNotNone(frame)
        self.assertEqual(frame["dlc"], 3)
        self.assertEqual(frame["data"], bytes((1, 2, 3)))

        # This is the data conversion performed by the legacy parser. It still
        # receives the same payload padded to eight bytes.
        unused_timestamp, unused_id, data_hex, bus = line.split(" ", 3)
        legacy_data = int(data_hex, 16).to_bytes(8, "big")[::-1]
        self.assertEqual(legacy_data, bytes((1, 2, 3, 0, 0, 0, 0, 0)))
        self.assertEqual(bus, "1")

    def test_v2_zero_length_frame_keeps_four_columns(self):
        line = "86116165210 0024 0 2"
        frame = main.parse_frame_record(line)

        self.assertIsNotNone(frame)
        self.assertEqual(frame["dlc"], 0)
        self.assertEqual(frame["data"], b"")
        self.assertEqual(len(line.split()), 4)


class DiagnosticTests(unittest.TestCase):
    def test_fast_diagnostic_scan_does_not_parse_normal_frames(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "202608102353.log")
            with open(log_path, "w", encoding="ascii") as log_file:
                log_file.write(
                    "86116165000 0024 030201 1\n"
                    "86116165100 0025 04030201 1\n"
                    "#CANEVENT v=1 t=86116165210 bus=1 flags=0x1 "
                    "state=active rec=0 tec=0 last=0x8 last_drop_id=0x000\n"
                )

            with mock.patch.object(
                main,
                "parse_frame_record",
                side_effect=AssertionError("normal frames must not be parsed"),
            ):
                records = main.collect_diagnostic_records([log_path])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["type"], "CANEVENT")

    def test_fast_diagnostic_scan_restores_chronological_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "202608102353.log")
            with open(log_path, "w", encoding="ascii") as log_file:
                log_file.write(
                    "#CANSTAT v=1 t=4000000 bus=1\n"
                    "2000000 0024 01 1\n"
                    "#CANEVENT v=1 t=100000 bus=1 flags=0x1\n"
                )

            records = main.collect_diagnostic_records([log_path])

        self.assertEqual(
            [record[1]["timestamp"] for record in records],
            [100000, 4000000],
        )

    def test_fast_diagnostic_scan_handles_midnight_between_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            before_midnight = os.path.join(temp_dir, "202608102359.log")
            after_midnight = os.path.join(temp_dir, "202608110000.log")
            with open(before_midnight, "w", encoding="ascii") as log_file:
                log_file.write("#CANSTAT v=1 t=86399000000 bus=1\n")
            with open(after_midnight, "w", encoding="ascii") as log_file:
                log_file.write("#CANSTAT v=1 t=100000 bus=1\n")

            records = main.collect_diagnostic_records(
                [after_midnight, before_midnight]
            )

        self.assertLess(records[0][0], records[1][0])
        self.assertEqual(records[1][0] - records[0][0], 1100000)

    def test_event_and_hal_masks_are_decoded(self):
        record = main.parse_diagnostic_record(
            "#CANEVENT v=1 t=86123469611 bus=1 flags=0x00000005 "
            "state=active rec=0 tec=0 last=0x0000000B last_drop_id=0x000"
        )

        formatted = main.format_diagnostic_record(record)
        self.assertIn("controller_error|state_change", formatted)
        self.assertIn("warning|error_passive|stuff", formatted)

    def test_human_event_explains_problem_and_id_limit(self):
        record = main.parse_diagnostic_record(
            "#CANEVENT v=1 t=86123469611 bus=1 flags=0x0000000D "
            "state=passive rec=128 tec=4 last=0x00000020 "
            "last_drop_id=0x301"
        )

        formatted = main.format_human_can_event(record)

        self.assertIn("CAN1 - BŁĄD", formatted)
        self.assertIn("brak potwierdzenia ramki (ACK error)", formatted)
        self.assertIn("Ostatnie ID utracone w RAM: 0x301", formatted)
        self.assertIn("ID uszkodzonej ramki: niedostępne", formatted)
        self.assertIn("Surowy wpis: #CANEVENT", formatted)

    def test_human_report_summarizes_counters_and_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "202608102353.log")
            with open(log_path, "w", encoding="ascii") as log_file:
                log_file.write(
                    "#SESSION v=1 frame_format=2 t=86116000000 "
                    "can1_bitrate=1000000 can2_bitrate=1000000\n"
                    "#CANEVENT v=1 t=86116100000 bus=1 flags=0x0000000D "
                    "state=passive rec=128 tec=4 last=0x00000020 "
                    "last_drop_id=0x301\n"
                    "#CANSTAT v=1 scope=boot t=86116200000 bus=1 rx=100 "
                    "valid=99 queued=98 payload_bytes=790 read_err=0 ext=0 "
                    "rtr=0 dlc_err=0 fifo_full=1 fifo_ovr=1 ring_drop=1 "
                    "last_drop_id=0x301 fifo_peak=3 state=passive rec=128 "
                    "tec=4 rec_peak=128 tec_peak=4\n"
                    "#CANERR v=1 scope=boot t=86116200000 bus=1 warning=1 "
                    "passive=1 bus_off=0 stuff=0 form=0 ack=3 bit_r=0 "
                    "bit_d=0 crc=0 other=0 last=0x00000020\n"
                    "#LOGGERSTAT v=1 t=86116200000 ring_peak=90 "
                    "ring_capacity=100 ring_drop_total=1\n"
                )

            output_path, event_count = main.write_human_diagnostics_report(
                [log_path], temp_dir, 0
            )
            with open(output_path, "r", encoding="utf-8-sig") as output:
                report = output.read()

        self.assertEqual(event_count, 1)
        self.assertIn("WYNIK: WYKRYTO PROBLEMY", report)
        self.assertIn("Ramki: odebrane 100, poprawne 99", report)
        self.assertIn("błędy ACK: 3", report)
        self.assertIn("Maksymalne zapełnienie RAM: 90/100 (90.0%)", report)
        self.assertIn("Ostatnie ID utracone w RAM: 0x301", report)


class AscExportTests(unittest.TestCase):
    def test_asc_contains_dlc_data_and_error_frame(self):
        try:
            import can
        except ImportError:
            self.skipTest("python-can is not installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "202608102353.log")
            with open(log_path, "w", encoding="ascii") as log_file:
                log_file.write(
                    "86116165000 0024 030201 1\n"
                    "#CANEVENT v=1 t=86116165210 bus=1 flags=0x00000001 "
                    "state=passive rec=128 tec=0 last=0x00000008 "
                    "last_drop_id=0x000\n"
                )

            asc_path, frame_count, error_count, diagnostic_count = (
                main.export_to_canoe_asc([log_path], temp_dir)
            )
            messages = list(can.ASCReader(asc_path))

        self.assertEqual(frame_count, 1)
        self.assertEqual(error_count, 1)
        self.assertEqual(diagnostic_count, 1)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].arbitration_id, 0x24)
        self.assertEqual(messages[0].dlc, 3)
        self.assertEqual(messages[0].data, bytes((1, 2, 3)))
        self.assertTrue(messages[1].is_error_frame)

    def test_late_diagnostic_event_cannot_make_asc_time_go_backwards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "202608102353.log")
            with open(log_path, "w", encoding="ascii") as log_file:
                log_file.write(
                    "2000000 0024 01 1\n"
                    "4000000 0024 02 1\n"
                    "#CANEVENT v=1 t=100000 bus=1 flags=0x00000001 "
                    "state=active rec=0 tec=0 last=0x00000008 "
                    "last_drop_id=0x000\n"
                )

            records = list(main.iter_chronological_log_records([log_path]))

        timestamps = [record[0] for record in records]
        self.assertEqual(timestamps, sorted(timestamps))
        late_event = next(record for record in records if record[2] == "diagnostic")
        self.assertEqual(late_event[3]["timestamp"], 100000)


if __name__ == "__main__":
    unittest.main()
