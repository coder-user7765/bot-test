<#
.SYNOPSIS
  Runs the crawler from the project's virtual environment, logs to data\logs, resumes safely.

.DESCRIPTION
  -Command crawl|resume   Both continue from the saved SQLite state (default: resume).
  -MaxListings / -MaxPages / -MaxRequests   Override config\crawler.yaml for this run (0 = unlimited).
  -Loop       Restart automatically after a crash or network failure (not after a block or a
              deliberate stop). Gives up after 5 consecutive failures.
  -Detached   Start the crawler in a hidden background process that keeps running after you
              disconnect from RDP or close this window.
  -Stop       Ask a running crawler to finish in-flight requests and exit (graceful).

.EXAMPLE
  .\scripts\run.ps1 -Command crawl -MaxListings 10        # small test
  .\scripts\run.ps1 -Command resume -MaxListings 0 -MaxPages 0 -Detached -Loop   # overnight
  .\scripts\run.ps1 -Stop
#>
param(
    [ValidateSet("crawl", "resume")][string]$Command = "resume",
    [int]$MaxListings = -1,
    [int]$MaxPages = -1,
    [int]$MaxRequests = -1,
    [switch]$Loop,
    [switch]$Detached,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "Virtual environment not found. Run .\scripts\install.ps1 first." }

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$logDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$consoleLog = Join-Path $logDir "console.log"

if ($Stop) {
    & $venvPy -m crawler stop
    exit $LASTEXITCODE
}

$crawlArgs = @($Command)
if ($MaxListings -ge 0) { $crawlArgs += @("--max-listings", $MaxListings) }
if ($MaxPages -ge 0) { $crawlArgs += @("--max-pages", $MaxPages) }
if ($MaxRequests -ge 0) { $crawlArgs += @("--max-requests", $MaxRequests) }

if ($Detached) {
    # Re-launch this script hidden and independent of the current session.
    $inner = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Command", $Command)
    if ($MaxListings -ge 0) { $inner += @("-MaxListings", $MaxListings) }
    if ($MaxPages -ge 0) { $inner += @("-MaxPages", $MaxPages) }
    if ($MaxRequests -ge 0) { $inner += @("-MaxRequests", $MaxRequests) }
    if ($Loop) { $inner += "-Loop" }
    $p = Start-Process -FilePath "powershell.exe" -ArgumentList $inner -WindowStyle Hidden -WorkingDirectory $Root -PassThru
    Set-Content -Path (Join-Path $Root "data\crawler.pid") -Value $p.Id
    Write-Host "Crawler started in the background (PID $($p.Id))."
    Write-Host "  Progress : .\scripts\run.ps1 -Stop    (graceful stop)   |   $venvPy -m crawler status"
    Write-Host "  Logs     : $logDir"
    exit 0
}

$ErrorActionPreference = "Continue"   # native stderr must not abort the loop (PowerShell 5.1)
$code = 0
$failures = 0
try {
    while ($true) {
        "[{0}] starting: python -m crawler {1}" -f (Get-Date -Format s), ($crawlArgs -join " ") | Tee-Object -FilePath $consoleLog -Append
        & $venvPy -m crawler @crawlArgs 2>&1 | Tee-Object -FilePath $consoleLog -Append
        $code = $LASTEXITCODE
        "[{0}] exit code {1}" -f (Get-Date -Format s), $code | Tee-Object -FilePath $consoleLog -Append

        if ($code -eq 0) { break }                       # finished or limit reached
        if ($code -eq 3) {                               # blocked: never retry automatically
            Write-Host "Crawler stopped because automated access appears restricted. No bypass will be attempted." -ForegroundColor Red
            break
        }
        if ($code -eq 130) { Write-Host "Stopped on request. Run again with -Command resume to continue."; break }
        if (-not $Loop) { break }
        $failures++
        if ($failures -ge 5) { Write-Host "Giving up after 5 consecutive failures." -ForegroundColor Red; break }
        Write-Host "Failure $failures/5 - retrying in 60 s (state is saved)..."
        Start-Sleep -Seconds 60
        $crawlArgs = @("resume") + @($crawlArgs | Select-Object -Skip 1)
    }
}
finally {
    Remove-Item -Path (Join-Path $Root "data\crawler.pid") -ErrorAction SilentlyContinue
    Write-Host "To continue later: .\scripts\run.ps1 -Command resume"
}
exit $code
