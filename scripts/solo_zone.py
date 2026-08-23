"""Light exactly ONE zone and turn everything else off, then hold.

Unambiguous identification: with a single run lit there is nothing to
misattribute.

    python solo_zone.py --list
    python solo_zone.py --dev 1 --zone 2            # white
    python solo_zone.py --dev 1 --zone 2 --rgb 255,0,0
    python solo_zone.py --all-off

Devices that failed to parse (None entries) are skipped. The daemon must be
paused - the dashboard does that automatically while it is open.
"""
import argparse

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

HOST, PORT = "127.0.0.1", 6742
SKIP_TYPES = {"KEYBOARD"}


def usable(client):
    return [d for d in client.devices
            if d is not None and getattr(d, "type", None) is not None
            and d.type.name not in SKIP_TYPES]


def direct(dev):
    for want in ("direct", "static"):
        try:
            dev.set_mode(want)
            return
        except Exception:
            continue


def blackout(client):
    for d in usable(client):
        direct(d)
        if len(d.leds):
            d.set_colors([RGBColor(0, 0, 0)] * len(d.leds), fast=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dev", type=int)
    ap.add_argument("--zone", type=int)
    ap.add_argument("--rgb", default="255,255,255")
    ap.add_argument("--all-off", action="store_true")
    ap.add_argument("--match", help="substring of device name (indices shift "
                                    "after a rescan; names do not)")
    ap.add_argument("--zname", help="substring of zone name")
    args = ap.parse_args()

    client = OpenRGBClient(HOST, PORT, "solo-zone")

    if args.list:
        for d in client.devices:
            if d is None:
                print("  <None - failed to parse; rescan OpenRGB>")
                continue
            print(f"[{d.id}] {d.name} ({d.type.name})")
            for z in d.zones:
                print(f"     zone {z.id}: {z.name}  ({len(z.leds)} LEDs)")
        return 0

    if args.all_off:
        blackout(client)
        print("everything off")
        return 0

    rgb = tuple(int(x) for x in args.rgb.split(","))

    if args.match:
        cands = [d for d in usable(client) if args.match.lower() in d.name.lower()]
        if not cands:
            print(f"no device matching {args.match!r}")
            return 1
        d = cands[0]
        if args.zname:
            zs = [z for z in d.zones if args.zname.lower() in z.name.lower()]
            if not zs:
                print(f"no zone matching {args.zname!r} on {d.name}")
                return 1
            z = zs[0]
        else:
            z = d.zones[args.zone or 0]
    elif args.dev is not None and args.zone is not None:
        d = client.devices[args.dev]
        z = d.zones[args.zone]
    else:
        print("need --match (preferred) or --dev/--zone; see --list")
        return 1

    blackout(client)
    if not len(z.leds):
        print(f"{d.name} / {z.name} has 0 LEDs - set a size first")
        return 1
    direct(d)
    z.set_colors([RGBColor(*rgb)] * len(z.leds), fast=True)
    print(f"ONLY lit: {d.name} / {z.name}  ({len(z.leds)} LEDs) = rgb{rgb}")
    print("everything else is off. What is glowing?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
