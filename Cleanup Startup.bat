@echo off
powershell -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\HardwareControl\cleanup_startup.ps1' -Verb RunAs"
