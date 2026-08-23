# Registers OpenRGB to start at logon, elevated, with its SDK server running.
# Elevation matters: without it OpenRGB cannot load PawnIO and loses SMBus
# access, which means no motherboard ARGB headers - i.e. no Arctic cooler and
# no GPU logo. Run ONCE as administrator.

$name = "HardwareControl-OpenRGB"
$exe  = "C:\HardwareControl\OpenRGB\OpenRGB.exe"

if (-not (Test-Path $exe)) { Write-Host "OpenRGB.exe not found at $exe" -ForegroundColor Red; Read-Host; exit 1 }

Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$action  = New-ScheduledTaskAction -Execute $exe `
             -Argument "--server --startminimized" `
             -WorkingDirectory "C:\HardwareControl\OpenRGB"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT15S"          # let USB/HID enumerate before we grab devices
$princ   = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
             -LogonType Interactive -RunLevel Highest
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Principal $princ -Settings $set `
    -Description "OpenRGB with SDK server on 6742, elevated for SMBus access" | Out-Null

Write-Host "Task '$name' registered (logon, +15s delay, highest privileges)." -ForegroundColor Green
Write-Host ""
Write-Host "NOTE: OpenRGB is already running from earlier, so this task is not"
Write-Host "started now - it takes effect on your next reboot."
Write-Host ""
Write-Host "To remove:  Unregister-ScheduledTask -TaskName '$name' -Confirm:`$false"
Write-Host ""
Read-Host "Press Enter to close"
