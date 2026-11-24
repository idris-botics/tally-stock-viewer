@echo off
rem SALES_PROBE.bat - a read-only look at what the sales sync would send. Nothing is pushed and nothing is saved.
rem Double-click it, or run it in a command window with a number of days:  SALES_PROBE.bat --days 60
rem It works from the feeder folder, or from tools\shop inside it.
if exist "%~dp0feeder.py" (cd /d "%~dp0") else (cd /d "%~dp0..\..")
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe was not found in %CD%
    goto :end
)
if not exist "tools\shop\sales_probe.py" (
    echo [ERROR] tools\shop\sales_probe.py was not found in %CD%. Run SHOP_UPDATE.bat first.
    goto :end
)
.venv\Scripts\python.exe tools\shop\sales_probe.py %*
:end
echo.
pause
