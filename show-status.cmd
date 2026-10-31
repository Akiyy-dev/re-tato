@echo off
cd /d "%~dp0"
chcp 65001 > nul
if exist ".data\collector-status.json" (
  type ".data\collector-status.json"
) else (
  echo Collector has not created a status file yet.
)
pause
