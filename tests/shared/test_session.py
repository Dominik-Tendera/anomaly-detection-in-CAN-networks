"""Checks the session identifier and the collision guard of can_detect.session.

Three properties are worth holding on to. The identifier has the shape the
receiver already writes, otherwise a capture and its analysis would not share a
name. The formatting is a pure function of a timestamp handed in, which is what
makes a session reproducible and this test possible at all. And an identifier
already present in the output directory stops the run instead of overwriting the
trace an earlier report was computed from.

Usage, from the repository root:
    powershell -ExecutionPolicy Bypass -File tests/run_tests.ps1
    python3 tests/test_session.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import session as sess  # noqa: E402

STAMP = datetime(2025, 3, 7, 14, 9, 2)


class SessionIdFormat(unittest.TestCase):
    def test_identifier_carries_date_and_time(self) -> None:
        self.assertEqual(sess.wall_clock_session_id(STAMP), "20250307_140902")

    def test_identifier_shape_matches_the_receiver_default(self) -> None:
        # rpi_receiver/can_stream_rx.py falls back to time.strftime with the
        # same format, so a capture and its analysis share one name.
        self.assertEqual(sess.SESSION_ID_FORMAT, "%Y%m%d_%H%M%S")
        produced = sess.wall_clock_session_id(STAMP)
        self.assertRegex(produced, re.compile(r"\A\d{8}_\d{6}\Z"))
        self.assertEqual(STAMP.strftime(sess.SESSION_ID_FORMAT), produced)

    def test_same_timestamp_gives_the_same_identifier(self) -> None:
        first = sess.wall_clock_session_id(STAMP)
        second = sess.wall_clock_session_id(datetime(2025, 3, 7, 14, 9, 2))
        self.assertEqual(first, second)

    def test_a_later_second_gives_a_different_identifier(self) -> None:
        later = sess.wall_clock_session_id(datetime(2025, 3, 7, 14, 9, 3))
        self.assertNotEqual(sess.wall_clock_session_id(STAMP), later)

    def test_timestamp_has_to_be_a_datetime(self) -> None:
        with self.assertRaises(TypeError):
            sess.wall_clock_session_id(1741356542)  # type: ignore[arg-type]


class ArtefactNames(unittest.TestCase):
    def test_every_artefact_starts_with_the_identifier(self) -> None:
        session_id = sess.wall_clock_session_id(STAMP)
        for kind in sess.ARTEFACT_SUFFIXES:
            name = sess.artefact_name(session_id, kind)
            self.assertTrue(name.startswith(session_id), name)

    def test_artefact_names_are_distinct(self) -> None:
        session_id = sess.wall_clock_session_id(STAMP)
        names = [sess.artefact_name(session_id, kind)
                 for kind in sess.ARTEFACT_SUFFIXES]
        self.assertEqual(len(names), len(set(names)))

    def test_trace_keeps_the_receiver_extension(self) -> None:
        self.assertEqual(sess.ARTEFACT_SUFFIXES["trace"], ".canbin")
        self.assertEqual(sess.ARTEFACT_SUFFIXES["report"], ".report.json")

    def test_unknown_artefact_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            sess.artefact_name("20250307_140902", "spectrogram")

    def test_identifier_unusable_in_a_file_name_is_rejected(self) -> None:
        for bad in ("", "../escape", "run/1", "with space", "star*", "-lead"):
            with self.subTest(bad=bad):
                with self.assertRaises(sess.SessionIdError):
                    sess.validate_session_id(bad)


class CollisionGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.output = Path(self._tmp.name) / "captures"
        self.session_id = sess.wall_clock_session_id(STAMP)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_free_identifier_yields_paths_in_the_output_directory(self) -> None:
        paths = sess.open_session(self.output, self.session_id)
        self.assertTrue(self.output.is_dir())
        self.assertEqual(paths.session_id, self.session_id)
        self.assertEqual(paths.path("trace"),
                         self.output / f"{self.session_id}.canbin")
        self.assertEqual(set(paths.all_paths()), set(sess.ARTEFACT_SUFFIXES))

    def test_missing_directory_has_no_collisions(self) -> None:
        self.assertEqual(sess.colliding_paths(self.output, self.session_id), [])

    def test_existing_artefact_is_reported_as_a_collision(self) -> None:
        self.output.mkdir(parents=True)
        trace = self.output / f"{self.session_id}.canbin"
        trace.write_bytes(b"earlier capture")

        with self.assertRaises(sess.SessionIdCollision) as caught:
            sess.open_session(self.output, self.session_id)

        self.assertEqual(caught.exception.session_id, self.session_id)
        self.assertEqual(caught.exception.existing, [trace])
        self.assertIn(trace.name, str(caught.exception))

    def test_collision_leaves_existing_files_untouched(self) -> None:
        self.output.mkdir(parents=True)
        report = self.output / f"{self.session_id}.report.json"
        report.write_text("{}", encoding="utf-8")
        before = sorted(p.name for p in self.output.iterdir())

        with self.assertRaises(sess.SessionIdCollision):
            sess.open_session(self.output, self.session_id)

        self.assertEqual(report.read_text(encoding="utf-8"), "{}")
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), before)

    def test_any_file_carrying_the_identifier_counts(self) -> None:
        # Not only the suffixes of this module: a ground truth file written by
        # the generator, or an export added by hand, also blocks the run.
        self.output.mkdir(parents=True)
        extra = self.output / f"{self.session_id}_wyniki.csv"
        extra.write_text("a,b\n", encoding="utf-8")

        with self.assertRaises(sess.SessionIdCollision):
            sess.open_session(self.output, self.session_id)

    def test_a_different_identifier_is_not_a_collision(self) -> None:
        self.output.mkdir(parents=True)
        (self.output / "20250307_140901.canbin").write_bytes(b"other session")
        paths = sess.open_session(self.output, self.session_id)
        self.assertEqual(paths.session_id, self.session_id)

    def test_create_dir_off_does_not_touch_the_file_system(self) -> None:
        paths = sess.open_session(self.output, self.session_id,
                                  create_dir=False)
        self.assertFalse(self.output.exists())
        self.assertEqual(paths.output_dir, self.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
