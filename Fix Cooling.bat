@echo off
REM Repairs a fan header that has stopped obeying its curve:
REM clears duplicate daemons and any program holding the same chip.
REM Needs administrator rights, so it asks for them.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0scripts\fix_cooling.ps1'"
