@echo off
REM Double-click this once to install Polybot auto-start at logon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-autostart.ps1"
