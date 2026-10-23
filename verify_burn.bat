@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROUNDS=%~1"
if "%ROUNDS%"=="" set "ROUNDS=1"
set "ACCEPTANCE=%~2"
if "%ACCEPTANCE%"=="" set "ACCEPTANCE=any_asr"

echo ============================================================
echo 压测验收：烧录 + 唤醒/ASR
echo 设备列表：config\devices.json（启用多少台就执行多少台）
echo Rounds: %ROUNDS%
echo Acceptance: %ACCEPTANCE%
echo ============================================================
python -u scripts\run_multi.py --mode burn --devices config\devices.json --rounds %ROUNDS% --acceptance %ACCEPTANCE% --no-hisi --stream
set "CODE=%ERRORLEVEL%"
echo.
echo EXIT_CODE=%CODE%
pause
exit /b %CODE%
