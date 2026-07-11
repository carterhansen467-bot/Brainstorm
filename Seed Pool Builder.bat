@echo off
rem Double-click me: opens the Brainstorm Seed Pool Builder in your browser.
rem Leave this window open while a scan is running; closing it pauses the
rem scan safely (press Build again later to resume).
rem Needs Python 3 from python.org or the Microsoft Store -- or use the
rem standalone "Seed Pool Builder.exe" from the release zip instead (no
rem Python needed).
cd /d "%~dp0"
where py >nul 2>nul && (py -3 tools\pool_builder_web.py & goto done)
where python >nul 2>nul && (python tools\pool_builder_web.py & goto done)
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo (check "Add python.exe to PATH") or use "Seed Pool Builder.exe" from the
echo release zip, then run this again.
pause
:done
