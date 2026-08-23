@echo off
powershell -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\HardwareControl\install_openrgb_task.ps1' -Verb RunAs"
