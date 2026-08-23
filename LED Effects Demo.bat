@echo off
REM Cycles all eight spatial effects live on the hardware, 12s each.
REM Takes control from the daemon and releases it on exit.
cd /d C:\HardwareControl\scripts
echo Cycling: wave radial spiral comet rain plasma breathe fire
echo Ctrl+C to stop early.
echo.
C:\Python314\python.exe effect_demo.py
pause
