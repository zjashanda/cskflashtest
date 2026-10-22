@echo off
setlocal
cd /d "%~dp0"
python -u scripts\run_multi.py --devices config\devices.json %*
if errorlevel 1 pause
