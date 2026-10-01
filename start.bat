@echo off
REM Double-click to run the monitor on Windows. Restarts automatically if it crashes.
cd /d "%~dp0"

REM Prefer the "py" launcher (installed by python.org), fall back to "python"
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY python --version >nul 2>&1 && set PY=python
if not defined PY (
    echo.
    echo Python was not found.
    echo Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation.
    echo Then close this window and run start.bat again.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo Missing .env file - copy .env.example to .env and fill in your Telegram details.
    echo.
    pause
    exit /b 1
)

%PY% -m pip install -q -r requirements.txt
:loop
%PY% monitor.py
echo Monitor stopped - restarting in 5 seconds (close this window to quit)
timeout /t 5 /nobreak >nul
goto loop
