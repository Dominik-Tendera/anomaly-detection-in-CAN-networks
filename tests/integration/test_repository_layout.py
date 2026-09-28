"""Path regression checks for the separated PC, Raspberry Pi and result tools."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from repo_paths import ROOT
import generate_can_traffic
import generate_anomaly_traffic
import live_detect


class RepositoryLayout(unittest.TestCase):
    def test_host_tests_use_the_relocated_firmware(self):
        for relative in ('can_logger/Core/Inc/can_stream_codec.h',
                         'can_logger/Core/Src/can_stream_codec.c'):
            self.assertTrue((ROOT / relative).is_file(), relative)
        for script in ('tests/can_logger/run_tests.ps1', 'tests/can_logger/run_tests.sh'):
            source = (ROOT / script).read_text(encoding='utf-8')
            self.assertIn('can_logger/Core/Inc', source)
            self.assertIn('can_logger/Core/Src/can_stream_codec.c', source)

    def test_dbc_copies_match_the_historical_input(self):
        paths = [ROOT / part / 'RTE_3.5_CAN1_CAR.dbc' for part in
                 ('generator/dbc', 'raspberry_pi/dbc', 'archive/CAN_PARSER')]
        self.assertEqual(len({hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}), 1)

    def test_default_outputs_are_next_to_their_programs(self):
        self.assertEqual(generate_can_traffic.parser().parse_args([]).output,
                         ROOT / 'generator/captures')
        self.assertEqual(generate_anomaly_traffic.parser().parse_args([]).output,
                         ROOT / 'generator/captures')
        self.assertEqual(live_detect.parser().parse_args([]).output,
                         ROOT / 'raspberry_pi/captures')

    def test_entry_points_work_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for script in ('generator/generate_can_traffic.py',
                           'generator/generate_anomaly_traffic.py',
                           'raspberry_pi/live_detect.py',
                           'raspberry_pi/benchmark_live_detect.py',
                           'raspberry_pi/rpi_receiver/can_stream_rx.py',
                           'results/analyze_measurement_campaign.py',
                           'results/render_detection_heatmap.py',
                           'results/render_timing_results.py'):
                with self.subTest(script=script):
                    result = subprocess.run([sys.executable, str(ROOT / script), '--help'],
                                            cwd=directory, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_reference_generator_dry_run_does_not_need_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / 'generator/generate_can_traffic.py'),
                                     '--dry-run', '--duration', '2', '--seed', '42',
                                     '--max-fps', '80', '--output', directory, '--session', 'reference'],
                                    cwd=directory, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertTrue((Path(directory) / 'reference/config.json').is_file())

    def test_anomaly_generator_resolves_scenario_and_shared_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / 'generator/generate_anomaly_traffic.py'),
                                     '--dry-run', '--duration', '155', '--seed', '42', '--max-fps', '80',
                                     '--scenario', str(ROOT / 'generator/scenarios/intensity_matrix.example.json'),
                                     '--output', directory, '--session', 'anomalies'],
                                    cwd=directory, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertTrue((Path(directory) / 'anomalies/truth.json').is_file())
