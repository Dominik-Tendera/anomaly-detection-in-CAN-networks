#!/bin/sh
# Builds and runs the wire format tests, then checks that the Python receiver
# agrees with the C encoder field by field. Same checks as run_tests.ps1, for a
# Raspberry Pi or any Linux host.
#
# Usage, from the repository root:
#   sh tests/can_logger/run_tests.sh

set -e

cd "$(dirname "$0")/../.."

EXE=tests/can_logger/test_codec
BIN=tests/can_logger/vectors.bin
CSV=tests/can_logger/vectors.csv

echo "== building host test =="
cc -std=c11 -O2 -Wall -Wextra -Werror -I can_logger/Core/Inc \
    tests/can_logger/test_can_stream_codec.c can_logger/Core/Src/can_stream_codec.c -o "$EXE"

echo "== codec properties =="
"$EXE"

echo "== cross language vectors =="
"$EXE" --emit-vectors "$BIN" "$CSV"
python3 tests/integration/test_vectors.py "$BIN" "$CSV"

echo "== receiver replay =="
# The vectors include malformed bytes and device loss counters: exit 1 is expected.
REPLAY_STATUS=0
python3 raspberry_pi/rpi_receiver/can_stream_rx.py --replay "$BIN" \
    --output tests/can_logger/captures --session vectors || REPLAY_STATUS=$?
if [ "$REPLAY_STATUS" -ne 1 ]; then
    echo "replay did not report the injected losses" >&2
    exit 1
fi
python3 tests/integration/check_replay.py tests/can_logger/captures/vectors.report.json

echo
echo "all checks passed"
