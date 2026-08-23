"""Connectivity check for the RGB + cooling stack.

Run AFTER launching OpenRGB and enabling its SDK server
(OpenRGB -> SDK Server tab -> Start Server, default 127.0.0.1:6742).
"""
import sys

print("=== OpenRGB SDK ===")
try:
    from openrgb import OpenRGBClient
    client = OpenRGBClient("127.0.0.1", 6742, "stack-check")
    print(f"connected, {len(client.devices)} device(s):")
    for d in client.devices:
        print(f"  [{d.id}] {d.name} ({d.type.name}) - {len(d.leds)} LEDs, "
              f"{len(d.zones)} zone(s), modes: {[m.name for m in d.modes]}")
except Exception as e:
    print(f"NOT REACHABLE: {e}")
    print("  -> start OpenRGB.exe (as admin) and enable the SDK server.")

print("\n=== liquidctl devices ===")
try:
    from liquidctl import find_liquidctl_devices
    found = list(find_liquidctl_devices())
    if not found:
        print("no supported USB cooler/hub found (normal if you have none)")
    for dev in found:
        with dev.connect():
            print(f"  {dev.description}")
            for key, value, unit in dev.get_status():
                print(f"    {key}: {value} {unit}")
except Exception as e:
    print(f"error: {e}")
    print("  -> may need admin rights, or a WinUSB driver via Zadig for some devices.")

print(f"\npython {sys.version.split()[0]}")
