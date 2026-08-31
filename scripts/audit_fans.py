"""Audit the cooling system against its own curves, on live data.

    python audit_fans.py 240        # sample for 240 seconds, then report

Reads only the files the daemons publish, so it changes nothing and can run
while everything is live. It answers questions that a snapshot cannot:

* Does each fan actually sit where its curve says it should, once the
  deadband and fall delay are allowed for?
* Does RPM track duty? A fan whose duty rises while its RPM does not is
  obstructed, failing, or unplugged.
* Is the pump genuinely steady, or drifting the way it did when the board
  reclaimed the header?
* Do the case fans follow the sensor that is supposed to lead them?

Every conclusion is reported with the evidence behind it - the range of
temperature actually covered, the number of samples, and the spread - because
a fan audit over a narrow idle range proves almost nothing, and saying so is
more useful than a confident number.
"""
import json
import pathlib
import statistics as st
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import fan_tuning                        # noqa: E402
import mobo_daemon as md                 # noqa: E402
import thermal_rgb_loop as trl           # noqa: E402

POLL = 2.0


def load(name):
    try:
        return json.loads((BASE / name).read_text())
    except Exception:
        return {}


def collect(seconds):
    rows = []
    seen = set()
    end = time.time() + seconds
    while time.time() < end:
        fs, sn = load("fan_state.json"), load("sensors.json")
        key = (fs.get("ts"), sn.get("ts"))
        if key not in seen and fs and sn:
            seen.add(key)
            temps = dict(fs.get("temps") or {})
            for k in ("cpu_tctl", "gpu_core", "gpu_vram"):
                if k in sn:
                    temps.setdefault(k, sn[k])
            rows.append({
                "t": time.time(), "temps": temps,
                "fans": fs.get("fans") or {},
                "pump_duty": sn.get("pump_duty"), "pump_rpm": sn.get("pump_rpm"),
                "rad_duty": sn.get("rad_duty"), "rad_rpm": sn.get("rad_rpm"),
            })
            print(f"\r  {len(rows)} samples  cpu {temps.get('cpu_tctl', 0):.0f}C "
                  f"gpu {temps.get('gpu_core', 0):.0f}C "
                  f"vram {temps.get('gpu_vram', 0):.0f}C   ", end="", flush=True)
        time.sleep(POLL)
    print()
    return rows


def demanded(ch, temps, trims):
    """What the curve asks of this channel, as the daemon computes it."""
    cfg = trl.FAN_CHANNELS[ch]
    best, lead = None, None
    for src, curve in cfg["curves"].items():
        if src not in temps:
            continue
        want = trl.interpolate(curve, temps[src])
        if best is None or want > best:
            best, lead = want, src
    if best is None:
        return None, None
    got = max(trl.MIN_DUTY,
              min(trl.MAX_DUTY, round(best + trims.get(ch, 0.0))))
    return got, lead


def corr(xs, ys):
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    return num / den if den else None


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    trims = fan_tuning.load_trims()
    print(f"sampling for {secs}s - vary the load if you can\n")
    rows = collect(secs)
    try:
        import csv
        with open(BASE / "fan_audit.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t", "cpu", "gpu", "vram", "fan1", "fan2", "fan3",
                        "pump_duty", "pump_rpm", "rad_duty"])
            for r in rows:
                w.writerow([f"{r['t']:.1f}",
                            r["temps"].get("cpu_tctl"),
                            r["temps"].get("gpu_core"),
                            r["temps"].get("gpu_vram")]
                           + [(r["fans"].get(c) or {}).get("duty")
                              for c in ("fan1", "fan2", "fan3")]
                           + [r["pump_duty"], r["pump_rpm"], r["rad_duty"]])
        print("raw samples written to fan_audit.csv")
    except Exception:
        pass
    if len(rows) < 5:
        print("not enough samples; are the daemons running?")
        return 1

    print(f"\n{'='*66}\n{len(rows)} samples over "
          f"{(rows[-1]['t']-rows[0]['t'])/60:.1f} minutes\n{'='*66}")

    # --- what range did we actually see? A verdict is only as good as this.
    print("\nTEMPERATURE RANGE COVERED")
    for k in ("cpu_tctl", "gpu_core", "gpu_vram"):
        v = [r["temps"][k] for r in rows if k in r["temps"]]
        if v:
            print(f"  {k:<9} {min(v):5.1f} - {max(v):5.1f} C   "
                  f"(spread {max(v)-min(v):4.1f})")

    # --- case fans against their curves
    print("\nCASE FANS vs THEIR CURVES")
    for ch, cfg in trl.FAN_CHANNELS.items():
        dev, want_s, got_s, leads = [], [], [], {}
        for r in rows:
            f = r["fans"].get(ch) or {}
            if f.get("duty") is None:
                continue
            want, lead = demanded(ch, r["temps"], trims)
            if want is None:
                continue
            want_s.append(want)
            got_s.append(f["duty"])
            dev.append(f["duty"] - want)
            leads[lead] = leads.get(lead, 0) + 1
        if not dev:
            print(f"  {cfg['label']:<20} no data")
            continue
        lead_txt = ", ".join(f"{k} {v*100//len(dev)}%" for k, v in
                             sorted(leads.items(), key=lambda x: -x[1]))
        worst = max(abs(d) for d in dev)
        inside = sum(1 for d in dev if abs(d) <= trl.DUTY_DEADBAND)
        # Direction is the whole safety question. Running ABOVE the curve is
        # the fall delay holding a higher duty while temperature drops - extra
        # cooling, and deliberate. Running BELOW means the fan has not caught
        # up with a rising temperature, which is the only direction that can
        # actually cost headroom.
        over = [d for d in dev if d > trl.DUTY_DEADBAND]
        under = [d for d in dev if d < -trl.DUTY_DEADBAND]
        print(f"  {cfg['label']:<20} demanded {min(want_s):3d}-{max(want_s):3d}%  "
              f"measured {min(got_s):3d}-{max(got_s):3d}%")
        print(f"  {'':<20} mean deviation {st.mean(dev):+5.1f}pts, worst "
              f"{worst:4.1f}, within deadband {inside*100//len(dev)}% of the time")
        print(f"  {'':<20} above curve {len(over)*100//len(dev)}% "
              f"(worst +{max(over) if over else 0:.0f}) - lagging a drop, safe")
        print(f"  {'':<20} BELOW curve {len(under)*100//len(dev)}% "
              f"(worst {min(under) if under else 0:.0f}) - lagging a rise")
        print(f"  {'':<20} led by: {lead_txt}")

    # --- rpm should track duty; if it does not, the fan is in trouble
    print("\nRPM TRACKS DUTY")
    for ch, cfg in trl.FAN_CHANNELS.items():
        d = [r["fans"][ch]["duty"] for r in rows
             if (r["fans"].get(ch) or {}).get("rpm") is not None]
        q = [r["fans"][ch]["rpm"] for r in rows
             if (r["fans"].get(ch) or {}).get("rpm") is not None]
        c = corr(d, q)
        if c is None:
            print(f"  {cfg['label']:<20} duty never moved "
                  f"({d[0] if d else '?'}%), cannot tell")
        else:
            verdict = "ok" if c > 0.8 else ("WEAK" if c > 0.4 else "SUSPECT")
            print(f"  {cfg['label']:<20} r={c:+.2f}  {verdict}   "
                  f"rpm {min(q):.0f}-{max(q):.0f}")

    # --- pump: fixed, so any movement is the story
    print("\nPUMP")
    pd = [r["pump_duty"] for r in rows if r["pump_duty"] is not None]
    pr = [r["pump_rpm"] for r in rows if r["pump_rpm"] is not None]
    cfg = load("pump_config.json")
    want = cfg.get("pump_duty")
    if pd:
        print(f"  commanded {want}%   measured {min(pd):.1f}-{max(pd):.1f}% "
              f"(spread {max(pd)-min(pd):.1f})")
        print(f"  rpm {min(pr):.0f}-{max(pr):.0f}, mean {st.mean(pr):.0f}")
        off = abs(st.mean(pd) - (want or 0))
        print(f"  {'HOLDING' if off <= 5 else 'DRIFTED - the board may have '
              'taken the header'}  (mean off by {off:.1f} pts)")
        floor = 1500
        if min(pr) < floor:
            print(f"  WARNING: dipped to {min(pr):.0f} rpm, below the "
                  f"{floor} rpm abort floor in the measured map")

    # --- radiator against its curve
    print("\nRADIATOR")
    rd = [(r["temps"].get("cpu_tctl"), r["rad_duty"]) for r in rows
          if r["rad_duty"] is not None and "cpu_tctl" in r["temps"]]
    if rd:
        devs = [got - max(md.RAD_MIN_DUTY,
                          min(100, round(md.interpolate(md.RAD_CURVE, t)
                                         + trims.get("rad", 0.0))))
                for t, got in rd]
        print(f"  measured {min(d for _, d in rd):.0f}-{max(d for _, d in rd):.0f}%"
              f"  mean deviation {st.mean(devs):+.1f}pts, worst "
              f"{max(abs(d) for d in devs):.1f}")
        c = corr([t for t, _ in rd], [d for _, d in rd])
        print(f"  follows CPU temperature: r={c:+.2f}" if c is not None
              else "  CPU temperature never moved enough to tell")
    return 0


if __name__ == "__main__":
    sys.exit(main())
