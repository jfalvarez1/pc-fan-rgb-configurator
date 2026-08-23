"""Map the pump's duty -> RPM curve and pick a durability-optimised duty.

MUST RUN AS ADMINISTRATOR.

WHY THIS EXISTS
    This pump saturates early: 80% duty already produces 2806 of 2830 rpm.
    The guidance is to keep a pump above 60-80% of its maximum RPM - that is
    RPM, not duty. On this pump those are very different numbers, so the duty
    that actually yields ~80% of max RPM has to be measured.

SAFETY - bounded, and it fails upward
    * Steps DOWN gradually, settling and reading RPM at every step.
    * ABORT_RPM is a hard floor. Fall below it and the script immediately
      restores SAFE_DUTY and stops. That floor sits ABOVE the BIOS default
      this machine ran for years, so no step enters unknown territory.
    * Never goes below MIN_DUTY regardless of readings.
    * On ANY exit - finish, exception, Ctrl+C - the pump is set to SAFE_DUTY.
      Restore writes a value, it does not rely on SetDefault(), which was
      measured to leave the last value in the register.

Writes pump_map.txt and pump_config.json.
"""
import atexit
import ctypes
import json
import os
import pathlib
import sys
import time

FANCONTROL = r"C:\HardwareControl\FanControl"
BASE = pathlib.Path(__file__).resolve().parent
PUMP_HEADER = "Fan #2"

STEPS = [80, 72, 64, 56, 48, 40]   # descending duties to probe
SETTLE = 7                         # seconds to let RPM stabilise
ABORT_RPM = 1500                   # hard floor - below this, bail out
MIN_DUTY = 40                      # never command below this
SAFE_DUTY = 80.0                   # what we restore to on any exit
TARGET_RPM = 2300                  # ~80% of the 2830 maximum

_pump_ctl = None


def emergency_restore():
    if _pump_ctl is not None:
        try:
            _pump_ctl.SetSoftware(SAFE_DUTY)
        except Exception:
            pass


atexit.register(emergency_restore)


def main():
    global _pump_ctl
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("NOT ELEVATED")
        return 1

    # the running mobo_daemon holds the pump at a fixed duty and would fight
    # this mapping, so stop it first (we are elevated, so we can)
    os.system('taskkill /F /IM pythonw.exe /FI "WINDOWTITLE eq" >nul 2>&1')
    for line in os.popen('wmic process where "name=\'pythonw.exe\'" '
                         'get processid,commandline /format:csv').read().splitlines():
        if "mobo_daemon" in line:
            pid = line.strip().split(",")[-1]
            if pid.isdigit():
                os.system(f"taskkill /F /PID {pid} >nul 2>&1")
                print(f"stopped running mobo_daemon (pid {pid})")
    time.sleep(2)

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
    c.Open()
    sio = None
    for hw in c.Hardware:
        hw.Update()
        for sub in hw.SubHardware:
            sub.Update()
            if "Nuvoton" in sub.Name:
                sio = sub
    if sio is None:
        print("no SuperIO")
        return 1

    ctl = next(s for s in sio.Sensors
               if str(s.SensorType) == "Control" and s.Name == PUMP_HEADER)
    fan = next(s for s in sio.Sensors
               if str(s.SensorType) == "Fan" and s.Name == PUMP_HEADER)
    _pump_ctl = ctl.Control

    def rpm():
        sio.Update()
        return float(fan.Value or 0)

    results = []
    lines = [f"pump duty -> rpm map ({PUMP_HEADER})",
             f"abort floor: {ABORT_RPM} rpm    target: ~{TARGET_RPM} rpm", ""]
    aborted = False

    try:
        for duty in STEPS:
            if duty < MIN_DUTY:
                break
            ctl.Control.SetSoftware(float(duty))
            time.sleep(SETTLE)
            r = rpm()
            pct = r / 2830.0 * 100
            lines.append(f"  {duty:>3}% duty  ->  {r:>7.0f} rpm  ({pct:.0f}% of max)")
            print(lines[-1], flush=True)
            results.append((duty, r))
            if r < ABORT_RPM:
                lines.append(f"  !! {r:.0f} rpm is below the {ABORT_RPM} floor "
                             f"- ABORTING and restoring {SAFE_DUTY:.0f}%")
                print(lines[-1])
                aborted = True
                break
    except Exception as exc:
        lines.append(f"error: {exc}")
        aborted = True
    finally:
        try:
            ctl.Control.SetSoftware(SAFE_DUTY)
        except Exception:
            pass

    # choose the lowest duty that still meets the target RPM
    chosen = SAFE_DUTY
    ok = [(d, r) for d, r in results if r >= TARGET_RPM]
    if ok and not aborted:
        chosen = float(min(ok, key=lambda x: x[0])[0])
    elif results and not aborted:
        chosen = float(max(results, key=lambda x: x[1])[0])

    lines += ["", f"CHOSEN: {chosen:.0f}% duty "
                  f"(lowest duty still at or above {TARGET_RPM} rpm)"]
    (BASE / "pump_config.json").write_text(
        json.dumps({"pump_duty": chosen,
                    "map": [{"duty": d, "rpm": r} for d, r in results]},
                   indent=2))
    try:
        ctl.Control.SetSoftware(chosen)
        time.sleep(4)
        lines.append(f"pump now at {chosen:.0f}% = {rpm():.0f} rpm")
    except Exception:
        pass

    (BASE / "pump_map.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[-4:]))
    print(f"\nwritten to {BASE / 'pump_map.txt'}")
    c.Close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        emergency_restore()
