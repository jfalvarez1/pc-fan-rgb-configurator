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
    # ---- Razer Huntsman Mini, on the desk below the case. OpenRGB exposes
    # it as a 15x5 matrix; column 0 and a few wide-key slots are gaps that
    # exist on the wire but not in plastic, so they are listed and skipped.
    {"id": "keyboard", "fx_group": "keyboard", "label": "Razer Huntsman Mini", "device": "Huntsman",
     "zone": "Keyboard", "start": 0, "count": 75,
     "x": 565, "y": 1030, "r": 0, "kind": "grid",
     "cols": 15, "rows": 5, "cell": 27,
     "blanks": [0, 15, 30, 43, 45, 47, 58, 60, 64, 65, 66, 68, 69, 70],
     "keys": {1: 'Escape', 2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0', 12: '-', 13: '=', 14: 'Backspace', 16: 'Tab', 17: 'Q', 18: 'W', 19: 'E', 20: 'R', 21: 'T', 22: 'Y', 23: 'U', 24: 'I', 25: 'O', 26: 'P', 27: '[', 28: ']', 29: '\\ (ANSI)', 31: 'Caps Lock', 32: 'A', 33: 'S', 34: 'D', 35: 'F', 36: 'G', 37: 'H', 38: 'J', 39: 'K', 40: 'L', 41: ';', 42: "'", 44: 'Enter', 46: 'Left Shift', 48: 'Z', 49: 'X', 50: 'C', 51: 'V', 52: 'B', 53: 'N', 54: 'M', 55: ',', 56: '.', 57: '/', 59: 'Right Shift', 61: 'Left Control', 62: 'Left Windows', 63: 'Left Alt', 67: 'Space', 71: 'Right Alt', 72: 'Right Fn', 73: 'Menu', 74: 'Right Control'}},
]

# canvas the coordinates above are expressed in
CANVAS_W, CANVAS_H = 1130.0, 1120.0


def _ring_xy(el, i):
    """Position of LED i within an element, honouring rot / flip / vflip."""
    kind = el.get("kind", "fan")
    n = el["count"]
    if kind == "strip_v":
        h = n * 21.0
        return el["x"], el["y"] - h / 2 + 10 + i * 21
    if kind == "grid":
        cols, cell = el.get("cols", 15), el.get("cell", 27)
        rows = el.get("rows", 5)
        cx = el["x"] - (cols - 1) * cell / 2
        cy = el["y"] - (rows - 1) * cell / 2
        return cx + (i % cols) * cell, cy + (i // cols) * cell
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
    """[(element, led_index, nx, ny)] with nx/ny in EFFECT space, 0..1.

    Effect space is normalised PER GROUP, not across the whole drawing. The
    keyboard sits below the case on screen, so sharing one bounding box would
    squash every key into the bottom tenth - a "stack" or vertical wave would
    treat the entire keyboard as one bottom row.

    Normalising per group means the case and the keyboard each span the full
    0..1 range, so the top radiator LEDs and the top keyboard row rise
    together, and the bottom fans and the bottom key row sit together. Effects
    then read as running side by side across both.
    """
    raw = []
    for el in LAYOUT:
        grp = el.get("fx_group", "case")
        for i in range(el["count"]):
            x, y = _ring_xy(el, i)
            raw.append([el, i, x, y, grp])

    bounds = {}
    for _el, _i, x, y, grp in raw:
        b = bounds.setdefault(grp, [x, y, x, y])
        b[0], b[1] = min(b[0], x), min(b[1], y)
        b[2], b[3] = max(b[2], x), max(b[3], y)

    out = []
    for el, i, x, y, grp in raw:
        x0, y0, x1, y1 = bounds[grp]
        w = (x1 - x0) or 1.0
        h = (y1 - y0) or 1.0
        # inset slightly so nothing sits exactly on 0.0 or 1.0
        nx = 0.04 + 0.92 * (x - x0) / w
        ny = 0.04 + 0.92 * (y - y0) / h
        out.append((el, i, nx, ny))
    return out


def group_grids():
    """(cols, rows) per fx_group, for groups that ARE a physical matrix.

    The keyboard is a real 15x5 grid, so an effect can snap a strand to one
    key wide and advance it a row at a time. Ring layouts have no rows or
    columns at all, so they get None and effects keep their spatial
    behaviour - quantising a fan ring to a made-up grid would only alias.
    """
    els = {}
    for el in LAYOUT:
        els.setdefault(el.get("fx_group", "case"), []).append(el)
    out = {}
    for g, lst in els.items():
        if len(lst) == 1 and lst[0].get("kind") == "grid":
            out[g] = (int(lst[0].get("cols", 1)), int(lst[0].get("rows", 1)))
        else:
            out[g] = None
    return out


def cell_of(el, i):
    """(col, row, cols, rows) for an LED on a matrix element, else None.

    Taken from the LED's INDEX on the wire, so it is exact - no rounding of
    normalised coordinates, and it stays correct inside an effect layer whose
    local coordinates do not line up with key boundaries.
    """
    if el.get("kind") != "grid":
        return None
    cols = int(el.get("cols", 1))
    rows = int(el.get("rows", 1))
    return (i % cols, i // cols, cols, rows)


# Which resource each element reports, for the usage-gradient effect.
#   cpu  the CPU's own cooling - pump and the three radiator fans
#   gpu  the card's lighting and the bottom intake that feeds it
#   ram  the DIMMs show their own figure
#   all  the general-airflow fans show overall system load
USAGE_SOURCES = {
    "pump": "cpu", "rad_l": "cpu", "rad_m": "cpu", "rad_r": "cpu",
    "gpu_text": "gpu", "gpu_logo": "gpu",
    "bot1": "gpu", "bot2": "gpu", "bot3": "gpu",
    "ram0": "ram", "ram1": "ram",
    "side1": "all", "side2": "all", "side3": "all", "rear": "all",
    "keyboard": "all",
}


def usage_source(el):
    """Resource key for an element: cpu, gpu, ram or all."""
    return USAGE_SOURCES.get(el["id"], "all")
