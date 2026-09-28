# Run all Python suites, or select one with -Suite raspberry_pi (etc.).
# Firmware and C/Python vectors: tests/can_logger/run_tests.ps1 (requires GCC).
param(
    [ValidateSet('all', 'raspberry_pi', 'generator', 'shared', 'integration')]
    [string]$Suite = 'all'
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot points at tests, one level below the repository root.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== analysis tool tests =="
$start = if ($Suite -eq 'all') { 'tests' } else { "tests/$Suite" }
python -m unittest discover --start-directory $start `
    --top-level-directory . --verbose
if ($LASTEXITCODE -ne 0) { throw "analysis tool tests failed" }

Write-Host ""
Write-Host "all checks passed"
