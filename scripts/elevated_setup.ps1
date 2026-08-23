# Elevated setup for the hardware control stack.
# Run as administrator. Every step is reversible - see UNDO notes at the bottom.

Write-Host "=== 1. NZXT CAM service ===" -ForegroundColor Cyan
try {
    $svc = Get-Service CAMService -ErrorAction Stop
    Write-Host "  before: $($svc.Status) / $($svc.StartType)"
    Set-Service -Name CAMService -StartupType Manual -ErrorAction Stop
    if ($svc.Status -eq 'Running') { Stop-Service -Name CAMService -Force -ErrorAction Stop }
    $svc.Refresh()
    Write-Host "  after : $((Get-Service CAMService).Status) / Manual" -ForegroundColor Green
} catch {
    Write-Host "  FAILED: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host "`n=== 2. OpenRGB with SDK server ===" -ForegroundColor Cyan
$openrgb = "C:\HardwareControl\OpenRGB\OpenRGB.exe"
if (Get-Process OpenRGB -ErrorAction SilentlyContinue) {
    Write-Host "  OpenRGB already running - stopping it so the SDK server can start"
    Stop-Process -Name OpenRGB -Force
    Start-Sleep -Seconds 2
}
if (Test-Path $openrgb) {
    Start-Process -FilePath $openrgb -ArgumentList "--server","--startminimized"
    Start-Sleep -Seconds 6
    $listening = Get-NetTCPConnection -LocalPort 6742 -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "  SDK server LISTENING on 6742" -ForegroundColor Green
    } else {
        Write-Host "  started, but port 6742 not open yet - check the SDK Server tab" -ForegroundColor Yellow
    }
} else {
    Write-Host "  OpenRGB.exe not found at $openrgb" -ForegroundColor Red
}

Write-Host "`n=== 3. FanControl (motherboard headers: radiator + pump) ===" -ForegroundColor Cyan
$fc = "C:\HardwareControl\FanControl\FanControl.exe"
if (Get-Process FanControl -ErrorAction SilentlyContinue) {
    Write-Host "  FanControl already running (restart it as admin if sensors are missing)"
} elseif (Test-Path $fc) {
    Start-Process -FilePath $fc
    Write-Host "  launched - it needs admin to see SuperIO / motherboard headers" -ForegroundColor Green
} else {
    Write-Host "  FanControl.exe not found at $fc" -ForegroundColor Red
}

Write-Host "`n=== DONE ===" -ForegroundColor Cyan
Write-Host @"

In FanControl, configure ONLY the motherboard headers:
  - the Arctic radiator fans  (usually CPU_FAN / CPU_OPT)
  - the pump                  (usually AIO_PUMP / W_PUMP - set a FIXED high
                               duty, typically 100%; pumps are not curve fans)
Leave the three NZXT channels alone - thermal_rgb_loop.py owns those.

UNDO:
  Set-Service CAMService -StartupType Automatic; Start-Service CAMService
  Restore CAM autostart from C:\HardwareControl\_downloads\cam_startup_backup.txt
    into HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run  (name NZXT.CAM)
"@
Write-Host "Press Enter to close..."
Read-Host
