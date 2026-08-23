# Disables the services that fight OpenRGB, plus Razer Cortex.
# Everything here is REVERSIBLE - the undo commands are printed at the end.
# Nothing is uninstalled; NZXT CAM in particular must stay installed because
# it is the only thing that can write the LED accessory config.

$services = @(
  'SignalRgb.Service',           # exclusive RGB control - the main conflict
  'CAMService',                  # NZXT CAM
  'Razer Chroma SDK Server',
  'Razer Chroma SDK Service',
  'Razer Chroma Stream Server',
  'RzActionSvc',                 # Razer Central
  'CortexLauncherService'        # Razer Cortex
)

Write-Host "=== services ===" -ForegroundColor Cyan
foreach ($s in $services) {
  $sv = Get-Service $s -ErrorAction SilentlyContinue
  if (-not $sv) { Write-Host "  $s : not present"; continue }
  try {
    Set-Service -Name $s -StartupType Manual -ErrorAction Stop
    if ($sv.Status -eq 'Running') { Stop-Service -Name $s -Force -ErrorAction SilentlyContinue }
    Write-Host ("  {0,-30} -> Manual, stopped" -f $s) -ForegroundColor Green
  } catch { Write-Host "  $s FAILED: $($_.Exception.Message)" -ForegroundColor Red }
}

Write-Host "`n=== scheduled tasks ===" -ForegroundColor Cyan
# Afterburner is a leftover: its own config says StartWithWindows = 0, the GPU
# is at stock (575 W default limit, no overclock), and the user is not
# overclocking. The task contradicts the app's own setting.
foreach ($t in 'MSIAfterburner') {
  try { Disable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
        Write-Host ("  {0,-30} -> disabled" -f $t) -ForegroundColor Green }
  catch { Write-Host "  $t : $($_.Exception.Message)" }
}

Write-Host "`n=== HKLM startup ===" -ForegroundColor Cyan
try {
  Remove-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name 'RazerCortex' -ErrorAction Stop
  Write-Host "  removed RazerCortex" -ForegroundColor Green
} catch { Write-Host "  RazerCortex: $($_.Exception.Message)" }

Write-Host "`n=== UNDO (keep this) ===" -ForegroundColor Yellow
foreach ($s in $services) { Write-Host "  Set-Service '$s' -StartupType Automatic; Start-Service '$s'" }
Write-Host "  Enable-ScheduledTask -TaskName 'MSIAfterburner'"
Write-Host "  Startup entries backed up in _downloads\startup_backup.txt"
Write-Host ""
Read-Host "Press Enter to close"
