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


def fx_matrix(nx, ny, t, palette, speed=0.5, cols=9.0, tail=0.34):
    """Digital rain: green columns falling, each with a white-hot head."""
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
IGNORES_PALETTE = {"matrix", "fire", "lightning"}


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
}


# ---------------------------------------------------------------------------
# VU meter. Bar count is tweakable at runtime - the UI writes VU_BARS.
# There is no audio capture here, so levels are synthesised: each bar gets its
# own bounce rate plus a shared "beat", which reads like music without
# pretending to be reactive.
# ---------------------------------------------------------------------------
VU_BARS = 8
VU_PEAKS = True          # draw the falling peak marker on top of each bar


def _vu_level(bar, bars, t):
    a = 0.55 + 0.45 * math.sin(t * (1.3 + bar * 0.37) + bar * 2.1)
    b = 0.30 * math.sin(t * 4.1 + bar)
    beat = 0.25 * max(0.0, math.sin(t * 3.0))          # shared pulse
    lvl = 0.30 + 0.55 * a + b * 0.25 + beat
    return max(0.05, min(1.0, lvl))


def fx_vu(nx, ny, t, palette, bars=None):
    """Classic VU meter: bars rising from the bottom, green -> amber -> red."""
    n = max(2, int(bars or VU_BARS))
    bar = min(n - 1, int(nx * n))
    lvl = _vu_level(bar, n, t)
    height = 1.0 - ny                      # 0 at the bottom, 1 at the top

    if VU_PEAKS:
        peak = max(lvl, _vu_level(bar, n, t - 0.35) * 0.96)
        if abs(height - peak) < 0.055 and peak > 0.08:
            return (255, 255, 255)

    if height > lvl:
        return (0, 0, 0)
    # colour by HEIGHT, the way a real meter is scaled
    if height < 0.55:
        g = 255
        r = int(255 * (height / 0.55) * 0.55)
        return (r, g, 30)
    if height < 0.80:
        f2 = (height - 0.55) / 0.25
        return (int(180 + 75 * f2), int(255 - 90 * f2), 0)
    f2 = (height - 0.80) / 0.20
    return (255, int(165 - 165 * f2), 0)


def fx_vu_palette(nx, ny, t, palette, bars=None):
    """Same meter, but coloured from the active palette instead of green-red."""
    n = max(2, int(bars or VU_BARS))
    bar = min(n - 1, int(nx * n))
    lvl = _vu_level(bar, n, t)
    height = 1.0 - ny
    if height > lvl:
        return (0, 0, 0)
    return gamma(cyclic_gradient(palette, height * 0.8 + bar / n * 0.2))


SPATIAL.update({"vu": fx_vu, "vu pal": fx_vu_palette})
EFFECT_GROUPS["Fill"] = ["concentric", "fill", "stack", "wipe", "bounce",
                         "starburst", "vu", "vu pal"]
