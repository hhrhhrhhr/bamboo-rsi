@echo off
setlocal
cd /d "%~dp0"
"%~dp0platform\.venv\Scripts\python.exe" "%~dp0scripts\genshin_dev_controller.py" stop
