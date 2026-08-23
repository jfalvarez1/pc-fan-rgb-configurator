"""Identify which motherboard header drives what - PUMP-SAFE.

MUST RUN AS ADMINISTRATOR (LHM loads a signed kernel driver).

SAFETY RULES, enforced in code:
  * Every probe RAISES a header to 100%. Nothing is ever lowered.
  * One of these headers is the AIO pump. Raising a pump is harmless;
    slowing or stopping one risks cavitation and impeller wear, so this
    script contains no code path that lowers any duty.
  * On exit - including Ctrl+C or a crash - every header is handed back to
    BIOS control via SetDefault(). It never leaves a header pinned.

Writes mobo_fan_report.txt next to this script.
"""
import atexit
import ctypes
import os
import sys
import time

FANCONTROL = r"C:\HardwareControl\FanControl"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "mobo_fan_report.txt")
HOLD = 7          # seconds at 100%
SETTLE = 5        # seconds back at default before the next header

_restore = []     # controls to hand back to BIOS no matter how we exit


def restore_all():
    for ctl in _restore:
        try:
            ctl.SetDefault()
        except Exception:
            pass


atexit.register(restore_all)


def main():
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("NOT ELEVATED - run as administrator")
            return 1
    except Exception:
        pass

    from pythonnet import load
    load("coreclr")
    import clr
    sys.path.insert(0, FANCONTROL)
    os.chdir(FANCONTROL)
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor.Hardware import Computer

    c = Computer()
    c.IsMotherboardEnabled = True
    c.IsControllerEnabled = True
    c.IsCpuEnabled = True
    c.Open()

    # find the SuperIO chip
    sio = None
    for hw in c.Hardware:
        hw.Update()
        for sub in hw.SubHardware:
            sub.Update()
            if "Nuvoton" in sub.Name or "SuperIO" in str(sub.HardwareType):
                sio = sub
    if sio is None:
        print("no SuperIO found")
        return 1

    def snap():
        sio.Update()
        fans, ctls = {}, {}
        for s in sio.Sensors:
            if str(s.SensorType) == "Fan":
                fans[s.Name] = s.Value
            elif str(s.SensorType) == "Control":
                ctls[s.Name] = s.Value
        return fans, ctls

    base_fans, base_ctls = snap()
    lines = [f"SuperIO: {sio.Name}", "", "BASELINE:"]
    for name in sorted(base_fans):
        lines.append(f"  {name:<10} {base_fans[name] or 0:>7.0f} rpm   "
                     f"duty {base_ctls.get(name, 0) or 0:>5.1f}%")

    # only probe headers that actually have something spinning
    live = [n for n, v in base_fans.items() if v and v > 0]
    lines += ["", f"live headers: {live}", ""]
    print("\n".join(lines))

    controls = {s.Name: s for s in sio.Sensors
                if str(s.SensorType) == "Control" and s.Control is not None}

    for name in live:
        ctl = controls.get(name)
        if ctl is None:
            continue
        before = base_fans[name] or 0
        print(f"\n>>> raising {name} to 100% for {HOLD}s "
              f"(baseline {before:.0f} rpm) - RAISE ONLY")
        try:
            ctl.Control.SetSoftware(100.0)
            _restore.append(ctl.Control)
        except Exception as exc:
            print(f"    could not set: {exc}")
            continue

        peak = before
        for _ in range(HOLD):
            time.sleep(1)
            f, _c = snap()
            peak = max(peak, f.get(name) or 0)
        delta = peak - before
        lines.append(f"{name}: {before:>7.0f} -> {peak:>7.0f} rpm  "
                     f"(+{delta:.0f})")
        print(f"    peak {peak:.0f} rpm  (+{delta:.0f})")

        try:
            ctl.Control.SetDefault()
        except Exception:
            pass
        time.sleep(SETTLE)

    restore_all()
    lines += ["", "all headers returned to BIOS control"]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines[-len(live) - 3:]))
    print(f"\nwritten to {OUT}")
    c.Close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore_all()
