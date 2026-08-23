"""Find which RGB zone each physical light run is on.

Two passes:
  1. Populated zones get a distinct colour each, held so you can name them.
  2. Empty (0-LED) zones are temporarily resized and lit WHITE one at a time,
     everything else dark - this is how we find the CPU cooler, which OpenRGB
     cannot see until its zone has a size.

    python identify_rgb.py            # both passes
    python identify_rgb.py --hunt     # only the empty-zone hunt
    python identify_rgb.py --restore  # put every zone back to 0 and dark

Nothing here is permanent: sizes are only written to disk by the dashboard.
"""
import argparse
import time

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

HOST, PORT = "127.0.0.1", 6742
PROBE_SIZE = 24          # temporary LED count for an unknown zone
HOLD = 6                 # seconds to hold each probe
SKIP_TYPES = {"KEYBOARD"}

NAMED = [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255)),
         ("YELLOW", (255, 255, 0)), ("CYAN", (0, 255, 255)),
         ("MAGENTA", (255, 0, 255))]


def connect():
    return OpenRGBClient(HOST, PORT, "identify-rgb")


def devices(client):
    """client.devices can contain None entries for devices that failed to
    parse - skip them rather than crashing."""
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
            return True
        except Exception:
            continue
    return False


def blackout(client):
    for d in devices(client):
        direct(d)
        if len(d.leds):
            d.set_colors([RGBColor(0, 0, 0)] * len(d.leds), fast=True)


def populated_pass(client):
    print("=== PASS 1: naming the zones that already work ===")
    i = 0
    for d in devices(client):
        direct(d)
        for z in d.zones:
            if not len(z.leds):
                continue
            name, rgb = NAMED[i % len(NAMED)]
            i += 1
            z.set_colors([RGBColor(*rgb)] * len(z.leds), fast=True)
            print(f"  {name:8} -> {d.name} / {z.name}  ({len(z.leds)} LEDs)")
    print("\nLook at your case and note which run is which colour.\n")


def hunt_pass(client):
    print("=== PASS 2: hunting the empty zones (CPU cooler) ===")
    targets = []
    for d in devices(client):
        for z in d.zones:
            if len(z.leds) == 0:
                targets.append((d, z))

    if not targets:
        print("  no empty zones - everything is already sized")
        return

    print(f"  {len(targets)} empty zone(s) to test, {HOLD}s each\n")
    for d, z in targets:
        if not resizable(d):
            print(f"  {d.name} / {z.name}: reports its own size - not resizable")
            continue
        try:
            z.resize(PROBE_SIZE)
        except Exception as exc:
            print(f"  {d.name} / {z.name}: resize failed ({exc}) - skipping")
            continue

        client.update()
        dev = client.devices[d.id]
        zone = dev.zones[z.id]
        if not len(zone.leds):
            print(f"  {d.name} / {z.name}: still 0 LEDs after resize - skipping")
            continue

        blackout(client)
        direct(dev)
        zone.set_colors([RGBColor(255, 255, 255)] * len(zone.leds), fast=True)
        print(f"  >>> WHITE now on: {dev.name} / {zone.name}"
              f"   (probed at {PROBE_SIZE} LEDs)")
        time.sleep(HOLD)

        zone.set_colors([RGBColor(0, 0, 0)] * len(zone.leds), fast=True)
        # DO NOT resize back to 0. Doing that corrupted the NZXT controller's
        # descriptor in OpenRGB - the device came back as an unparseable None
        # entry and needed a rescan. Leaving the probe size is harmless.
        client.update()

    print("\nWhichever step lit your CPU cooler names its zone.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hunt", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    client = connect()
    print(f"connected: {len(client.devices)} device(s)\n")

    if args.restore:
        # Only blank them. Resizing zones to 0 corrupts some controllers in
        # OpenRGB (it broke the NZXT once), so sizes are left alone.
        blackout(client)
        print("all zones blanked (sizes left intact on purpose)")
        return 0

    if not args.hunt:
        populated_pass(client)
        time.sleep(HOLD)
    hunt_pass(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
