"""Physical layout of the case - the single source of truth.

Every element is one light run. The transforms were each verified by lighting
an LED and looking at the machine; NOT ONE of them was correct by default.
LED chain direction depends on how a fan is mounted and which way its cable
exits, and no manufacturer follows a convention.

    rot   rotate the ring, degrees
    flip  mirror LEFT<->RIGHT   (top/bottom unchanged)
    vflip mirror TOP<->BOTTOM   (left/right unchanged)

Both mirrors are applied AFTER rot, so they act on final drawn positions.

led_positions() returns every LED with a normalised (nx, ny) position in the
case, which is what lets effects be computed SPATIALLY - a wave then sweeps
across the case correctly instead of scrambling at every fan boundary.
"""
import math

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

# canvas the coordinates above are expressed in
CANVAS_W, CANVAS_H = 660.0, 580.0


def _ring_xy(el, i):
    """Position of LED i within an element, honouring rot / flip / vflip."""
    kind = el.get("kind", "fan")
    n = el["count"]
    if kind == "strip_v":
        h = n * 13.0
        return el["x"], el["y"] - h / 2 + 6 + i * 13
    if kind == "strip_h":
        w = n * 11.0
        return el["x"] - w / 2 + 5.5 + i * 11, el["y"]
    a = (i / n) * math.tau - math.pi / 2 + math.radians(el.get("rot", 0))
    if el.get("flip"):
        a = math.pi - a
    if el.get("vflip"):
        a = -a
    r = el.get("r", 30) or 30
    return el["x"] + math.cos(a) * r, el["y"] + math.sin(a) * r


def led_positions():
    """[(element, led_index, nx, ny)] with nx/ny normalised to 0..1."""
    out = []
    for el in LAYOUT:
        for i in range(el["count"]):
            x, y = _ring_xy(el, i)
            out.append((el, i, x / CANVAS_W, y / CANVAS_H))
    return out
