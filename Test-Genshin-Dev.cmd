@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title StartLux - Local Genshin Development Test
powershell.exe -NoProfile -NonInteractive -Command "$identity=[Security.Principal.WindowsIdentity]::GetCurrent(); $principal=[Security.Principal.WindowsPrincipal]::new($identity); if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if errorlevel 1 (
  echo Please right-click this file and choose Run as administrator.
  echo The game and BetterGI require the same elevated permissions.
  pause
  exit /b 1
)
if not exist "%~dp0platform\.venv\Scripts\pythonw.exe" (
  echo The local Python development environment is missing.
  pause
  exit /b 2
)
if not exist "%~dp0scripts\genshin_dev_controller.py" (
  echo The local test controller is missing.
  pause
  exit /b 3
)
"%~dp0platform\.venv\Scripts\python.exe" "%~dp0scripts\genshin_dev_controller.py" run
if errorlevel 1 (
  echo Could not prepare the local test command.
  pause
  exit /b 4
)
start "" /b "%~dp0platform\.venv\Scripts\pythonw.exe" "%~dp0scripts\genshin_dev_controller.py" serve
echo The development test controller has started.
echo Return to the Genshin game window. Codex will monitor the test logs.
echo Credentials are read from your existing Windows encrypted storage.
echo Local status: %~dp0.runtime\live-state.json
pause
