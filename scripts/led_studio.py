"""LED Studio - visual per-LED editor for the whole case.

    python led_studio.py          then open http://localhost:8770

A SignalRGB-style editor: the case layout is drawn to scale, every LED is an
individually clickable dot, and edits preview instantly in the browser. The
hardware is only written when you press Apply (or enable Live mode).

While it holds control it writes manual_override.flag so thermal_rgb_loop
stands down. Releasing control hands the lights back to the daemon.

The physical layout below is this specific build - NZXT H9 Flow, side F360
intake, bottom F420 intake, rear exhaust, top-mounted Arctic 360 radiator,
vertically mounted GPU.
"""
import json
import pathlib
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent
OVERRIDE = BASE / "manual_override.flag"
PORT = 8770
HOST, RGB_PORT = "127.0.0.1", 6742

# ---------------------------------------------------------------- LAYOUT ---
# Each element is one physical light run, drawn as a ring of `count` dots.
# device / zone are substring matches; start is the LED offset within the zone.
# "rot"  rotates the drawn ring in degrees - LED 0 is not always at 12
#        o'clock; it depends on mounting and which way the cable exits.
# "flip"  mirrors the ring LEFT<->RIGHT (top/bottom unchanged).
# "vflip" mirrors the ring TOP<->BOTTOM (left/right unchanged).
#        Both are applied after "rot", so they act on final drawn positions.
LAYOUT = [
    # top: Arctic 360 radiator (exhaust) + pump block, all on mobo header 1
    {"id": "pump",     "label": "Arctic pump",      "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 0,  "count": 12,
     "x": 300, "y": 300, "r": 34, "kind": "pump", "flip": True, "vflip": True},
    {"id": "rad_r",    "label": "Rad fan RIGHT",    "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 12, "count": 12,
     "x": 470, "y": 70, "r": 42, "kind": "fan", "rot": 90, "vflip": True},
    {"id": "rad_m",    "label": "Rad fan MIDDLE",   "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 24, "count": 12,
     "x": 370, "y": 70, "r": 42, "kind": "fan", "rot": 90, "vflip": True},
    {"id": "rad_l",    "label": "Rad fan LEFT",     "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 36, "count": 12,
     "x": 270, "y": 70, "r": 42, "kind": "fan", "rot": 90, "vflip": True},

    # side intake F360 (user calls these the front fans) - vertical stack
    {"id": "side1",    "label": "Side F360 bottom",    "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 0,  "count": 8,
     "x": 570, "y": 365, "r": 40, "kind": "fan", "flip": True},
    {"id": "side2",    "label": "Side F360 mid",    "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 8,  "count": 8,
     "x": 570, "y": 265, "r": 40, "kind": "fan", "flip": True},
    {"id": "side3",    "label": "Side F360 top", "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 16, "count": 8,
     "x": 570, "y": 165, "r": 40, "kind": "fan", "flip": True},

    # bottom intake F420
    {"id": "bot1",     "label": "Bottom F420 L",    "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 0,  "count": 8,
     "x": 250, "y": 500, "r": 44, "kind": "fan", "rot": -90, "flip": True},
    {"id": "bot2",     "label": "Bottom F420 M",    "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 8,  "count": 8,
     "x": 355, "y": 500, "r": 44, "kind": "fan", "rot": -90, "flip": True},
    {"id": "bot3",     "label": "Bottom F420 R",    "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 16, "count": 8,
     "x": 460, "y": 500, "r": 44, "kind": "fan", "rot": -90, "flip": True},

    # rear exhaust
    {"id": "rear",     "label": "Rear exhaust",     "device": "NZXT",
     "zone": "Hue 2 Channel 3", "start": 0,  "count": 8,
     "x": 100, "y": 195, "r": 38, "kind": "fan", "rot": -45},

    # GPU logo (vertically mounted card) - cabled to mobo header 2
    {"id": "gpu_text", "label": "ZOTAC text",  "device": "PRIME",
     "zone": "Aura Addressable 2", "start": 0, "count": 5,
     "x": 232, "y": 378, "r": 0, "kind": "strip_h"},
    {"id": "gpu_logo", "label": "ZOTAC logo",  "device": "PRIME",
     "zone": "Aura Addressable 2", "start": 5, "count": 3,
     "x": 300, "y": 378, "r": 0, "kind": "strip_h"},
    # NOTE: this zone is sized to 24 but only LEDs 0-7 drive anything. Tested:
    # 8-23 light nothing - spare motherboard ARGB header capacity with nothing
    # connected. Left sized at 24 so a future strip on that header just works;
    # not drawn, because there is nothing there to click.,

    # RAM - two sticks, separate devices sharing a name (address by id)
    {"id": "ram0",     "label": "RAM stick 1",      "device": "Corsair",
     "zone": "Corsair DRAM", "start": 0, "count": 10, "dev_index": 0,
     "x": 430, "y": 205, "r": 22, "kind": "strip_v"},
    {"id": "ram1",     "label": "RAM stick 2",      "device": "Corsair",
     "zone": "Corsair DRAM", "start": 0, "count": 10, "dev_index": 1,
     "x": 468, "y": 205, "r": 22, "kind": "strip_v"},
]


class Hardware:
    """Owns the OpenRGB connection. All writes send whole device buffers."""

    def __init__(self):
        self.client = None
        self.lock = threading.Lock()
        self.buf = {}      # dev_id -> [(r,g,b)] for every LED on the device
        self.connect()

    def connect(self):
        try:
            from openrgb import OpenRGBClient
            self.client = OpenRGBClient(HOST, RGB_PORT, "led-studio")
            sizes = {}
            try:
                sizes = json.loads((BASE / "rgb_zone_sizes.json").read_text())
            except Exception:
                pass
            for d in self.client.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                for z in d.zones:
                    want = sizes.get(f"{d.name}|{z.name}")
                    if want and len(z.leds) != want and "NZXT" not in d.name:
                        try:
                            z.resize(want)
                        except Exception:
                            pass
            self.client.update()
            for d in self.client.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                for want in ("direct", "custom", "static"):
                    try:
                        d.set_mode(want)
                        break
                    except Exception:
                        continue
                self.buf[d.id] = [(0, 0, 0)] * len(d.leds)
            return True
        except Exception as exc:
            print(f"OpenRGB unavailable: {exc}")
            self.client = None
            return False

    def resolve(self, el):
        """Map a layout element to (device, absolute LED offset)."""
        if self.client is None:
            return None, None
        matches = [d for d in self.client.devices
                   if d is not None and getattr(d, "type", None) is not None
                   and el["device"].lower() in d.name.lower()]
        if not matches:
            return None, None
        # RAM sticks share a name, so pick by index when told to
        dev = matches[min(el.get("dev_index", 0), len(matches) - 1)]
        off = 0
        for z in dev.zones:
            if el["zone"].lower() in z.name.lower():
                return dev, off + el["start"]
            off += len(z.leds)
        return None, None

    def apply(self, colours):
        """colours: {element_id: [[r,g,b], ...]} - one entry per LED."""
        if self.client is None and not self.connect():
            return {"ok": False, "error": "no OpenRGB connection"}
        from openrgb.utils import RGBColor
        touched = {}
        with self.lock:
            for el in LAYOUT:
                vals = colours.get(el["id"])
                if not vals:
                    continue
                dev, off = self.resolve(el)
                if dev is None:
                    continue
                buf = self.buf.setdefault(dev.id, [(0, 0, 0)] * len(dev.leds))
                if len(buf) != len(dev.leds):
                    buf = [(0, 0, 0)] * len(dev.leds)
                    self.buf[dev.id] = buf
                for i, c in enumerate(vals):
                    if off + i < len(buf):
                        buf[off + i] = tuple(int(x) for x in c)
                touched[dev.id] = dev
            errs = []
            for dev_id, dev in touched.items():
                try:
                    dev.set_colors([RGBColor(*c) for c in self.buf[dev_id]],
                                   fast=False)
                except Exception as exc:
                    errs.append(f"{dev.name}: {exc}")
        return {"ok": not errs, "error": "; ".join(errs) if errs else None,
                "devices": len(touched)}


HW = Hardware()


def layout_payload():
    out = []
    for el in LAYOUT:
        dev, off = HW.resolve(el)
        out.append({**{k: el[k] for k in
                       ("id", "label", "count", "x", "y", "r", "kind")},
                    "rot": el.get("rot", 0),
                    "flip": bool(el.get("flip", False)),
                    "vflip": bool(el.get("vflip", False)),
                    "connected": dev is not None,
                    "device": dev.name if dev is not None else None})
    return out


def live_state():
    st = {}
    for name in ("fan_state.json", "sensors.json"):
        try:
            st[name.split(".")[0]] = json.loads((BASE / name).read_text())
        except Exception:
            st[name.split(".")[0]] = {}
    st["controlling"] = OVERRIDE.exists()
    return st


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (BASE / "led_studio.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path == "/api/layout":
            return self._send(200, json.dumps(layout_payload()))
        if self.path == "/api/state":
            return self._send(200, json.dumps(live_state()))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}))

        if self.path == "/api/apply":
            return self._send(200, json.dumps(HW.apply(body.get("colours", {}))))
        if self.path == "/api/control":
            if body.get("take"):
                OVERRIDE.write_text("led_studio")
            else:
                OVERRIDE.unlink(missing_ok=True)
            return self._send(200, json.dumps({"controlling": OVERRIDE.exists()}))
        if self.path == "/api/reconnect":
            return self._send(200, json.dumps({"ok": HW.connect()}))
        self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"LED Studio on {url}")
    print("Ctrl+C to stop. Closing the page does NOT release the lights -")
    print("use the Release button, or stop this server.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping; releasing control")
        OVERRIDE.unlink(missing_ok=True)
