"""Enumerate every motherboard/CPU sensor and control via LibreHardwareMonitor.

MUST RUN AS ADMINISTRATOR - LHM loads a signed kernel driver to reach the
SuperIO chip. Without elevation every value reads 0.0.

Writes lhm_report.txt next to this script.
"""
import os
import sys
import ctypes

FANCONTROL = r"C:\HardwareControl\FanControl"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lhm_report.txt")


def main():
    admin = False
    try:
        admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        pass

    lines = [f"elevated: {admin}"]
    if not admin:
        lines.append("NOT ELEVATED - values will read 0.0")

    from pythonnet import load
    load("coreclr")
    import clr
    sys.path.insert(0, FANCONTROL)
    os.chdir(FANCONTROL)
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    c = Computer()
    c.IsCpuEnabled = True
    c.IsMotherboardEnabled = True
    c.IsControllerEnabled = True
    c.IsStorageEnabled = False
    c.IsGpuEnabled = True
    c.Open()

    def dump(hw, depth=0):
        hw.Update()
        lines.append("  " * depth + f"[{hw.HardwareType}] {hw.Name}")
        for s in hw.Sensors:
            st = str(s.SensorType)
            if st in ("Temperature", "Fan", "Control"):
                v = "n/a" if s.Value is None else f"{s.Value:.1f}"
                ctl = ""
                if st == "Control" and s.Control is not None:
                    ctl = f"  [WRITABLE mode={s.Control.ControlMode}]"
                lines.append("  " * (depth + 1) +
                             f"{st:<12} {s.Name:<32} {v}{ctl}")
        for sub in hw.SubHardware:
            dump(sub, depth + 1)

    for hw in c.Hardware:
        dump(hw)
    c.Close()

    text = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"\nwritten to {OUT}")


if __name__ == "__main__":
    main()
    if os.environ.get("LHM_PAUSE"):
        input("\nPress Enter to close...")
