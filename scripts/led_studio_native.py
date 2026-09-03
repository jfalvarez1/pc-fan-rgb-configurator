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
import ctypes.wintypes          # not implied by `import ctypes`
import json
import math
import os
import pathlib
import queue
import sys
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
import usage_levels
from led_render import LedRenderer
from ui_widgets import RoundButton, Slider
import tray_icon

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

# Shared with the daemons, and stable when frozen - see app_paths.
import app_paths
from app_paths import DATA as BASE
OVERRIDE = BASE / "manual_override.flag"
STATE = BASE / "led_studio_state.json"

# Frozen, the editor and the player are the same executable told apart by a
# flag, so the player is found by exe name rather than by script name.
PLAYER_EXE_NAME = os.path.basename(sys.executable).lower() if app_paths.FROZEN \
    else "ledstudio.exe"

# Lets led_player reuse THIS module object when the frozen exe dispatches to
# it. Without the marker `from led_studio_native import ...` loads a second
# copy of this whole module - Tk, PIL, the renderer, the widgets - into a
# process that only needs the hardware thread.
_LED_STUDIO_MAIN = True


def log_start(what):
    """One line per launch, so a start that goes nowhere leaves a trace.

    Kept to the last 200 lines: this is a breadcrumb trail, not a log the user
    has to manage.
    """
    try:
        p = BASE / "led_studio_start.log"
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  pid={os.getpid():<6} "
                f"frozen={int(app_paths.FROZEN)}  {what}\n")
        old = p.read_text(encoding="utf-8").splitlines(True)[-199:] \
            if p.exists() else []
        p.write_text("".join(old) + line, encoding="utf-8")
    except Exception:
        pass


def focus_existing():
    """Bring the editor that is already running to the front.

    Called when this process loses the single-instance race. Matched on the
    window title AND on the pid not being ours, because the title is the only
    thing shared between a frozen exe and a pythonw script - the two ways this
    app can be running - and a window we own is not the one we are looking for.

    Best effort throughout: failing to raise a window is a cosmetic
    disappointment, whereas a second editor is a real problem, so this must
    never turn into a reason to keep running.
    """
    try:
        user32 = ctypes.windll.user32
        me = os.getpid()
        found = []

        def visit(hwnd, _):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == me:
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if not n:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == "LED Studio":
                    found.append(hwnd)
                    return False
            except Exception:
                pass
            return True

        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                                   ctypes.wintypes.LPARAM)
        user32.EnumWindows(proto(visit), 0)
        if found:
            user32.ShowWindow(found[0], 9)          # SW_RESTORE
            user32.SetForegroundWindow(found[0])
    except Exception:
        pass


def icon_path():
    """The window icon, wherever this happens to be running from.

    Frozen it is inside the bundle, so the exe carries its own icon even if it
    is copied somewhere with no scripts folder beside it. As a script it is at
    the top of the project. Callers still guard the iconbitmap call: the icon
    is cosmetic and must never stop the app starting.
    """
    for cand in (app_paths.bundle_dir() / "led_studio.ico",
                 BASE.parent / "led_studio.ico"):
        if cand.exists():
            return cand
    return BASE.parent / "led_studio.ico"
AUTO_CONTROL = True      # take the hardware on launch, release it on close
FLAG_BEAT_MS = 20000     # refresh the override flag this often while holding
HOST, PORT = "127.0.0.1", 6742

SCALE = 1.0
W = int(case_layout.CANVAS_W * SCALE)
H = int(case_layout.CANVAS_H * SCALE)
HW_FPS = 20.0            # hardware write rate; the canvas runs faster
UI_MS = 33               # ~30 fps effect clock
# The LED image is repainted more slowly than the effect runs. Compositing it
# costs ~8 ms and Tk then redraws a 1130x1120 picture on top of that, which at
# 30 fps is enough to make the window feel heavy. The hardware is only written
# at 20 Hz anyway, so painting the preview faster buys nothing.
PAINT_MS = 50            # ~20 fps for the rendered LED field


# ---- palette -------------------------------------------------------------
BG      = "#0b0d12"      # deeper ground so the lit LEDs carry the image
PANEL   = "#12151d"
CARD    = "#181c26"
LINE    = "#242a37"
INK     = "#eef1f7"
MUTED   = "#8b93a7"
ACCENT  = "#ff2d95"
ACCENT2 = "#7c5cff"      # violet, for the header rule
BTN     = "#1c2130"
BTN_HOV = "#262d40"
BTN_ON  = "#241b2c"      # an "on" toggle: tinted, not a slab of accent

# An unlit LED must still be visible against the background, or the layout
# reads as a field of empty holes.
LED_OFF      = "#242c3a"
LED_OFF_EDGE = "#39445a"

# Segoe UI Variable is the current Windows face; fall back where absent.
def _face():
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        for name in ("Segoe UI Variable Text", "Segoe UI"):
            if name in fams:
                return name
    except Exception:
        pass
    return "Segoe UI"


_F = _face()
FONT   = (_F, 11)
FONT_H = (_F, 9, "bold")
FONT_L = (_F, 10)
FONT_T = (_F, 16, "bold")


def mkbtn(parent, text, cmd, kind="normal", toggle=False):
    """Rounded flat button. Tk's own Button cannot lose its square corners
    or its 3D relief, which is most of what dates a Tk window.

    `kind="accent"` is a PRIMARY ACTION and is filled solid. Toggles use
    `toggle=True` instead: a status dot and a tinted background. Filling every
    toggle with the accent colour produced a stack of identical pink slabs,
    at which point the accent stopped marking anything out.
    """
    bg = {"normal": BTN, "accent": ACCENT, "ghost": PANEL}[kind]
    b = RoundButton(parent, text=text, command=cmd, bg=bg,
                    fg="#ffffff" if kind == "accent" else INK,
                    hover="#ff56ab" if kind == "accent" else BTN_HOV,
                    border=ACCENT if kind == "accent" else LINE,
                    dot=toggle, font=FONT)
    b._bg = bg
    b._toggle = toggle
    return b


def setbtn(b, active):
    """Mark a button on or off.

    A toggle shows an accent dot and accent text on a tinted background; it
    does not become a solid accent block, because that is what a primary
    action looks like and the two must stay distinguishable.
    """
    if getattr(b, "_toggle", False):
        b._bg = BTN_ON if active else BTN
        b.config(bg=b._bg, fg=ACCENT if active else MUTED,
                 hover=BTN_HOV, dot_on=bool(active),
                 highlightbackground=ACCENT if active else LINE)
        return
    b._bg = ACCENT if active else BTN
    b.config(bg=b._bg, fg="#ffffff" if active else INK,
             hover="#ff56ab" if active else BTN_HOV,
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

    def forget(self, name_fragment):
        """Drop a device from the written-state cache so the next frame is
        sent even if the colours match. Needed when a device stops being
        written: whatever it is holding is no longer what we think."""
        for el in case_layout.LAYOUT:
            if name_fragment in el.get("fx_group", "") or                name_fragment in el.get("device", "").lower():
                dev, _off = self.resolved.get(el["id"], (None, None))
                if dev is not None:
                    self.written.pop(dev.id, None)

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
        ico = icon_path()
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
        self.bright = tk.IntVar(value=100)     # master intensity
        self.selbright = tk.IntVar(value=100)  # for the selection
        # palette per effect, so each remembers its own look
        self.palettes = {}
        self.palette_name = "synthwave"
        self.custom = list(fx.SYNTHWAVE)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True)
        left = tk.Frame(wrap, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=10)
        tabs = tk.Frame(left, bg=BG)
        tabs.pack(fill="x", pady=(0, 10))
        self.tabbtns = {}
        for _n in ("Lighting", "Fans"):
            _b = mkbtn(tabs, _n, lambda n=_n: self.show_tab(n))
            _b.pack(side="left", padx=(0, 8), ipadx=10)
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

        # One anti-aliased image under everything, redrawn each frame. Tk
        # cannot antialias a canvas item, so the LEDs are composited in numpy
        # and uploaded as a single picture instead.
        self.renderer = LedRenderer(W, H, master=self.cv)
        self.led_img = self.cv.create_image(0, 0, anchor="nw")
        self.cv.tag_lower(self.led_img)
        self._dirty = True
        self._last_paint = 0.0

        # Minimised or otherwise not on screen. Tk sends <Unmap> when a window
        # is iconified and <Map> when it comes back, and those fire on child
        # widgets too, so the handler filters to the toplevel itself - binding
        # without that check made every panel that got packed or hidden look
        # like the window disappearing.
        self._hidden = False
        root.bind("<Unmap>", self._on_visibility, add="+")
        root.bind("<Map>", self._on_visibility, add="+")

        self._build_case()
        self._build_panel(self.panel_light)
        self.fan_side = fan_side.FanSidePanel(self.panel_fan, PANEL,
                                              on_change=self.say)

        self.status = tk.Label(root, text="starting...", anchor="w",
                               bg=BG, fg=MUTED, font=FONT_L)
        self.status.pack(fill="x", padx=20, pady=(0, 12))

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
        self.stop_player()          # never share the LEDs with a player
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
        # X hides to the notification area rather than exiting, so the
        # lighting keeps running in THIS process - no state to save and
        # reload, no second process to hand the animation to, and the flag is
        # never let go. Quit from the tray menu is the real exit.
        #
        # If the tray is unavailable for any reason, X closes the app as it
        # always did: an icon you cannot see must not become a window you
        # cannot close.
        self.tray = tray_icon.TrayIcon(
            root, on_open=self.restore_window, on_quit=self.close,
            icon=icon_path() if icon_path().exists() else None)
        if self.tray.start():
            root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        else:
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
                                      "manual": (0, 0, 0), "item": None, "gain": 1.0,
                                      "cell": case_layout.cell_of(el, i),
                                      "usrc": case_layout.usage_source(el)})
                    continue
                item = "led"     # drawn into the rendered image, not as an
            else:                #  item; kept non-None so selection works
                item = "led"
            rec = {"el": el, "i": i, "x": x, "y": y, "nx": nx, "ny": ny,
                   "rgb": (0, 0, 0), "manual": (0, 0, 0), "item": item, "gain": 1.0,
                   "cell": case_layout.cell_of(el, i),
                   "usrc": case_layout.usage_source(el)}
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
            wrap = tk.Frame(p, bg=PANEL)
            wrap.pack(fill="x", padx=16, pady=(18, 8))
            lb = tk.Label(wrap, text=t, bg=PANEL, fg=MUTED, font=FONT_H,
                          anchor="w")
            lb.pack(side="left")
            # hairline rule running to the right of the label - groups the
            # panel visually without boxing every section in a border
            tk.Frame(wrap, bg=LINE, height=1).pack(
                side="left", fill="x", expand=True, padx=(10, 0), pady=(7, 0))
            return lb

        def row():
            f = tk.Frame(p, bg=PANEL)
            f.pack(fill="x", padx=16, pady=2)
            return f

        tk.Label(p, text="LED STUDIO", bg=PANEL, fg=INK, font=FONT_T,
                 anchor="w").pack(fill="x", padx=16, pady=(18, 0))
        tk.Label(p, text=f"207 LEDs · 16 runs · {len(fx.SPATIAL)} effects",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w"
                 ).pack(fill="x", padx=16, pady=(1, 0))
        rule = tk.Canvas(p, height=3, bg=PANEL, highlightthickness=0, bd=0)
        rule.pack(fill="x", padx=16, pady=(10, 0))

        def _rule(_e=None):
            # accent fading into violet: one small piece of colour to anchor
            # the panel, rather than tinting every control
            rule.delete("all")
            w = max(1, rule.winfo_width())
            for i in range(w):
                f = i / w
                c = tuple(int(a + (b - a) * f) for a, b in
                          zip(App.hex2rgb(ACCENT), App.hex2rgb(ACCENT2)))
                rule.create_line(i, 0, i, 3, fill="#%02x%02x%02x" % c)
        rule.bind("<Configure>", _rule)

        head("HARDWARE")
        self.ctl_btn = mkbtn(p, "Take control", self.toggle_ctl,
                             toggle=True)
        self.ctl_btn.pack(fill="x", padx=16, pady=2)
        self.hw_var = tk.BooleanVar(value=True)
        self.kb_var = tk.BooleanVar(value=True)
        self.keep_var = tk.BooleanVar(value=True)   # lighting persists on exit
        self.hw_btn = mkbtn(p, "Drive hardware", self.toggle_hw,
                            toggle=True)
        self.hw_btn.pack(fill="x", padx=16, pady=2)
        self.kb_btn = mkbtn(p, "Light keyboard", self.toggle_kb,
                            toggle=True)
        self.kb_btn.pack(fill="x", padx=16, pady=2)
        self.keep_btn = mkbtn(p, "Keep lighting on exit", self.toggle_keep,
                              toggle=True)
        self.keep_btn.pack(fill="x", padx=16, pady=2)
        tk.Label(p, text="The keyboard is also an input device. Turn this\n"
                         "off if you get stuck or repeating keys - lighting\n"
                         "it means writing its USB HID endpoint.",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(0, 2))

        head("SELECTION")
        r = row()
        for txt, fn in (("All", self.sel_all), ("None", self.sel_none),
                        ("Invert", self.sel_inv)):
            mkbtn(r, txt, fn).pack(side="left", expand=True, fill="x", padx=2)
        self.brush_btn = mkbtn(p, "Brush", self.toggle_brush, toggle=True)
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

        head("INTENSITY")
        self.bright_lbl = tk.Label(p, text="Master intensity: 100%", bg=PANEL,
                                   fg=INK, font=FONT_L, anchor="w")
        self.bright_lbl.pack(fill="x", padx=16)
        Slider(p, from_=0, to=100, orient="horizontal", variable=self.bright,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_bright
                 ).pack(fill="x", padx=14)
        self.selbright_lbl = tk.Label(p, text="Selected LEDs: 100%", bg=PANEL,
                                      fg=MUTED, font=FONT_L, anchor="w")
        self.selbright_lbl.pack(fill="x", padx=16, pady=(6, 0))
        Slider(p, from_=0, to=100, orient="horizontal",
                 variable=self.selbright, bg=PANEL, fg=INK, troughcolor=BTN,
                 highlightthickness=0, bd=0, sliderrelief="flat",
                 activebackground=ACCENT, font=FONT_L, showvalue=False,
                 command=self.set_sel_bright).pack(fill="x", padx=14)
        tk.Label(p, text="Master scales everything. The second slider sets a\n"
                         "per-LED level for whatever is selected, so one fan\n"
                         "can sit dimmer than the rest. Both are multiplied.",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(2, 2))
        mkbtn(p, "Reset intensities", self.reset_intensity, "ghost"
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
        # Wrapped onto rows of four. Seven across a 344px panel left 43px per
        # button, which is narrower than the word "Scatter".
        cats = list(self.groups)
        per = 4
        for start in range(0, len(cats), per):
            cr = row()
            for cat in cats[start:start + per]:
                b = mkbtn(cr, cat, lambda c=cat: self.show_cat(c))
                b.config(padx=2, font=FONT_L)
                b.pack(side="left", expand=True, fill="x", padx=2)
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
        Slider(p, from_=0.1, to=8.0, resolution=0.1, orient="horizontal",
                 variable=self.speed, bg=PANEL, fg=INK, troughcolor=BTN,
                 highlightthickness=0, bd=0, sliderrelief="flat",
                 activebackground=ACCENT, font=FONT_L
                 ).pack(fill="x", padx=14)
        # VU bar count - only meaningful for the meter effects, so it says so
        self.bars_lbl = tk.Label(p, text="VU bars: 8", bg=PANEL, fg=MUTED,
                                 font=FONT_L, anchor="w")
        self.bars_lbl.pack(fill="x", padx=16, pady=(8, 0))
        self.bars = tk.IntVar(value=getattr(fx, "VU_BARS", 8))
        Slider(p, from_=2, to=20, orient="horizontal", variable=self.bars,
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
        Slider(p, from_=3, to=30, orient="horizontal", variable=self.gain,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_gain
                 ).pack(fill="x", padx=14)

        self.apply_btn = mkbtn(p, "Apply now", self.apply_now, "accent")
        self.apply_btn.pack(fill="x", padx=16, pady=(6, 2))
        tk.Label(p, text="Re-sends the current frame to the hardware, and\n"
                         "restarts the effect if it had stopped.",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(0, 2))

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
        self.lyr_btn = mkbtn(p, "Layer mode", self.toggle_layers,
                             toggle=True)
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
        Slider(p, from_=5, to=100, orient="horizontal", variable=self.opacity,
                 bg=PANEL, fg=INK, troughcolor=BTN, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground=ACCENT,
                 font=FONT_L, showvalue=False, command=self.set_layer_opacity
                 ).pack(fill="x", padx=14)
        self.wpm_lbl = tk.Label(p, text="Max typing speed: 200 wpm", bg=PANEL,
                                fg=MUTED, font=FONT_L, anchor="w")
        self.wpm_lbl.pack(fill="x", padx=16, pady=(10, 0))
        self.wpmcap = tk.IntVar(value=int(usage_levels.WPM_CAP))
        Slider(p, from_=int(usage_levels.WPM_CAP_MIN),
                 to=int(usage_levels.WPM_CAP_MAX), resolution=5,
                 orient="horizontal", variable=self.wpmcap, bg=PANEL, fg=INK,
                 troughcolor=BTN, highlightthickness=0, bd=0,
                 sliderrelief="flat", activebackground=ACCENT, font=FONT_L,
                 showvalue=False, command=self.set_wpm_cap
                 ).pack(fill="x", padx=14)
        tk.Label(p, text="The speed that reads as fully red on the keyboard\n"
                         "in the usage effect. Lower it if you never reach\n"
                         "the top of the scale.",
                 bg=PANEL, fg=MUTED, font=FONT_L, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(0, 2))

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
        # Drop the old entries with the widgets. They were left behind and
        # only removed later, lazily, when configuring one raised TclError -
        # so the dict briefly described buttons that no longer existed.
        self.fxbtns.clear()
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

    def set_wpm_cap(self, _v=None):
        got = usage_levels.SHARED.set_cap(self.wpmcap.get())
        self.wpm_lbl.config(text=f"Max typing speed: {got:.0f} wpm")

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
            top.iconbitmap(str(icon_path()))
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
        # The selection ring is drawn into the rendered image, so there is
        # nothing to reconfigure here - just ask for a repaint.
        self._dirty = True
        self.repaint(force=True)

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
        hit = False
        for r in self.leds:
            if (r["x"] - x) ** 2 + (r["y"] - y) ** 2 < 90:
                self.set_led(r, rgb, manual=True)
                hit = True
        if hit:
            self.push()

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
        # Intensity scales what is EMITTED, never the intended colour. Keeping
        # them apart means turning brightness down and back up is lossless -
        # scaling r["rgb"] in place would quantise the colour away a little
        # more on every adjustment.
        r["out"] = self.scaled(r)
        if r["item"] is None:
            return                      # matrix gap: addressed, never drawn
        self._dirty = True

    def scaled(self, r):
        """Emitted colour: intended colour x master intensity x this LED's own."""
        f = (self.bright.get() / 100.0) * r.get("gain", 1.0)
        f = max(0.0, min(1.0, f))       # never trust a variable to be in range
        if f >= 0.999:
            return r["rgb"]
        return tuple(max(0, min(255, int(v * f))) for v in r["rgb"])

    def reapply(self):
        """Recompute every LED's output after an intensity change."""
        for r in self.leds:
            self.set_led(r, r["rgb"])
        self.push()

    def set_bright(self, _v=None):
        self.bright_lbl.config(text=f"Master intensity: {self.bright.get()}%")
        self.reapply()

    def set_sel_bright(self, _v=None):
        pct = self.selbright.get()
        self.selbright_lbl.config(
            text=f"Selected LEDs: {pct}%"
            + ("" if self.sel else "   (nothing selected)"))
        if not self.sel:
            return
        for r in self.leds:
            if (r["el"]["id"], r["i"]) in self.sel:
                r["gain"] = pct / 100.0
        self.reapply()

    def reset_intensity(self):
        for r in self.leds:
            r["gain"] = 1.0
        self.bright.set(100)
        self.selbright.set(100)
        self.bright_lbl.config(text="Master intensity: 100%")
        self.selbright_lbl.config(text="Selected LEDs: 100%")
        self.reapply()
        self.say("all intensities reset to 100%")

    def paint_sel(self, rgb=None):
        rgb = rgb or self.hex2rgb(self.colour)
        for r in self.leds:
            if (r["el"]["id"], r["i"]) in self.sel:
                self.set_led(r, rgb, manual=True)
        # tick() only pushes while an effect or a layer is live, so without
        # this a painted colour reached the canvas and never the hardware.
        self.push()

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

    def start_player(self):
        """Hand the running animation to led_player so it carries on.

        Without this, keeping the lighting froze the last frame - the colours
        stayed but the movement stopped, which is not what "keep it running"
        means to anyone watching the case.
        """
        try:
            import subprocess
            exe = sys.executable
            if app_paths.FROZEN:
                # Frozen, sys.executable IS this app, so re-launch ourselves
                # with a flag instead of hunting for a python interpreter and
                # a .py file that are not there any more.
                argv = [exe, "--player"]
            else:
                if exe.lower().endswith("python.exe"):
                    exe = exe[:-len("python.exe")] + "pythonw.exe"
                # An ABSOLUTE path from this file, not "led_player.py"
                # relative to the data directory. Those are the same folder
                # today, but they are two different ideas - one is where the
                # code lives, the other is where the settings live - and the
                # moment they diverge the relative form silently launches
                # nothing at all.
                argv = [exe, str(pathlib.Path(__file__).resolve()
                                 .with_name("led_player.py"))]
            subprocess.Popen(
                argv, cwd=str(pathlib.Path(argv[-1]).parent
                              if not app_paths.FROZEN else BASE),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self.say(f"could not start the player: {type(exc).__name__}")

    def stop_player(self):
        """Stop any player before taking the LEDs, so two processes never
        drive them at once."""
        try:
            import psutil
        except Exception:
            return
        me = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == me:
                    continue
                # Match on an actual ARGUMENT, never on a substring of the
                # joined command line. The substring form matched anything
                # that merely mentioned the name - an editor with the file
                # open, a grep, or the very shell that launched this - and
                # terminated it. Killing a bystander because its command line
                # contains a filename is not a cleanup, it is a hazard.
                #
                # There are two shapes to match, because there are two ways
                # the player can have been started:
                #   script : pythonw.exe ... led_player.py
                #   frozen : LEDStudio.exe --player
                # Matching only the first left the frozen player running while
                # the editor took the LEDs back - two processes writing the
                # same devices, which is the exact failure this guards.
                name = (proc.info.get("name") or "").lower()
                argv = proc.info.get("cmdline") or []
                if name.startswith("python"):
                    hit = any(os.path.basename(a).lower() == "led_player.py"
                              for a in argv)
                elif name == PLAYER_EXE_NAME:
                    hit = "--player" in argv
                else:
                    continue
                if hit:
                    proc.terminate()
            except Exception:
                continue

    def apply_now(self):
        """Force everything to the hardware right now.

        Takes control and enables writing if they were off, forgets what each
        device is believed to be holding so the next frame is sent even if the
        colours match, restarts the effect if one is selected, and pushes.
        """
        if not self.controlling:
            self.toggle_ctl()
        if not self.hw_var.get():
            self.toggle_hw()
        self.hw.written.clear()          # re-send even if nothing changed
        if self.effect or any(l.on for l in self.layers):
            self.t0 = time.monotonic()
            self.tick()
            what = self.effect or f"{sum(1 for l in self.layers if l.on)} layer(s)"
        else:
            for r in self.leds:
                self.set_led(r, r["rgb"])
            self.push()
            what = "current colours"
        self.say(f"applied {what} to the hardware")

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

    def toggle_keep(self):
        self.keep_var.set(not self.keep_var.get())
        setbtn(self.keep_btn, self.keep_var.get())
        self.say("lighting will stay as-is after closing"
                 if self.keep_var.get()
                 else "on closing, the daemon takes the LEDs back")

    def toggle_kb(self):
        self.kb_var.set(not self.kb_var.get())
        setbtn(self.kb_btn, self.kb_var.get())
        if not self.kb_var.get():
            self.hw.forget("keyboard")
        self.say("keyboard lighting on" if self.kb_var.get()
                 else "keyboard lighting OFF - it is left alone entirely")
        self.push()

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
            frame.setdefault(r["el"]["id"], []).append(
                r.get("out", r["rgb"]))
        # The keyboard is the one device here that is also an INPUT device.
        # Lighting it means writing its USB HID endpoint, and a keyboard that
        # is busy servicing those writes can drop or repeat keystrokes. Turn
        # this off and the keyboard is left alone entirely - everything else
        # keeps working.
        if not self.kb_var.get():
            for el in case_layout.LAYOUT:
                if el.get("fx_group") == "keyboard":
                    frame.pop(el["id"], None)
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

    def hide_to_tray(self):
        """X was pressed: go to the tray, keep everything running.

        The state file is written on the way down so a later crash or a
        power cut costs nothing, exactly as a real close would.
        """
        self.save_state()
        self.tray.hide()
        self._hidden = True
        self.say("minimised to the tray - lighting is still running")

    def restore_window(self):
        self.tray.show()
        self._hidden = False
        self._dirty = True
        self.repaint(force=True)

    def _on_visibility(self, ev):
        """Track whether the window is actually on screen.

        `state()` is the authority rather than the event type: a window can be
        unmapped for reasons other than minimising, and asking outright avoids
        having to enumerate them.
        """
        if ev.widget is not self.root:
            return                      # a child widget, not the window
        try:
            hidden = self.root.state() in ("iconic", "withdrawn")
        except Exception:
            hidden = False
        if hidden == self._hidden:
            return
        self._hidden = hidden
        if not hidden:
            self._dirty = True
            self.repaint(force=True)    # come back to a current picture

    def repaint(self, force=False):
        """Composite the LED field, at most every PAINT_MS.

        Skipped entirely when nothing has changed, so a static image costs
        nothing at all rather than re-rendering the same picture 30 times a
        second.
        """
        if not (self._dirty or force):
            return
        # Minimised, there is nobody to show it to. Compositing the LED field
        # is the single most expensive thing this app does - supersampled
        # masks, a numpy max-composite and a PhotoImage upload, every 50 ms -
        # and Tk will happily keep doing all of it into a window that is not
        # on screen. The EFFECT clock and the hardware writes are untouched,
        # so the case keeps animating; only the picture of it stops.
        if self._hidden:
            self._dirty = True       # so the first frame back is drawn
            return
        now = time.monotonic()
        if not force and (now - self._last_paint) * 1000.0 < PAINT_MS:
            return
        self._last_paint = now
        self._dirty = False
        try:
            photo = self.renderer.render(self.leds, selected=self.sel)
            self.cv.itemconfigure(self.led_img, image=photo)
        except Exception as exc:
            self.say(f"render error: {type(exc).__name__}: {exc}")

    def tick(self):
        try:
            live = [l for l in self.layers if l.on and l.effect in fx.SPATIAL]
            if self.effect or live:
                t = (time.monotonic() - self.t0) * self.speed.get()
                pal = self.active_palette()
                fn = fx.SPATIAL[self.effect] if self.effect else None
                # resolved once per frame, not per LED
                base_cells = self.effect in fx.CELL_AWARE
                base_usage = self.effect in fx.USAGE_AWARE
                if base_usage or any(l.effect in fx.USAGE_AWARE for l in live):
                    usage_levels.SHARED.start()
                    # Derived from the mapping itself, never a hand-written
                    # list. A hard-coded ("cpu","gpu","ram","all") went stale
                    # the moment the keyboard was pointed at a new "wpm"
                    # source: the lookup raised KeyError on the first frame,
                    # tick() swallowed it, and the whole effect silently
                    # stopped reaching the hardware.
                    lv = {k: usage_levels.SHARED.value(k)
                          for k in set(case_layout.USAGE_SOURCES.values())}
                else:
                    lv = None
                # Base pass. With no global effect the manually painted
                # colour is the background, so layers composite over painting
                # instead of erasing it.
                for r in self.leds:
                    if fn is None:
                        r["c"] = r.get("manual", (0, 0, 0))
                    elif base_usage:
                        r["c"] = fn(r["nx"], r["ny"], t, pal,
                                    usage=lv[r["usrc"]])
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
                    uses = lay.effect in fx.USAGE_AWARE
                    for r in lay.members:
                        u, v = lay.local(r["x"], r["y"])
                        if uses:
                            col = lfn(u, v, lt, lpal, usage=lv[r["usrc"]])
                        elif cells and r["cell"]:
                            col = lfn(u, v, lt, lpal, cell=r["cell"])
                        else:
                            col = lfn(u, v, lt, lpal)
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
        self.repaint()

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
                "light_keyboard": bool(self.kb_var.get()),
                "keep_on_exit": bool(self.keep_var.get()),
                "wpm_cap": int(self.wpmcap.get()),
                "brightness": int(self.bright.get()),
                "gains": [round(float(r.get("gain", 1.0)), 3)
                          for r in self.leds],
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
            if data.get("wpm_cap"):
                try:
                    self.wpmcap.set(int(data["wpm_cap"]))
                    self.set_wpm_cap()
                except Exception:
                    pass
            if data.get("keep_on_exit") is False:
                self.keep_var.set(False)
                setbtn(self.keep_btn, False)
            if data.get("light_keyboard") is False:
                self.kb_var.set(False)
                setbtn(self.kb_btn, False)
            if data.get("brightness") is not None:
                try:
                    self.bright.set(int(data["brightness"]))
                    self.bright_lbl.config(
                        text=f"Master intensity: {self.bright.get()}%")
                except Exception:
                    pass
            gains = data.get("gains")
            if isinstance(gains, list) and len(gains) == len(self.leds):
                for r, g in zip(self.leds, gains):
                    try:
                        r["gain"] = max(0.0, min(1.0, float(g)))
                    except (TypeError, ValueError):
                        r["gain"] = 1.0
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
        # Take the tray icon down first. Leaving it behind gives Windows an
        # icon for a process that no longer exists - it lingers until the
        # user hovers over it, which looks exactly like the app failed to
        # exit.
        try:
            self.tray.stop()
        except Exception:
            pass
        # Recorded BEFORE self.effect is cleared further down, or the handoff
        # would always see "nothing was running" and never start.
        animating = bool(self.effect or any(l.on for l in self.layers))
        if self.keep_var.get() and self.controlling:
            # Push the final frame and give the hardware thread a moment to
            # write it, THEN mark the flag as held. Without the wait the
            # process can exit before the write lands, and the LEDs keep
            # whatever the previous frame was.
            try:
                self.push()
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    with self.hw.lock:
                        if self.hw.pending is None:
                            break
                    time.sleep(0.05)
                OVERRIDE.write_text(
                    "led_studio_native\nscope=leds\nhold=1\n")
            except Exception:
                pass
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
            usage_levels.SHARED.stop()
        except Exception:
            pass
        self._holding = bool(self.keep_var.get() and self.controlling)
        if not getattr(self, "_holding", False):
            OVERRIDE.unlink(missing_ok=True)
        self.hw.stop_flag.set()
        if self._holding and animating:
            self.start_player()
        self._destroy_id = self.root.after(300, self.root.destroy)


def main():
    # Frozen, there is only one executable, so it carries both entry points.
    # The editor hands its animation to the player by re-launching itself with
    # this flag rather than looking for a python interpreter that a standalone
    # build does not have.
    if "--player" in sys.argv:
        import led_player
        return led_player.main()

    # One editor, always. There is a shortcut in the Startup folder and one on
    # the desktop, both pointing here, so "the user clicks the icon while the
    # logon copy is still coming up" is the ordinary case, not a corner. Two
    # editors would both claim the override flag, both drive the same LEDs and
    # both write led_studio_state.json - the last one to close winning. The
    # daemons have had this guard since two of them were caught fighting over
    # the radiator header; the editor never did.
    import single_instance
    if not single_instance.claim("LEDStudio"):
        # Silently exiting looks like a broken icon. Raise the window that is
        # already open instead, which is what the click meant anyway.
        log_start("another instance holds the lock - focusing it")
        focus_existing()
        return 0

    log_start("starting")
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A windowed build has no console, so an unhandled exception here is a
        # window that never appears and an exit code nobody sees. That is not
        # debuggable from the outside - it already cost an afternoon. Write the
        # traceback where the rest of the state lives, then re-raise so the
        # behaviour is otherwise unchanged.
        import traceback
        try:
            with open(BASE / "led_studio_crash.log", "a",
                      encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"frozen={app_paths.FROZEN} argv={sys.argv}\n")
                traceback.print_exc(file=fh)
        except Exception:
            pass
        raise
