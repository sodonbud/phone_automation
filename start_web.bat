@echo off
title Phone Test Builder
cd /d "%~dp0"

echo Starting Phone Test Builder...
echo Auto-reload is ON — save any .py file to restart the server automatically.
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python and add to PATH.
    pause
    exit /b 1
)

:: Install dependencies if needed
pip install flask openpyxl -q

echo.
echo Opening http://localhost:5000
echo Press Ctrl+C to stop.
echo.

:: Small delay so Flask binds before browser opens
ping -n 2 127.0.0.1 >nul
start "" "http://localhost:5000"

python web_builder.py

pause
