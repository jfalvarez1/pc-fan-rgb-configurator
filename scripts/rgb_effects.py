"""Synthwave wave effect for OpenRGB devices.

Pure colour maths plus a renderer. Importable by thermal_rgb_loop.py, and
runnable on its own for tuning:

    python rgb_effects.py              # run the wave until Ctrl+C
    python rgb_effects.py --speed 0.3  # faster
    python rgb_effects.py --preview    # print the palette ramp, no hardware

DEVICE CONSTRAINTS on this machine:
  NZXT strip   56 LEDs, Direct mode  -> the real canvas, animated per-LED
  Motherboard   1 LED,  Direct mode  -> single point, samples the wave
  GPU           2 LEDs, STATIC ONLY  -> Static can write to flash, so it is
                                        updated slowly, never per-frame
  Keyboard     excluded - Razer Synapse owns it
"""
import argparse
import math
import time

# Classic synthwave triad: hot pink -> neon purple -> electric blue.
#
# The palettes are SYMMETRIC (…-> blue -> purple -> back to pink) on purpose.
# A bare 3-stop loop has to interpolate blue straight back to pink, and that
# line passes through the desaturated middle of the RGB cube - measured
# rgb(142,150,209), a muddy grey-lilac. Returning via purple keeps every
# frame fully saturated, which is the whole point of neon.
SYNTHWAVE = [
    (255, 45, 149),    # neon pink
    (176, 38, 255),    # neon purple
    (0, 229, 255),     # electric blue
    (176, 38, 255),    # neon purple (return leg)
]
SUNSET = [
    (255, 45, 149),    # neon pink
    (255, 122, 0),     # sunset orange
    (140, 40, 255),    # deep violet
    (255, 122, 0),     # sunset orange (return leg)
]

DEFAULT_PALETTE = SYNTHWAVE


def cyclic_gradient(palette, pos):
    """Colour at pos in [0,1) around a looping gradient.

    The palette wraps, so pos=0.999 blends back into palette[0] and the
    animation has no visible seam.
    """
    n = len(palette)
    pos = pos % 1.0
    scaled = pos * n
    i = int(scaled)
    f = scaled - i
    c0 = palette[i % n]
    c1 = palette[(i + 1) % n]
    return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))


def gamma(rgb, g=0.85):
    """Slight gamma lift - neon reads washed out on LEDs without it."""
    return tuple(min(255, round(255 * (c / 255) ** g)) for c in rgb)


class OutwardGlow:
    """Orange pulses radiating outward from the centre of each zone.

    Rendered PER ZONE rather than across the whole strip: the NZXT channels
    are physically separate runs (24 + 24 + 8 LEDs here), so one wavefront
    spanning all 56 would look like it was sliding sideways rather than
    blooming outward from each one.

    Two wavefronts run half a cycle apart so the bloom is continuous instead
    of pulsing to darkness between rings.
    """

    def __init__(self, colour=(255, 120, 0), core=(255, 205, 120),
                 speed=0.85, width=0.22, base=0.12):
        self.colour = colour     # the body of the glow
        self.core = core         # hotter centre of each wavefront
        self.speed = speed       # wavefronts per second
        self.width = width       # thickness of a wavefront
        self.base = base         # floor brightness so it never goes black

    def _intensity(self, d, t):
        """Brightness at normalised distance d (0 centre, 1 edge)."""
        total = 0.0
        for offset in (0.0, 0.5):
            phase = (t * self.speed + offset) % 1.0
            x = d - phase
            # wrap so a wavefront leaving the edge re-enters at the centre
            x = min(abs(x), abs(x + 1.0), abs(x - 1.0))
            total += math.exp(-(x / self.width) ** 2)
        return min(1.0, total)

    def render_zone(self, count, t):
        if count <= 0:
            return []
        if count == 1:
            i = self._intensity(0.0, t)
            return [self._mix(i)]
        out = []
        for k in range(count):
            p = k / (count - 1)
            d = abs(p - 0.5) * 2.0      # 0 at centre, 1 at both ends
            out.append(self._mix(self._intensity(d, t)))
        return out

    def _mix(self, i):
        """Blend base -> colour -> hot core as intensity rises."""
        level = self.base + (1.0 - self.base) * i
        if i > 0.6:
            f = (i - 0.6) / 0.4
            rgb = tuple(round(a + (b - a) * f)
                        for a, b in zip(self.colour, self.core))
        else:
            rgb = self.colour
        return tuple(max(0, min(255, round(c * level))) for c in rgb)

    def render_zones(self, zone_sizes, t):
        """Flat colour list for a device, each zone blooming from its centre."""
        out = []
        for n in zone_sizes:
            out.extend(self.render_zone(n, t))
        return out


class Wave:
    """A travelling gradient across an LED strip."""

    def __init__(self, palette=None, cycles=1.5, speed=0.12, shimmer=0.0):
        self.palette = palette or DEFAULT_PALETTE
        self.cycles = cycles      # palette repeats across the strip
        self.speed = speed        # cycles per second of travel
        self.shimmer = shimmer    # 0..1 subtle brightness breathing

    def render(self, count, t):
        """Return `count` colours for time `t` (seconds)."""
        if count <= 0:
            return []
        out = []
        phase = t * self.speed
        for i in range(count):
            pos = (i / count) * self.cycles + phase
            rgb = gamma(cyclic_gradient(self.palette, pos))
            if self.shimmer:
                b = 1.0 - self.shimmer * 0.5 * (
                    1 - math.cos(2 * math.pi * (pos * 0.5 + t * 0.07)))
                rgb = tuple(max(0, min(255, round(c * b))) for c in rgb)
            out.append(rgb)
        return out

    def sample(self, t, offset=0.0):
        """Single colour from the wave - for 1-LED devices."""
        return gamma(cyclic_gradient(self.palette, t * self.speed + offset))


def _preview(wave):
    steps = 24
    print("palette ramp:")
    for i in range(steps):
        r, g, b = gamma(cyclic_gradient(wave.palette, i / steps))
        print(f"  {i/steps:5.2f}  #{r:02x}{g:02x}{b:02x}  rgb({r:3d},{g:3d},{b:3d})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--speed", type=float, default=0.12)
    ap.add_argument("--cycles", type=float, default=1.5)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--shimmer", type=float, default=0.0)
    ap.add_argument("--sunset", action="store_true", help="use the sunset palette")
    ap.add_argument("--preview", action="store_true",
                    help="print the palette ramp and exit")
    args = ap.parse_args()

    wave = Wave(palette=SUNSET if args.sunset else SYNTHWAVE,
                cycles=args.cycles, speed=args.speed, shimmer=args.shimmer)

    if args.preview:
        _preview(wave)
        return 0

    from openrgb import OpenRGBClient
    from openrgb.utils import RGBColor

    client = OpenRGBClient("127.0.0.1", 6742, "synthwave")
    strips, points = [], []
    for dev in client.devices:
        if dev.type.name == "KEYBOARD":
            continue
        modes = [m.name.lower() for m in dev.modes]
        if "direct" not in modes:
            print(f"skipping {dev.name} (no Direct mode, static-only)")
            continue
        dev.set_mode("direct")
        (strips if len(dev.leds) > 4 else points).append(dev)
        print(f"driving {dev.name}: {len(dev.leds)} LEDs")

    if not strips and not points:
        print("nothing to drive")
        return 1

    period = 1.0 / args.fps
    t0 = time.monotonic()
    try:
        while True:
            t = time.monotonic() - t0
            for dev in strips:
                cols = wave.render(len(dev.leds), t)
                dev.set_colors([RGBColor(*c) for c in cols], fast=True)
            for dev in points:
                c = wave.sample(t)
                dev.set_colors([RGBColor(*c)] * len(dev.leds), fast=True)
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ===========================================================================
# SPATIAL EFFECTS
#
# These take a NORMALISED position in the case (nx, ny in 0..1) plus a time,
# and return a colour. That is the whole point: the old wave was indexed by
# LED number, so it scrambled at every fan boundary. Driving by physical
# position makes a wave actually sweep across the case.
#
# Every function has the signature f(nx, ny, t, palette) -> (r, g, b).
# ===========================================================================


def _wrap(v):
    return v - math.floor(v)


def fx_wave(nx, ny, t, palette, speed=0.12, angle=35.0, cycles=1.4):
    """Travelling gradient at an angle across the case."""
    a = math.radians(angle)
    proj = nx * math.cos(a) + ny * math.sin(a)
    return gamma(cyclic_gradient(palette, proj * cycles + t * speed))


def fx_radial(nx, ny, t, palette, speed=0.22, cycles=2.0):
    """Ripples expanding from the centre of the case."""
    d = math.hypot(nx - 0.5, ny - 0.5) * 1.6
    return gamma(cyclic_gradient(palette, d * cycles - t * speed))


def fx_spiral(nx, ny, t, palette, speed=0.18, arms=2.0):
    """Pinwheel spiralling about the case centre."""
    dx, dy = nx - 0.5, ny - 0.5
    ang = math.atan2(dy, dx) / math.tau
    d = math.hypot(dx, dy) * 1.6
    return gamma(cyclic_gradient(palette, ang * arms + d + t * speed))


def fx_comet(nx, ny, t, palette, speed=0.30, tail=0.22):
    """A bright head orbiting the case with a fading tail."""
    head = _wrap(t * speed)
    ang = _wrap(math.atan2(ny - 0.5, nx - 0.5) / math.tau)
    d = _wrap(head - ang)
    b = max(0.0, 1.0 - d / tail) ** 2
    base = cyclic_gradient(palette, head)
    return tuple(max(0, min(255, round(c * b))) for c in gamma(base))


def fx_rain(nx, ny, t, palette, speed=0.45, drops=7.0):
    """Streaks falling down the case."""
    col = math.floor(nx * drops)
    phase = _wrap(t * speed + col * 0.37)
    d = _wrap(ny - phase)
    b = max(0.0, 1.0 - d / 0.30) ** 2
    base = cyclic_gradient(palette, _wrap(col / drops + t * 0.05))
    return tuple(max(0, min(255, round(c * b))) for c in gamma(base))


def fx_plasma(nx, ny, t, palette, speed=0.16, scale=3.0):
    """Classic interfering sine plasma."""
    v = (math.sin(nx * scale + t * speed * 3)
         + math.sin(ny * scale * 1.3 - t * speed * 2)
         + math.sin((nx + ny) * scale * 0.8 + t * speed * 2.5))
    return gamma(cyclic_gradient(palette, _wrap(v / 6 + 0.5)))


def fx_breathe(nx, ny, t, palette, speed=0.10):
    """Whole case pulsing through the palette together."""
    b = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(t * speed * math.tau))
    base = cyclic_gradient(palette, _wrap(t * speed * 0.25))
    return tuple(max(0, min(255, round(c * b))) for c in gamma(base))


def fx_fire(nx, ny, t, palette, speed=0.55, scale=6.0):
    """Heat rising from the bottom of the case."""
    flick = (math.sin(nx * scale + t * speed * 4)
             + math.sin(nx * scale * 2.3 - t * speed * 3)) * 0.08
    h = max(0.0, min(1.0, (1.0 - ny) + flick))
    r = 255
    g = int(max(0, min(255, 240 * h ** 1.7)))
    b = int(max(0, min(255, 90 * max(0.0, h - 0.72) / 0.28)))
    lvl = 0.25 + 0.75 * h
    return (int(r * lvl), int(g * lvl), int(b * lvl))


SPATIAL = {
    "wave": fx_wave,
    "radial": fx_radial,
    "spiral": fx_spiral,
    "comet": fx_comet,
    "rain": fx_rain,
    "plasma": fx_plasma,
    "breathe": fx_breathe,
    "fire": fx_fire,
}


# ===========================================================================
# EXPANDED EFFECT LIBRARY
#
# Effect families follow the conventions the LED community has settled on
# (WLED's 180+ effect list is the de-facto reference): directional waves,
# scanner/Larson, theater chase, matrix rain, twinkle, confetti, juggle,
# meteor, wipe, lightning, aurora.
#
# Same signature throughout: f(nx, ny, t, palette) -> (r, g, b), driven by
# PHYSICAL position so everything sweeps the real case correctly.
#
# Randomness is hashed from position, never random() - each LED must produce
# the same value every frame or the effect flickers instead of animating.
# ===========================================================================


def _hash(x, y=0.0, z=0.0):
    """Deterministic 0..1 noise from coordinates (GLSL-style)."""
    n = math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453
    return n - math.floor(n)


def _dir_wave(nx, ny, t, palette, dx, dy, speed=0.16, cycles=1.3):
    proj = nx * dx + ny * dy
    return gamma(cyclic_gradient(palette, proj * cycles + t * speed))


def fx_wave_right(nx, ny, t, palette):
    """Sweeps left -> right."""
    return _dir_wave(nx, ny, t, palette, -1.0, 0.0)


def fx_wave_left(nx, ny, t, palette):
    """Sweeps right -> left."""
    return _dir_wave(nx, ny, t, palette, 1.0, 0.0)


def fx_wave_up(nx, ny, t, palette):
    """Sweeps bottom -> top."""
    return _dir_wave(nx, ny, t, palette, 0.0, 1.0)


def fx_wave_down(nx, ny, t, palette):
    """Sweeps top -> bottom."""
    return _dir_wave(nx, ny, t, palette, 0.0, -1.0)


# Matrix rain on a real grid. Tuned against the 15x5 keyboard by measuring
# what the look actually depends on: mean vertical run (is it a strand?), how
# many columns are lit at once (is it sparse?), and how long a strand takes to
# cross (is it frantic?). At tail 0.45 and 0.60 the 3.0-row floor dominated
# and both gave identical output, which is what showed the floor was doing the
# work rather than the parameter.
MATRIX_TAIL = 1.0            # tail length, as a multiple of the grid height
MATRIX_GAP = 1.4             # dark gap between strands, ditto
MATRIX_ROWS_PER_SEC = 6.0    # descent rate at speed=1.0


def _matrix_cell(cell, t, speed):
    """Digital rain snapped to a real matrix.

    The spatial version below divides the surface into 9 fixed columns and
    gives the tail a length measured as a FRACTION OF HEIGHT. On the keyboard
    that is 1.7 keys wide and, across only 5 rows, about 2 rows tall - so it
    filled large chunks instead of reading as a strand. Here the strand is
    exactly one column wide and the tail is measured in ROWS, which is what
    makes it look like the familiar falling glyph column.

    The cell comes from the LED's index on the grid, not from rounding its
    position, so it is exact by construction - and it stays exact inside an
    effect layer, where the box's local coordinates would not line up with
    key boundaries at all.
    """
    col, row, gc, gr = cell
    tail = max(3.0, gr * MATRIX_TAIL)
    period = gr + tail + gr * MATRIX_GAP
    rate = 0.6 + _hash(col, 2.3) * 0.9
    # Fall speed is in ROWS PER SECOND. Scaling a 0..1 phase by the period
    # instead would couple the two: widening the dark gap between strands
    # would also make them fall faster, which is not what a gap means.
    head = ((t * speed * MATRIX_ROWS_PER_SEC * rate)
            + _hash(col, 7.7) * period) % period
    d = head - row                      # rows behind the head
    if d < 0.0 or d >= tail:
        return (0, 0, 0)
    if d < 1.0:                         # the leading glyph
        return (200, 255, 210)
    f = 1.0 - (d - 1.0) / (tail - 1.0)
    g = int(255 * f ** 1.6)
    return (int(g * 0.15), g, int(g * 0.30))


def fx_matrix(nx, ny, t, palette, speed=0.5, cols=9.0, tail=0.34, cell=None):
    """Digital rain: green columns falling, each with a white-hot head.

    On a surface that is a real matrix (the keyboard) the strand snaps to the
    physical grid - one key wide, advancing a row at a time. Ring layouts have
    no rows or columns, so they keep the smooth spatial version; quantising a
    fan ring to an invented grid would only alias.
    """
    if cell:
        return _matrix_cell(cell, t, speed)
    col = math.floor(nx * cols)
    rate = 0.6 + _hash(col, 2.3) * 0.9
    head = _wrap(t * speed * rate + _hash(col, 7.7))
    d = _wrap(ny - head)
    if d > tail:
        return (0, 0, 0)
    f = 1.0 - d / tail
    if d < 0.035:                      # the leading glyph
        return (200, 255, 210)
    g = int(255 * f ** 1.6)
    return (int(g * 0.15), g, int(g * 0.30))


def fx_scanner(nx, ny, t, palette, speed=0.45, width=0.16):
    """Larson scanner - a bar sweeping side to side with a fading trail."""
    pos = 0.5 - 0.5 * math.cos(t * speed * math.tau)
    d = abs(nx - pos)
    b = max(0.0, 1.0 - d / width) ** 2
    base = cyclic_gradient(palette, _wrap(t * 0.05))
    return tuple(max(0, min(255, round(c * b))) for c in gamma(base))


def fx_theater(nx, ny, t, palette, speed=3.0, groups=14.0):
    """Theater chase - every third LED lit, marching along."""
    idx = math.floor(nx * groups + ny * 2.0)
    on = (idx - math.floor(t * speed)) % 3 == 0
    if not on:
        return (0, 0, 0)
    return gamma(cyclic_gradient(palette, _wrap(idx / groups * 0.5 + t * 0.05)))


def fx_twinkle(nx, ny, t, palette, speed=0.55, density=0.30):
    """Random LEDs fading up and down independently."""
    seed = _hash(nx * 97.0, ny * 61.0)
    if seed > density:
        return (0, 0, 0)
    phase = _wrap(t * speed * (0.5 + seed * 2.0) + seed * 9.7)
    b = math.sin(phase * math.pi) ** 2
    return tuple(max(0, min(255, round(c * b)))
                 for c in gamma(cyclic_gradient(palette, seed)))


def fx_confetti(nx, ny, t, palette, speed=1.6):
    """Coloured pops appearing at random and fading out."""
    seed = _hash(nx * 131.0, ny * 71.0)
    cycle = math.floor(t * speed + seed * 5.0)
    if _hash(seed * 33.0, cycle) > 0.22:
        return (0, 0, 0)
    f = 1.0 - _wrap(t * speed + seed * 5.0)
    return tuple(max(0, min(255, round(c * f * f)))
                 for c in gamma(cyclic_gradient(palette,
                                                _hash(cycle, seed * 17.0))))


def fx_juggle(nx, ny, t, palette, dots=5, width=0.11):
    """Several dots crossing the case at different speeds."""
    best, colour = 0.0, (0, 0, 0)
    for k in range(dots):
        speed = 0.20 + k * 0.075
        pos = 0.5 - 0.5 * math.cos(t * speed * math.tau + k)
        d = math.hypot(nx - pos, ny - (0.5 - 0.5 * math.sin(t * speed * 1.7 + k)))
        b = max(0.0, 1.0 - d / width) ** 2
        if b > best:
            best, colour = b, cyclic_gradient(palette, k / dots)
    return tuple(max(0, min(255, round(c * best))) for c in gamma(colour))


def fx_meteor(nx, ny, t, palette, speed=0.4, tail=0.30):
    """A bright head falling with a decaying trail."""
    head = _wrap(t * speed)
    d = _wrap(ny - head)
    if d > tail:
        return (0, 0, 0)
    f = (1.0 - d / tail) ** 1.8
    base = cyclic_gradient(palette, head)
    if d < 0.03:
        base = tuple(min(255, c + 90) for c in base)
    return tuple(max(0, min(255, round(c * f))) for c in gamma(base))


def fx_wipe(nx, ny, t, palette, speed=0.28):
    """A colour sweeping in and filling, then the next colour."""
    cycle = t * speed
    edge = _wrap(cycle)
    n = len(palette)
    i = int(cycle) % n
    cur, prev = palette[i], palette[(i - 1) % n]
    return gamma(cur if nx <= edge else prev)


def fx_lightning(nx, ny, t, palette, rate=1.1):
    """Occasional bright strikes over a dim base."""
    strike = math.floor(t * rate)
    if _hash(strike, 3.1) > 0.35:
        return (6, 8, 16)
    band = _hash(strike, 8.8)
    if abs(nx - band) > 0.22 + _hash(strike, 1.4) * 0.2:
        return (6, 8, 16)
    f = 1.0 - _wrap(t * rate)
    v = int(255 * f ** 3)
    return (v, v, min(255, int(v * 1.05)))


def fx_aurora(nx, ny, t, palette, speed=0.09):
    """Soft shifting curtains."""
    v = (math.sin(nx * 2.1 + t * speed * 2.0)
         + math.sin(ny * 1.7 - t * speed * 1.3)
         + math.sin((nx * 1.3 + ny * 0.9) * 2.4 + t * speed))
    b = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(ny * 3.0 + t * speed * 1.7))
    base = cyclic_gradient(palette, _wrap(v / 6 + 0.5))
    return tuple(max(0, min(255, round(c * b))) for c in gamma(base))


def fx_pulse(nx, ny, t, palette, speed=0.55):
    """Heartbeat - a double thump."""
    p = _wrap(t * speed)
    b = (math.exp(-((p - 0.10) ** 2) / 0.0016)
         + 0.65 * math.exp(-((p - 0.26) ** 2) / 0.0016))
    b = min(1.0, 0.10 + b)
    return tuple(max(0, min(255, round(c * b)))
                 for c in gamma(cyclic_gradient(palette, _wrap(t * 0.04))))


SPATIAL.update({
    "wave >": fx_wave_right,
    "wave <": fx_wave_left,
    "wave ^": fx_wave_up,
    "wave v": fx_wave_down,
    "matrix": fx_matrix,
    "scanner": fx_scanner,
    "theater": fx_theater,
    "twinkle": fx_twinkle,
    "confetti": fx_confetti,
    "juggle": fx_juggle,
    "meteor": fx_meteor,
    "wipe": fx_wipe,
    "lightning": fx_lightning,
    "aurora": fx_aurora,
    "pulse": fx_pulse,
})

# Order used by the UI: directional waves first, then the classics.
EFFECT_ORDER = [
    "wave", "wave >", "wave <", "wave ^", "wave v",
    "radial", "spiral", "plasma", "aurora",
    "matrix", "scanner", "theater", "meteor", "comet",
    "rain", "twinkle", "confetti", "juggle",
    "wipe", "lightning", "fire", "breathe", "pulse",
]


# Grouped for the UI. 23 flat buttons is a wall of options; five categories of
# four or five is scannable, and only one group is on screen at a time so the
# panel stays a fixed height however many effects exist.
EFFECT_GROUPS = {
    "Waves":   ["wave", "wave >", "wave <", "wave ^", "wave v"],
    "Flow":    ["radial", "spiral", "plasma", "aurora"],
    "Classic": ["matrix", "scanner", "theater", "meteor", "comet"],
    "Scatter": ["rain", "twinkle", "confetti", "juggle"],
    "Other":   ["wipe", "lightning", "fire", "breathe", "pulse"],
}


# Named palettes. All are SYMMETRIC where they wrap (…-> back through a middle
# stop) for the reason documented on SYNTHWAVE: a bare loop has to interpolate
# the last colour straight back to the first, and that line crosses the
# desaturated middle of the RGB cube.
PALETTES = {
    "synthwave": SYNTHWAVE,
    "sunset":    SUNSET,
    "ocean":     [(0, 90, 255), (0, 200, 220), (0, 255, 150), (0, 200, 220)],
    "fire":      [(255, 30, 0), (255, 140, 0), (255, 225, 90), (255, 140, 0)],
    "forest":    [(0, 190, 60), (140, 230, 40), (0, 120, 90), (140, 230, 40)],
    "ice":       [(120, 200, 255), (255, 255, 255), (0, 160, 255), (255, 255, 255)],
    "toxic":     [(160, 255, 0), (0, 255, 120), (220, 255, 0), (0, 255, 120)],
    "candy":     [(255, 60, 160), (255, 210, 90), (120, 200, 255), (255, 210, 90)],
    "mono red":  [(255, 0, 0), (120, 0, 0), (255, 60, 30), (120, 0, 0)],
    "mono blue": [(0, 80, 255), (0, 20, 120), (60, 180, 255), (0, 20, 120)],
    "white":     [(255, 255, 255), (170, 180, 200), (255, 255, 255), (200, 210, 230)],
    "rainbow":   [(255, 0, 0), (255, 200, 0), (0, 230, 60),
                  (0, 200, 255), (90, 60, 255), (255, 0, 180)],
}

# Per-effect palette overrides, so each effect can carry its own look.
# Effects that are intrinsically coloured (matrix green, fire, lightning)
# ignore the palette by design - noted here rather than hidden.
IGNORES_PALETTE = {"matrix", "fire", "lightning", "usage"}


# ---- resource usage gradient ---------------------------------------------
#
# Blue when a component is doing nothing, green when it is working lightly,
# red when it is loaded. Each run of LEDs reports a different resource - see
# case_layout.USAGE_SOURCES - so the case reads as a dashboard rather than as
# one number smeared over everything.
#
# The stops are not evenly spaced on purpose. Load spends most of its life
# under 50%, so an even ramp would leave the case green almost always and
# waste the whole top half of the scale. Green is reached early and the warm
# half is stretched across the range that actually distinguishes "busy" from
# "pinned".
USAGE_STOPS = [
    (0.00, (0, 60, 255)),      # idle - blue
    (0.10, (0, 255, 120)),     # ticking over - green
    (0.45, (215, 255, 0)),     # working - yellow-green
    (0.75, (255, 140, 0)),     # busy - orange
    (1.00, (255, 0, 0)),       # pinned - red
]


def usage_colour(u):
    """Colour for a 0..1 usage level, interpolated between USAGE_STOPS."""
    u = 0.0 if u is None else max(0.0, min(1.0, float(u)))
    for (t0, c0), (t1, c1) in zip(USAGE_STOPS, USAGE_STOPS[1:]):
        if u <= t1:
            span = t1 - t0
            f = 0.0 if span <= 0 else (u - t0) / span
            return tuple(int(round(a + (b - a) * f)) for a, b in zip(c0, c1))
    return USAGE_STOPS[-1][1]


def fx_usage(nx, ny, t, palette, usage=None):
    """Solid colour per run, showing that component's load.

    Flat rather than animated: this is an instrument. A gradient across each
    fan would look livelier and make two fans at 40% and 55% impossible to
    tell apart, which is the entire point of the effect.
    """
    return usage_colour(usage)


def fx_usage_bar(nx, ny, t, palette, usage=None):
    """Same colours, but each run also FILLS in proportion to its load, so it
    can be read at a glance from across the room rather than by judging hue."""
    u = 0.0 if usage is None else max(0.0, min(1.0, float(usage)))
    col = usage_colour(u)
    lit = (1.0 - ny - _FX_LO) / _FX_SPAN
    lit = max(0.0, min(1.0, lit))
    if lit > u:
        return tuple(int(c * 0.10) for c in col)     # unfilled: a dim hint
    return col

# Effects that render better when told the LED's physical grid cell. The
# renderer passes cell=(col, row, cols, rows) for LEDs on a matrix element and
# omits it everywhere else, so ring layouts are untouched.
CELL_AWARE = {"matrix"}

# Effects that are given their element's resource level by the renderer.
USAGE_AWARE = {"usage", "usage bar"}


# ===========================================================================
# EFFECT LIBRARY - BATCH 2
# Stacking, chasers, concentric fills and the other staples commercial RGB
# suites ship. Same signature and the same position-hashed randomness rule.
# ===========================================================================


def fx_stack(nx, ny, t, palette, speed=0.30, layers=8.0):
    """Blocks fall and stack up from the bottom, then the pile clears."""
    cycle = _wrap(t * speed / 3.0)
    filled = math.floor(cycle * (layers + 1))          # how many are settled
    band = math.floor((1.0 - ny) * layers)
    if band < filled:
        return gamma(cyclic_gradient(palette, band / layers))
    if band == filled:                                  # the block in flight
        drop = _wrap(cycle * (layers + 1))
        y_of_band = 1.0 - (band + 1) / layers
        if ny <= y_of_band + drop * (1.0 - y_of_band) + 0.02:
            return gamma(cyclic_gradient(palette, band / layers))
    return (0, 0, 0)


def fx_chaser(nx, ny, t, palette, speed=0.35, width=0.10, dots=3):
    """Dots chasing each other around the perimeter of the case."""
    ang = _wrap(math.atan2(ny - 0.5, nx - 0.5) / math.tau)
    best, colour = 0.0, (0, 0, 0)
    for k in range(dots):
        head = _wrap(t * speed + k / dots)
        d = min(abs(ang - head), 1 - abs(ang - head))
        b = max(0.0, 1.0 - d / width) ** 2
        if b > best:
            best, colour = b, cyclic_gradient(palette, k / dots)
    return tuple(max(0, min(255, round(c * best))) for c in gamma(colour))


def fx_concentric(nx, ny, t, palette, speed=0.28):
    """Everything lights from the centre outward, then unlights the same way."""
    d = math.hypot(nx - 0.5, ny - 0.5) / 0.72
    p = _wrap(t * speed)
    if p < 0.5:
        on = d <= p * 2.0                     # filling outward
    else:
        on = d > (p - 0.5) * 2.0              # clearing outward
    if not on:
        return (0, 0, 0)
    return gamma(cyclic_gradient(palette, d * 0.7 + t * 0.05))


def fx_sweep_fill(nx, ny, t, palette, speed=0.26):
    """Fills left to right, then empties left to right."""
    p = _wrap(t * speed)
    on = nx <= p * 2.0 if p < 0.5 else nx > (p - 0.5) * 2.0
    return gamma(cyclic_gradient(palette, nx * 0.6 + t * 0.05)) if on else (0, 0, 0)


def fx_bounce(nx, ny, t, palette, speed=0.40, width=0.13):
    """A block bouncing off the walls, leaving a soft trail."""
    p = abs(_wrap(t * speed) * 2.0 - 1.0)
    d = abs(nx - p)
    b = max(0.0, 1.0 - d / width) ** 1.6
    return tuple(max(0, min(255, round(c * b)))
                 for c in gamma(cyclic_gradient(palette, p)))


def fx_strobe(nx, ny, t, palette, rate=2.2):
    """Hard on/off flashes."""
    n = math.floor(t * rate)
    if _wrap(t * rate) > 0.18:
        return (0, 0, 0)
    return gamma(cyclic_gradient(palette, _hash(n, 5.5)))


def fx_ripple(nx, ny, t, palette, speed=0.30, drops=3):
    """Several expanding rings from fixed points, like rain on water."""
    total = 0.0
    colour = (0, 0, 0)
    for k in range(drops):
        cx, cy = _hash(k, 1.7), _hash(k, 9.2)
        phase = _wrap(t * speed + k / drops)
        d = math.hypot(nx - cx, ny - cy)
        ring = abs(d - phase * 0.9)
        b = max(0.0, 1.0 - ring / 0.09) ** 2 * (1.0 - phase)
        if b > total:
            total, colour = b, cyclic_gradient(palette, k / drops + t * 0.05)
    return tuple(max(0, min(255, round(c * total))) for c in gamma(colour))


def fx_spectrum(nx, ny, t, palette, speed=0.10):
    """Whole case cycling through the palette in unison."""
    return gamma(cyclic_gradient(palette, _wrap(t * speed)))


def fx_gradient_shift(nx, ny, t, palette, speed=0.12):
    """A static-looking corner-to-corner gradient that slowly drifts."""
    return gamma(cyclic_gradient(palette, (nx * 0.6 + ny * 0.4) + t * speed))


def fx_starburst(nx, ny, t, palette, speed=0.45, arms=6):
    """Spokes flashing outward from the centre."""
    ang = math.atan2(ny - 0.5, nx - 0.5) / math.tau
    d = math.hypot(nx - 0.5, ny - 0.5) / 0.72
    spoke = _wrap(ang * arms)
    near = min(spoke, 1 - spoke)
    if near > 0.16:
        return (0, 0, 0)
    phase = _wrap(t * speed)
    b = max(0.0, 1.0 - abs(d - phase) / 0.28) ** 2
    return tuple(max(0, min(255, round(c * b)))
                 for c in gamma(cyclic_gradient(palette, _wrap(ang + t * 0.06))))


def fx_snake(nx, ny, t, palette, speed=0.22, length=0.30):
    """A long body winding around the case."""
    ang = _wrap(math.atan2(ny - 0.5, nx - 0.5) / math.tau)
    head = _wrap(t * speed)
    d = _wrap(head - ang)
    if d > length:
        return (0, 0, 0)
    f = 1.0 - d / length
    return tuple(max(0, min(255, round(c * f)))
                 for c in gamma(cyclic_gradient(palette, _wrap(ang + t * 0.05))))


def fx_breathe_half(nx, ny, t, palette, speed=0.14):
    """Two halves of the case breathing in opposition."""
    p = _wrap(t * speed)
    left = 0.5 - 0.5 * math.cos(p * math.tau)
    b = left if nx < 0.5 else 1.0 - left
    b = 0.12 + 0.88 * b
    return tuple(max(0, min(255, round(c * b)))
                 for c in gamma(cyclic_gradient(palette, 0.0 if nx < 0.5 else 0.5)))


SPATIAL.update({
    "stack": fx_stack,
    "chaser": fx_chaser,
    "concentric": fx_concentric,
    "fill": fx_sweep_fill,
    "bounce": fx_bounce,
    "strobe": fx_strobe,
    "ripple": fx_ripple,
    "spectrum": fx_spectrum,
    "gradient": fx_gradient_shift,
    "starburst": fx_starburst,
    "snake": fx_snake,
    "split": fx_breathe_half,
})

EFFECT_GROUPS = {
    "Waves":   ["wave", "wave >", "wave <", "wave ^", "wave v", "gradient"],
    "Flow":    ["radial", "spiral", "plasma", "aurora", "ripple", "snake"],
    "Classic": ["matrix", "scanner", "theater", "meteor", "comet", "chaser"],
    "Fill":    ["concentric", "fill", "stack", "wipe", "bounce", "starburst"],
    "Scatter": ["rain", "twinkle", "confetti", "juggle", "strobe", "lightning"],
    "Glow":    ["breathe", "pulse", "split", "spectrum", "fire"],
    # Not decoration: these read out live load, so they get their own group
    # rather than hiding among the animations.
    "System":  ["usage", "usage bar"],
}


# ---------------------------------------------------------------------------
# VU meter. Bar count is tweakable at runtime - the UI writes VU_BARS.
# There is no audio capture here, so levels are synthesised: each bar gets its
# own bounce rate plus a shared "beat", which reads like music without
# pretending to be reactive.
# ---------------------------------------------------------------------------
VU_BARS = 8
VU_PEAKS = True          # draw the falling peak marker on top of each bar

# A meter is a stack of discrete segments, not a continuous fill. That matters
# here: the lowest LED row sits at height ~0.04 in effect space, so a
# continuous "lit if height <= level" test lights it for ANY level above 4% -
# which is why the bottom fans and the bottom keyboard row never went out.
# Quantising into VU_ROWS segments means the bottom row needs a level of
# 1/VU_ROWS before it lights, so it modulates like every other row.
VU_ROWS = 10
VU_LO = 0.10             # level the BOTTOM row must clear (stops the fill)
VU_HI = 0.95             # level the TOP row must clear (kept reachable)

# With continuous audio a bottom segment is legitimately lit most of the time -
# that is how a real meter behaves, and no threshold makes it blink without
# also making the meter twitchy. So a lit segment is also DIMMED by the current
# level: the bottom fans and the bottom keyboard row breathe with the music
# instead of sitting at a constant green.
VU_DIM = 0.22            # brightness of a lit segment at the lowest level
VU_GAIN = 1.0            # user sensitivity; also pushed into the capture
VU_PEAK_FALL = 0.55      # peak marker fall, in bar-heights per second

# Effect space is inset by 4% at each end (see case_layout.led_positions), so
# raw height runs 0.04..0.96 rather than 0..1. Undo that before quantising, or
# the top row could never reach full and the bottom could never reach zero.
_FX_LO, _FX_SPAN = 0.04, 0.92

# Real audio, when it is available. audio_levels taps the speaker's WASAPI
# loopback and FFTs it into logarithmic bands. If capture is unavailable the
# meters fall back to synthesised motion - and the UI says which is in use,
# because a meter that bounces to nothing is worse than no meter.
try:
    from audio_levels import SHARED as AUDIO
except Exception:
    AUDIO = None


def audio_ready():
    return bool(AUDIO and AUDIO.available)


def audio_active():
    return bool(AUDIO and AUDIO.active)


def _vu_level(bar, bars, t):
    a = 0.55 + 0.45 * math.sin(t * (1.3 + bar * 0.37) + bar * 2.1)
    b = 0.30 * math.sin(t * 4.1 + bar)
    beat = 0.25 * max(0.0, math.sin(t * 3.0))          # shared pulse
    lvl = 0.30 + 0.55 * a + b * 0.25 + beat
    return max(0.05, min(1.0, lvl))


# One frame's worth of levels, computed once and reused for every LED in that
# frame. fx_vu is called per LED - 207 times a frame here - and recomputing the
# band fold each time was both wasteful and made a real peak-hold impossible,
# since there was no single point at which a frame advanced.
_FRAME = {"t": None, "n": None, "lv": None, "peak": []}


def _frame(n, t):
    """(levels, peaks) for this frame, computing at most once per frame."""
    f = _FRAME
    if f["t"] == t and f["n"] == n and f["lv"] is not None:
        return f["lv"], f["peak"]

    lv = None
    if AUDIO is not None:
        got = AUDIO.levels(n)
        if got:
            lv = got                      # capture applies VU_GAIN itself
    if lv is None:
        # synthesised fallback: apply gain here, or it would be ignored
        lv = [_vu_level(b, n, t) for b in range(n)]
        if VU_GAIN != 1.0:
            lv = [max(0.0, min(1.0, v * VU_GAIN)) for v in lv]

    # falling peak hold, decaying in real time rather than per frame
    prev_t, peak = f["t"], f["peak"]
    if len(peak) != n or prev_t is None or t < prev_t:
        peak = list(lv)
    else:
        drop = VU_PEAK_FALL * max(0.0, t - prev_t)
        peak = [max(lv[i], peak[i] - drop) for i in range(n)]

    f.update(t=t, n=n, lv=lv, peak=peak)
    return lv, peak


def _cell(nx, ny, n):
    """(bar index, row index, row height 0..1) for an LED in effect space."""
    bar = min(n - 1, max(0, int(nx * n)))
    h = (1.0 - ny - _FX_LO) / _FX_SPAN          # undo the effect-space inset
    h = max(0.0, min(1.0, h))
    row = min(VU_ROWS - 1, int(h * VU_ROWS))
    return bar, row, (row + 0.5) / VU_ROWS


def _row_threshold(row):
    """Level a segment must clear to light.

    Spread across VU_LO..VU_HI rather than the textbook (row+1)/VU_ROWS: that
    formula makes the bottom row need 1/VU_ROWS (good, it is what stops the
    permanent fill) but the TOP row need exactly 1.0, which real audio
    essentially never reaches, so the top segment would never light.
    """
    if VU_ROWS < 2:
        return VU_LO
    return VU_LO + (VU_HI - VU_LO) * (row / (VU_ROWS - 1))


def _lit(lvl, row):
    return lvl >= _row_threshold(row)


def _dim(col, lvl):
    """Scale a lit segment by the current level, so every row - the bottom
    ones included - keeps moving even while it stays lit."""
    b = VU_DIM + (1.0 - VU_DIM) * max(0.0, min(1.0, lvl))
    return (int(col[0] * b), int(col[1] * b), int(col[2] * b))


def _top_row(lvl):
    """Highest row this level lights, or -1 if it clears none."""
    if lvl < VU_LO:
        return -1
    if VU_ROWS < 2 or VU_HI <= VU_LO:
        return 0
    r = int((lvl - VU_LO) * (VU_ROWS - 1) / (VU_HI - VU_LO))
    return min(VU_ROWS - 1, r)


def fx_vu(nx, ny, t, palette, bars=None):
    """Classic VU meter: bars rising from the bottom, green -> amber -> red.

    Driven by real loopback audio when available.
    """
    n = max(2, int(bars or VU_BARS))
    lv, peaks = _frame(n, t)
    bar, row, height = _cell(nx, ny, n)
    lvl = lv[bar]

    if VU_PEAKS and not _lit(lvl, row) and _top_row(peaks[bar]) == row:
        return (255, 255, 255)      # falling peak marker, above the bar

    if not _lit(lvl, row):
        return (0, 0, 0)
    # colour by HEIGHT, the way a real meter is scaled
    if height < 0.55:
        g = 255
        r = int(255 * (height / 0.55) * 0.55)
        col = (r, g, 30)
    elif height < 0.80:
        f2 = (height - 0.55) / 0.25
        col = (int(180 + 75 * f2), int(255 - 90 * f2), 0)
    else:
        f2 = (height - 0.80) / 0.20
        col = (255, int(165 - 165 * f2), 0)
    return _dim(col, lvl)


def fx_vu_palette(nx, ny, t, palette, bars=None):
    """Same meter, but coloured from the active palette instead of green-red."""
    n = max(2, int(bars or VU_BARS))
    lv, _ = _frame(n, t)
    bar, row, height = _cell(nx, ny, n)
    if not _lit(lv[bar], row):
        return (0, 0, 0)
    col = gamma(cyclic_gradient(palette, height * 0.8 + bar / n * 0.2))
    return _dim(col, lv[bar])


def set_vu_gain(g):
    """Sensitivity, roughly 0.3 (calm) .. 3.0 (hot). Applied to the capture
    when it is running, and to the synthesised fallback either way."""
    global VU_GAIN
    VU_GAIN = max(0.1, min(4.0, float(g)))
    if AUDIO is not None:
        try:
            AUDIO.set_gain(VU_GAIN)
        except Exception:
            pass
    return VU_GAIN


SPATIAL.update({"vu": fx_vu, "vu pal": fx_vu_palette,
                "usage": fx_usage, "usage bar": fx_usage_bar})
EFFECT_GROUPS["Fill"] = ["concentric", "fill", "stack", "wipe", "bounce",
                         "starburst", "vu", "vu pal"]
