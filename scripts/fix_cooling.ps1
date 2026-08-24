# Clears the two things that make a fan header stop obeying its curve:
# a duplicate daemon, and another program holding the same chip.
#
# Must run elevated - the daemon runs as administrator, so only an
# administrator can enumerate its command line or stop it.

Write-Host ""
Write-Host "=== HardwareControl: cooling repair ===" -ForegroundColor Cyan
Write-Host ""

$scripts = "C:\HardwareControl\scripts"

function Show-Pump {
    param([string]$tag)
    try {
        $s = Get-Content "$scripts\sensors.json" -Raw | ConvertFrom-Json
        # NOT Get-Date -UFormat %s: this script runs under Windows
        # PowerShell 5.1 (the .bat calls `powershell`, not `pwsh`), where that
        # returns a LOCAL-time epoch - measured 17999 s adrift here, which
        # printed the sensor age as "-18000s old". DateTimeOffset is correct
        # under both 5.1 and 7.
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $age = [math]::Round($now - $s.ts)
        Write-Host ("  {0,-8} pump {1,5:N1}%  {2,6:N0} rpm   radiator {3,5:N1}%  {4,6:N0} rpm   ({5}s old)" -f `
            $tag, $s.pump_duty, $s.pump_rpm, $s.rad_duty, $s.rad_rpm, $age)
    } catch { Write-Host "  $tag  (no sensors.json yet)" }
}

Show-Pump "before:"
Write-Host ""

# --- 1. duplicate daemons -------------------------------------------------
Write-Host "1. Checking for duplicate daemons..." -ForegroundColor Yellow
$daemons = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'mobo_daemon|thermal_rgb_loop' }
foreach ($d in $daemons) {
    $which = if ($d.CommandLine -match 'mobo_daemon') { 'mobo_daemon' } else { 'thermal_rgb_loop' }
    Write-Host ("   found {0}  pid {1}" -f $which, $d.ProcessId)
}
if ($daemons) {
    Write-Host "   stopping all of them (the scheduled task will start a clean one)"
    foreach ($d in $daemons) {
        try { Stop-Process -Id $d.ProcessId -Force -ErrorAction Stop }
        catch { Write-Host ("   could not stop pid {0}: {1}" -f $d.ProcessId, $_.Exception.Message) -ForegroundColor Red }
    }
} else {
    Write-Host "   none running"
}

# --- 2. programs that fight for the same chip -----------------------------
Write-Host ""
Write-Host "2. Checking for programs that own the same hardware..." -ForegroundColor Yellow
$rivals = 'FanControl', 'CAM', 'SignalRgb', 'SignalRgbLauncher'
$found = $false
foreach ($name in $rivals) {
    $p = Get-Process $name -ErrorAction SilentlyContinue
    if ($p) {
        $found = $true
        foreach ($proc in $p) {
            Write-Host ("   stopping {0} (pid {1})" -f $proc.ProcessName, $proc.Id)
            try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop }
            catch { Write-Host ("   could not stop it: " + $_.Exception.Message) -ForegroundColor Red }
        }
    }
}
if (-not $found) { Write-Host "   none running" }

# --- 3. restart clean -----------------------------------------------------
Write-Host ""
Write-Host "3. Restarting the daemons..." -ForegroundColor Yellow
schtasks /End /TN "HardwareControl-MoboDaemon" *> $null
Start-Sleep -Seconds 2
schtasks /Run /TN "HardwareControl-MoboDaemon" *> $null
Write-Host "   mobo_daemon (elevated task) restarted"

Start-Process -FilePath "C:\Python314\pythonw.exe" `
    -ArgumentList "thermal_rgb_loop.py --apply --log --csv" `
    -WorkingDirectory $scripts
Write-Host "   thermal_rgb_loop restarted"

Write-Host ""
Write-Host "   waiting for the pump to settle..."
Start-Sleep -Seconds 12
Show-Pump "after: "

# --- verdict --------------------------------------------------------------
Write-Host ""
try {
    $s = Get-Content "$scripts\sensors.json" -Raw | ConvertFrom-Json
    $cfg = Get-Content "$scripts\pump_config.json" -Raw | ConvertFrom-Json
    $want = [double]$cfg.pump_duty
    $got = [double]$s.pump_duty
    if ([math]::Abs($want - $got) -le 5) {
        Write-Host ("OK - pump is at {0:N1}% as commanded ({1:N0} rpm)" -f $got, $s.pump_rpm) -ForegroundColor Green
    } else {
        Write-Host ("STILL WRONG - commanded {0:N0}%, hardware reports {1:N1}%." -f $want, $got) -ForegroundColor Red
        Write-Host "Something else is still holding the header. Check the Fans tab in LED Studio."
    }
} catch { Write-Host "Could not verify - see the Fans tab in LED Studio." -ForegroundColor Yellow }

Write-Host ""
Read-Host "Press Enter to close"
