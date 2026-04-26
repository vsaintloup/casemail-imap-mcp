@echo off
setlocal
cd /d "%~dp0"
if exist "dist\CaseMailLauncher.exe" (
  "dist\CaseMailLauncher.exe"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\build_launcher.ps1"
  if errorlevel 1 (
    pause
    exit /b 1
  )
  "dist\CaseMailLauncher.exe"
)
