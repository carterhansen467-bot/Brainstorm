@echo off
rem Double-click me: opens the Brainstorm Seed Pool Organizer in your browser.
rem Leave this window open while using the organizer.
cd /d "%~dp0"
if exist "%~dp0Seed Pool Builder\Seed Pool Organizer.exe" (
  "%~dp0Seed Pool Builder\Seed Pool Organizer.exe"
  goto done
)
where py >nul 2>nul && (py -3 tools\pool_organizer_web.py & goto done)
where python >nul 2>nul && (python tools\pool_organizer_web.py & goto done)
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo and check "Add python.exe to PATH", or run the latest Brainstorm Windows
echo installer to add the standalone Seed Pool Organizer, then run this again.
pause
:done
