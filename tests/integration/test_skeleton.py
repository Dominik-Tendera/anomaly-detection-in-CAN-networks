"""Checks that the project skeleton is wired the way the later tasks assume.

Three things are worth a test even before any detection logic exists. The two
packages have to import from the tools directory alone, without an installed
distribution. The record decoder has to resolve to the copy in rpi_receiver,
because a second copy would drift away from the C encoder without any test
noticing. And the dependency file has to pin exact versions, since a floating
range would let a library change its numerics between two runs the session
report claims are comparable.

Usage, from the repository root:
    sh tests/run_tests.sh
    python3 tests/test_skeleton.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
RECEIVER_DIR = TOOLS_DIR.parent / "raspberry_pi/rpi_receiver"

# Mirrors the import convention of rpi_receiver/can_stream_rx.py: the decoder is
# a top level module, reached by putting its directory on the path.
for entry in (str(TOOLS_DIR), str(RECEIVER_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import can_detect  # noqa: E402
import can_generate  # noqa: E402
import can_stream_protocol as proto  # noqa: E402

PINNED_PACKAGES = ("pyserial", "cantools", "numpy", "scikit-learn")


def requirement_lines() -> list[str]:
    text = (TOOLS_DIR / "requirements.txt").read_text(encoding="utf-8")
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


class SkeletonLayout(unittest.TestCase):
    def test_packages_import_from_the_tools_directory(self) -> None:
        for package in (can_detect, can_generate):
            path = Path(package.__file__).resolve()
            self.assertEqual(path.parent.parent, TOOLS_DIR)
            self.assertTrue(package.__doc__, f"{package.__name__} has no docstring")

    def test_decoder_resolves_to_the_receiver_copy(self) -> None:
        expected = (RECEIVER_DIR / "can_stream_protocol.py").resolve()
        self.assertEqual(Path(proto.__file__).resolve(), expected)
        self.assertEqual(proto.PROTO_VERSION, 1)

    def test_no_second_decoder_in_the_analysis_packages(self) -> None:
        for package in ("can_detect", "can_generate"):
            copies = list((TOOLS_DIR / package).rglob("can_stream_protocol.py"))
            self.assertEqual(copies, [], f"decoder copied into {package}")


class Requirements(unittest.TestCase):
    def test_every_requirement_is_pinned(self) -> None:
        for line in requirement_lines():
            self.assertIn("==", line, f"requirement is not pinned: {line}")

    def test_required_packages_are_listed(self) -> None:
        names = {line.split("==", 1)[0].strip().lower()
                 for line in requirement_lines()}
        for package in PINNED_PACKAGES:
            self.assertIn(package, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
