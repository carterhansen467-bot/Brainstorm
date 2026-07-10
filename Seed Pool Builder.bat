@echo off
rem Double-click me: opens the Brainstorm Seed Pool Builder in your browser.
rem Leave this window open while a scan is running; closing it pauses the
rem scan safely at a checkpoint (press Build again later to resume).
rem
rem Needs Python 3 (https://www.python.org/downloads/ -- any recent version,
rem stdlib only). Using the release zip instead? Just double-click
rem "Seed Pool Builder.exe"; this launcher is for from-source users.
cd /d "%~dp0"
where py >NUL 2>&1
if %errorlevel%==0 (
	py -3 tools\pool_builder_web.py
) else (
	python tools\pool_builder_web.py
)
if %errorlevel% neq 0 pause
