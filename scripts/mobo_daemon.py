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
import single_instance
import os
import pathlib
import sys
import time

FANCONTROL = r"C:\HardwareControl\FanControl"
# Not __file__: the LED Studio exe imports this module for its Fans tab, and
# there __file__ points inside the bundle's _internal folder. The daemon would
# then read a pump_config.json that is not the one it was tuned with, silently.
from app_paths import DATA as BASE          # noqa: E402
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
# Steeper through the band this CPU actually runs in. It reached 100% at
# 78 C before, but the measured load range was 80-88 C - so the top of the
# curve was doing nothing and the climb to it was the only part that mattered.
# Full speed now arrives at 72 C. The idle end is unchanged: there is nothing
# to cool at 40 C and moving that would only add noise for no temperature.
RAD_CURVE = [(40, 40), (50, 58), (58, 76), (65, 90), (72, 100)]
RAD_MIN_DUTY = 40

# QUIET profile for the radiator: later and lower, same shape.
RAD_CURVE_QUIET = [(45, 32), (58, 45), (68, 62), (78, 82), (88, 100)]
RAD_MIN_DUTY_QUIET = 30


def rad_curve_for(profile):
    if profile == "quiet":
        return RAD_CURVE_QUIET, RAD_MIN_DUTY_QUIET
    return RAD_CURVE, RAD_MIN_DUTY

# --- the pump is pinned ONCE at startup, and that turned out not to be enough.
# Found with the pump sitting at 24.7% (1369 rpm - below the 1500 rpm abort
# floor in the measured map) for an unknown length of time, while this daemon
# logged "pinned at 56%" every poll and never checked. FanControl had taken the
# header; nothing re-asserted, and nothing noticed.
#
# So: watch what the hardware actually reports, and put the pump back if it
# drifts. Bounded on purpose - if something is actively fighting for the
# header, re-writing PWM forever would be its own bug, so after a few attempts
# it stops trying and just keeps saying so.
PUMP_DRIFT = 5.0            # duty points from target before it counts as drift
PUMP_DRIFT_POLLS = 3        # consecutive drifting polls before re-asserting
PUMP_MAX_REASSERTS = 5      # then give up and warn rather than fight
PUMP_NAG_SECONDS = 300.0    # once given up, repeat the warning this rarely
PUMP_RESTART_LIMIT = 3      # self-restarts before giving up entirely

# What actually happens, measured over 24k log samples: software control held
# the pump at a flat 55.7% for 11.5 hours (correlation with CPU temperature:
# 0.000), then a CPU spike from 46.9 to 62.5 C and the BOARD took the header
# back. From that moment duty tracked CPU temperature with correlation +0.955 -
# a curve, not a stray write. No amount of SetSoftware() got it back; only
# starting a fresh process did.

POLL = 3.0
# CPU temperature is far jitterier than GPU - a 9800X3D moves several degrees
# between samples at idle. Heavier smoothing and a wider deadband stop that
# jitter turning into a stream of redundant PWM writes (observed flip-flopping
# between 35% and 38% while the hardware sat still at 34.9%).
EMA_ALPHA = 0.08            # falling: slow, so jitter does not cause writes
# RISING IS DIFFERENT. At 0.08 the time constant is about 38 seconds, so on a
# spike the duty was being computed from a temperature half a minute old -
# measured: the curve demanded 100% while the radiator sat at 83-96%. On a CPU
# with roughly 6 C of headroom that lag is the whole problem, and it is not
# fixed by a steeper curve: the curve already asks for 100% at 78 C and this
# CPU runs into the high 80s.
#
# So rise fast and fall slow, the same asymmetry the VU meter uses. Ramping up
# late costs headroom; ramping down late costs nothing but a little noise.
EMA_RISE = 0.5              # rising: ~6 s, so a spike is met almost at once
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

    # Only one process may drive these devices. Two mobo_daemons were
    # found running at once, fighting over the same headers - and
    # schtasks /End had reported success while leaving one alive.
    if args.apply and not single_instance.claim("MoboDaemon"):
        print("another MoboDaemon is already driving hardware - exiting")
        return 1

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

    # Full sensor inventory, written once at startup. Read-only, and the
    # only way to see what the board actually exposes - voltage rails, VRM
    # and SoC temperatures, power draw - rather than guessing from a spec
    # sheet. Nothing consumes this yet; it is evidence.
    try:
        inv = {}
        for _hw in hw_all:
            rows = []
            for _s in _hw.Sensors:
                rows.append({"name": _s.Name,
                             "type": str(_s.SensorType),
                             "value": (float(_s.Value)
                                       if _s.Value is not None else None)})
            if rows:
                inv[_hw.Name] = rows
        (BASE / "sensors_all.json").write_text(json.dumps(inv, indent=1))
        print(f"sensor inventory: {sum(len(v) for v in inv.values())} sensors "
              f"across {len(inv)} devices -> sensors_all.json")
    except Exception as _exc:
        print(f"inventory failed: {type(_exc).__name__}: {_exc}")

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

        # One-shot, read-only look at the board's own fan interface. On a
        # background thread because it shells out to PowerShell eight times
        # and must not delay taking the pump.
        def _probe_asus():
            try:
                import asus_wmi
                data = asus_wmi.probe()
                (BASE / "asus_wmi_probe.json").write_text(
                    json.dumps(data, indent=1))
                for line in asus_wmi.summarise(data):
                    print(line, flush=True)
            except Exception as exc:
                print(f"asus probe failed: {type(exc).__name__}: {exc}",
                      flush=True)
        import threading
        threading.Thread(target=_probe_asus, daemon=True).start()
    else:
        print(f"[dry run] would pin pump ({PUMP_HEADER}) at {pump_duty:.0f}%")

    smoothed = None
    commanded = None
    fall_since = None
    drift_polls = 0
    reasserts = 0
    restarts = 0
    last_nag = 0.0
    try:
        while True:
            now = time.monotonic()
            s = sensors()
            cpu = s.get("cpu_tctl")

            # pump: verify, do not assume
            measured = s.get("pump_duty")
            if measured is not None and args.apply:
                if abs(measured - pump_duty) > PUMP_DRIFT:
                    drift_polls += 1
                    if drift_polls >= PUMP_DRIFT_POLLS:
                        drift_polls = 0
                        if reasserts < PUMP_MAX_REASSERTS:
                            reasserts += 1
                            print(f"WARNING: pump at {measured:.1f}%, expected "
                                  f"{pump_duty:.0f}% - something else wrote "
                                  f"this header. Re-asserting "
                                  f"({reasserts}/{PUMP_MAX_REASSERTS}).",
                                  flush=True)
                            try:
                                # RELEASE, then re-take. Measured: a bare
                                # SetSoftware() had no effect at all once the
                                # board had reclaimed the header - 5 attempts,
                                # zero change in duty, every time. The control
                                # object still believes it is in software mode,
                                # so writing the value again does not re-arm
                                # the chip's manual mode. Dropping to default
                                # first forces the transition.
                                ctl[PUMP_HEADER].Control.SetDefault()
                                time.sleep(0.3)
                                ctl[PUMP_HEADER].Control.SetSoftware(pump_duty)
                            except Exception as exc:
                                print(f"  re-assert failed: {exc}", flush=True)
                        elif restarts < PUMP_RESTART_LIMIT:
                            # A fresh process re-enumerates the hardware and
                            # takes the header cleanly - that is the one thing
                            # observed to work. Bounded, so a board that keeps
                            # reclaiming cannot put this into a restart loop.
                            restarts += 1
                            print(f"pump still at {measured:.1f}% after "
                                  f"{PUMP_MAX_REASSERTS} re-asserts - "
                                  f"restarting to re-take {PUMP_HEADER} "
                                  f"({restarts}/{PUMP_RESTART_LIMIT})",
                                  flush=True)
                            sys.stdout.flush()
                            # Deliberately NOT restoring the header first.
                            # os.execv does not run atexit handlers, so the
                            # duty stays put across the swap and the fresh
                            # process takes it from there - the same handoff
                            # as killing and relaunching, which is the case
                            # that was observed to work.
                            os.execv(sys.executable,
                                     [sys.executable] + sys.argv)
                        elif now - last_nag >= PUMP_NAG_SECONDS:
                            # Once it has given up, say so rarely. Repeating
                            # this every cycle produced 3500 identical lines,
                            # which buries the surrounding telemetry and makes
                            # the warning easy to scroll past.
                            last_nag = now
                            print(f"WARNING: pump still at {measured:.1f}% "
                                  f"after {PUMP_MAX_REASSERTS} attempts - "
                                  f"cannot hold {PUMP_HEADER}. The board's "
                                  f"Q-Fan has it; disable Q-Fan for this "
                                  f"header in BIOS for a permanent fix.",
                                  flush=True)
                else:
                    drift_polls = 0
                    reasserts = 0
            if cpu is not None:
                if smoothed is None:
                    smoothed = cpu
                else:
                    a = EMA_RISE if cpu > smoothed else EMA_ALPHA
                    smoothed = a * cpu + (1 - a) * smoothed

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
                _curve, _floor = rad_curve_for(fan_tuning.load_profile())
                target = max(_floor,
                             min(100, round(interpolate(_curve, smoothed)
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
