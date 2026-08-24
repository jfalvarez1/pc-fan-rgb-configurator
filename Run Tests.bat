@echo off
REM Regression suite. Touches no hardware; safe to run while everything is live.
cd /d "%~dp0scripts"
"C:\Python314\python.exe" selftest.py %*
echo.
pause
