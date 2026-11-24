@echo off
rem SALES_LOCAL_CHECK.bat - checks the whole sales chain on this laptop: real Tally -> sales sync -> a throw-away website.
rem Double-click it from the repo folder (or run it from tools\dev). It needs no .env edit and sends nothing to any real website.
rem Tally Prime must be open with the company loaded and its HTTP server on (port 9000).
if exist "%~dp0feeder.py" (cd /d "%~dp0") else (cd /d "%~dp0..\..")
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe was not found in %CD%
    goto :end
)
if not exist "tools\dev\sales_local_check.py" (
    echo [ERROR] tools\dev\sales_local_check.py was not found in %CD%
    goto :end
)
rem --today is a FIXED test date for this laptop clone (its Tally data ends on 2026-11-09). Leave it as it is.
.venv\Scripts\python.exe tools\dev\sales_local_check.py --today 2026-11-24 %*
:end
echo.
pause
