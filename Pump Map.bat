@echo off
REM Maps pump duty -> RPM and picks a durability-optimised fixed duty.
REM Stops the mobo daemon first. Window stays open so you can read the result.
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','C:\Python314\python.exe C:\HardwareControl\scripts\pump_map.py' -Verb RunAs"
