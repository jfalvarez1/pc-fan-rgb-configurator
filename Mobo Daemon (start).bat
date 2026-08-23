@echo off
REM APPLIES settings: pins the pump at a fixed duty, curves the radiator fans.
powershell -NoProfile -Command "Start-Process -FilePath 'C:\Python314\pythonw.exe' -ArgumentList 'C:\HardwareControl\scripts\mobo_daemon.py','--apply','--log' -Verb RunAs"
