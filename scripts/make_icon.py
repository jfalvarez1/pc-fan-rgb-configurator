"""Generate the LED Studio icon.

    python make_icon.py

Design: a ring of individually-lit LEDs around a dark fan hub, in the app's
own synthwave palette. It deliberately echoes what the app actually draws -
rings of addressable LEDs - rather than imitating anyone's branding.

Renders at 8x and downsamples, so the small sizes stay clean. Writes a
multi-resolution .ico (16-256) plus a PNG for the README.
"""
import math
import pathlib

from PIL import Image, ImageDraw

BASE = pathlib.Path(__file__).resolve().parent
OUT_ICO = BASE.parent / "led_studio.ico"
OUT_PNG = BASE.parent / "led_studio.png"

SS = 8                      # supersample factor
SIZE = 256
BG = (13, 15, 20, 255)      # matches the app background

# same palette the effects use: pink -> purple -> blue -> purple
PALETTE = [(255, 45, 149), (176, 38, 255), (0, 229, 255), (176, 38, 255)]


def cyc(pos):
    n = len(PALETTE)
    pos %= 1.0
    s = pos * n
    i = int(s)
    f = s - i
    a, b = PALETTE[i % n], PALETTE[(i + 1) % n]
    return tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def glow(draw, cx, cy, r, colour, layers=4):
    """A restrained halo. The first attempt stacked five wide layers and the
    alpha summed to near-white, washing the whole icon out - keep it tight
    and faint so the LED itself stays the brightest thing."""
    # Halo uses a DIMMED colour. At full brightness the halos of adjacent
    # LEDs overlapped and summed toward white - the pink ones read as white.
    halo = tuple(int(c * 0.55) for c in colour)
    for k in range(layers, 0, -1):
        alpha = int(22 * (k / layers) ** 2)
        rr = r * (1 + k * 0.13)
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                     fill=halo + (alpha,))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour + (255,))


def build(size):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    # rounded dark tile
    pad = S * 0.02
    d.rounded_rectangle([pad, pad, S - pad, S - pad],
                        radius=S * 0.22, fill=BG)

    cx = cy = S / 2
    ring_r = S * 0.310
    led_r = S * 0.062

    # faint ring track, so the LEDs read as mounted on something
    d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
              outline=(38, 46, 60, 255), width=max(1, int(S * 0.010)))

    # the LEDs
    n = 10
    for i in range(n):
        a = (i / n) * math.tau - math.pi / 2
        colour = cyc(i / n)
        glow(d, cx + math.cos(a) * ring_r, cy + math.sin(a) * ring_r,
             led_r, colour)

    # dark hub with a thin lit rim - reads as a fan centre
    hub = S * 0.155
    d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub],
              fill=(18, 21, 28, 255), outline=(60, 70, 90, 255),
              width=max(1, int(S * 0.008)))

    # three blade hints, angled so it reads as a fan rather than a dial
    for i in range(3):
        a = math.radians(i * 120 - 30)
        x1 = cx + math.cos(a) * hub * 0.35
        y1 = cy + math.sin(a) * hub * 0.35
        x2 = cx + math.cos(a + 0.5) * (ring_r - led_r * 2.1)
        y2 = cy + math.sin(a + 0.5) * (ring_r - led_r * 2.1)
        d.line([x1, y1, x2, y2], fill=(48, 57, 74, 255),
               width=max(1, int(S * 0.016)))

    # centre pip picks up the palette
    pip = S * 0.045
    d.ellipse([cx - pip, cy - pip, cx + pip, cy + pip], fill=(0, 229, 255, 255))

    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [build(s) for s in sizes]
    frames[-1].save(OUT_PNG)
    frames[-1].save(OUT_ICO, format="ICO",
                    sizes=[(s, s) for s in sizes])
    print(f"wrote {OUT_ICO}  ({', '.join(str(s) for s in sizes)})")
    print(f"wrote {OUT_PNG}")


if __name__ == "__main__":
    main()
