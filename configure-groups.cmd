@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0.tools\probe"
set "PYTHONUTF8=1"
python -m tatocollector.group_picker %*
echo.
pause
