param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv")) {
    & $Python -3.10 -m venv .venv
}

$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -e ".[build]"
& $PythonExe scripts/build_executable.py

Write-Host ""
Write-Host "Windows executable created at:"
Write-Host "  $Root\dist\screenfile.exe"
