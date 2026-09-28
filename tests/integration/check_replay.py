"""Check replay of the deliberately imperfect vectors emitted by the C test."""
import json
from pathlib import Path
import sys


def main():
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    expected = {
        "frames": (report["frames"], 504),
        "records_ok": (report["decoder"]["records_ok"], 510),
        "cobs_errors": (report["decoder"]["cobs_errors"], 3),
        "crc_errors": (report["decoder"]["crc_errors"], 0),
        "missing_records": (report["sequence"]["records_missing_on_link"], 0),
        "lossless": (report["lossless"], False),
    }
    for name, (actual, wanted) in expected.items():
        if actual != wanted:
            raise ValueError(f"{name}: expected {wanted!r}, got {actual!r}")
    print("Replay report verified (injected errors correctly reported).")


if __name__ == "__main__":
    main()
