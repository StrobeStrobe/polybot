@echo off
REM Fire one sample buy + sell alert to Discord, then exit. Use this to
REM confirm the webhook works before relying on it.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat first to set up the environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run.py watch --test
pause
