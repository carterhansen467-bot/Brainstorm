@echo off
setlocal
title Brainstorm Installer
echo Brainstorm Windows installer / updater
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-or-update.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo The update was not completed. Read the message above; your seed pools
  echo and settings were not intentionally removed.
) else (
  echo Done. You can close this window and start Balatro.
)
pause
exit /b %RESULT%
