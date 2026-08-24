# Disables the services that fight OpenRGB, plus Razer Cortex.
# Everything here is REVERSIBLE - the undo commands are printed at the end.
# Nothing is uninstalled; NZXT CAM in particular must stay installed because
# it is the only thing that can write the LED accessory config.

# Snapshot the CURRENT state first. The previous version pointed at a backup
# file written earlier in the day, which is not the same thing as a record of
# what is about to be changed.
$stamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$backup = "C:\HardwareControl\_downloads\startup_backup_$stamp.txt"
$services = @(
  'SignalRgb.Service',           # exclusive RGB control - the main conflict
  'CAMService',                  # NZXT CAM
  'Razer Chroma SDK Server',
  'Razer Chroma SDK Service',
  'Razer Chroma Stream Server',
  'RzActionSvc',                 # Razer Central
  'CortexLauncherService'        # Razer Cortex
)

$snap = @()
$snap += "startup state before cleanup, $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$snap += "--- services ---"
foreach ($s in $services) {
  $sv = Get-Service $s -ErrorAction SilentlyContinue
  if ($sv) {
    $mode = (Get-CimInstance Win32_Service -Filter "Name='$s'").StartMode
    $snap += ("{0,-30} {1,-9} start={2}" -f $s, $sv.Status, $mode)
  } else { $snap += ("{0,-30} not present" -f $s) }
}
$snap += "--- HKLM Run ---"
(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run').PSObject.Properties |
  Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { $snap += ("  {0} = {1}" -f $_.Name, $_.Value) }
$snap += "--- HKCU Run ---"
(Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run').PSObject.Properties |
  Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { $snap += ("  {0} = {1}" -f $_.Name, $_.Value) }
$snap += "--- scheduled task ---"
$t = Get-ScheduledTask -TaskName 'MSIAfterburner' -ErrorAction SilentlyContinue
$snap += ("MSIAfterburner " + $(if ($t) { $t.State } else { "not present" }))
New-Item -ItemType Directory -Force "C:\HardwareControl\_downloads" | Out-Null
$snap | Set-Content -Path $backup -Encoding utf8
Write-Host "snapshot written to $backup" -ForegroundColor Green
Write-Host ""

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

# Verify rather than assume - the whole point of this session's pump bug was
# a program reporting what it intended instead of what actually happened.
Write-Host "`n=== VERIFY ===" -ForegroundColor Cyan
$bad = 0
foreach ($s in $services) {
  $sv = Get-Service $s -ErrorAction SilentlyContinue
  if (-not $sv) { continue }
  $mode = (Get-CimInstance Win32_Service -Filter "Name='$s'").StartMode
  $ok = ($mode -ne 'Auto') -and ($sv.Status -ne 'Running')
  if (-not $ok) { $bad++ }
  Write-Host ("  {0,-30} {1,-9} start={2}  {3}" -f $s, $sv.Status, $mode,
              $(if ($ok) { 'OK' } else { 'STILL ACTIVE' })) `
             -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}
$still = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run').PSObject.Properties |
         Where-Object { $_.Name -eq 'RazerCortex' }
if ($still) { $bad++; Write-Host "  RazerCortex STILL in HKLM Run" -ForegroundColor Red }
else { Write-Host "  RazerCortex removed from HKLM Run" -ForegroundColor Green }
$t = Get-ScheduledTask -TaskName 'MSIAfterburner' -ErrorAction SilentlyContinue
if ($t) {
  $ok = $t.State -eq 'Disabled'
  if (-not $ok) { $bad++ }
  Write-Host ("  MSIAfterburner {0}  {1}" -f $t.State, $(if ($ok) { 'OK' } else { 'STILL ENABLED' })) `
             -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}
Write-Host ""
if ($bad -eq 0) { Write-Host "ALL CLEAN" -ForegroundColor Green }
else { Write-Host "$bad item(s) did not change - see red above" -ForegroundColor Red }

Write-Host "`n=== UNDO (keep this) ===" -ForegroundColor Yellow
foreach ($s in $services) { Write-Host "  Set-Service '$s' -StartupType Automatic; Start-Service '$s'" }
Write-Host "  Enable-ScheduledTask -TaskName 'MSIAfterburner'"
Write-Host "  RazerCortex: re-add from the snapshot file if wanted"
Write-Host "  Snapshot of the previous state: $backup"
Write-Host ""
Read-Host "Press Enter to close"
