@echo off
REM One-time setup: registers the elevated motherboard daemon to run at logon.
powershell -NoProfile -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\HardwareControl\install_task.ps1' -Verb RunAs"
