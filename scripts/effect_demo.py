"""Preview the spatial effects live on the hardware.

    python effect_demo.py                 # cycle every effect, 12s each
    python effect_demo.py --effect plasma # hold one
    python effect_demo.py --list

Renders by PHYSICAL POSITION via case_layout, so effects sweep across the
case correctly. Takes control while running and releases on exit.
"""
import argparse
import pathlib
import time

import case_layout
import rgb_effects as fx

BASE = pathlib.Path(__file__).resolve().parent
OVERRIDE = BASE / "manual_override.flag"
FPS = 30.0


def resolve(client, el):
    m = [d for d in client.devices
         if d is not None and getattr(d, "type", None) is not None
         and el["device"].lower() in d.name.lower()]
    if not m:
        return None, None
    dev = m[min(el.get("dev_index", 0), len(m) - 1)]
    off = 0
    for z in dev.zones:
        if el["zone"].lower() in z.name.lower():
            return dev, off + el["start"]
        off += len(z.leds)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--effect")
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--sunset", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("effects:", ", ".join(sorted(fx.SPATIAL)))
        return 0

    from openrgb import OpenRGBClient
    from openrgb.utils import RGBColor
    client = OpenRGBClient("127.0.0.1", 6742, "effect-demo")

    runs, byel = [], {}
    for el, i, nx, ny in case_layout.led_positions():
        byel.setdefault(el["id"], (el, []))[1].append((i, nx, ny))
    for el, pts in byel.values():
        dev, off = resolve(client, el)
        if dev is None:
            continue
        for want in ("direct", "custom", "static"):
            try:
                dev.set_mode(want)
                break
            except Exception:
                continue
        runs.append((dev, off, pts))
    total = sum(len(p) for _d, _o, p in runs)
    print(f"{total} LEDs across {len(runs)} runs")

    palette = fx.SUNSET if args.sunset else fx.SYNTHWAVE
    order = [args.effect] if args.effect else sorted(fx.SPATIAL)
    OVERRIDE.write_text("effect_demo")
    print("took control (daemon paused)\n")

    try:
        for name in order:
            fn = fx.SPATIAL[name]
            print(f"  >>> {name}")
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                t = time.monotonic()
                bufs = {}
                for dev, off, pts in runs:
                    if dev.id not in bufs:
                        bufs[dev.id] = ([RGBColor(0, 0, 0)] * len(dev.leds), dev)
                    buf = bufs[dev.id][0]
                    for i, nx, ny in pts:
                        k = off + i
                        if 0 <= k < len(buf):
                            buf[k] = RGBColor(*fn(nx, ny, t, palette))
                for buf, dev in bufs.values():
                    dev.set_colors(buf, fast=True)
                time.sleep(1.0 / FPS)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        OVERRIDE.unlink(missing_ok=True)
        print("released control back to the daemon")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
