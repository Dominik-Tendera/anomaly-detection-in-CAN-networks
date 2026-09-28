"""Locate shared modules after splitting generator, receiver and result tools."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def configure_imports():
    """Support repository scripts without a global install or a fixed cwd."""
    for directory in (ROOT / "tools", ROOT / "generator", ROOT / "raspberry_pi",
                      ROOT / "raspberry_pi" / "rpi_receiver"):
        value = str(directory)
        if value not in sys.path:
            sys.path.insert(0, value)
