' Launches LED Studio.
'
' It is a standalone executable now, not a script. The exe carries its own
' icon and its own taskbar identity, so nothing here has to hide a console or
' borrow pythonw's. This file is kept only so older shortcuts still work -
' point new ones straight at the exe.
'
' Falls back to running the script under pythonw if the exe has not been built
' yet (python scripts\build_exe.py), so a fresh checkout is not dead on
' arrival.
Option Explicit
Dim sh, fso, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

exe = "C:\HardwareControl\LEDStudio\LEDStudio.exe"
If fso.FileExists(exe) Then
    sh.CurrentDirectory = "C:\HardwareControl\LEDStudio"
    sh.Run """" & exe & """", 1, False
Else
    sh.CurrentDirectory = "C:\HardwareControl\scripts"
    sh.Run """C:\Python314\pythonw.exe"" ""C:\HardwareControl\scripts\led_studio_native.py""", 1, False
End If
