"""High-resolution thermal logger for a benchmark or gaming session.

    python bench_logger.py                 # log until Ctrl+C
    python bench_logger.py --label hzd     # name the session
    python bench_logger.py --seconds 600   # stop automatically

Samples every second and writes bench_<label>_<start>.csv, then prints a
summary: peaks, steady-state averages, and which sensor was the hottest
relative to its own limit.

Reads GPU core/util/power from nvidia-smi, and CPU Tctl / VRAM junction /
pump / radiator from sensors.json (published by the elevated mobo_daemon).
Case-fan RPM comes from liquidctl; a failed read logs blank rather than
dying, since the daemon polls the same controller.
"""
import argparse
import csv
import json
import os
import pathlib
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Per-sensor limits, used to report which sensor is closest to ITS OWN limit.
# Comparing raw temperatures across CPU/GPU/VRAM is meaningless - 85 C is
# alarming on a GPU core and routine on GDDR7.
# Tjmax / spec limits, NOT comfort targets.
#   9800X3D Tjmax is 95 C - AMD X3D parts are designed to boost right up to
#   it, so brief spikes into the high 80s are normal behaviour, not distress.
#   I had this at 89 C, which made healthy spikes look alarming.
LIMITS = {"gpu_core": 88.0, "gpu_vram": 95.0, "cpu_tctl": 95.0}


def gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu,power.draw,clocks.sm",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=NO_WINDOW)
        t, u, p, c = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return (float(t), float(u), float(p), float(c))
    except Exception:
        return (None, None, None, None)


def published():
    try:
        d = json.loads((BASE / "sensors.json").read_text())
        if time.time() - d.get("ts", 0) > 30:
            return {}
        return d
    except Exception:
        return {}


def fans():
    try:
        import nzxt_util as nz
        dev = nz.find_nzxt()
        if dev is None:
            return {}, {}
        with dev.connect():
            return nz.read_duties(dev), nz.read_speeds(dev)
    except Exception:
        return {}, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="session")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    start = int(time.time())
    path = BASE / f"bench_{args.label}_{start}.csv"
    cols = ["ts", "elapsed", "gpu_core", "gpu_util", "gpu_power", "gpu_clock",
            "gpu_vram", "cpu_tctl", "cpu_ccd1", "pump_rpm", "rad_rpm",
            "rad_duty", "f1_duty", "f2_duty", "f3_duty",
            "f1_rpm", "f2_rpm", "f3_rpm"]
    rows = []

    print(f"logging every {args.interval}s -> {path.name}")
    print("Ctrl+C to stop and print the summary\n")
    fh = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(cols)

    t0 = time.time()
    try:
        while True:
            gt, gu, gp, gc = gpu()
            pub = published()
            d, r = fans()
            row = [int(time.time()), round(time.time() - t0, 1),
                   gt, gu, gp, gc,
                   pub.get("gpu_vram"), pub.get("cpu_tctl"), pub.get("cpu_ccd1"),
                   pub.get("pump_rpm"), pub.get("rad_rpm"), pub.get("rad_duty"),
                   d.get(1), d.get(2), d.get(3), r.get(1), r.get(2), r.get(3)]
            w.writerow(row)
            fh.flush()
            rows.append(row)
            if len(rows) % 15 == 0:
                print(f"  {row[1]:>6.0f}s  gpu={gt} util={gu}% vram={pub.get('gpu_vram')} "
                      f"cpu={round(pub.get('cpu_tctl') or 0, 1)} "
                      f"fans={d.get(1)}/{d.get(2)}/{d.get(3)}%", flush=True)
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        fh.close()

    if not rows:
        return 0

    idx = {c: i for i, c in enumerate(cols)}

    def col(name):
        return [r[idx[name]] for r in rows
                if isinstance(r[idx[name]], (int, float))]

    print(f"\n===== {args.label}: {len(rows)} samples, "
          f"{rows[-1][1]:.0f}s =====")
    for name in ("gpu_core", "gpu_vram", "cpu_tctl", "gpu_util", "gpu_power",
                 "pump_rpm", "rad_rpm", "f1_rpm", "f2_rpm", "f3_rpm"):
        v = col(name)
        if v:
            print(f"  {name:<10} min={min(v):>7.1f}  avg={sum(v)/len(v):>7.1f}  "
                  f"max={max(v):>7.1f}")

    # steady state = last third of the run, once thermals have saturated
    tail = rows[len(rows) * 2 // 3:]

    def tavg(name):
        v = [r[idx[name]] for r in tail if isinstance(r[idx[name]], (int, float))]
        return sum(v) / len(v) if v else None

    print("\n  STEADY STATE (last third):")
    for name in ("gpu_core", "gpu_vram", "cpu_tctl"):
        a = tavg(name)
        if a is not None:
            head = a / LIMITS[name] * 100
            print(f"    {name:<10} {a:>6.1f} C   = {head:>5.1f}% of its "
                  f"{LIMITS[name]:.0f} C limit")
    print("\n  -> the highest percentage is the sensor that should be "
          "driving your fans")
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
