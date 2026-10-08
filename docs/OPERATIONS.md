# Operations (Windows)

## Install
```powershell
cd ouedkniss-data-lab
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```
Needs Python 3.12+ (python.org installer, tick *Add to PATH*). The script creates `.venv`, installs dependencies,
creates `data\…` folders and runs the tests.

## Run
```powershell
.\scripts\run.ps1 -Command crawl -MaxListings 10        # small test, foreground
.\scripts\run.ps1 -Command resume                       # continue from SQLite state
.\scripts\run.ps1 -Command resume -MaxListings 0 -MaxPages 0 -Loop     # no limits, auto-restart after crashes
```
Console output is also appended to `data\logs\console.log`; structured events go to `data\logs\crawler.log`
(rotating, 10 MB × 5).

## Overnight / surviving RDP disconnects
```powershell
.\scripts\run.ps1 -Command resume -MaxListings 0 -MaxPages 0 -Detached -Loop
```
`-Detached` uses `Start-Process … -WindowStyle Hidden`; the process is **not** tied to the RDP window, so it keeps
running after you disconnect (do **Disconnect**, not **Sign out** – signing out ends your processes).
Equivalent manual command:
```powershell
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\path\ouedkniss-data-lab\scripts\run.ps1','-Command','resume','-Loop'
```
Check on it any time: `.\.venv\Scripts\python.exe -m crawler status`
Stop it gracefully: `.\scripts\run.ps1 -Stop` (drops `data\STOP`; the crawler finishes in-flight requests and exits).
Killing the process is also safe (state is transactional), the next run just re-queues the interrupted listings.

## Task Scheduler (survives logoff and reboot)
GUI: *Task Scheduler → Create Task…*
* **General**: name `ouedkniss-data-lab`; *Run whether user is logged on or not*; *Do not store password* is fine if it only needs local files.
* **Triggers**: *At startup* (delay 1 min) and/or *Daily 22:00*.
* **Actions**: Program `powershell.exe`; Arguments
  `-NoProfile -ExecutionPolicy Bypass -File "C:\path\ouedkniss-data-lab\scripts\run.ps1" -Command resume -MaxListings 0 -MaxPages 0 -Loop`;
  *Start in* `C:\path\ouedkniss-data-lab`.
* **Settings**: *If the task is already running: Do not start a new instance*; *Stop the task if it runs longer than* → untick or set 12 h.

PowerShell equivalent:
```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory "C:\path\ouedkniss-data-lab" `
     -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\path\ouedkniss-data-lab\scripts\run.ps1" -Command resume -MaxListings 0 -MaxPages 0 -Loop'
$t = New-ScheduledTaskTrigger -Daily -At 10pm
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName "ouedkniss-data-lab" -Action $a -Trigger $t -Settings $s
```
Because every run resumes from SQLite, re-triggering is harmless.

## Exit codes
`0` finished or a limit was reached · `1` error (network down …) · `3` **blocked** – automated access appears restricted
(the wrapper never auto-restarts after this) · `130` stopped by user.

## Rate limiting & safety
* Defaults: 2 s delay + 0–1 s jitter, 2 concurrent requests (~0.4 req/s). Do not raise concurrency above what you need.
* HTTP 429: all workers pause (`Retry-After` or exponential backoff), request rate is halved (up to 16×), 3 consecutive
  429s stop the crawl. 3 consecutive 403s, any CAPTCHA/Cloudflare challenge, or robots.txt denial stop it immediately with
  *"Crawler stopped because automated access appears restricted. No bypass will be attempted."*
  Wait (hours), then `resume`. There is no proxy, UA rotation or CAPTCHA/challenge bypass anywhere
  (one fixed browser User-Agent is sent, configurable via `user_agent`).
* Temporary errors (408 425 429 500 502 503 504, timeouts) are retried with backoff `retry_count` times, then the
  listing is re-queued up to `max_url_attempts` runs, then `FAILED`. Permanent errors (404 etc.) are not retried.

## Maintenance
| Task | Command |
|---|---|
| Progress | `python -m crawler status` |
| Failed list / retry | `python -m crawler failed` · `python -m crawler failed --requeue` |
| Pick up new listings in finished categories | `python -m crawler resume --restart-categories` |
| Rebuild outputs | `python -m crawler export` |
| Quality report | `python -m crawler report` |
| Start over (deletes DB + JSONL) | `python -m crawler reset` |
| Back up | copy `data\crawler.db` (stop the crawler first, or copy `crawler.db-wal` too) |

## Troubleshooting
* `running scripts is disabled` → use `powershell -ExecutionPolicy Bypass -File …` (the examples do).
* Garbled Arabic in the console → the scripts set `PYTHONUTF8=1`; files are always UTF-8 (open with VS Code, not old Notepad).
* Every listing `FAILED` with `GraphQL error` → the site changed its API; see `docs/PARSING.md` §5.
* `database is locked` → another crawler instance is running; check `data\crawler.pid` / Task Manager.
