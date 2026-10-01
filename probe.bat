@echo off
REM Double-click to run the one-off probe (close the monitor window first)
cd /d "%~dp0"
py -3 probe.py 2>nul || python probe.py
