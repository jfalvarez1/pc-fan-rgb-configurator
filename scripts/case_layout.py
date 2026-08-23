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
    # ---- top: Arctic 360 radiator (exhaust) + pump, all on mobo header 1
    {"id": "rad_l",    "label": "Radiator L",        "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 36, "count": 12,
     "x": 414, "y": 145, "r": 62, "kind": "fan", "rot": 90, "vflip": True},
    {"id": "rad_m",    "label": "Radiator M",        "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 24, "count": 12,
     "x": 593, "y": 145, "r": 62, "kind": "fan", "rot": 90, "vflip": True},
    {"id": "rad_r",    "label": "Radiator R",        "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 12, "count": 12,
     "x": 773, "y": 145, "r": 62, "kind": "fan", "rot": 90, "vflip": True},
    {"id": "pump",     "label": "Arctic pump",       "device": "PRIME",
     "zone": "Aura Addressable 1", "start": 0,  "count": 12,
     "x": 414, "y": 400, "r": 50, "kind": "pump",
     "flip": True, "vflip": True},

    # ---- RAM: vertical sticks, to the RIGHT of the pump
    {"id": "ram0",     "label": "RAM 1",             "device": "Corsair",
     "zone": "Corsair DRAM", "start": 0, "count": 10, "dev_index": 0,
     "x": 585, "y": 400, "r": 26, "kind": "strip_v"},
    {"id": "ram1",     "label": "RAM 2",             "device": "Corsair",
     "zone": "Corsair DRAM", "start": 0, "count": 10, "dev_index": 1,
     "x": 643, "y": 400, "r": 26, "kind": "strip_v"},

    # ---- rear exhaust, LEFT side
    {"id": "rear",     "label": "Rear exhaust",      "device": "NZXT",
     "zone": "Hue 2 Channel 3", "start": 0,  "count": 8,
     "x": 163, "y": 370, "r": 55, "kind": "fan", "rot": -45},

    # ---- GPU: vertically mounted. The lit run sits toward the RIGHT end of
    # the card, so text then logo are placed right-adjusted, not centred.
    {"id": "gpu_text", "label": "ZOTAC text",        "device": "PRIME",
     "zone": "Aura Addressable 2", "start": 0, "count": 5,
     "x": 624, "y": 599, "r": 0, "kind": "strip_h"},
    {"id": "gpu_logo", "label": "logo",              "device": "PRIME",
     "zone": "Aura Addressable 2", "start": 5, "count": 3,
     "x": 729, "y": 599, "r": 0, "kind": "strip_h"},

    # ---- side intake F360 (the user calls these the front fans), RIGHT side
    {"id": "side3",    "label": "Side F360 top",     "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 16, "count": 8,
     "x": 974, "y": 241, "r": 60, "kind": "fan", "flip": True},
    {"id": "side2",    "label": "Side F360 mid",     "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 8,  "count": 8,
     "x": 974, "y": 435, "r": 60, "kind": "fan", "flip": True},
    {"id": "side1",    "label": "Side F360 bottom",  "device": "NZXT",
     "zone": "Hue 2 Channel 1", "start": 0,  "count": 8,
     "x": 974, "y": 628, "r": 60, "kind": "fan", "flip": True},

    # ---- bottom intake F420
    {"id": "bot1",     "label": "Bottom F420 L",     "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 0,  "count": 8,
     "x": 345, "y": 800, "r": 65, "kind": "fan", "rot": -90, "flip": True},
    {"id": "bot2",     "label": "Bottom F420 M",     "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 8,  "count": 8,
     "x": 552, "y": 800, "r": 65, "kind": "fan", "rot": -90, "flip": True},
    {"id": "bot3",     "label": "Bottom F420 R",     "device": "NZXT",
     "zone": "Hue 2 Channel 2", "start": 16, "count": 8,
     "x": 759, "y": 800, "r": 65, "kind": "fan", "rot": -90, "flip": True},
]

# canvas the coordinates above are expressed in
CANVAS_W, CANVAS_H = 1130.0, 940.0


def _ring_xy(el, i):
    """Position of LED i within an element, honouring rot / flip / vflip."""
    kind = el.get("kind", "fan")
    n = el["count"]
    if kind == "strip_v":
        h = n * 21.0
        return el["x"], el["y"] - h / 2 + 10 + i * 21
    if kind == "strip_h":
        w = n * 21.0
        return el["x"] - w / 2 + 10.5 + i * 21, el["y"]
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
