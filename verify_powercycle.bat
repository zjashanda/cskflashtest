@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROUNDS=%~1"
if "%ROUNDS%"=="" set "ROUNDS=1"
set "DEVICE=%~2"
if "%DEVICE%"=="" set "DEVICE=dev01"
set "ACCEPTANCE=%~3"
if "%ACCEPTANCE%"=="" set "ACCEPTANCE=any_asr"

echo ============================================================
echo 压测验收：上下电 + CSK 初始化 + 唤醒/ASR
echo 设备：%DEVICE%
echo 轮数：%ROUNDS%
echo 验收模式：%ACCEPTANCE%
echo ============================================================
python -u scripts\run.py --mode powercycle --devices config\devices.json --device-id %DEVICE% --rounds %ROUNDS% --acceptance %ACCEPTANCE% --no-hisi
set "CODE=%ERRORLEVEL%"
echo.
echo EXIT_CODE=%CODE%
pause
exit /b %CODE%
