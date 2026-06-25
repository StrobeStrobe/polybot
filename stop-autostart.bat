@echo off
REM Stop the running watcher and remove it from auto-start.
schtasks /End /TN "Polybot Watcher" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'Polybot Watcher' -Confirm:$false"
echo Polybot auto-start removed. It will no longer run at logon.
pause
