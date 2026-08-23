"""Raise ONE motherboard header to 100% for a long hold, so it can be found.

    python probe_one.py "Fan #2" 30

RAISE ONLY - never lowers any duty. Hands the header back to BIOS on exit,
including on crash or Ctrl+C.
"""
import atexit, ctypes, os, sys, time

FANCONTROL = r"C:\HardwareControl\FanControl"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_one.txt")
_restore = []

def restore():
    for c in _restore:
        try: c.SetDefault()
        except Exception: pass
atexit.register(restore)

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "Fan #2"
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    if not ctypes.windll.shell32.IsUserAnAdmin():
        open(OUT,"w").write("NOT ELEVATED"); return 1

    from pythonnet import load
    load("coreclr")
    import clr
    sys.path.insert(0, FANCONTROL); os.chdir(FANCONTROL)
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    c = Computer(); c.IsMotherboardEnabled=True; c.IsControllerEnabled=True
    c.Open()
    sio = None
    for hw in c.Hardware:
        hw.Update()
        for sub in hw.SubHardware:
            sub.Update()
            if "Nuvoton" in sub.Name: sio = sub
    log = []
    ctl = next((s for s in sio.Sensors
                if str(s.SensorType)=="Control" and s.Name==target), None)
    fan = next((s for s in sio.Sensors
                if str(s.SensorType)=="Fan" and s.Name==target), None)
    base = fan.Value or 0
    log.append(f"{target}: baseline {base:.0f} rpm")
    ctl.Control.SetSoftware(100.0); _restore.append(ctl.Control)
    peak = base
    for _ in range(hold):
        time.sleep(1); sio.Update(); peak = max(peak, fan.Value or 0)
    log.append(f"{target}: peak {peak:.0f} rpm at 100% (+{peak-base:.0f})")
    ctl.Control.SetDefault(); restore()
    log.append("returned to BIOS control")
    open(OUT,"w",encoding="utf-8").write("\n".join(log))
    c.Close(); return 0

if __name__ == "__main__":
    try: sys.exit(main())
    finally: restore()
