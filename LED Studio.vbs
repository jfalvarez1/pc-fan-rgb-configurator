' Launcher for LED Studio.
' Reuses the server if it is already running, otherwise starts it hidden,
' then opens the editor in the default browser. No console window either way.
Option Explicit
Dim sh, http, alive
Set sh = CreateObject("WScript.Shell")

alive = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://localhost:8770/api/layout", False
http.Send
If Err.Number = 0 And http.Status = 200 Then alive = True
Err.Clear
On Error GoTo 0

If Not alive Then
    sh.Run """C:\Python314\pythonw.exe"" ""C:\HardwareControl\scripts\led_studio.py""", 0, False
    WScript.Sleep 2500
End If

sh.Run "http://localhost:8770", 1, False
