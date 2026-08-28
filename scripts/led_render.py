"""Anti-aliased rendering of the LED field.

    from led_render import LedRenderer
    r = LedRenderer(width, height)
    photo = r.render(leds)        # leds: records with x, y, rgb, el, i

Tk's Canvas has no anti-aliasing. `create_oval` writes hard pixels, so 207
small circles come out visibly stair-stepped - the single thing that made this
window look hand-drawn rather than designed. Everything is composited here
into a numpy buffer instead and handed to Tk as one image.

Two shapes:

* **Ring and strip LEDs** are a pre-rendered sprite: a bright core with a soft
  halo, built once at 4x and averaged down, so the edge is smooth and lit LEDs
  bleed into each other the way real diffused ones do.
* **Keyboard keys** are rounded rectangles at their true widths, also
  supersampled. A key mask is cached per size, so the spacebar is rendered
  once and reused.

Speed decides the design. Measured on this machine: compositing in float32 and
converting the whole 1130x1120 buffer to uint8 at the end costs 14.5 ms a
frame, because that conversion touches 3.8M values. Tinting each small sprite
to uint8 as it is placed touches a tenth of that, and the finished buffer can
then go to PIL through frombuffer with no copy at all: 4.2 ms to composite,
0.8 ms to wrap, 2.7 ms to upload. Against a 33 ms frame that leaves room to
spare.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageTk

import case_layout

SS = 4                      # supersampling factor for every mask
GLOW = 22                   # sprite half-size in pixels, including the halo


def _dot_mask(core, glow=GLOW):
    """Bright core fading into a soft halo, as a 0..1 alpha mask."""
    n = int(glow * 2)
    yy, xx = np.mgrid[0:n * SS, 0:n * SS]
    c = n * SS / 2
    d = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / SS
    body = np.clip(1.0 - (d - core * 0.55) / (core * 0.45), 0, 1) ** 0.9
    halo = np.exp(-(d / (glow * 0.42)) ** 2) * 0.55
    m = np.clip(body + halo * (1 - body), 0, 1)
    return m.reshape(n, SS, n, SS).mean(axis=(1, 3)).astype(np.float32)


def _ring_mask(core, glow=GLOW, width=2.0):
    """A thin annulus, for marking a selected LED."""
    n = int(glow * 2)
    yy, xx = np.mgrid[0:n * SS, 0:n * SS]
    c = n * SS / 2
    d = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / SS
    r = core + 3.0
    m = np.clip(1.0 - np.abs(d - r) / width, 0, 1)
    return m.reshape(n, SS, n, SS).mean(axis=(1, 3)).astype(np.float32)


def _key_ring_mask(w, h, radius=5, pad=7, width=2):
    W, H = int(w) + pad * 2, int(h) + pad * 2
    img = Image.new("L", (W * SS, H * SS), 0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [pad * SS, pad * SS, (pad + w) * SS - 1, (pad + h) * SS - 1],
        radius=radius * SS, outline=255, width=width * SS)
    return np.asarray(img.resize((W, H), Image.LANCZOS),
                      dtype=np.float32) / 255.0


def _key_mask(w, h, radius=5, pad=7):
    """Rounded-rectangle key face with a soft surround, as a 0..1 mask."""
    W, H = int(w) + pad * 2, int(h) + pad * 2
    img = Image.new("L", (W * SS, H * SS), 0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [pad * SS, pad * SS, (pad + w) * SS - 1, (pad + h) * SS - 1],
        radius=radius * SS, fill=255)
    small = img.resize((W, H), Image.LANCZOS)
    return (np.asarray(small, dtype=np.float32) / 255.0)


class LedRenderer:
    def __init__(self, width, height, master=None):
        # An explicit master matters: without one PhotoImage falls back to
        # Tk's implicit default root, which raises "no default root window"
        # the moment more than one Tk instance has existed in the process.
        self.master = master
        self.w, self.h = int(width), int(height)
        self._dots = {}
        self._keys = {}
        self._rings = {}
        self._keyrings = {}
        self._photo = None          # kept alive; Tk drops unreferenced images

    def _dot(self, core):
        key = round(core, 1)
        if key not in self._dots:
            self._dots[key] = _dot_mask(key)
        return self._dots[key]

    def _key(self, w, h):
        key = (int(w), int(h))
        if key not in self._keys:
            self._keys[key] = _key_mask(*key)
        return self._keys[key]

    def _ring(self, core):
        key = round(core, 1)
        if key not in self._rings:
            self._rings[key] = _ring_mask(key)
        return self._rings[key]

    def _keyring(self, w, h):
        key = (int(w), int(h))
        if key not in self._keyrings:
            self._keyrings[key] = _key_ring_mask(*key)
        return self._keyrings[key]

    def _blit(self, buf, mask, cx, cy, rgb):
        """Max-composite one tinted mask into the buffer."""
        mh, mw = mask.shape
        x0, y0 = int(cx) - mw // 2, int(cy) - mh // 2
        x1, y1 = x0 + mw, y0 + mh
        sx, sy = max(0, -x0), max(0, -y0)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w, x1), min(self.h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        sub = mask[sy:sy + (y1 - y0), sx:sx + (x1 - x0), None]
        tile = (sub * rgb).astype(np.uint8)
        region = buf[y0:y1, x0:x1]
        np.maximum(region, tile, out=region)

    def render(self, leds, selected=(), dim=(36, 40, 52),
               mark=(255, 255, 255)):
        """Composite every LED and return a PhotoImage.

        `dim` is the colour an unlit LED is drawn in - an unlit ring still has
        to be visible, or the case reads as a field of empty holes. `selected`
        is a set of (element id, index) marked with a ring.
        """
        sel = set(selected or ())
        markc = np.array(mark, dtype=np.float32)
        buf = np.zeros((self.h, self.w, 3), np.uint8)
        keygeo = {}
        for r in leds:
            el = r["el"]
            rgb = r.get("out", r["rgb"])
            lit = sum(rgb) >= 24
            colour = np.array(rgb if lit else dim, dtype=np.float32)
            if el.get("kind") == "grid":
                eid = el["id"]
                if eid not in keygeo:
                    keygeo[eid] = case_layout.key_geometry(el)
                g = keygeo[eid].get(r["i"])
                if g is None:
                    continue            # matrix slot with no key on it
                cx, cy, kw, kh = g
                self._blit(buf, self._key(kw - 3, kh - 3), cx, cy, colour)
                if (el["id"], r["i"]) in sel:
                    self._blit(buf, self._keyring(kw - 3, kh - 3), cx, cy,
                               markc)
            else:
                core = 8.5 if el["count"] <= 12 else 7.0
                self._blit(buf, self._dot(core), r["x"], r["y"], colour)
                if (el["id"], r["i"]) in sel:
                    self._blit(buf, self._ring(core), r["x"], r["y"], markc)
        img = Image.frombuffer("RGB", (self.w, self.h), buf, "raw", "RGB", 0, 1)
        self._photo = (ImageTk.PhotoImage(img, master=self.master)
                       if self.master is not None else ImageTk.PhotoImage(img))
        return self._photo
