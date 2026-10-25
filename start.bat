@echo off
chcp 65001 >nul
cd /d %~dp0
set PORT=8085
title RT Workshop - http://localhost:%PORT%  (ma t2felsh el shasha di)
if not exist "%~dp0tools\python\python.exe" (
  echo Dev mode: run "powershell -ExecutionPolicy Bypass -File setup.ps1" first (or use dist\install.exe to install).
  pause
  exit /b 1
)
rem starts PostgreSQL (and finishes any missing setup step); RT_QUIET shows errors in a popup
set RT_QUIET=1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
set RT_QUIET=
if errorlevel 1 goto failed
rem open the browser after 3 seconds so the server is up first
start "" /b cmd /c "timeout /t 3 >nul & start "" http://localhost:%PORT%"
"%~dp0tools\python\python.exe" server.py
:failed
echo.
echo El barnameg 2afal. Law fe error fo2, sawarha we ab3atha.
pause
