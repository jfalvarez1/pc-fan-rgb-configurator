"""LED Studio (native) - per-LED case editor with live animated effects.

    python led_studio_native.py

A standalone Tk application. No web server, no browser, no HTTP: it talks to
OpenRGB directly. Built after the browser version proved awkward to debug -
this removes the HTTP layer, caching, and the browser entirely.

    click an LED            select that LED
    click a fan centre      select the whole fan
    shift-click             add to the selection
    drag on empty space     rubber-band select
    Brush mode + drag       paint LEDs directly

Effects animate in the canvas immediately; the hardware is only written when
"Drive hardware" is ticked. Takes manual_override.flag while it has control
so the daemon stands down, and releases on exit.
"""
import ctypes
import pathlib
import queue
import threading
import time
import tkinter as tk
from tkinter import colorchooser, ttk

import case_layout
import openrgb_boot
import rgb_effects as fx

# Windows groups taskbar buttons by AppUserModelID. A script launched through
# pythonw.exe inherits PYTHON'S identity, so the taskbar shows the Python icon
# even when the window and the shortcut both carry ours. Declaring our own ID
# - before any window exists - makes Windows treat this as its own app and use
# the window icon on the taskbar.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "HardwareControl.LEDStudio")
except Exception:
    pass

BASE = pathlib.Path(__file__).resolve().parent
OVERRIDE = BASE / "manual_override.flag"
HOST, PORT = "127.0.0.1", 6742

SCALE = 1.0
W = int(case_layout.CANVAS_W * SCALE)
H = int(case_layout.CANVAS_H * SCALE)
HW_FPS = 20.0            # hardware write rate; the canvas runs faster
UI_MS = 33               # ~30 fps canvas


# ---- palette -------------------------------------------------------------
BG      = "#0d0f14"
PANEL   = "#151922"
CARD    = "#1c222d"
LINE    = "#2a3140"
INK     = "#e6e9ef"
MUTED   = "#7d8697"
ACCENT  = "#ff3aa2"
BTN     = "#232b39"
BTN_HOV = "#2f3949"

# An unlit LED must still be visible against the background, or the layout
# reads as a field of empty holes.
LED_OFF      = "#242c3a"
LED_OFF_EDGE = "#39445a"

FONT   = ("Segoe UI", 11)
FONT_H = ("Segoe UI", 9, "bold")
FONT_L = ("Segoe UI", 10)


def mkbtn(parent, text, cmd, kind="normal"):
    """Flat button. Tk's default 3D relief looks like Windows XP."""
    bg = {"normal": BTN, "accent": ACCENT, "ghost": PANEL}[kind]
    b = tk.Label(parent, text=text, bg=bg,
                 fg="#ffffff" if kind == "accent" else INK,
                 font=FONT, padx=12, pady=9, cursor="hand2",
                 highlightthickness=1,
                 highlightbackground=ACCENT if kind == "accent" else LINE)
    b._bg = bg
    hov = "#ff5cb4" if kind == "accent" else BTN_HOV
    b.bind("<Enter>", lambda e: b.config(bg=hov))
    b.bind("<Leave>", lambda e: b.config(bg=b._bg))
    b.bind("<Button-1>", lambda e: cmd())
    return b


def setbtn(b, active):
    b._bg = ACCENT if active else BTN
    b.config(bg=b._bg, fg="#ffffff" if active else INK,
             highlightbackground=ACCENT if active else LINE)

SWATCHES = ["#ff3aa2", "#ba33ff", "#00e9ff", "#00ff88",
            "#ffd400", "#ff5a00", "#ffffff", "#000000"]


class Hardware(threading.Thread):
    """Owns the OpenRGB connection. Frames are posted; only the newest is
    written, so a slow device can never build a backlog."""

    def __init__(self, out):
        super().__init__(daemon=True)
        self.out = out
        self.pending = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.client = None
        self.resolved = {}
        self.buf = {}

    def connect(self):
        try:
            openrgb_boot.ensure_running(quiet=True)
            from openrgb import OpenRGBClient
            c = OpenRGBClient(HOST, PORT, "led-studio-native")
            sizes = {}
            try:
                import json
                sizes = json.loads((BASE / "rgb_zone_sizes.json").read_text())
            except Exception:
                pass
            for d in c.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                for z in d.zones:
                    want = sizes.get(f"{d.name}|{z.name}")
                    if want and len(z.leds) != want and "NZXT" not in d.name:
                        try:
                            z.resize(want)
                        except Exception:
                            pass
            c.update()
            for d in c.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                for m in ("direct", "custom", "static"):
                    try:
                        d.set_mode(m)
                        break
                    except Exception:
                        continue
                self.buf[d.id] = [(0, 0, 0)] * len(d.leds)
            self.client = c
            self.resolved = {}
            n = sum(1 for d in c.devices if d is not None)
            self.out.put(("log", f"OpenRGB connected: {n} devices"))
            return True
        except Exception as exc:
            self.client = None
            self.out.put(("log", f"OpenRGB unavailable: {type(exc).__name__}: {exc}"))
            return False

    def resolve(self, el):
        hit = self.resolved.get(el["id"])
        if hit is not None:
            return hit
        if self.client is None:
            return (None, None)
        m = [d for d in self.client.devices
             if d is not None and getattr(d, "type", None) is not None
             and el["device"].lower() in d.name.lower()]
        if not m:
            self.resolved[el["id"]] = (None, None)
            return (None, None)
        dev = m[min(el.get("dev_index", 0), len(m) - 1)]
        off = 0
        for z in dev.zones:
            if el["zone"].lower() in z.name.lower():
                self.resolved[el["id"]] = (dev, off + el["start"])
                return self.resolved[el["id"]]
            off += len(z.leds)
        self.resolved[el["id"]] = (None, None)
        return (None, None)

    def post(self, frame):
        """frame: {element_id: [(r,g,b), ...]}. Newest wins."""
        with self.lock:
            self.pending = frame

    def run(self):
        self.connect()
        period = 1.0 / HW_FPS
        while not self.stop_flag.is_set():
            with self.lock:
                frame, self.pending = self.pending, None
            if frame is None:
                time.sleep(0.01)
                continue
            if self.client is None and not self.connect():
                time.sleep(2.0)
                continue
            try:
                from openrgb.utils import RGBColor
                touched = {}
                for el in case_layout.LAYOUT:
                    vals = frame.get(el["id"])
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
                            buf[off + i] = c
                    touched[dev.id] = dev
                for dev_id, dev in touched.items():
                    dev.set_colors([RGBColor(*c) for c in self.buf[dev_id]],
                                   fast=True)
            except Exception as exc:
                self.out.put(("log", f"write failed: {type(exc).__name__}: {exc}"))
                self.client = None
            time.sleep(period)


class App:
    def __init__(self, root):
        self.root = root
        root.title("LED Studio")
        root.configure(bg=BG)
        ico = BASE.parent / "led_studio.ico"
        try:
            # default=True also applies it to future toplevels
            root.iconbitmap(default=str(ico))
            root.iconbitmap(str(ico))
        except Exception:
            pass    # icon is cosmetic; never let it stop the app starting

        self.out = queue.Queue()
        self.hw = Hardware(self.out)
        self.leds, self.byel = [], {}
        self.sel, self.order = set(), []
        self.effect, self.fxbtns = None, {}
        self.t0 = time.monotonic()
        self.frames = 0
        self.brush = False
        self.drag = self.marq = None
        self.controlling = False
        self.colour = "#ff3aa2"
        # palette per effect, so each remembers its own look
        self.palettes = {}
        self.palette_name = "synthwave"
        self.custom = list(fx.SYNTHWAVE)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.cv = tk.Canvas(wrap, width=W, height=H, bg=BG,
                            highlightthickness=0)
        self.cv.pack(side="left", fill="both", expand=True,
                     padx=(10, 6), pady=10)
        side = tk.Frame(wrap, bg=PANEL, width=360)
        side.pack(side="right", fill="y", padx=(0, 10), pady=10)
        side.pack_propagate(False)

        self._build_case()
        self._build_panel(side)

        self.status = tk.Label(root, text="starting...", anchor="w",
                               bg=BG, fg=MUTED, font=FONT)
        self.status.pack(fill="x", padx=14, pady=(0, 10))

        self.cv.bind("<Button-1>", self.on_down)
        self.cv.bind("<B1-Motion>", self.on_move)
        self.cv.bind("<ButtonRelease-1>", self.on_up)

        # Size to content. Previously the window kept its default size and the
        # panel simply overflowed off the bottom, needing a manual resize.
        root.update_idletasks()
        want_w = W + 360 + 40
        want_h = max(H + 60, side.winfo_reqheight() + 60)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight() - 60
        w, h = min(want_w, sw - 40), min(want_h, sh)
        root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+20")
        root.minsize(900, 620)

        self.pal_strip.bind("<Configure>", lambda e: self.draw_palette())
        root.after(200, self.draw_palette)

        self.hw.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(UI_MS, self.tick)

    # ---------- canvas

    def _build_case(self):
        c = self.cv
        c.create_rectangle(30, 30, W - 30, H - 30, outline=LINE, width=2)
        c.create_line(262, 30, 262, H - 30, fill="#212736", dash=(6, 6))
        c.create_rectangle(345, 560, 808, 650, outline=LINE, fill=CARD)
        c.create_text(372, 636, text="RTX 5090", fill=MUTED, font=FONT_L,
                      anchor="w")
        c.create_text(594, 52, text="TOP - radiator exhaust",
                      fill="#5c6577", font=FONT_L)
        c.create_text(552, H - 52, text="BOTTOM - F420 intake",
                      fill="#5c6577", font=FONT_L)
        c.create_text(W - 52, 435, text="SIDE - F360 intake",
                      fill="#5c6577", font=FONT_L, angle=90)
        c.create_text(152, 54, text="cable chamber", fill="#5c6577",
                      font=FONT_L)

        for el, i, nx, ny in case_layout.led_positions():
            x, y = case_layout._ring_xy(el, i)
            r = 8.5 if el["count"] <= 12 else 7.0
            item = c.create_oval(x - r, y - r, x + r, y + r,
                                 fill=LED_OFF, outline=LED_OFF_EDGE, width=1)
            rec = {"el": el, "i": i, "x": x, "y": y, "nx": nx, "ny": ny,
                   "rgb": (0, 0, 0), "item": item}
            self.leds.append(rec)
            self.byel.setdefault(el["id"], []).append(rec)

        # labels placed clear of the LEDs, never on top of them
        for recs in self.byel.values():
            el = recs[0]["el"]
            kind = el.get("kind", "fan")
            if kind == "strip_v":
                lx, ly = el["x"], el["y"] + el["count"] * 10.5 + 26
            elif kind == "strip_h":
                lx, ly = el["x"], el["y"] - 28
            else:
                lx, ly = el["x"], el["y"] + (el.get("r") or 30) + 26
            c.create_text(lx, ly, text=el["label"], fill=MUTED, font=FONT_L)

    # ---------- panel

    def _build_panel(self, p):
        def head(t):
            tk.Label(p, text=t, bg=PANEL, fg=MUTED, font=FONT_H, anchor="w"
                     ).pack(fill="x", padx=16, pady=(14, 6))

        def row():
            f = tk.Frame(p, bg=PANEL)
            f.pack(fill="x", padx=16, pady=2)
            return f

        tk.Label(p, text="LED STUDIO", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 15), anchor="w"
                 ).pack(fill="x", padx=16, pady=(16, 0))
        tk.Label(p, text=f"132 LEDs · 15 runs · {len(fx.SPATIAL)} effects", bg=PANEL, fg=MUTED,
                 font=FONT_L, anchor="w").pack(fill="x", padx=16)

        head("HARDWARE")
        self.ctl_btn = mkbtn(p, "Take control", self.toggle_ctl)
        self.ctl_btn.pack(fill="x", padx=16, pady=2)
        self.hw_var = tk.BooleanVar(value=True)
        self.hw_btn = mkbtn(p, "Drive hardware", self.toggle_hw, "accent")
        self.hw_btn.pack(fill="x", padx=16, pady=2)

        head("SELECTION")
        r = row()
        for txt, fn in (("All", self.sel_all), ("None", self.sel_none),
                        ("Invert", self.sel_inv)):
            mkbtn(r, txt, fn).pack(side="left", expand=True, fill="x", padx=2)
        self.brush_btn = mkbtn(p, "Brush", self.toggle_brush)
        self.brush_btn.pack(fill="x", padx=16, pady=2)

        head("COLOUR")
        sw = row()
        for h in SWATCHES:
            s = tk.Frame(sw, bg=h, width=34, height=34, cursor="hand2",
                         highlightthickness=1, highlightbackground=LINE)
            s.pack(side="left", expand=True, padx=1)
            s.pack_propagate(False)
            s.bind("<Button-1>", lambda e, x=h: self.set_colour(x))
        r = row()
        mkbtn(r, "Pick...", self.pick).pack(side="left", expand=True,
                                            fill="x", padx=2)
        mkbtn(r, "Paint", self.paint_sel, "accent").pack(side="left",
                                                         expand=True,
                                                         fill="x", padx=2)
        mkbtn(p, "Blank selection", lambda: self.paint_sel((0, 0, 0)), "ghost"
              ).pack(fill="x", padx=16, pady=2)

        head("PALETTE")
        self.pal_lbl = tk.Label(p, text="synthwave", bg=PANEL, fg=INK,
                                font=FONT, anchor="w")
        self.pal_lbl.pack(fill="x", padx=16)
        self.pal_strip = tk.Canvas(p, height=26, bg=PANEL,
                                   highlightthickness=1,
                                   highlightbackground=LINE)
        self.pal_strip.pack(fill="x", padx=16, pady=(4, 4))
        pr = row()
        mkbtn(pr, "<", lambda: self.cycle_palette(-1)).pack(side="left", padx=2)
        mkbtn(pr, "Edit...", self.edit_palette).pack(side="left", expand=True,
                                                     fill="x", padx=2)
        mkbtn(pr, ">", lambda: self.cycle_palette(1)).pack(side="left", padx=2)

        head("ANIMATIONS")
        # One category on screen at a time: 23 flat buttons is a wall, and it
        # also made the panel taller than the window. This keeps the panel a
        # fixed height no matter how many effects exist.
        self.groups = getattr(fx, "EFFECT_GROUPS", {"All": sorted(fx.SPATIAL)})
        self.catbtns = {}
        cr = row()
        for cat in self.groups:
            b = mkbtn(cr, cat, lambda c=cat: self.show_cat(c))
            b.config(padx=2, font=FONT_L)
            b.pack(side="left", expand=True, fill="x", padx=1)
            self.catbtns[cat] = b

        # fixed-height holder so switching category never resizes the panel
        self.fxbox = tk.Frame(p, bg=PANEL, height=96)
        self.fxbox.pack(fill="x", padx=16, pady=(4, 0))
        self.fxbox.pack_propagate(False)
        self.show_cat(next(iter(self.groups)))

        tk.Label(p, text="speed", bg=PANEL, fg=MUTED, font=FONT_L, anchor="w"
                 ).pack(fill="x", padx=16, pady=(10, 0))
        self.speed = tk.DoubleVar(value=1.0)
        tk.Scale(p, from_=0.2, to=3.0, resolution=0.1, orient="horizontal",
                 variable=self.speed, bg=PANEL, fg=INK, troughcolor=BTN,
                 highlightthickness=0, bd=0, sliderrelief="flat",
                 activebackground=ACCENT, font=FONT_L
                 ).pack(fill="x", padx=14)
        r = row()
        mkbtn(r, "Stop", self.stop_fx).pack(side="left", expand=True,
                                            fill="x", padx=2)
        mkbtn(r, "All OFF", self.all_off, "ghost").pack(side="left",
                                                        expand=True,
                                                        fill="x", padx=2)

    # ---------- palettes

    def active_palette(self):
        name = self.palettes.get(self.effect, self.palette_name)
        if name == "custom":
            return self.custom
        return fx.PALETTES.get(name, fx.SYNTHWAVE)

    def draw_palette(self):
        c = self.pal_strip
        c.delete("all")
        w = max(c.winfo_width(), 240)
        pal = self.active_palette()
        for i in range(w):
            col = fx.gamma(fx.cyclic_gradient(pal, i / w))
            c.create_line(i, 0, i, 26, fill="#%02x%02x%02x" % col)
        name = self.palettes.get(self.effect, self.palette_name)
        note = ""
        if self.effect in getattr(fx, "IGNORES_PALETTE", ()):
            note = "  (this effect uses fixed colours)"
        self.pal_lbl.config(text=name + note)

    def cycle_palette(self, step):
        names = list(fx.PALETTES) + ["custom"]
        cur = self.palettes.get(self.effect, self.palette_name)
        i = (names.index(cur) if cur in names else 0) + step
        new = names[i % len(names)]
        if self.effect:
            self.palettes[self.effect] = new     # remembered per effect
        self.palette_name = new
        self.draw_palette()
        self.say(f"palette: {new}"
                 + (f" for {self.effect}" if self.effect else " (default)"))

    def edit_palette(self):
        """Build a custom palette by picking each stop in turn."""
        stops = []
        for n in range(4):
            rgb, h = colorchooser.askcolor(
                color="#%02x%02x%02x" % self.custom[min(n, len(self.custom)-1)],
                title=f"Custom palette - stop {n+1} of 4 (Cancel to finish)")
            if not h:
                break
            stops.append(tuple(int(v) for v in rgb))
        if len(stops) < 2:
            self.say("custom palette needs at least two stops")
            return
        # mirror back through the middle so the wrap stays saturated
        self.custom = stops + stops[-2:0:-1] if len(stops) > 2 else stops + [stops[0]]
        if self.effect:
            self.palettes[self.effect] = "custom"
        self.palette_name = "custom"
        self.draw_palette()
        self.say(f"custom palette: {len(stops)} stops")

    def show_cat(self, cat):
        """Render just this category's effects into the fixed-height holder."""
        for b in self.catbtns.values():
            setbtn(b, False)
        setbtn(self.catbtns[cat], True)
        for w in self.fxbox.winfo_children():
            w.destroy()
        names = [n for n in self.groups[cat] if n in fx.SPATIAL]
        per = 3
        for i in range(0, len(names), per):
            r = tk.Frame(self.fxbox, bg=PANEL)
            r.pack(fill="x", pady=2)
            for name in names[i:i + per]:
                b = mkbtn(r, name, lambda x=name: self.start_fx(x))
                b.config(padx=2, font=FONT_L)
                b.pack(side="left", expand=True, fill="x", padx=2)
                self.fxbtns[name] = b
                setbtn(b, name == self.effect)

    # ---------- selection

    def hit(self, x, y, rad=9):
        best, bd = None, rad * rad
        for r in self.leds:
            d = (r["x"] - x) ** 2 + (r["y"] - y) ** 2
            if d < bd:
                best, bd = r, d
        return best

    def hit_centre(self, x, y):
        for el_id, recs in self.byel.items():
            el = recs[0]["el"]
            if (el["x"] * SCALE - x) ** 2 + (el["y"] * SCALE - y) ** 2 < (13 ** 2):
                return el_id
        return None

    def refresh_sel(self):
        for r in self.leds:
            key = (r["el"]["id"], r["i"])
            on = key in self.sel
            self.cv.itemconfig(r["item"],
                               outline="#ffffff" if on else LED_OFF_EDGE,
                               width=2 if on else 1)

    def add(self, r, keep):
        if not keep:
            self.sel.clear(); self.order.clear()
        k = (r["el"]["id"], r["i"])
        if k not in self.sel:
            self.sel.add(k); self.order.append(r)
        self.refresh_sel()

    def sel_all(self):
        self.sel = {(r["el"]["id"], r["i"]) for r in self.leds}
        self.order = list(self.leds); self.refresh_sel()

    def sel_none(self):
        self.sel.clear(); self.order.clear(); self.refresh_sel()

    def sel_inv(self):
        cur = set(self.sel)
        self.sel = {(r["el"]["id"], r["i"]) for r in self.leds
                    if (r["el"]["id"], r["i"]) not in cur}
        self.order = [r for r in self.leds if (r["el"]["id"], r["i"]) in self.sel]
        self.refresh_sel()

    # ---------- mouse

    def on_down(self, e):
        if self.brush:
            self.drag = "paint"; self.paint_at(e.x, e.y); return
        el_id = self.hit_centre(e.x, e.y)
        if el_id:
            keep = bool(e.state & 0x0001)
            if not keep:
                self.sel.clear(); self.order.clear()
            for r in self.byel[el_id]:
                k = (r["el"]["id"], r["i"])
                if k not in self.sel:
                    self.sel.add(k); self.order.append(r)
            self.refresh_sel(); return
        r = self.hit(e.x, e.y)
        if r:
            self.add(r, bool(e.state & 0x0001)); return
        self.drag = ("marq", e.x, e.y, bool(e.state & 0x0001))
        self.marq = self.cv.create_rectangle(e.x, e.y, e.x, e.y,
                                             outline="#ff3aa2", dash=(4, 3))

    def on_move(self, e):
        if self.drag == "paint":
            self.paint_at(e.x, e.y); return
        if isinstance(self.drag, tuple) and self.marq:
            self.cv.coords(self.marq, self.drag[1], self.drag[2], e.x, e.y)

    def on_up(self, e):
        if isinstance(self.drag, tuple) and self.marq:
            _, x0, y0, keep = self.drag
            x1, y1 = min(x0, e.x), min(y0, e.y)
            x2, y2 = max(x0, e.x), max(y0, e.y)
            if not keep:
                self.sel.clear(); self.order.clear()
            for r in self.leds:
                if x1 <= r["x"] <= x2 and y1 <= r["y"] <= y2:
                    k = (r["el"]["id"], r["i"])
                    if k not in self.sel:
                        self.sel.add(k); self.order.append(r)
            self.cv.delete(self.marq); self.marq = None
            self.refresh_sel()
        self.drag = None

    def paint_at(self, x, y):
        rgb = self.hex2rgb(self.colour)
        for r in self.leds:
            if (r["x"] - x) ** 2 + (r["y"] - y) ** 2 < 90:
                self.set_led(r, rgb)

    # ---------- colour

    @staticmethod
    def hex2rgb(h):
        return tuple(int(h[i:i+2], 16) for i in (1, 3, 5))

    def set_colour(self, h):
        self.colour = h
        if self.sel:
            self.paint_sel()

    def pick(self):
        rgb, h = colorchooser.askcolor(color=self.colour)
        if h:
            self.set_colour(h)

    def set_led(self, r, rgb):
        r["rgb"] = tuple(max(0, min(255, int(v))) for v in rgb)
        dark = sum(r["rgb"]) < 24
        self.cv.itemconfig(r["item"],
                           fill=LED_OFF if dark else "#%02x%02x%02x" % r["rgb"])

    def paint_sel(self, rgb=None):
        rgb = rgb or self.hex2rgb(self.colour)
        for r in self.leds:
            if (r["el"]["id"], r["i"]) in self.sel:
                self.set_led(r, rgb)

    def all_off(self):
        self.stop_fx()
        for r in self.leds:
            self.set_led(r, (0, 0, 0))
        self.push()
        self.say("all LEDs off")

    # ---------- animation

    def start_fx(self, name):
        for n, b in list(self.fxbtns.items()):
            try:
                setbtn(b, n == name)
            except tk.TclError:
                self.fxbtns.pop(n, None)   # button belonged to another category
        self.effect = name
        self.palette_name = self.palettes.get(name, self.palette_name)
        self.draw_palette()
        self.t0 = time.monotonic()
        self.frames = 0
        self.say(f"animating: {name}")

    def stop_fx(self):
        for n, b in list(self.fxbtns.items()):
            try:
                setbtn(b, False)
            except tk.TclError:
                self.fxbtns.pop(n, None)
        self.effect = None
        self.say("animation stopped")

    def toggle_brush(self):
        self.brush = not self.brush
        setbtn(self.brush_btn, self.brush)

    def toggle_hw(self):
        self.hw_var.set(not self.hw_var.get())
        setbtn(self.hw_btn, self.hw_var.get())

    def toggle_ctl(self):
        self.controlling = not self.controlling
        if self.controlling:
            OVERRIDE.write_text("led_studio_native")
        else:
            OVERRIDE.unlink(missing_ok=True)
        self.ctl_btn.config(text="Release to daemon" if self.controlling
                            else "Take control")
        setbtn(self.ctl_btn, self.controlling)

    def push(self):
        if not (self.controlling and self.hw_var.get()):
            return
        frame = {}
        for r in self.leds:
            frame.setdefault(r["el"]["id"], []).append(r["rgb"])
        self.hw.post(frame)

    def tick(self):
        try:
            if self.effect:
                fn = fx.SPATIAL[self.effect]
                t = (time.monotonic() - self.t0) * self.speed.get()
                pal = self.active_palette()
                for r in self.leds:
                    self.set_led(r, fn(r["nx"], r["ny"], t, pal))
                self.frames += 1
                if self.frames % 30 == 0:
                    self.say(f"{self.effect}: {self.frames} frames, t={t:.1f}s"
                             + ("  (driving hardware)" if self.controlling
                                and self.hw_var.get() else "  (preview only)"))
                self.push()
        except Exception as exc:
            self.say(f"animation error: {type(exc).__name__}: {exc}")
            self.effect = None
        try:
            while True:
                kind, payload = self.out.get_nowait()
                if kind == "log":
                    self.say(payload)
        except queue.Empty:
            pass
        self.root.after(UI_MS, self.tick)

    def say(self, t):
        self.status.config(text=t)

    def close(self):
        self.effect = None
        OVERRIDE.unlink(missing_ok=True)
        self.hw.stop_flag.set()
        self.root.after(300, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
