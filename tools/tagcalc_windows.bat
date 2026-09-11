@echo off
setlocal DisableDelayedExpansion
title Brainstorm Tag Calculator
set "TAGCALC_SCRIPT=%~dp0tagcalc.py"
set "TAGCALC_PY="

if not exist "%TAGCALC_SCRIPT%" goto missing_script
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "TAGCALC_PY=py -3"
if defined TAGCALC_PY goto python_ready
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "TAGCALC_PY=python"
if defined TAGCALC_PY goto python_ready
python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1
if not errorlevel 1 set "TAGCALC_PY=python3"
if defined TAGCALC_PY goto python_ready
echo Python 3.9 or newer is required. Install Python, then reopen this launcher.
echo The packaged Seed Pool Program does not provide a Python interpreter.
pause
exit /b 1

:python_ready
set "TAGCALC_FIRST=%~1"
if not defined TAGCALC_FIRST goto interactive
if "%TAGCALC_FIRST:~0,1%"=="-" goto command_line
if not "%~2"=="" goto too_many_inputs

:interactive
set "TAGCALC_INPUT="
if not "%~1"=="" set "TAGCALC_INPUT=%~f1"
echo Batch tag calculator
echo CSV and NDJSON rows can each have their own second_tag or baseline_copy.
echo Leave the shared second-tag answer blank to use those saved values.
echo.
rem Pass prompted values as Python arguments, not as shell command text.
%TAGCALC_PY% -c "import os,pathlib,subprocess,sys,uuid; raw=os.environ.get('TAGCALC_INPUT') or input('Input CSV, NDJSON, or .bspool path: '); raw.strip() or sys.exit('No input file selected.'); source=pathlib.Path(raw.strip().strip(chr(34))).expanduser().resolve(); source.is_file() or sys.exit('Input file was not found: '+str(source)); second=input('Shared second-tag position, e.g. a4b (optional): ').strip(); snapshot=input('Matching native_search.cfg path (blank only if A1-38 tags are already recorded): ').strip().strip(chr(34)) if source.suffix.lower()=='.bspool' else ''; output=source.parent/(source.stem+'-tagcalc-'+uuid.uuid4().hex[:8]); print('Results folder: '+str(output),flush=True); args=[sys.executable,os.environ['TAGCALC_SCRIPT'],'--input',str(source),'--output-dir',str(output),'--top','1000']; args+=['--second-tag',second] if second else []; args+=['--snapshot',snapshot] if snapshot else []; sys.exit(subprocess.call(args))"
set "TAGCALC_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %TAGCALC_RESULT%

:command_line
%TAGCALC_PY% "%TAGCALC_SCRIPT%" %*
exit /b %ERRORLEVEL%

:too_many_inputs
echo Drop one input file at a time. Put multiple labeled seeds in that file.
echo For command-line options, begin with --input. See TAGCALC.md.
pause
exit /b 1

:missing_script
echo Keep this launcher beside tagcalc.py and the other files in tools.
pause
exit /b 1
