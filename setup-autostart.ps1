# Polybot — install hidden auto-start at logon (Windows Task Scheduler).
# Run once via setup-autostart.bat. Sets up the Python environment if needed,
# then registers a task that launches the watcher hidden at every logon and
# restarts it if it ever crashes. No console window, no babysitting.

$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
Set-Location $dir

if (-not (Test-Path '.env')) {
    Write-Host 'No .env found in this folder.' -ForegroundColor Yellow
    Write-Host 'Create a file named  .env  with one line:'
    Write-Host '  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...'
    Read-Host 'Press Enter to exit'
    exit 1
}

# Locate Python (py launcher preferred).
$py = 'python'
if (Get-Command py -ErrorAction SilentlyContinue) { $py = 'py' }

$pyw = Join-Path $dir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pyw)) {
    Write-Host 'First-time setup: creating Python environment...'
    & $py -m venv .venv
    & '.venv\Scripts\python.exe' -m pip install --upgrade pip
    & '.venv\Scripts\python.exe' -m pip install -r requirements.txt
}

# pythonw.exe = windowless Python, so the watcher runs with no console.
$action  = New-ScheduledTaskAction -Execute $pyw -Argument 'run.py watch' -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$settings.ExecutionTimeLimit = 'PT0S'  # no run-time limit (default kills after 3 days)

Register-ScheduledTask -TaskName 'Polybot Watcher' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Polymarket copy-trade alerter' -Force | Out-Null

Start-ScheduledTask -TaskName 'Polybot Watcher'

Write-Host ''
Write-Host 'Done. Polybot now starts hidden at every logon — and is running now.' -ForegroundColor Green
Write-Host 'Confirm it is alive any time by opening:  state\last_check.txt'
Write-Host 'Stop / remove it with:  stop-autostart.bat'
Read-Host 'Press Enter to exit'
