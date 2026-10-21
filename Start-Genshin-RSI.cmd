@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.runtime" mkdir "%~dp0.runtime"
>>"%~dp0.runtime\rsi-startup-entry.log" echo [%date% %time%] Launcher entered.
"%~dp0platform\.venv\Scripts\python.exe" -u "%~dp0scripts\run_genshin_rsi.py"
set "RSI_EXIT_CODE=%errorlevel%"
>>"%~dp0.runtime\rsi-startup-entry.log" echo [%date% %time%] Python exited with code %RSI_EXIT_CODE%.
echo RSI exited with code %RSI_EXIT_CODE%.
echo Startup logs: "%~dp0.runtime\rsi-startup"
pause
exit /b %RSI_EXIT_CODE%
