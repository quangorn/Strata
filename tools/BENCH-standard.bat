@echo off
rem ===========================================================================
rem  Strata - standard benchmark (standard-v1, docs/BENCHMARK_PROTOCOL.md)
rem  A thin wrapper around bench_standard.py - double click it or pass
rem  arguments:  bench-standard.bat [options]
rem ===========================================================================
setlocal EnableExtensions

set "HERE=%~dp0"
set "PY="
if exist "%LOCALAPPDATA%\Strata\terminal\current\python\python.exe" set "PY=%LOCALAPPDATA%\Strata\terminal\current\python\python.exe"
if not defined PY for %%P in (python.exe python3.exe py.exe) do if not defined PY if exist "%%~$PATH:P" set "PY=%%~$PATH:P"
if not defined PY (
    echo [x] no Python found - install Strata, the terminal carries one
    pause
    exit /b 1
)

"%PY%" "%HERE%bench_standard.py" %*
set "RC=%errorlevel%"
echo.
pause
endlocal & exit /b %RC%
