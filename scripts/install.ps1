<#
.SYNOPSIS
  One-time setup: virtual environment, dependencies, tests, data folders.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    # Prefer the Windows launcher, fall back to python on PATH. Need 3.12+.
    $candidates = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3"), @("python"), @("python3"))
    foreach ($c in $candidates) {
        try {
            $exe = $c[0]
            $extra = @($c | Select-Object -Skip 1)
            $ok = & $exe @extra -c "import sys; print(int(sys.version_info >= (3, 12)))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ok -eq "1") { return ,$c }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    throw "Python 3.12+ was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then re-run this script."
}
$pyExe = $py[0]
$pyArgs = @($py | Select-Object -Skip 1)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment (.venv)..."
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "Upgrading pip..."
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

Write-Host "Installing dependencies..."
& $venvPy -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Write-Host "Installing Chrome for Playwright..."
& $venvPy -m playwright install chrome
if ($LASTEXITCODE -ne 0) { throw "Chrome installation failed." }

Write-Host "Creating data folders..."
foreach ($d in "raw", "normalized", "exports", "logs", "images") {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\$d") | Out-Null
}

Write-Host "Running tests..."
& $venvPy -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - see output above." }

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Small test crawl :  .\scripts\run.ps1 -Command crawl -MaxListings 10"
Write-Host "Resume           :  .\scripts\run.ps1 -Command resume"
Write-Host "Detached/overnight: .\scripts\run.ps1 -Command resume -Detached -Loop"
