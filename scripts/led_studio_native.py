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
import pathlib
import queue
import threading
import time
import tkinter as tk
from tkinter import colorchooser, ttk

import case_layout
import openrgb_boot
import rgb_effects as fx

BASE = pathlib.Path(__file__).resolve().parent
OVERRIDE = BASE / "manual_override.flag"
HOST, PORT = "127.0.0.1", 6742

SCALE = 1.15
W = int(case_layout.CANVAS_W * SCALE)
H = int(case_layout.CANVAS_H * SCALE)
HW_FPS = 20.0            # hardware write rate; the canvas runs faster
UI_MS = 33               # ~30 fps canvas

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
        root.configure(bg="#0d0f14")
        try:
            root.iconbitmap(str(BASE.parent / "led_studio.ico"))
        except Exception:
            pass    # icon is cosmetic; never let it stop the app starting

        self.out = queue.Queue()
        self.hw = Hardware(self.out)
        self.leds = []          # {el, i, x, y, nx, ny, rgb, item}
        self.byel = {}
        self.sel = set()
        self.order = []
        self.effect = None
        self.t0 = time.monotonic()
        self.frames = 0
        self.brush = False
        self.drag = None
        self.marq = None
        self.controlling = False

        wrap = tk.Frame(root, bg="#0d0f14")
        wrap.pack(fill="both", expand=True)
        self.cv = tk.Canvas(wrap, width=W, height=H, bg="#0d0f14",
                            highlightthickness=0)
        self.cv.pack(side="left", fill="both", expand=True)
        side = tk.Frame(wrap, bg="#161a22", width=280)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)

        self._build_case()
        self._build_panel(side)

        self.status = tk.Label(root, text="starting…", anchor="w",
                               bg="#0d0f14", fg="#8b93a3", font=("Segoe UI", 9))
        self.status.pack(fill="x", padx=8, pady=(0, 6))

        self.cv.bind("<Button-1>", self.on_down)
        self.cv.bind("<B1-Motion>", self.on_move)
        self.cv.bind("<ButtonRelease-1>", self.on_up)

        self.hw.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(UI_MS, self.tick)

    # ---------- layout

    def _build_case(self):
        c, s = self.cv, SCALE
        c.create_rectangle(24*s, 24*s, 636*s, 556*s, outline="#2b3341", width=2)
        c.create_line(150*s, 24*s, 150*s, 556*s, fill="#222936", dash=(5, 5))
        c.create_rectangle(195*s, 392*s, 415*s, 458*s,
                           outline="#2b3341", fill="#1b2029")
        for txt, x, y in (("TOP - radiator exhaust", 370, 14),
                          ("BOTTOM - F420 intake", 355, 570),
                          ("RTX 5090 (vertical)", 305, 452),
                          ("cable chamber", 87, 40)):
            c.create_text(x*s, y*s, text=txt, fill="#8b93a3",
                          font=("Segoe UI", 8))

        for el, i, nx, ny in case_layout.led_positions():
            x, y = case_layout._ring_xy(el, i)
            r = 4.6 if el["count"] <= 12 else 3.6
            item = c.create_oval((x-r)*s, (y-r)*s, (x+r)*s, (y+r)*s,
                                 fill="#11151c", outline="")
            rec = {"el": el, "i": i, "x": x*s, "y": y*s,
                   "nx": nx, "ny": ny, "rgb": (0, 0, 0), "item": item}
            self.leds.append(rec)
            self.byel.setdefault(el["id"], []).append(rec)

        for el_id, recs in self.byel.items():
            el = recs[0]["el"]
            c.create_text(el["x"]*s, (el["y"] + (el.get("r") or 26) + 15)*s,
                          text=el["label"], fill="#6d7687", font=("Segoe UI", 8))

    def _build_panel(self, p):
        def head(t):
            tk.Label(p, text=t, bg="#161a22", fg="#8b93a3",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12,
                                                        pady=(12, 4))
        head("HARDWARE")
        self.ctl_btn = tk.Button(p, text="Take control", command=self.toggle_ctl)
        self.ctl_btn.pack(fill="x", padx=12)
        self.hw_var = tk.BooleanVar(value=True)
        tk.Checkbutton(p, text="Drive hardware", variable=self.hw_var,
                       bg="#161a22", fg="#e6e9ef", selectcolor="#212836",
                       activebackground="#161a22", activeforeground="#e6e9ef"
                       ).pack(anchor="w", padx=10)

        head("SELECTION")
        row = tk.Frame(p, bg="#161a22"); row.pack(fill="x", padx=12)
        for txt, fn in (("All", self.sel_all), ("None", self.sel_none),
                        ("Invert", self.sel_inv)):
            tk.Button(row, text=txt, width=6, command=fn).pack(side="left", padx=1)
        self.brush_btn = tk.Button(p, text="Brush: off", command=self.toggle_brush)
        self.brush_btn.pack(fill="x", padx=12, pady=(4, 0))

        head("COLOUR")
        self.colour = "#ff3aa2"
        sw = tk.Frame(p, bg="#161a22"); sw.pack(fill="x", padx=12)
        for h in SWATCHES:
            tk.Button(sw, bg=h, width=2, relief="flat",
                      command=lambda x=h: self.set_colour(x)).pack(side="left", padx=1)
        tk.Button(p, text="Pick colour…", command=self.pick).pack(fill="x", padx=12, pady=(4, 0))
        tk.Button(p, text="Paint selection", command=self.paint_sel).pack(fill="x", padx=12, pady=(4, 0))
        tk.Button(p, text="Blank selection", command=lambda: self.paint_sel((0, 0, 0))).pack(fill="x", padx=12, pady=(3, 0))

        head("ANIMATIONS")
        g = tk.Frame(p, bg="#161a22"); g.pack(fill="x", padx=12)
        for n, name in enumerate(sorted(fx.SPATIAL)):
            tk.Button(g, text=name, width=8,
                      command=lambda x=name: self.start_fx(x)
                      ).grid(row=n // 2, column=n % 2, padx=1, pady=1)
        self.speed = tk.DoubleVar(value=1.0)
        tk.Scale(p, from_=0.2, to=3.0, resolution=0.1, orient="horizontal",
                 variable=self.speed, bg="#161a22", fg="#8b93a3",
                 troughcolor="#212836", highlightthickness=0, label="speed"
                 ).pack(fill="x", padx=12)
        tk.Button(p, text="Stop animation", command=self.stop_fx).pack(fill="x", padx=12)
        tk.Button(p, text="All OFF", command=self.all_off).pack(fill="x", padx=12, pady=(8, 12))

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
            self.cv.itemconfig(r["item"],
                               outline="#ffffff" if key in self.sel else "",
                               width=2 if key in self.sel else 0)

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
                           fill="#11151c" if dark else "#%02x%02x%02x" % r["rgb"])

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
        self.effect = name
        self.t0 = time.monotonic()
        self.frames = 0
        self.say(f"animating: {name}")

    def stop_fx(self):
        self.effect = None
        self.say("animation stopped")

    def toggle_brush(self):
        self.brush = not self.brush
        self.brush_btn.config(text=f"Brush: {'ON' if self.brush else 'off'}")

    def toggle_ctl(self):
        self.controlling = not self.controlling
        if self.controlling:
            OVERRIDE.write_text("led_studio_native")
        else:
            OVERRIDE.unlink(missing_ok=True)
        self.ctl_btn.config(text="Release to daemon" if self.controlling
                            else "Take control")

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
                for r in self.leds:
                    self.set_led(r, fn(r["nx"], r["ny"], t, fx.SYNTHWAVE))
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
