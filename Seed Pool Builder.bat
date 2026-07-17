@echo off
rem Double-click me: opens the Brainstorm Seed Pool Builder in your browser.
rem Leave this window open while a scan is running; closing it pauses the
rem scan safely (press Build again later to resume).
rem Release installs keep the standalone app and scanner together in the
rem "Seed Pool Builder" folder. Source checkouts fall back to Python 3.
cd /d "%~dp0"
if exist "%~dp0Seed Pool Builder\Seed Pool Builder.exe" (
  "%~dp0Seed Pool Builder\Seed Pool Builder.exe"
  goto done
)
where py >nul 2>nul && (py -3 tools\pool_builder_web.py & goto done)
where python >nul 2>nul && (python tools\pool_builder_web.py & goto done)
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo (check "Add python.exe to PATH") or run the latest Brainstorm Windows
echo installer to add the standalone Seed Pool Builder, then run this again.
pause
:done
