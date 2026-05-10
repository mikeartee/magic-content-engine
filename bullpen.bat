@echo off
cd /d "%~dp0"

REM Kill anything already on port 5000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 "') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Launch Bullpen — logs errors to bullpen-error.log
python scripts/run_gui.py 2>bullpen-error.log
if %errorlevel% neq 0 (
    echo Error starting Bullpen. Check bullpen-error.log for details.
    pause
)
