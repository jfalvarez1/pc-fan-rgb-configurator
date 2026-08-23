@echo off
REM High-resolution thermal logging for a benchmark or gaming session.
REM Leave this window open while you play. Ctrl+C stops it and prints a summary.
set /p LABEL="Session name (e.g. forza-bench): "
if "%LABEL%"=="" set LABEL=session
cd /d C:\HardwareControl\scripts
C:\Python314\python.exe bench_logger.py --label "%LABEL%" --interval 1
pause
