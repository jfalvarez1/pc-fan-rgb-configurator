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

In Layer mode the canvas edits effect boxes instead of LEDs:

    drag inside a box       move it
    drag a corner           resize, opposite corner pinned
    drag the top handle     rotate
    arrows / shift-arrows   nudge by 1 / 10 px
    Delete                  remove the selected box

A box applies its effect only to the LEDs it covers, evaluated in the box's
own local space, so the effect spans the box wherever it sits or however it is
turned. Later layers paint over earlier ones.

Effects animate in the canvas immediately; the hardware is only written when
"Drive hardware" is ticked. Takes manual_override.flag while it has control
so the daemon stands down, and releases on exit.
"""
import ctypes
import json
import math
import os
import pathlib
import queue
import threading
import time
import tkinter as tk
from tkinter import colorchooser, ttk

import case_layout
import fan_panel
import fan_side
import fx_layers
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
STATE = BASE / "led_studio_state.json"
AUTO_CONTROL = True      # take the hardware on launch, release it on close
FLAG_BEAT_MS = 20000     # refresh the override flag this often while holding
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
        self.written = {}       # dev_id -> what was last actually sent
        self.writes = 0
        self.skipped = 0

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
            self.written = {}       # cannot trust device state after reconnect
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
                    # Only write a device whose colours actually CHANGED.
                    # Every frame used to write every device unconditionally -
                    # 20 Hz of USB HID traffic to the keyboard for as long as
                    # anything was running, even a completely static image.
                    # That matters more now the editor autostarts and holds
                    # control all day, and it is the kind of load that makes a
                    # keyboard share its HID endpoint badly.
                    cur = tuple(self.buf[dev_id])
                    if self.written.get(dev_id) == cur:
                        self.skipped += 1
                        continue
                    self.written[dev_id] = cur
                    self.writes += 1
                    dev.set_colors([RGBColor(*c) for c in cur], fast=True)
            except Exception as exc:
                self.out.put(("log", f"write failed: {type(exc).__name__}: {exc}"))
                self.client = None
                self.written = {}
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
        # effect layers: movable/rotatable boxes that own the LEDs they cover
        self.layers = []
        self.active = None          # the selected Layer, or None
        self.layer_mode = False
        self.lyr_items = []         # canvas items, rebuilt on change
        self.ldrag = None
        self.controlling = False
        self.colour = "#ff3aa2"
        # palette per effect, so each remembers its own look
        self.palettes = {}
        self.palette_name = "synthwave"
        self.custom = list(fx.SYNTHWAVE)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True)
        left = tk.Frame(wrap, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=10)
        tabs = tk.Frame(left, bg=BG)
        tabs.pack(fill="x", pady=(0, 6))
        self.tabbtns = {}
        for _n in ("Lighting", "Fans"):
            _b = mkbtn(tabs, _n, lambda n=_n: self.show_tab(n))
            _b.pack(side="left", padx=(0, 6))
            self.tabbtns[_n] = _b
        self.stack = tk.Frame(left, bg=BG)
        self.stack.pack(fill="both", expand=True)
        self.cv = tk.Canvas(self.stack, width=W, height=H, bg=BG,
                            highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.fan_cv = tk.Canvas(self.stack, width=W, height=H, bg=BG,
                                highlightthickness=0)
        self.fans = fan_panel.FanPanel(self.fan_cv, W, H)
        self.tab = "Lighting"
        self.fan_ticks = 0
        side = tk.Frame(wrap, bg=PANEL, width=360)
        side.pack(side="right", fill="y", padx=(0, 10), pady=10)
        side.pack_propagate(False)

        # The panel scrolls. Twice now a new section has pushed the controls
        # past the bottom of the window, and both times the fix was to size
        # the window to the content - which only works until the content grows
        # again, or the screen is smaller than the panel. Scrolling ends that
        # class of bug rather than deferring it.
        pcv = tk.Canvas(side, bg=PANEL, highlightthickness=0)
        vsb = tk.Scrollbar(side, orient="vertical", command=pcv.yview,
                           troughcolor=PANEL, bg=BTN, activebackground=ACCENT,
                           highlightthickness=0, bd=0, width=12)
        pcv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        pcv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(pcv, bg=PANEL)
        iwin = pcv.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: pcv.configure(scrollregion=pcv.bbox("all")))
        pcv.bind("<Configure>",
                 lambda e: pcv.itemconfigure(iwin, width=e.width))

        def _wheel(e):
            pcv.yview_scroll(-1 if e.delta > 0 else 1, "units")
        pcv.bind("<Enter>", lambda e: pcv.bind_all("<MouseWheel>", _wheel))
        pcv.bind("<Leave>", lambda e: pcv.unbind_all("<MouseWheel>"))
        self.panel_inner = inner
        # Each tab owns its side panel. The lighting controls do not apply to
        # fans, so they are hidden rather than left there to be clicked.
        self.panel_light = tk.Frame(inner, bg=PANEL)
        self.panel_light.pack(fill="both", expand=True)
        self.panel_fan = tk.Frame(inner, bg=PANEL)

        self._build_case()
        self._build_panel(self.panel_light)
        self.fan_side = fan_side.FanSidePanel(self.panel_fan, PANEL,
                                              on_change=self.say)

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
        want_h = max(H + 110, inner.winfo_reqheight() + 60)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight() - 60
        w, h = min(want_w, sw - 40), min(want_h, sh)
        root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+20")
        root.minsize(900, 620)

        self.pal_strip.bind("<Configure>", lambda e: self.draw_palette())
        self._pal_id = root.after(200, self.draw_palette)
        self.refresh_layer_list()
        self.show_tab("Lighting")
        self.load_state()
        if AUTO_CONTROL and not self.controlling:
            self.toggle_ctl()          # released again by close()
        self.beat_flag()

        # Layer keys. Bound on the root so they work wherever focus sits, but
        # only act in layer mode - otherwise Delete would fire while the user
        # is doing something else entirely.
        root.bind("<Delete>", lambda e: self.layer_mode and self.del_layer())
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0),
                            ("Up", 0, -1), ("Down", 0, 1)):
            root.bind(f"<{key}>",
                      lambda e, a=dx, b=dy: self.nudge_layer(a, b))
            root.bind(f"<Shift-{key}>",
                      lambda e, a=dx, b=dy: self.nudge_layer(a * 10, b * 10))

        self.hw.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._tick_id = root.after(UI_MS, self.tick)

    # ---------- canvas

    def _build_case(self):
        c = self.cv
        c.create_rectangle(30, 30, W - 30, 940, outline=LINE, width=2)
        c.create_line(262, 30, 262, 940, fill="#212736", dash=(6, 6))
        c.create_rectangle(345, 560, 808, 650, outline=LINE, fill=CARD)
        c.create_text(372, 636, text="RTX 5090", fill=MUTED, font=FONT_L,
                      anchor="w")
        c.create_text(594, 52, text="TOP - radiator exhaust",
                      fill="#5c6577", font=FONT_L)
        c.create_text(552, 912, text="BOTTOM - F420 intake",
                      fill="#5c6577", font=FONT_L)
        c.create_text(W - 52, 435, text="SIDE - F360 intake",
                      fill="#5c6577", font=FONT_L, angle=90)
        c.create_text(152, 54, text="cable chamber", fill="#5c6577",
                      font=FONT_L)

        for el, i, nx, ny in case_layout.led_positions():
            x, y = case_layout._ring_xy(el, i)
            if el.get("kind") == "grid":
                # A gap in the matrix: absent in plastic, but REAL ON THE WIRE.
                # It still needs a slot in the colour list or every later key
                # shifts and the tail indices are never written at all.
                if i in el.get("blanks", ()):
                    self.leds.append({"el": el, "i": i, "x": x, "y": y,
                                      "nx": nx, "ny": ny, "rgb": (0, 0, 0),
                                      "manual": (0, 0, 0), "item": None,
                                      "cell": case_layout.cell_of(el, i)})
                    continue
                h = el.get("cell", 27) / 2 - 2
                item = c.create_rectangle(x - h, y - h, x + h, y + h,
                                          fill=LED_OFF, outline=LED_OFF_EDGE,
                                          width=1)
            else:
                r = 8.5 if el["count"] <= 12 else 7.0
                item = c.create_oval(x - r, y - r, x + r, y + r,
                                     fill=LED_OFF, outline=LED_OFF_EDGE,
                                     width=1)
            rec = {"el": el, "i": i, "x": x, "y": y, "nx": nx, "ny": ny,
                   "rgb": (0, 0, 0), "manual": (0, 0, 0), "item": item,
                   "cell": case_layout.cell_of(el, i)}
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
            elif kind == "grid":
                lx = el["x"]
                ly = el["y"] + el.get("rows", 5) * el.get("cell", 27) / 2 + 22
            else:
                lx, ly = el["x"], el["y"] + (el.get("r") or 30) + 26
            c.create_text(lx, ly, text=el["label"], fill=MUTED, font=FONT_L)

    # ---------- panel

    def _build_panel(self, p):
        def head(t):
            lb = tk.Label(p, text=t, bg=PANEL, fg=MUTED, font=FONT_H,
                          anchor="w")
            lb.pack(fill="x", padx=16, pady=(14, 6))
            return lb

        def row():
            f = tk.Frame(p, bg=PANEL)
            f.pack(fill="x", padx=16, pady=2)
            return f

        tk.Label(p, text="LED STUDIO", bg=PANEL, fg=INK,
                 font=("Segoe UI Semibold", 15), anchor="w"
                 ).pack(fill="x", padx=16, pady=(16, 0))
        tk.Label(p, text=f"207 LEDs · 16 runs · {len(fx.SPATIAL)} effects", bg=PANEL, fg=MUTED,
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

        self.anim_lbl = head("ANIMATIONS")
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

        # Fixed-height holder so switching category never resizes the panel -
        # but sized to the LARGEST category, not a guess. It was hardcoded to
        # 96px (two rows) and the eight-effect Fill group clipped its last row.
        per_row = 3
        rows_needed = max((len(v) + per_row - 1) // per_row
                          for v in self.groups.values())
        self.fxbox = tk.Frame(p, bg=PANEL, height=rows_needed * 37 + 6)
        self.fxbox.pack(fill="x", padx=16, pady=(4, 0))
        self.fxbox.pack_propagate(False)
        self.show_cat(next(iter(self.groups)))

        tk.Label(p, text="speed", bg=PANEL, fg=MUTED, font=FONT_L, anchor="w"
                 ).pack(fill="x", padx=16, pady=(10, 0))
        self.speed = tk.DoubleVar(value=1.0)
        tk.Scale(p, from_=0.1, to=8.0, resolution=0.1, orient="horizontal",
                 variable=self.speed, bg=PANEL, fg=INK, troughcolor=BTN,
                 highlightthickness=0, bd=0, sliderrelief="flat",
                 activebackground=ACCENT, font=FONT_L
                 ).pack(fill="x", padx=14)
        # VU bar count - only meaningful for the meter effects, so it says so
        self.bars_lbl = tk.Label(p, text="VU bars: 8", bg=PANEL, fg=MUTED,
                                 font=FONT_L, anchor="w")
        self.bars_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self.bars = tk.IntVar(value=getattr(fx, "VU_BARS", 8))
        tk.Scale(p, from_=2, to=20, orient="horizontal", variable=self.bars,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_bars
                 ).pack(fill="x", padx=14)
        # VU sensitivity. Content loudness varies far too much for one fixed
        # setting - a game sits near the top of the range where a quiet track
        # barely leaves the bottom - so this is the escape hatch.
        self.gain_lbl = tk.Label(p, text="VU sensitivity: 1.0x", bg=PANEL,
                                 fg=MUTED, font=FONT_L, anchor="w")
        self.gain_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self.gain = tk.IntVar(value=int(getattr(fx, "VU_GAIN", 1.0) * 10))
        tk.Scale(p, from_=3, to=30, orient="horizontal", variable=self.gain,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_gain
                 ).pack(fill="x", padx=14)

        r = row()
        mkbtn(r, "Stop", self.stop_fx).pack(side="left", expand=True,
                                            fill="x", padx=2)
        mkbtn(r, "All OFF", self.all_off, "ghost").pack(side="left",
                                                        expand=True,
                                                        fill="x", padx=2)

        head("EFFECT LAYERS")
        tk.Label(p, text="A box that applies its effect only to the LEDs it\n"
                         "covers. Drag to move, corners resize, the handle\n"
                         "above the top edge rotates.",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w",
                 justify="left").pack(fill="x", padx=16, pady=(0, 4))
        self.lyr_btn = mkbtn(p, "Layer mode", self.toggle_layers)
        self.lyr_btn.pack(fill="x", padx=16, pady=2)
        r = row()
        mkbtn(r, "+ Add", self.add_layer, "accent").pack(
            side="left", expand=True, fill="x", padx=2)
        mkbtn(r, "Delete", self.del_layer).pack(
            side="left", expand=True, fill="x", padx=2)
        r = row()
        mkbtn(r, "Lower", lambda: self.raise_layer(-1)).pack(
            side="left", expand=True, fill="x", padx=2)
        mkbtn(r, "Raise", lambda: self.raise_layer(1)).pack(
            side="left", expand=True, fill="x", padx=2)
        self.lyr_list = tk.Frame(p, bg=PANEL)
        self.lyr_list.pack(fill="x", padx=16, pady=(6, 2))
        self.lyr_fx_btn = mkbtn(p, "Effect: (select a layer)",
                                self.choose_effect, "accent")
        self.lyr_fx_btn.pack(fill="x", padx=16, pady=(6, 2))
        self.opa_lbl = tk.Label(p, text="Layer opacity: 100%", bg=PANEL,
                                fg=MUTED, font=FONT_L, anchor="w")
        self.opa_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self.opacity = tk.IntVar(value=100)
        tk.Scale(p, from_=5, to=100, orient="horizontal", variable=self.opacity,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_layer_opacity
                 ).pack(fill="x", padx=14)
        self.blend_btn = mkbtn(p, "Blend: normal", self.cycle_layer_blend)
        self.blend_btn.pack(fill="x", padx=16, pady=2)

    def set_bars(self, _v=None):
        n = int(self.bars.get())
        fx.VU_BARS = n
        if (self.effect or "").startswith("vu"):
            if fx.AUDIO is not None:
                fx.AUDIO.start()
            if fx.audio_ready():
                note = "  · live audio" + ("" if fx.audio_active()
                                                 else " (silent)")
            else:
                note = "  · SIMULATED (no audio capture)"
        else:
            note = "  (vu effects)"
        self.bars_lbl.config(text=f"VU bars: {n}{note}")

    def set_gain(self, _v=None):
        g = fx.set_vu_gain(self.gain.get() / 10.0)
        self.gain_lbl.config(text=f"VU sensitivity: {g:.1f}x")

    # ---------- palettes

    def active_palette(self):
        return self.palette_for(self.palettes.get(self.effect,
                                                  self.palette_name))

    def palette_for(self, name, default=None):
        """Resolve a palette NAME to colours. Layers store the name, so a
        layer keeps its look even after the global palette changes."""
        if name is None:
            return default if default is not None else fx.SYNTHWAVE
        if name == "custom":
            return self.custom
        return fx.PALETTES.get(name, fx.SYNTHWAVE)

    def shown_palette_name(self):
        if self.layer_mode and self.active:
            return self.active.palette or self.palette_name
        return self.palettes.get(self.effect, self.palette_name)

    def draw_palette(self):
        c = self.pal_strip
        c.delete("all")
        w = max(c.winfo_width(), 240)
        name = self.shown_palette_name()
        pal = self.palette_for(name)
        for i in range(w):
            col = fx.gamma(fx.cyclic_gradient(pal, i / w))
            c.create_line(i, 0, i, 26, fill="#%02x%02x%02x" % col)
        owner = self.effect
        note = ""
        if self.layer_mode and self.active:
            owner = self.active.effect
            note = f"  ({self.active.name})"
        if owner in getattr(fx, "IGNORES_PALETTE", ()):
            note = "  (this effect uses fixed colours)"
        self.pal_lbl.config(text=name + note)

    def cycle_palette(self, step):
        names = list(fx.PALETTES) + ["custom"]
        if self.layer_mode and self.active:
            cur = self.active.palette or self.palette_name
            i = (names.index(cur) if cur in names else 0) + step
            self.active.palette = names[i % len(names)]
            self.draw_palette()
            self.say(f"{self.active.name} palette: {self.active.palette}")
            return
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
        self.cur_cat = cat
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
                setbtn(b, name == self.target_effect())

    # ---------- effect layers

    def add_layer(self):
        """New box in the middle of the case, carrying the current effect."""
        eff = self.effect or "wave"
        lay = fx_layers.Layer(eff, W * 0.5, H * 0.42, W * 0.42, H * 0.22,
                              palette=self.palette_name)
        lay.t0 = time.monotonic()
        self.layers.append(lay)
        self.active = lay
        lay.reindex(self.leds)
        if not self.layer_mode:
            self.toggle_layers()
        self.draw_layers()
        self.refresh_layer_list()
        self.say(f"{lay.name}: {eff}, {len(lay.members)} LEDs covered")

    def del_layer(self):
        if not self.active:
            return
        name = self.active.name
        self.layers.remove(self.active)
        self.active = self.layers[-1] if self.layers else None
        self.draw_layers()
        self.refresh_layer_list()
        self.say(f"removed {name}")

    def raise_layer(self, step):
        """Reorder. Later layers paint over earlier ones."""
        if not self.active or len(self.layers) < 2:
            return
        i = self.layers.index(self.active)
        j = max(0, min(len(self.layers) - 1, i + step))
        if i == j:
            return
        self.layers.insert(j, self.layers.pop(i))
        self.refresh_layer_list()
        self.say(f"{self.active.name} -> position {j + 1} of {len(self.layers)}")

    def toggle_layers(self):
        self.layer_mode = not self.layer_mode
        setbtn(self.lyr_btn, self.layer_mode)
        if self.layer_mode:
            self.sel_none()
        self.draw_layers()
        self.sync_target()
        self.say("layer mode: drag to move, corners resize, top handle rotates"
                 if self.layer_mode else "layer mode off - LED selection active")

    def toggle_layer_on(self, lay=None):
        lay = lay or self.active
        if not lay:
            return
        lay.on = not lay.on
        self.refresh_layer_list()

    def set_layer_opacity(self, _v=None):
        if self.active:
            self.active.opacity = self.opacity.get() / 100.0
        self.opa_lbl.config(text=f"Layer opacity: {self.opacity.get()}%")

    def cycle_layer_blend(self):
        if not self.active:
            return
        self.blend_btn.config(text=f"Blend: {self.active.cycle_blend()}")

    def reindex_active(self):
        if self.active:
            self.active.reindex(self.leds)

    def nudge_layer(self, dx, dy):
        if not (self.layer_mode and self.active):
            return
        self.active.nudge(dx, dy)
        self.reindex_active()
        self.draw_layers()

    def draw_layers(self):
        """Redraw the boxes. Cheap enough to rebuild wholesale, and that keeps
        the handles from drifting out of sync with the geometry."""
        for it in self.lyr_items:
            self.cv.delete(it)
        self.lyr_items = []
        if not self.layer_mode:
            return
        for lay in self.layers:
            act = lay is self.active
            col = ACCENT if act else "#5f6b82"
            pts = [c for xy in lay.corners() for c in xy]
            self.lyr_items.append(self.cv.create_polygon(
                pts, outline=col, fill="", width=2 if act else 1,
                dash=() if lay.on else (5, 4)))
            self.lyr_items.append(self.cv.create_text(
                lay.x, lay.y, text=f"{lay.name}\n{lay.effect}",
                fill=col, font=FONT_L, justify="center"))
            if not act:
                continue
            hx, hy = lay.rot_handle()
            tx, ty = lay.top_mid()
            self.lyr_items.append(self.cv.create_line(
                tx, ty, hx, hy, fill=col, width=2))
            r = fx_layers.HANDLE_R
            self.lyr_items.append(self.cv.create_oval(
                hx - r, hy - r, hx + r, hy + r, fill=col, outline="#ffffff"))
            for cx, cy in lay.corners():
                self.lyr_items.append(self.cv.create_rectangle(
                    cx - r, cy - r, cx + r, cy + r,
                    fill="#ffffff", outline=col))

    def target_effect(self):
        """The effect the controls currently edit: the selected layer's, or
        the global one. Everything that shows state asks this, so the panel
        can never claim to be editing one thing while it edits another."""
        if self.layer_mode and self.active:
            return self.active.effect
        return self.effect

    def sync_target(self):
        """Make the retarget VISIBLE.

        Selecting a layer silently repointed the effect buttons at it, which
        is invisible if you are looking at the layer section - the buttons are
        a whole scroll away. Now the header names the target, the grid
        highlights that layer's effect, and the layer section carries its own
        effect button so the common case needs no scrolling at all.
        """
        lay = self.active if self.layer_mode else None
        lbl = getattr(self, "anim_lbl", None)
        if lbl is not None:
            lbl.config(text=f"ANIMATIONS  →  {lay.name.upper()}" if lay
                       else "ANIMATIONS",
                       fg=ACCENT if lay else MUTED)
        cur = self.target_effect()
        for n, b in list(self.fxbtns.items()):
            try:
                setbtn(b, n == cur)
            except tk.TclError:
                self.fxbtns.pop(n, None)
        btn = getattr(self, "lyr_fx_btn", None)
        if btn is not None:
            btn.config(text=f"Effect: {lay.effect}" if lay
                       else "Effect: (select a layer)")

    def reveal_effect(self, name):
        """Switch the visible category to the one holding `name`, so the grid
        actually shows the effect it is highlighting."""
        for cat, names in self.groups.items():
            if name in names and getattr(self, "cur_cat", None) != cat:
                self.show_cat(cat)
                return

    def choose_effect(self):
        """Pick an effect for the selected layer, without leaving the layer
        section. Same categorised grid as the main panel."""
        if not (self.layer_mode and self.active):
            self.say("select a layer first - or turn on Layer mode")
            return
        lay = self.active
        top = tk.Toplevel(self.root)
        top.title(f"Effect for {lay.name}")
        top.configure(bg=PANEL)
        top.transient(self.root)
        try:
            top.iconbitmap(str(BASE.parent / "led_studio.ico"))
        except Exception:
            pass

        def pick(name):
            lay.effect = name
            lay.t0 = time.monotonic()
            top.destroy()
            self.draw_layers()
            self.refresh_layer_list()
            self.reveal_effect(name)
            self.say(f"{lay.name}: {name}")

        tk.Label(top, text=f"{lay.name}  ·  currently {lay.effect}",
                 bg=PANEL, fg=INK, font=FONT, anchor="w"
                 ).pack(fill="x", padx=14, pady=(12, 4))
        for cat, names in self.groups.items():
            names = [n for n in names if n in fx.SPATIAL]
            if not names:
                continue
            tk.Label(top, text=cat.upper(), bg=PANEL, fg=MUTED, font=FONT_H,
                     anchor="w").pack(fill="x", padx=14, pady=(8, 2))
            for i in range(0, len(names), 3):
                r = tk.Frame(top, bg=PANEL)
                r.pack(fill="x", padx=10)
                for n in names[i:i + 3]:
                    b = mkbtn(r, n, lambda x=n: pick(x))
                    b.config(padx=2, font=FONT_L, width=12)
                    b.pack(side="left", expand=True, fill="x", padx=2, pady=2)
                    setbtn(b, n == lay.effect)
        mkbtn(top, "Cancel", top.destroy, "ghost").pack(fill="x", padx=14,
                                                        pady=(10, 12))
        top.update_idletasks()
        # beside the main window, not on top of the case it is previewing
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        top.geometry(f"+{rx + 60}+{max(0, ry + 40)}")
        top.grab_set()

    def refresh_layer_list(self):
        """Rebuild the layer rows. Topmost first, matching how they paint."""
        for w in self.lyr_list.winfo_children():
            w.destroy()
        if not self.layers:
            tk.Label(self.lyr_list, text="no layers yet", bg=PANEL, fg=MUTED,
                     font=FONT_L, anchor="w").pack(fill="x")
        for lay in reversed(self.layers):
            f = tk.Frame(self.lyr_list, bg=CARD if lay is self.active else PANEL,
                         highlightthickness=1,
                         highlightbackground=ACCENT if lay is self.active
                         else LINE)
            f.pack(fill="x", pady=1)
            dot = tk.Label(f, text="on" if lay.on else "off", width=4,
                           bg=f["bg"], fg=ACCENT if lay.on else MUTED,
                           font=FONT_L, cursor="hand2")
            dot.pack(side="left", padx=(6, 2), pady=3)
            dot.bind("<Button-1>", lambda e, l=lay: self.toggle_layer_on(l))
            txt = tk.Label(f, text=f"{lay.name} · {lay.effect}", bg=f["bg"],
                           fg=INK if lay is self.active else MUTED,
                           font=FONT_L, anchor="w", cursor="hand2")
            txt.pack(side="left", fill="x", expand=True, pady=3)
            txt.bind("<Button-1>", lambda e, l=lay: self.select_layer(l))
            txt.bind("<Double-Button-1>",
                     lambda e, l=lay: (self.select_layer(l),
                                       self.choose_effect()))
        if self.active:
            self.opacity.set(int(round(self.active.opacity * 100)))
            self.opa_lbl.config(text=f"Layer opacity: {self.opacity.get()}%")
            self.blend_btn.config(text=f"Blend: {self.active.blend}")
        self.draw_palette()
        self.sync_target()

    def select_layer(self, lay):
        self.active = lay
        if not self.layer_mode:
            self.toggle_layers()
        self.draw_layers()
        self.refresh_layer_list()
        self.reveal_effect(lay.effect)

    def layer_at(self, x, y):
        """Topmost layer whose body or handles are under the point."""
        for lay in reversed(self.layers):
            if lay is self.active:
                if lay.hit_rot(x, y):
                    return lay, ("rot", None)
                h = lay.hit_handle(x, y)
                if h is not None:
                    return lay, ("size", h)
            if lay.contains(x, y):
                return lay, ("move", (x - lay.x, y - lay.y))
        return None, None

    # ---------- selection

    def hit(self, x, y, rad=9):
        best, bd = None, rad * rad
        for r in self.leds:
            if r["item"] is None:
                continue
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
            if r["item"] is None:
                continue
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
        if self.layer_mode:
            lay, act = self.layer_at(e.x, e.y)
            if lay is not None:
                if lay is not self.active:
                    self.active = lay
                    self.refresh_layer_list()
                self.ldrag = act
                self.draw_layers()
            else:
                self.active = None
                self.ldrag = None
                self.draw_layers()
                self.refresh_layer_list()
            return
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
        if self.layer_mode:
            if not (self.ldrag and self.active):
                return
            kind, data = self.ldrag
            if kind == "move":
                self.active.move_to(e.x - data[0], e.y - data[1])
            elif kind == "size":
                self.active.resize_from(data, e.x, e.y)
            elif kind == "rot":
                self.active.rotate_to(e.x, e.y)
            self.reindex_active()
            self.draw_layers()
            return
        if self.drag == "paint":
            self.paint_at(e.x, e.y); return
        if isinstance(self.drag, tuple) and self.marq:
            self.cv.coords(self.marq, self.drag[1], self.drag[2], e.x, e.y)

    def on_up(self, e):
        if self.layer_mode:
            if self.ldrag and self.active:
                a = self.active
                self.say(f"{a.name}: {len(a.members)} LEDs covered, "
                         f"{int(round(math.degrees(a.angle))) % 360} deg")
            self.ldrag = None
            return
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
                self.set_led(r, rgb, manual=True)

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

    def set_led(self, r, rgb, manual=False):
        r["rgb"] = tuple(max(0, min(255, int(v))) for v in rgb)
        if manual:
            # remembered separately, so it survives as the background under
            # the layers rather than being overwritten by the last frame
            r["manual"] = r["rgb"]
        if r["item"] is None:
            return                      # matrix gap: addressed, never drawn
        dark = sum(r["rgb"]) < 24
        self.cv.itemconfig(r["item"],
                           fill=LED_OFF if dark else "#%02x%02x%02x" % r["rgb"])

    def paint_sel(self, rgb=None):
        rgb = rgb or self.hex2rgb(self.colour)
        for r in self.leds:
            if (r["el"]["id"], r["i"]) in self.sel:
                self.set_led(r, rgb, manual=True)

    def all_off(self):
        self.stop_fx()
        for lay in self.layers:
            lay.on = False
        self.refresh_layer_list()
        for r in self.leds:
            self.set_led(r, (0, 0, 0), manual=True)
        self.push()
        self.say("all LEDs off")

    # ---------- animation

    def start_fx(self, name):
        # With a layer selected, the effect buttons set THAT layer's effect.
        # Selecting a layer is the explicit act that redirects them, which is
        # how SignalRGB behaves and keeps one set of buttons doing both jobs.
        if self.layer_mode and self.active:
            self.active.effect = name
            self.active.t0 = time.monotonic()
            self.draw_layers()
            self.refresh_layer_list()
            self.say(f"{self.active.name}: {name}")
            return
        for n, b in list(self.fxbtns.items()):
            try:
                setbtn(b, n == name)
            except tk.TclError:
                self.fxbtns.pop(n, None)   # button belonged to another category
        self.effect = name
        self.palette_name = self.palettes.get(name, self.palette_name)
        self.draw_palette()
        self.set_bars()
        self.set_gain()
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
        self.sync_target()
        self.say("animation stopped")

    def toggle_brush(self):
        self.brush = not self.brush
        setbtn(self.brush_btn, self.brush)

    def toggle_hw(self):
        self.hw_var.set(not self.hw_var.get())
        setbtn(self.hw_btn, self.hw_var.get())

    def claim_flag(self):
        """Stamp the override flag with our pid.

        Refreshed on a timer, so a session lasting longer than the daemon's
        one-hour window does not silently lose control mid-session - and the
        pid lets the daemon spot a crashed editor at once instead of standing
        down for the rest of that hour.
        """
        try:
            # scope=leds: this editor never touches fans. Without the scope
            # the daemon pauses the CASE FAN CURVES for as long as this window
            # is open - which, now that it autostarts, is always.
            OVERRIDE.write_text(
                "led_studio_native\npid=%d\nscope=leds\n" % os.getpid())
        except OSError:
            pass

    def beat_flag(self):
        if self.controlling:
            self.claim_flag()
        self.save_state()      # so a crash costs at most one interval
        self._beat_id = self.root.after(FLAG_BEAT_MS, self.beat_flag)

    def toggle_ctl(self):
        self.controlling = not self.controlling
        if self.controlling:
            self.claim_flag()
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
        # Guard the failure that produced a stuck white key: a short list
        # silently shifts every LED after a gap and leaves the tail unwritten.
        for el in case_layout.LAYOUT:
            got = len(frame.get(el["id"], ()))
            if got and got != el["count"]:
                self.say(f"frame length mismatch on {el['label']}: "
                         f"{got} != {el['count']}")
                frame[el["id"]] = (list(frame[el["id"]])
                                   + [(0, 0, 0)] * el["count"])[:el["count"]]
        self.hw.post(frame)

    def tick(self):
        try:
            live = [l for l in self.layers if l.on and l.effect in fx.SPATIAL]
            if self.effect or live:
                t = (time.monotonic() - self.t0) * self.speed.get()
                pal = self.active_palette()
                fn = fx.SPATIAL[self.effect] if self.effect else None
                # resolved once per frame, not per LED
                base_cells = self.effect in fx.CELL_AWARE
                # Base pass. With no global effect the manually painted
                # colour is the background, so layers composite over painting
                # instead of erasing it.
                for r in self.leds:
                    if fn is None:
                        r["c"] = r.get("manual", (0, 0, 0))
                    elif base_cells and r["cell"]:
                        r["c"] = fn(r["nx"], r["ny"], t, pal, cell=r["cell"])
                    else:
                        r["c"] = fn(r["nx"], r["ny"], t, pal)
                # Layers, in order: later ones paint over earlier ones. Only
                # the LEDs a box covers are touched, using the box's own local
                # coordinates so the effect spans the box wherever it sits.
                for lay in live:
                    lfn = fx.SPATIAL[lay.effect]
                    lpal = self.palette_for(lay.palette, pal)
                    lt = t * lay.speed
                    cells = lay.effect in fx.CELL_AWARE
                    for r in lay.members:
                        u, v = lay.local(r["x"], r["y"])
                        col = (lfn(u, v, lt, lpal, cell=r["cell"])
                               if cells and r["cell"]
                               else lfn(u, v, lt, lpal))
                        r["c"] = lay.apply(r["c"], col)
                for r in self.leds:
                    self.set_led(r, r["c"])
                self.frames += 1
                vu_live = ((self.effect or "").startswith("vu")
                           or any(l.effect.startswith("vu") for l in live))
                if self.frames % 30 == 0 and vu_live:
                    self.set_bars()
                if self.frames % 30 == 0:
                    what = self.effect or "layers"
                    extra = f" +{len(live)} layer(s)" if live and self.effect \
                        else (f"{len(live)} layer(s)" if live else "")
                    self.say(f"{what}{extra}: {self.frames} frames, t={t:.1f}s"
                             + ("  (driving hardware)" if self.controlling
                                and self.hw_var.get() else "  (preview only)"))
                self.push()
        except Exception as exc:
            self.say(f"animation error: {type(exc).__name__}: {exc}")
            self.effect = None
        if self.tab == "Fans":
            self.fan_ticks += 1
            if self.fan_ticks % 30 == 0:
                self.refresh_fans()

        try:
            while True:
                kind, payload = self.out.get_nowait()
                if kind == "log":
                    self.say(payload)
        except queue.Empty:
            pass
        self._tick_id = self.root.after(UI_MS, self.tick)

    def show_tab(self, name):
        """Swap the left-hand view. The lighting canvas keeps animating either
        way - the effect loop does not care whether it is on screen."""
        self.tab = name
        for n, b in self.tabbtns.items():
            setbtn(b, n == name)
        if name == "Fans":
            self.cv.pack_forget()
            self.fan_cv.pack(fill="both", expand=True)
            self.panel_light.pack_forget()
            self.panel_fan.pack(fill="both", expand=True)
            fan_side.GPU.start()
            self.refresh_fans()
        else:
            self.fan_cv.pack_forget()
            self.cv.pack(fill="both", expand=True)
            self.panel_fan.pack_forget()
            self.panel_light.pack(fill="both", expand=True)

    def refresh_fans(self):
        try:
            notes = self.fans.refresh()
            self.fan_side.refresh(self.fans.gather())
        except Exception as exc:
            self.say(f"fan view error: {type(exc).__name__}: {exc}")
            return
        if notes:
            self.say(f"fans: {notes[0]}"
                     + (f"  (+{len(notes)-1} more)" if len(notes) > 1 else ""))

    def say(self, t):
        self.status.config(text=t)

    def save_state(self):
        """Remember the look for next launch. Written on close and after any
        change worth keeping, so a crash costs at most the last edit."""
        try:
            data = {
                "effect": self.effect,
                "palette_name": self.palette_name,
                "palettes": self.palettes,
                "custom": [list(c) for c in self.custom],
                "speed": self.speed.get(),
                "bars": self.bars.get(),
                "gain": self.gain.get(),
                "layer_mode": self.layer_mode,
                "layers": [{
                    "name": l.name, "effect": l.effect, "palette": l.palette,
                    "x": l.x, "y": l.y, "w": l.w, "h": l.h, "angle": l.angle,
                    "opacity": l.opacity, "blend": l.blend, "on": l.on,
                    "speed": l.speed,
                } for l in self.layers],
                "manual": [list(r.get("manual", (0, 0, 0))) for r in self.leds],
            }
            STATE.write_text(json.dumps(data, indent=1))
        except Exception as exc:
            self.say(f"could not save state: {type(exc).__name__}: {exc}")

    def load_state(self):
        """Restore the last look. Every field is optional and validated - a
        stale file from an older layout must not stop the app starting."""
        try:
            data = json.loads(STATE.read_text())
        except Exception:
            return
        try:
            self.palettes = dict(data.get("palettes") or {})
            self.palette_name = data.get("palette_name") or self.palette_name
            cust = data.get("custom")
            if isinstance(cust, list) and cust:
                self.custom = [tuple(c) for c in cust]
            for var, key in ((self.speed, "speed"), (self.bars, "bars"),
                             (self.gain, "gain")):
                if data.get(key) is not None:
                    try:
                        var.set(data[key])
                    except Exception:
                        pass
            # manual colours only if the layout still matches
            man = data.get("manual")
            if isinstance(man, list) and len(man) == len(self.leds):
                for r, c in zip(self.leds, man):
                    self.set_led(r, tuple(c), manual=True)
            for d in data.get("layers") or []:
                try:
                    lay = fx_layers.Layer(
                        d["effect"], d["x"], d["y"], d["w"], d["h"],
                        angle=d.get("angle", 0.0), palette=d.get("palette"),
                        opacity=d.get("opacity", 1.0),
                        blend=d.get("blend", "normal"),
                        name=d.get("name"), speed=d.get("speed", 1.0))
                    lay.on = bool(d.get("on", True))
                    lay.t0 = time.monotonic()
                    lay.reindex(self.leds)
                    self.layers.append(lay)
                except Exception:
                    continue
            self.set_bars()
            self.set_gain()
            eff = data.get("effect")
            if eff and eff in fx.SPATIAL:
                self.start_fx(eff)
            n = len(self.layers)
            self.say(f"restored: {eff or 'no effect'}"
                     + (f", {n} layer(s)" if n else ""))
        except Exception as exc:
            self.say(f"could not restore state: {type(exc).__name__}: {exc}")

    def close(self):
        self.save_state()
        # Cancel the pending callbacks first. Without this Tk tries to run
        # them after the interpreter is torn down and prints
        # 'invalid command name ...tick' on the way out.
        for attr in ("_tick_id", "_beat_id", "_pal_id"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self.effect = None
        try:
            fan_side.GPU.stop()
        except Exception:
            pass
        OVERRIDE.unlink(missing_ok=True)
        self.hw.stop_flag.set()
        self._destroy_id = self.root.after(300, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
