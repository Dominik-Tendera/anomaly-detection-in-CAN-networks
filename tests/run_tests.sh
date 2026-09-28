#!/bin/sh
# Run all Python suites, or select one: sh tests/run_tests.sh raspberry_pi.
# Firmware and C/Python vectors: tests/can_logger/run_tests.sh (requires GCC).

set -e

cd "$(dirname "$0")/.."
case "${1:-all}" in
    all) START=tests ;;
    raspberry_pi|generator|shared|integration) START="tests/$1" ;;
    *) echo "Unknown suite: $1" >&2; exit 2 ;;
esac

echo "== analysis tool tests =="
python3 -m unittest discover --start-directory "$START" \
    --top-level-directory . --verbose

echo
echo "all checks passed"
