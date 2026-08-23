@echo off
REM Matched-duration capture: 1908 seconds, exactly the same as the baseline
REM run, so equilibrium can be compared like-for-like. Stops automatically.
set /p LABEL="Session name (e.g. forza-tuned-long): "
if "%LABEL%"=="" set LABEL=tuned-long
cd /d C:\HardwareControl\scripts
echo.
echo Logging for 1908s (31.8 min) - matches the baseline run exactly.
echo Drive FREE ROAM, same as the baseline. It will stop on its own.
echo.
C:\Python314\python.exe bench_logger.py --label "%LABEL%" --interval 1 --seconds 1908
pause
