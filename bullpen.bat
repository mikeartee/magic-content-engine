@echo off
cd /d "%~dp0"

REM Kill anything already on port 5000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 "') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Launch Bullpen (tray icon + browser open handled by run_gui.py)
pythonw scripts/run_gui.py
