' Launches the native LED Studio (Tk app - no browser, no HTTP).
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\HardwareControl\scripts"
sh.Run """C:\Python314\pythonw.exe"" ""C:\HardwareControl\scripts\led_studio_native.py""", 1, False
