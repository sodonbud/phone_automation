@echo off
title Phone Test Builder - Update
cd /d "%~dp0"

echo ================================================
echo   Phone Test Builder - Update
echo ================================================
echo.

:: Check git
git --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Git not found. Install Git from https://git-scm.com
    pause
    exit /b 1
)

:: Check if this is a git repo
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo ERROR: Not a git repository.
    echo Clone first: git clone https://github.com/sodonbud/phone_automation.git
    pause
    exit /b 1
)

:: Save current version
for /f %%i in ('git rev-parse --short HEAD') do set OLD_VER=%%i

echo Pulling latest changes from main...
echo.
git pull origin main

if errorlevel 1 (
    echo.
    echo ERROR: Update failed. Check your internet connection or contact Sodonbud.
    pause
    exit /b 1
)

:: Get new version
for /f %%i in ('git rev-parse --short HEAD') do set NEW_VER=%%i

echo.
if "%OLD_VER%"=="%NEW_VER%" (
    echo Already up to date. [%NEW_VER%]
) else (
    echo Updated: %OLD_VER% → %NEW_VER%
    echo.
    echo Changes:
    git log %OLD_VER%..%NEW_VER% --oneline
)

echo.
echo Installing dependencies...
pip install -r requirements.txt -q

echo.
echo ================================================
echo   Done! Run start_web.bat to launch.
echo ================================================
echo.
pause
