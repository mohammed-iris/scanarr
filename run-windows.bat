@echo off
title Scanarr
cd /d "%~dp0"

rem ---- change the web UI port here ----
set SCANARR_PORT=9797

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is needed. Get it from https://www.python.org/downloads/
  echo During setup tick "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

if not exist .venv (
  echo First run: setting up Scanarr...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt

set CONFIG_DIR=%~dp0config
if not exist config mkdir config
if not exist config\settings.json (
  > config\settings.json echo {"qbit": {"url": "http://localhost:9090"}, "prowlarr": {"url": "http://localhost:9696"}}
)

echo.
echo Scanarr is starting at http://localhost:%SCANARR_PORT%  (keep this window open)
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do for /f "tokens=*" %%b in ("%%a") do echo From your phone on the same Wi-Fi: http://%%b:%SCANARR_PORT%
echo (If Windows asks, allow Python through the firewall on Private networks.)
start "" cmd /c "timeout /t 3 >nul & start http://localhost:%SCANARR_PORT%"
python -m uvicorn app.main:app --host 0.0.0.0 --port %SCANARR_PORT%
pause
