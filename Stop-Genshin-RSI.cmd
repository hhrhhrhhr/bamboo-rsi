@echo off
cd /d "%~dp0"
"%~dp0platform\.venv\Scripts\python.exe" -m startlux.cli rsi-genshin stop --home "%~dp0.runtime\genshin-home"
