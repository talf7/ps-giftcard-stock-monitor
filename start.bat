@echo off
REM Double-click to run the monitor on Windows. Restarts automatically if it crashes.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
:loop
python monitor.py
echo Monitor stopped - restarting in 5 seconds (close this window to quit)
timeout /t 5 /nobreak >nul
goto loop
