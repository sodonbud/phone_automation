@echo off
title Phone Test Builder - Update
cd /d "%~dp0"

echo ================================================
echo   Phone Test Builder - Update
echo ================================================
echo.

:: Check if git is available
git --version >nul 2>&1
if errorlevel 1 goto :no_git

:: ── Git update ────────────────────────────────────
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 goto :no_git

for /f %%i in ('git rev-parse --short HEAD') do set OLD_VER=%%i

echo Pulling latest changes...
echo.
git pull origin main

if errorlevel 1 (
    echo.
    echo ERROR: git pull failed.
    pause
    exit /b 1
)

for /f %%i in ('git rev-parse --short HEAD') do set NEW_VER=%%i

if "%OLD_VER%"=="%NEW_VER%" (
    echo Already up to date. [%NEW_VER%]
) else (
    echo Updated: %OLD_VER% ^> %NEW_VER%
    echo.
    echo Changes:
    git log %OLD_VER%..%NEW_VER% --oneline
)
goto :install_deps

:: ── No git — download ZIP via PowerShell ──────────
:no_git
echo Git not found. Downloading update via PowerShell...
echo.

set REPO_ZIP=https://github.com/sodonbud/phone_automation/archive/refs/heads/main.zip
set TMP_ZIP=%TEMP%\phone_automation_update.zip
set TMP_DIR=%TEMP%\phone_automation_update

:: Download ZIP
powershell -Command "Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%TMP_ZIP%' -UseBasicParsing"
if errorlevel 1 (
    echo ERROR: Download failed. Check your internet connection.
    pause
    exit /b 1
)

:: Extract ZIP
if exist "%TMP_DIR%" rd /s /q "%TMP_DIR%"
powershell -Command "Expand-Archive -Path '%TMP_ZIP%' -DestinationPath '%TMP_DIR%' -Force"

:: Copy files — skip config.py and data/ to preserve local settings
echo Copying updated files...
set SRC=%TMP_DIR%\phone_automation-main

xcopy "%SRC%\*.py"  "%~dp0" /Y /Q
xcopy "%SRC%\*.bat" "%~dp0" /Y /Q
xcopy "%SRC%\*.txt" "%~dp0" /Y /Q
xcopy "%SRC%\*.md"  "%~dp0" /Y /Q

:: Restore config.py if it got overwritten (xcopy won't overwrite excluded files, but just in case)
if exist "%~dp0config.example.py" (
    if not exist "%~dp0config.py" (
        copy "%~dp0config.example.py" "%~dp0config.py" >nul
        echo Created config.py from config.example.py — please fill in your device settings.
    )
)

:: Cleanup
del "%TMP_ZIP%" >nul 2>&1
rd /s /q "%TMP_DIR%" >nul 2>&1

echo Update complete.

:: ── Install dependencies ──────────────────────────
:install_deps
echo.
echo Installing dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo WARNING: pip install failed. Try running: pip install flask openpyxl pillow
)

echo.
echo ================================================
echo   Done! Run start_web.bat to launch.
echo ================================================
echo.
pause
