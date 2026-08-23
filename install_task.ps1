# Creates a Scheduled Task so the elevated motherboard daemon starts at logon
# with highest privileges - no UAC prompt, ever again.
# Run this ONCE as administrator.

$name = "HardwareControl-MoboDaemon"
$exe  = "C:\Python314\pythonw.exe"
$args = "C:\HardwareControl\scripts\mobo_daemon.py --apply --log"

Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$action  = New-ScheduledTaskAction -Execute $exe -Argument $args `
             -WorkingDirectory "C:\HardwareControl\scripts"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$princ   = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
             -LogonType Interactive -RunLevel Highest
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
             -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $princ -Settings $set `
    -Description "Elevated pump + radiator fan control, publishes sensors.json" | Out-Null

Write-Host "Task '$name' registered (runs at logon, highest privileges)." -ForegroundColor Green
Write-Host "Starting it now..."
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 8
Get-ScheduledTask -TaskName $name | Select-Object TaskName,State | Format-Table -AutoSize
Write-Host ""
Write-Host "To remove later:  Unregister-ScheduledTask -TaskName '$name' -Confirm:`$false"
Write-Host ""
Read-Host "Press Enter to close"
