@echo off
REM Launches the motherboard fan GUI with administrator rights.
REM LibreHardwareMonitor needs elevation to load its signed kernel driver.
powershell -NoProfile -Command "Start-Process -FilePath 'C:\Python314\pythonw.exe' -ArgumentList 'C:\HardwareControl\scripts\mobo_fan_gui.py' -Verb RunAs"
