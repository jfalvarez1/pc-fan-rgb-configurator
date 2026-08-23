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
