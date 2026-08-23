"""Hardware dashboard - control and label every detected fan and RGB device.

    python dashboard.py

While it is open it writes manual_override.flag, which makes thermal_rgb_loop
stand down so the two never fight over the hardware. Closing the window
removes the flag and the daemon resumes its curves and effects.

Fan labels  -> fan_map.json
RGB labels  -> rgb_labels.json
Zone sizes  -> rgb_zone_sizes.json  (re-applied on every launch)

ZONE RESIZING
-------------
OpenRGB cannot detect how many LEDs sit on a motherboard ARGB header, so
those zones report size 0 and nothing is ever sent to them. Set the count
here to bring them to life.
"""
import json
import pathlib
import queue
import threading
import tkinter as tk
from tkinter import colorchooser, ttk

import nzxt_util as nz

BASE = pathlib.Path(__file__).resolve().parent
FAN_MAP = BASE / "fan_map.json"
RGB_LABELS = BASE / "rgb_labels.json"
ZONE_SIZES = BASE / "rgb_zone_sizes.json"
OVERRIDE = BASE / "manual_override.flag"

CHANNELS = ["fan1", "fan2", "fan3"]
HOST, PORT = "127.0.0.1", 6742


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, data):
    path.write_text(json.dumps(data, indent=2))


class Worker(threading.Thread):
    """Owns both hardware handles. All device I/O happens here."""

    def __init__(self, cmd_q, out_q):
        super().__init__(daemon=True)
        self.cmd_q, self.out_q = cmd_q, out_q
        self.stop_flag = threading.Event()
        self.dev = None
        self.client = None

    # ---- setup

    def _connect_rgb(self):
        try:
            from openrgb import OpenRGBClient
            self.client = OpenRGBClient(HOST, PORT, "dashboard")
            sizes = load(ZONE_SIZES, {})
            for d in self.client.devices:
                for z in d.zones:
                    key = f"{d.name}|{z.name}"
                    want = sizes.get(key)
                    if want and len(z.leds) != want:
                        try:
                            z.resize(want)
                            self.out_q.put(("log", f"resized {key} -> {want}"))
                        except Exception as exc:
                            self.out_q.put(("log", f"resize {key} failed: {exc}"))
            self.client.update()
            self.out_q.put(("devices", [
                {"id": d.id, "name": d.name, "type": d.type.name,
                 "leds": len(d.leds),
                 "modes": [m.name for m in d.modes],
                 "zones": [{"id": z.id, "name": z.name, "leds": len(z.leds)}
                           for z in d.zones]}
                for d in self.client.devices]))
        except Exception as exc:
            self.out_q.put(("log", f"OpenRGB unavailable: {exc}"))
            self.out_q.put(("devices", []))

    def run(self):
        import time
        self.dev = nz.find_nzxt()
        if self.dev is None:
            self.out_q.put(("log", "no NZXT controller found"))
        self._connect_rgb()

        ctx = self.dev.connect() if self.dev else None
        if ctx:
            ctx.__enter__()
            self.dev.initialize()
            self.out_q.put(("log", f"fans: {self.dev.description}"))

        next_poll = 0.0
        try:
            while not self.stop_flag.is_set():
                now = time.monotonic()
                try:
                    while True:
                        kind, payload = self.cmd_q.get_nowait()
                        self._handle(kind, payload)
                except queue.Empty:
                    pass

                if self.dev and now >= next_poll:
                    try:
                        self.out_q.put(("fans", {
                            "duties": nz.read_duties(self.dev),
                            "speeds": nz.read_speeds(self.dev)}))
                    except Exception as exc:
                        self.out_q.put(("log", f"fan poll error: {exc}"))
                    next_poll = now + 1.0
                time.sleep(0.05)
        finally:
            if ctx:
                ctx.__exit__(None, None, None)
            self.out_q.put(("closed", None))

    # ---- commands

    def _handle(self, kind, payload):
        if kind == "fan":
            ch, duty = payload
            ok = nz.set_duty(self.dev, ch, duty)
            self.out_q.put(("log", f"{ch} -> {duty}%"
                                   f"{'' if ok else '  [WRITE DROPPED]'}"))

        elif kind == "colour":
            dev_id, zone_id, rgb = payload
            if self.client is None:
                self.out_q.put(("log", "no OpenRGB connection"))
                return
            try:
                from openrgb.utils import RGBColor
                d = self.client.devices[dev_id]
                for want in ("direct", "static"):
                    try:
                        d.set_mode(want)
                        break
                    except Exception:
                        continue
                c = RGBColor(*rgb)
                if zone_id is None:
                    d.set_colors([c] * len(d.leds), fast=True)
                    where = d.name
                else:
                    z = d.zones[zone_id]
                    if not len(z.leds):
                        self.out_q.put(("log",
                                        f"{z.name}: 0 LEDs - set its size first"))
                        return
                    z.set_colors([c] * len(z.leds), fast=True)
                    where = f"{d.name}/{z.name}"
                self.out_q.put(("log", f"{where} -> rgb{rgb}"))
            except Exception as exc:
                self.out_q.put(("log", f"colour failed: {exc}"))

        elif kind == "resize":
            dev_id, zone_id, size = payload
            try:
                d = self.client.devices[dev_id]
                z = d.zones[zone_id]
                z.resize(size)
                sizes = load(ZONE_SIZES, {})
                sizes[f"{d.name}|{z.name}"] = size
                save(ZONE_SIZES, sizes)
                self.out_q.put(("log", f"{d.name}/{z.name} resized to {size}"))
                self._connect_rgb()
            except Exception as exc:
                self.out_q.put(("log", f"resize failed: {exc}"))

        elif kind == "rescan":
            self._connect_rgb()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Hardware Dashboard")
        root.geometry("900x760")

        OVERRIDE.write_text("dashboard")      # daemon stands down

        self.cmd_q, self.out_q = queue.Queue(), queue.Queue()
        self.worker = Worker(self.cmd_q, self.out_q)

        self.fan_names = load(FAN_MAP, {})
        self.rgb_names = load(RGB_LABELS, {})
        self.fan_widgets = {}
        self.rgb_rows = []

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.fan_tab = ttk.Frame(nb)
        self.rgb_tab = ttk.Frame(nb)
        nb.add(self.fan_tab, text="Fans")
        nb.add(self.rgb_tab, text="RGB")

        self._build_fans()
        self.rgb_container = ttk.Frame(self.rgb_tab)
        self.rgb_container.pack(fill="both", expand=True)
        ttk.Label(self.rgb_container, text="connecting to OpenRGB...").pack(pady=20)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="Save labels", command=self.save_labels).pack(side="left")
        ttk.Button(bar, text="Rescan RGB",
                   command=lambda: self.cmd_q.put(("rescan", None))).pack(side="left", padx=6)
        self.ov = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Daemon paused (manual control)",
                        variable=self.ov, command=self.toggle_override).pack(side="left", padx=12)

        self.logbox = tk.Text(root, height=9, wrap="word")
        self.logbox.pack(fill="both", expand=False, padx=8, pady=8)

        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain)

    # ---- fans

    def _build_fans(self):
        for ch in CHANNELS:
            f = ttk.LabelFrame(self.fan_tab, text=ch.upper())
            f.pack(fill="x", padx=8, pady=6)
            top = ttk.Frame(f); top.pack(fill="x", padx=6, pady=4)

            rpm = ttk.Label(top, text="---- rpm", width=11, font=("Consolas", 13, "bold"))
            rpm.pack(side="left")
            duty = ttk.Label(top, text="--%", width=6, font=("Consolas", 11))
            duty.pack(side="left")
            ttk.Label(top, text="label:").pack(side="left", padx=(10, 2))
            var = tk.StringVar(value=self.fan_names.get(ch, ""))
            ttk.Entry(top, textvariable=var, width=30).pack(side="left")

            mid = ttk.Frame(f); mid.pack(fill="x", padx=6, pady=4)
            sc = ttk.Scale(mid, from_=20, to=100, orient="horizontal")
            sc.set(30); sc.pack(side="left", fill="x", expand=True)
            sc.bind("<ButtonRelease-1>", lambda e, c=ch: self.set_fan(c, None))
            for lbl, val in (("25", 25), ("50", 50), ("75", 75), ("100", 100)):
                ttk.Button(mid, text=lbl, width=4,
                           command=lambda c=ch, v=val: self.set_fan(c, v)).pack(side="left", padx=2)

            self.fan_widgets[ch] = {"rpm": rpm, "duty": duty, "name": var, "scale": sc}

    def set_fan(self, ch, duty):
        w = self.fan_widgets[ch]
        if duty is None:
            duty = int(float(w["scale"].get()))
        else:
            w["scale"].set(duty)
        self.cmd_q.put(("fan", (ch, duty)))

    # ---- rgb

    def build_rgb(self, devices):
        for child in self.rgb_container.winfo_children():
            child.destroy()
        self.rgb_rows = []

        if not devices:
            ttk.Label(self.rgb_container,
                      text="No OpenRGB devices.\nIs the SDK server running?  "
                           "Is SignalRGB holding the hardware?",
                      justify="left").pack(pady=20)
            return

        canvas = tk.Canvas(self.rgb_container, highlightthickness=0)
        sb = ttk.Scrollbar(self.rgb_container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        for d in devices:
            f = ttk.LabelFrame(inner, text=f"[{d['id']}] {d['name']}  ({d['type']})")
            f.pack(fill="x", padx=8, pady=6)

            top = ttk.Frame(f); top.pack(fill="x", padx=6, pady=4)
            ttk.Label(top, text=f"{d['leds']} LEDs", width=10).pack(side="left")
            direct = "Direct" in d["modes"]
            ttk.Label(top, text="Direct" if direct else "STATIC only",
                      foreground="green" if direct else "orange").pack(side="left", padx=6)
            ttk.Label(top, text="label:").pack(side="left", padx=(10, 2))
            var = tk.StringVar(value=self.rgb_names.get(d["name"], ""))
            ttk.Entry(top, textvariable=var, width=26).pack(side="left")
            self.rgb_names.setdefault(d["name"], var.get())
            ttk.Button(top, text="Set colour (all)",
                       command=lambda i=d["id"]: self.pick(i, None)).pack(side="right")
            self.rgb_rows.append((d["name"], var))

            for z in d["zones"]:
                zr = ttk.Frame(f); zr.pack(fill="x", padx=20, pady=2)
                warn = "  <- 0 LEDs, set size" if z["leds"] == 0 else ""
                ttk.Label(zr, text=f"zone {z['id']}: {z['name']} ({z['leds']}){warn}",
                          width=46, anchor="w",
                          foreground="red" if z["leds"] == 0 else "").pack(side="left")
                ttk.Button(zr, text="colour", width=7,
                           command=lambda i=d["id"], j=z["id"]: self.pick(i, j)).pack(side="left", padx=3)
                sv = tk.StringVar(value=str(z["leds"]))
                ttk.Spinbox(zr, from_=0, to=200, width=5, textvariable=sv).pack(side="left", padx=3)
                ttk.Button(zr, text="set size", width=8,
                           command=lambda i=d["id"], j=z["id"], v=sv: self.resize(i, j, v)).pack(side="left")

    def pick(self, dev_id, zone_id):
        rgb, _ = colorchooser.askcolor(title="Pick a flat colour")
        if rgb:
            self.cmd_q.put(("colour", (dev_id, zone_id,
                                       tuple(int(c) for c in rgb))))

    def resize(self, dev_id, zone_id, var):
        try:
            self.cmd_q.put(("resize", (dev_id, zone_id, int(var.get()))))
        except ValueError:
            self.log("size must be a number")

    # ---- misc

    def toggle_override(self):
        if self.ov.get():
            OVERRIDE.write_text("dashboard")
            self.log("daemon paused - dashboard has the hardware")
        else:
            OVERRIDE.unlink(missing_ok=True)
            self.log("daemon resumed - it will retake the fans and LEDs")

    def save_labels(self):
        fans = {c: w["name"].get().strip()
                for c, w in self.fan_widgets.items() if w["name"].get().strip()}
        save(FAN_MAP, fans)
        rgb = {n: v.get().strip() for n, v in self.rgb_rows if v.get().strip()}
        save(RGB_LABELS, rgb)
        self.log(f"saved {len(fans)} fan labels, {len(rgb)} rgb labels")

    def log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")

    def drain(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "fans":
                    for ch, w in self.fan_widgets.items():
                        i = int(ch[-1])
                        w["rpm"].configure(text=f"{payload['speeds'].get(i, '----')} rpm")
                        w["duty"].configure(text=f"{payload['duties'].get(i, '--')}%")
                elif kind == "devices":
                    self.build_rgb(payload)
                elif kind == "log":
                    self.log(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def on_close(self):
        self.save_labels()
        OVERRIDE.unlink(missing_ok=True)
        self.worker.stop_flag.set()
        self.root.after(800, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
