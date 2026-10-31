@echo off
cd /d "%~dp0"
if not exist ".data" mkdir ".data"
type nul > ".data\stop.request"
echo Stop requested. The collector will finish its current HTTP attempt and exit.
pause
