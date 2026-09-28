# Builds and runs the wire format tests, then checks that the Python receiver
# agrees with the C encoder field by field.
#
# Usage, from the repository root:
#   powershell -ExecutionPolicy Bypass -File tests/can_logger/run_tests.ps1

$ErrorActionPreference = "Stop"
# $PSScriptRoot points at tests/can_logger, the repository root is two levels up.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$exe = "tests/can_logger/test_codec.exe"
$bin = "tests/can_logger/vectors.bin"
$csv = "tests/can_logger/vectors.csv"

Write-Host "== building host test =="
gcc -std=c11 -O2 -Wall -Wextra -Werror -I "can_logger/Core/Inc" `
    "tests/can_logger/test_can_stream_codec.c" "can_logger/Core/Src/can_stream_codec.c" -o $exe
if ($LASTEXITCODE -ne 0) { throw "build failed" }

Write-Host "== codec properties =="
& $exe
if ($LASTEXITCODE -ne 0) { throw "codec tests failed" }

Write-Host "== cross language vectors =="
& $exe --emit-vectors $bin $csv
if ($LASTEXITCODE -ne 0) { throw "vector generation failed" }

python "tests/integration/test_vectors.py" $bin $csv
if ($LASTEXITCODE -ne 0) { throw "python decoder disagrees with the C encoder" }

Write-Host "== receiver replay =="
python "raspberry_pi/rpi_receiver/can_stream_rx.py" --replay $bin `
    --output "tests/can_logger/captures" --session vectors | Out-Host
# The vectors include malformed bytes and device loss counters: exit 1 is expected.
if ($LASTEXITCODE -ne 1) { throw "replay did not report the injected losses" }
python "tests/integration/check_replay.py" "tests/can_logger/captures/vectors.report.json"
if ($LASTEXITCODE -ne 0) { throw "unexpected replay report" }

Write-Host ""
Write-Host "all checks passed"
