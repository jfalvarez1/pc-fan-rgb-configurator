@echo off
REM DRY RUN - reads sensors and reports duty + control mode. Changes nothing.
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k','C:\Python314\python.exe C:\HardwareControl\scripts\mobo_daemon.py --once' -Verb RunAs"
