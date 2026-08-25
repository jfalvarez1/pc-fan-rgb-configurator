"""ASUS motherboard WMI - the board's own fan/BIOS interface.

    python asus_wmi.py            # read-only probe, needs elevation

The board exposes `ASUSManagement` in root\\wmi, backed by the ACPI device
`ACPI\\PNP0C14\\ASUSMBSWINTERFACE`. It is ASUS's supported management
interface - the same one their fleet tooling uses - and it carries:

    GetFanPolicy / SetFanPolicy               per-header mode and profile
    GetManualFanCurve / SetManualFanCurve     3-point curve
    GetManualFanCurvePro / SetManualFanCurvePro   8-point curve
    GetOptionData / SetOptionData             generic BIOS setup options

That matters because the board's Q-Fan keeps reclaiming the pump header from
software control. Setting the header's policy through the board's own API is a
supported way to stop it competing, rather than fighting it from outside.

WHAT THIS MODULE WILL NOT DO
    It will not write BIOS setup variables by name through SetOptionData, and
    it will not touch UEFI NVRAM directly. Those are undocumented per-board
    offsets where a wrong value can leave a machine that will not boot. The
    fan policy calls are purpose-built, reversible, and take effect without a
    reboot - that is the whole reason to prefer them.

Everything here reads first and records the original values, so any later
write has something to put back.

Talks to WMI through PowerShell rather than pywin32, so it has no dependency
beyond what Windows already ships.
"""
import json
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NAMESPACE = "root/wmi"
CLASS = "ASUSManagement"

# FanType is a UInt8 with no published mapping, so the probe sweeps a small
# range and reports whatever answers rather than assuming an index.
FAN_TYPES = range(0, 8)


def _ps(script):
    """Run PowerShell and parse its JSON output. Returns (ok, data_or_error)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=90,
            creationflags=NO_WINDOW)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    body = (out.stdout or "").strip()
    if not body:
        return False, (out.stderr or "no output").strip()[:300]
    try:
        return True, json.loads(body)
    except Exception:
        return False, body[:300]


PROBE = r"""
$ErrorActionPreference = 'Stop'
$result = @{ available = $false; error = $null; fans = @(); curves = @() }
try {
    $inst = Get-CimInstance -Namespace root/wmi -ClassName ASUSManagement
    if (-not $inst) { $result.error = 'class present but no instance' }
    else {
        $result.available = $true
        foreach ($t in 0..7) {
            try {
                $r = Invoke-CimMethod -InputObject $inst -MethodName GetFanPolicy `
                        -Arguments @{ FanType = [byte]$t }
                $result.fans += @{
                    fanType = $t; errorCode = $r.ErrorCode; mode = $r.Mode
                    profile = $r.Profile; source = $r.Source; lowLimit = $r.LowLimit
                }
            } catch {
                $result.fans += @{ fanType = $t; failed = $_.Exception.Message }
            }
            try {
                $c = Invoke-CimMethod -InputObject $inst -MethodName GetManualFanCurvePro `
                        -Arguments @{ FanType = [byte]$t; Mode = '' }
                $result.curves += @{
                    fanType = $t; errorCode = $c.ErrorCode
                    points = @(
                        @($c.Point1Temp, $c.Point1Duty), @($c.Point2Temp, $c.Point2Duty),
                        @($c.Point3Temp, $c.Point3Duty), @($c.Point4Temp, $c.Point4Duty),
                        @($c.Point5Temp, $c.Point5Duty), @($c.Point6Temp, $c.Point6Duty),
                        @($c.Point7Temp, $c.Point7Duty), @($c.Point8Temp, $c.Point8Duty))
                }
            } catch {
                $result.curves += @{ fanType = $t; failed = $_.Exception.Message }
            }
        }
    }
} catch {
    $result.error = $_.Exception.Message
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


def probe():
    """Read every fan policy and curve the board will report. Writes nothing."""
    ok, data = _ps(PROBE)
    if not ok:
        return {"available": False, "error": data}
    return data


def summarise(data):
    """Human-readable lines, for the daemon log."""
    out = []
    if not data.get("available"):
        out.append(f"ASUS WMI unavailable: {data.get('error')}")
        return out
    out.append("ASUS WMI (ASUSManagement) is available")
    for f in data.get("fans", []):
        if f.get("failed"):
            out.append(f"  fanType {f['fanType']}: {f['failed'][:70]}")
        else:
            out.append(f"  fanType {f['fanType']}: err={f.get('errorCode')} "
                       f"mode={f.get('mode')!r} profile={f.get('profile')!r} "
                       f"source={f.get('source')!r} low={f.get('lowLimit')}")
    for c in data.get("curves", []):
        if c.get("failed"):
            continue
        pts = c.get("points") or []
        if any(p and p[0] for p in pts):
            shown = " ".join(f"{p[0]}C:{p[1]}%" for p in pts if p and p[0])
            out.append(f"  curve  {c['fanType']}: err={c.get('errorCode')} {shown}")
    return out


if __name__ == "__main__":
    import ctypes
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = False
    print("elevated:", elevated)
    if not elevated:
        print("NOTE: this interface returns 'Access denied' without elevation.")
    for line in summarise(probe()):
        print(line)
