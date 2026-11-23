@echo off
rem The fork's checkout launcher: `bin\` is the first entry of the user PATH on this PC, so `strata` here runs
rem this checkout. Upstream moved the Terminal into `terminal\`, so this script forwards to the terminal's own
rem launcher (`terminal\bin\strata.cmd`), which finds `terminal\strata.py` and the interpreter.
rem
rem The one thing it adds: the checkout's own `.venv` (it carries `rich` and `textual`, which the screens need) is
rem named through STRATA_PYTHON, because the terminal launcher looks for `.venv` beside its own `strata.py`
rem (`terminal\.venv`), not at the checkout root.
setlocal
set "HERE=%~dp0"
if exist "%HERE%..\.venv\Scripts\python.exe" set "STRATA_PYTHON=%HERE%..\.venv\Scripts\python.exe"
"%HERE%..\terminal\bin\strata.cmd" %*
exit /b %errorlevel%