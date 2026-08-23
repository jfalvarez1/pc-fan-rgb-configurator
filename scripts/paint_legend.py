"""Light every RGB zone a distinct colour and LEAVE it on.

Walk around the case at your own pace and tell me what each colour is.

    python paint_legend.py           # paint and hold
    python paint_legend.py --size 24 # LED count to probe unknown zones at

Empty (0-LED) zones are resized first, otherwise they cannot show anything.
The daemon must be paused (dashboard open, or manual_override.flag present)
or it will repaint over this.

NOTE: OpenRGB occasionally returns a None entry in client.devices - a device
that failed to parse. Every loop here skips those instead of crashing.
"""
import argparse
import time

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

HOST, PORT = "127.0.0.1", 6742
SKIP_TYPES = {"KEYBOARD"}
WRITE_GAP = 0.35   # controllers drop rapid consecutive writes

PALETTE = [
    ("RED",        (255, 0, 0)),
    ("GREEN",      (0, 255, 0)),
    ("BLUE",       (0, 0, 255)),
    ("YELLOW",     (255, 255, 0)),
    ("CYAN",       (0, 255, 255)),
    ("MAGENTA",    (255, 0, 255)),
    ("ORANGE",     (255, 90, 0)),
    ("WHITE",      (255, 255, 255)),
    ("PURPLE",     (140, 0, 255)),
    ("LIME",       (170, 255, 0)),
    ("PINK",       (255, 105, 180)),
    ("TEAL",       (0, 180, 140)),
]


def devices(client):
    """client.devices can contain None entries - filter them out."""
    return [d for d in client.devices
            if d is not None and getattr(d, "type", None) is not None
            and d.type.name not in SKIP_TYPES]


# Controllers that report their own LED counts. Resizing their zones does
# NOT work (the size snaps back) and CORRUPTS the device in OpenRGB - it
# returns as an unparseable None entry until you rescan. Measured twice on
# the NZXT controller. Never resize these.
NO_RESIZE = ("NZXT",)


def resizable(dev):
    return not any(x in dev.name for x in NO_RESIZE)


def direct(dev):
    for want in ("direct", "static"):
        try:
            dev.set_mode(want)
            return want
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=24,
                    help="LED count to give unknown (0-LED) zones")
    args = ap.parse_args()

    client = OpenRGBClient(HOST, PORT, "paint-legend")
    total = len(client.devices)
    usable = devices(client)
    if total != len(usable):
        print(f"note: {total - len(usable)} device(s) skipped "
              f"(keyboard or unparseable)\n")

    # size up anything empty so it can actually display
    changed = False
    for d in usable:
        for z in d.zones:
            if len(z.leds) == 0 and resizable(d):
                try:
                    z.resize(args.size)
                    changed = True
                except Exception:
                    pass
    if changed:
        client.update()
        usable = devices(client)

    print(f"{'COLOUR':<9} {'LEDs':>5}  DEVICE / ZONE")
    print("-" * 68)

    # ONE write per device, covering every LED, with a gap between devices.
    # Writing zone-by-zone back-to-back silently loses updates: the NZXT
    # controller drops rapid consecutive writes (the same quirk that ate fan
    # duty commands), so only the first zone - and only part of it - applied.
    i = 0
    for d in usable:
        mode = direct(d)
        time.sleep(WRITE_GAP)

        buf = []
        for z in d.zones:
            n = len(z.leds)
            if n == 0:
                print(f"{'(none)':<9} {0:>5}  {d.name} / {z.name}"
                      f"   <- cannot be sized, nothing connected")
                continue
            name, rgb = PALETTE[i % len(PALETTE)]
            i += 1
            buf.extend([RGBColor(*rgb)] * n)
            note = "" if mode == "direct" else f"  ({mode} mode)"
            print(f"{name:<9} {n:>5}  {d.name} / {z.name}{note}")

        if not buf:
            continue
        if len(buf) != len(d.leds):
            print(f"           note: {d.name} zone LEDs ({len(buf)}) != device "
                  f"LEDs ({len(d.leds)}); padding")
            buf = (buf + [RGBColor(0, 0, 0)] * len(d.leds))[:len(d.leds)]
        try:
            d.set_colors(buf, fast=True)
            time.sleep(WRITE_GAP)
        except Exception as exc:
            print(f"           WRITE FAILED on {d.name}: {exc}")

    print("\nHolding. Tell me which colour is which and I will label them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
