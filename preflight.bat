@echo off
setlocal
cd /d "%~dp0.."
python -u cskflashtest\scripts\run_multi.py --devices cskflashtest\config\devices.json --dry-run
pause
