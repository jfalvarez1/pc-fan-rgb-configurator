"""Elevated motherboard daemon - pump, radiator fans, and sensor publishing.

MUST RUN AS ADMINISTRATOR (LibreHardwareMonitor's signed kernel driver).

    python mobo_daemon.py            # DRY RUN - prints, writes nothing
    python mobo_daemon.py --apply    # drives the hardware
    python mobo_daemon.py --apply --log

WHAT IT OWNS
    Fan #2  AIO pump          -> ONE fixed duty. Never a curve.
    Fan #7  radiator fans x3  -> curve on CPU Tctl/Tdie

WHAT IT PUBLISHES
    sensors.json - CPU Tctl, CPU CCD1, GPU core, GPU memory junction, pump
    and radiator RPM. thermal_rgb_loop reads this so the case fans can follow
    the HOTTEST of CPU / GPU core / VRAM without needing elevation itself.

PUMP SAFETY
    * PUMP_DUTY is clamped to [PUMP_MIN_SAFE, 100]. It cannot be set low.
    * The pump is never put on a curve - steady speed is what extends life;
      the wear risks are cavitation at low RPM and constant speed cycling.
    * Every header is released to BIOS on exit, including on crash.
"""
import argparse
import atexit
import ctypes
import json

import fan_tuning
import os
import pathlib
import sys
import time

FANCONTROL = r"C:\HardwareControl\FanControl"
BASE = pathlib.Path(__file__).resolve().parent
SENSORS = BASE / "sensors.json"

PUMP_HEADER = "Fan #2"
RAD_HEADER = "Fan #7"

# --- pump: one fixed value, clamped so it can never be set slow.
#
# TUNED FOR DURABILITY, not peak cooling:
#   * STEADY, never a curve. Repeated speed cycling is a wear mechanism;
#     a constant duty avoids it entirely. This is the biggest single factor.
#   * NOT maxed out. A pump is rated for continuous full speed, but running
#     it there all day is more bearing wear and heat than this loop needs -
#     an AIO is nowhere near flow-limited at 80%.
#   * WELL ABOVE the low end. Cavitation - vapour bubbles collapsing on the
#     impeller - is the actual damage mechanism, and it lives at low RPM.
# 80% sits above cavitation risk and below maximum wear. ~2260 of 2830 rpm.
PUMP_DUTY = 80.0
PUMP_MIN_SAFE = 40.0        # hard floor on DUTY. This pump saturates
                            # early (80% duty = 99% of max rpm), so a
                            # duty floor of 70 was meaningless. RPM is
                            # what matters; pump_map targets ~2300 rpm.

# --- radiator fans follow CPU temperature
# The 9800X3D is happy into the 80s, but the radiator is the CPU's only
# cooling, so this ramps earlier and harder than the case-fan curves.
# Measured: CPU had the LEAST headroom of the three sensors (7.4 C to Tjmax
# vs 15 C for GPU core, 19 C for VRAM), and the radiator is the CPU's only
# cooling. Noise is not a constraint, so this ramps hard and early.
RAD_CURVE = [(40, 40), (50, 55), (60, 72), (70, 88), (78, 100)]
RAD_MIN_DUTY = 40

POLL = 3.0
# CPU temperature is far jitterier than GPU - a 9800X3D moves several degrees
# between samples at idle. Heavier smoothing and a wider deadband stop that
# jitter turning into a stream of redundant PWM writes (observed flip-flopping
# between 35% and 38% while the hardware sat still at 34.9%).
EMA_ALPHA = 0.08
DEADBAND = 6
FALL_DELAY = 45.0


# RESTORING HEADERS - do not simplify this.
#
# LHM's SetDefault() releases SOFTWARE control but LEAVES the last value in
# the SuperIO register. Measured: a probe raised the pump to 100%, called
# SetDefault(), printed "released to BIOS" - and the pump stayed at 100%
# across process restarts. The message was a lie.
#
# Correct restore = write the ORIGINAL duty back, THEN release.

_restore = []          # list of (control, baseline_duty)


def restore_all():
    for c, base in _restore:
        try:
            if base is not None:
                c.SetSoftware(float(base))   # put the ORIGINAL value back
        except Exception:
            pass
        try:
            c.SetDefault()                   # then hand control to firmware
        except Exception:
            pass
    _restore.clear()


atexit.register(restore_all)


def interpolate(curve, t):
    if t <= curve[0][0]:
        return float(curve[0][1])
    if t >= curve[-1][0]:
        return float(curve[-1][1])
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= t <= t1:
            span = t1 - t0
            return d1 if span == 0 else d0 + (d1 - d0) * (t - t0) / span
    return float(curve[-1][1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.log:
        f = open(BASE / "mobo_daemon.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = f
        print(f"\n===== started pid {os.getpid()} =====")

    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("NOT ELEVATED - cannot read or drive motherboard headers")
            return 1
    except Exception:
        pass

    # pump_map.py measures this pump's duty->RPM curve and writes the duty
    # that actually yields ~80% of max RPM. Prefer the measured value.
    pump_duty = PUMP_DUTY
    try:
        _cfg = json.loads((BASE / "pump_config.json").read_text())
        if _cfg.get("pump_duty"):
            pump_duty = float(_cfg["pump_duty"])
            print(f"using measured pump duty from pump_config.json: {pump_duty:.0f}%")
    except Exception:
        pass
    pump_duty = max(PUMP_MIN_SAFE, min(100.0, pump_duty))
    if pump_duty != PUMP_DUTY:
        print(f"pump duty {PUMP_DUTY} clamped to {pump_duty}")

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
    c.IsGpuEnabled = True
    c.Open()

    sio = None
    hw_all = []
    for hw in c.Hardware:
        hw.Update()
        hw_all.append(hw)
        for sub in hw.SubHardware:
            sub.Update()
            hw_all.append(sub)
            if "Nuvoton" in sub.Name:
                sio = sub
    if sio is None:
        print("no SuperIO found")
        return 1

    ctl = {s.Name: s for s in sio.Sensors if str(s.SensorType) == "Control"}
    fan = {s.Name: s for s in sio.Sensors if str(s.SensorType) == "Fan"}
    # snapshot BEFORE touching anything - this is what restore writes back
    baseline = {n: (float(s.Value) if s.Value is not None else None)
                for n, s in ctl.items()}
    print("baseline duties: " + ", ".join(
        f"{n}={baseline[n]:.0f}%" for n in sorted(baseline)
        if baseline[n] is not None))

    def sensors():
        out = {}
        for hw in hw_all:
            try:
                hw.Update()
            except Exception:
                continue
            for s in hw.Sensors:
                st, n = str(s.SensorType), s.Name
                if st == "Temperature":
                    if n == "Core (Tctl/Tdie)":
                        out["cpu_tctl"] = s.Value
                    elif n == "CCD1 (Tdie)":
                        out["cpu_ccd1"] = s.Value
                    elif n == "GPU Core" and "NVIDIA" in hw.Name.upper():
                        out["gpu_core"] = s.Value
                    elif n == "GPU Memory Junction":
                        out["gpu_vram"] = s.Value
        out["pump_rpm"] = fan[PUMP_HEADER].Value if PUMP_HEADER in fan else None
        out["rad_rpm"] = fan[RAD_HEADER].Value if RAD_HEADER in fan else None
        # duty + who is driving each header, so "released to BIOS" can be
        # verified rather than assumed
        for tag, hdr in (("pump", PUMP_HEADER), ("rad", RAD_HEADER)):
            if hdr in ctl:
                out[f"{tag}_duty"] = ctl[hdr].Value
                try:
                    out[f"{tag}_mode"] = str(ctl[hdr].Control.ControlMode)
                except Exception:
                    out[f"{tag}_mode"] = "?"
        return out

    # ---- pump: set ONCE, fixed, never touched again
    if args.apply:
        try:
            _restore.append((ctl[PUMP_HEADER].Control,
                             baseline.get(PUMP_HEADER)))
            ctl[PUMP_HEADER].Control.SetSoftware(pump_duty)
            print(f"pump ({PUMP_HEADER}) pinned at {pump_duty:.0f}% - fixed, no curve")
        except Exception as exc:
            print(f"pump set FAILED: {exc}")
    else:
        print(f"[dry run] would pin pump ({PUMP_HEADER}) at {pump_duty:.0f}%")

    smoothed = None
    commanded = None
    fall_since = None
    try:
        while True:
            now = time.monotonic()
            s = sensors()
            cpu = s.get("cpu_tctl")
            if cpu is not None:
                smoothed = cpu if smoothed is None else (
                    EMA_ALPHA * cpu + (1 - EMA_ALPHA) * smoothed)

            def r1(v):
                return "n/a" if v is None else f"{v:.1f}"
            line = (f"cpu={r1(cpu)}C ccd1={r1(s.get('cpu_ccd1'))} "
                    f"gpu={r1(s.get('gpu_core'))} vram={r1(s.get('gpu_vram'))} | "
                    f"PUMP {r1(s.get('pump_rpm'))}rpm @{r1(s.get('pump_duty'))}% "
                    f"[{s.get('pump_mode')}] | "
                    f"RAD {r1(s.get('rad_rpm'))}rpm @{r1(s.get('rad_duty'))}% "
                    f"[{s.get('rad_mode')}]")

            if smoothed is not None:
                rad_trim = fan_tuning.load_trims().get("rad", 0.0)
                target = max(RAD_MIN_DUTY,
                             min(100, round(interpolate(RAD_CURVE, smoothed)
                                            + rad_trim)))
                change = False
                if commanded is None:
                    change = True
                elif abs(target - commanded) >= DEADBAND:
                    if target < commanded:
                        fall_since = fall_since or now
                        change = (now - fall_since) >= FALL_DELAY
                    else:
                        fall_since = None
                        change = True
                if change:
                    fall_since = None
                    commanded = target
                    line += f" | rad->{target}%"
                    if args.apply:
                        try:
                            if not any(c is ctl[RAD_HEADER].Control
                                       for c, _b in _restore):
                                _restore.append((ctl[RAD_HEADER].Control,
                                                 baseline.get(RAD_HEADER)))
                            ctl[RAD_HEADER].Control.SetSoftware(float(target))
                        except Exception as exc:
                            line += f" (FAILED {exc})"
                else:
                    line += f" | rad={commanded}%"

            try:
                s["ts"] = time.time()
                SENSORS.write_text(json.dumps(s, indent=2))
            except Exception:
                pass

            print(line, flush=True)
            if args.once:
                break
            time.sleep(POLL)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        restore_all()
        print("all headers released to BIOS")
        try:
            c.Close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        restore_all()
