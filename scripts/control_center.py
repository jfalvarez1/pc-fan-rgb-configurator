"""Control Center - per-segment LED testing and fan speed control.

    python control_center.py

While open it writes manual_override.flag so thermal_rgb_loop stands down.
Closing restores automatic control.

PUMP SAFETY
-----------
This tool CANNOT touch your pump. liquidctl exposes only fan1/fan2/fan3 on
the NZXT controller (case fans). The Arctic Liquid Freezer III pump and its
radiator fans are on MOTHERBOARD headers (AIO_PUMP / CPU_FAN), which liquidctl
cannot reach. There is therefore no code path here that can slow or stop the
pump - it is unreachable, not merely guarded.

For the record, the pump guidance is: run it at a FIXED high duty (>=80%),
never on a curve, and never let it stop. Low/unstable pump RPM risks
cavitation and air pockets, which wear the impeller. That is configured in
BIOS or FanControl, not here.

Case fans DO have a floor: MIN_SAFE_DUTY. Fans below roughly 20% can stall
and stop reporting RPM, so the sliders will not go lower.
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
LABELS = BASE / "rgb_labels.json"
SIZES = BASE / "rgb_zone_sizes.json"
OVERRIDE = BASE / "manual_override.flag"

HOST, PORT = "127.0.0.1", 6742
MIN_SAFE_DUTY = 20        # below this, case fans can stall
CHANNELS = ["fan1", "fan2", "fan3"]

# Verified segment map: (device match, zone match, [(label, start, count)])
# Counts confirmed one segment at a time against the physical hardware.
SEGMENTS = [
    ("PRIME", "Aura Addressable 1", [
        ("Arctic pump housing",     0, 12),
        ("Arctic rad fan RIGHT",   12, 12),
        ("Arctic rad fan MIDDLE",  24, 12),
        ("Arctic rad fan LEFT",    36, 12),
    ]),
    ("PRIME", "Aura Addressable 2", [("GPU ZOTAC logo", 0, 24)]),
    # "Aura Mainboard" is the board's 4-pin 12V Aura RGB header. Nothing is
    # plugged into it on this build, so it lights nothing - kept for
    # completeness, not a fault.
    ("PRIME", "Aura Mainboard",     [("4-pin Aura header (empty)", 0, 1)]),
    ("NZXT", "Hue 2 Channel 1", [
        ("front F360 fan 1",  0, 8),
        ("front F360 fan 2",  8, 8),
        ("front F360 fan 3", 16, 8),
    ]),
    ("NZXT", "Hue 2 Channel 2", [
        ("bottom F420 fan 1",  0, 8),
        ("bottom F420 fan 2",  8, 8),
        ("bottom F420 fan 3", 16, 8),
    ]),
    ("NZXT", "Hue 2 Channel 3", [("rear exhaust fan", 0, 8)]),
    ("Corsair", "Corsair DRAM", [("RAM stick", 0, 10)]),
]


def load(p, d):
    try:
        return json.loads(p.read_text())
    except Exception:
        return d


class Worker(threading.Thread):
    def __init__(self, cq, oq):
        super().__init__(daemon=True)
        self.cq, self.oq = cq, oq
        self.stop_flag = threading.Event()
        self.dev = None
        self.client = None
        self.state = {}        # (dev_name, zone_name) -> list[(r,g,b)]

    # ---- helpers

    def _zones(self):
        """Yield (device, zone, segments) for everything we can segment.

        Devices are identified by d.id, never by name: the two Corsair DDR5
        sticks report the SAME name, so a name lookup always resolved to the
        first one and the second stick could never be addressed.
        """
        if self.client is None:
            return
        seen = {}
        for dmatch, zmatch, segs in SEGMENTS:
            for d in self.client.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                if dmatch.lower() not in d.name.lower():
                    continue
                for z in d.zones:
                    if zmatch.lower() in z.name.lower():
                        n = seen.get(d.name, 0) + 1
                        seen[d.name] = n
                        yield d, z, segs, n

    def _mode(self, dev):
        for want in ("direct", "custom", "static"):
            try:
                dev.set_mode(want)
                return want
            except Exception:
                continue
        return None

    def _push(self, dev, zone):
        """Send the whole DEVICE buffer - partial writes get dropped."""
        from openrgb.utils import RGBColor
        buf = []
        for z in dev.zones:
            key = (dev.id, z.name)
            cur = self.state.get(key)
            if cur is None or len(cur) != len(z.leds):
                cur = [(0, 0, 0)] * len(z.leds)
                self.state[key] = cur
            buf.extend(cur)
        if len(buf) != len(dev.leds):
            buf = (buf + [(0, 0, 0)] * len(dev.leds))[:len(dev.leds)]
        dev.set_colors([RGBColor(*c) for c in buf], fast=False)

    # ---- lifecycle

    def run(self):
        import time
        self.dev = nz.find_nzxt()
        try:
            from openrgb import OpenRGBClient
            self.client = OpenRGBClient(HOST, PORT, "control-center")
            sizes = load(SIZES, {})
            for d in self.client.devices:
                if d is None or getattr(d, "type", None) is None:
                    continue
                for z in d.zones:
                    want = sizes.get(f"{d.name}|{z.name}")
                    if want and len(z.leds) != want and "NZXT" not in d.name:
                        try:
                            z.resize(want)
                        except Exception:
                            pass
            self.client.update()
            for d, z, segs, n in self._zones():
                self._mode(d)
                self.state[(d.id, z.name)] = [(0, 0, 0)] * len(z.leds)
            rows = [(d.id, d.name, z.name, len(z.leds), segs, n)
                    for d, z, segs, n in self._zones()]
            self.oq.put(("zones", rows))
            self.oq.put(("log", f"OpenRGB: {len(rows)} segmented zone(s)"))
        except Exception as exc:
            self.oq.put(("log", f"OpenRGB unavailable: {exc}"))
            self.oq.put(("zones", []))

        ctx = self.dev.connect() if self.dev else None
        if ctx:
            ctx.__enter__()
            self.dev.initialize()
            self.oq.put(("log", f"fans: {self.dev.description}"))
        else:
            self.oq.put(("log", "no NZXT controller - fan control disabled"))

        nxt = 0.0
        try:
            while not self.stop_flag.is_set():
                now = time.monotonic()
                try:
                    while True:
                        self._handle(*self.cq.get_nowait())
                except queue.Empty:
                    pass
                if self.dev and now >= nxt:
                    try:
                        self.oq.put(("fans", {"duties": nz.read_duties(self.dev),
                                              "speeds": nz.read_speeds(self.dev)}))
                    except Exception as exc:
                        self.oq.put(("log", f"fan poll: {exc}"))
                    nxt = now + 1.0
                time.sleep(0.05)
        finally:
            if ctx:
                ctx.__exit__(None, None, None)
            self.oq.put(("closed", None))

    # ---- commands

    def _handle(self, kind, payload):
        if kind == "fan":
            ch, duty = payload
            duty = max(MIN_SAFE_DUTY, min(100, int(duty)))
            ok = nz.set_duty(self.dev, ch, duty)
            self.oq.put(("log", f"{ch} -> {duty}%"
                                f"{'' if ok else '  [WRITE DROPPED]'}"))

        elif kind == "seg":
            did, zname, start, count, rgb = payload
            for d, z, segs, n in self._zones():
                if d.id == did and z.name == zname:
                    cur = self.state.setdefault((did, zname),
                                                [(0, 0, 0)] * len(z.leds))
                    for i in range(start, min(start + count, len(cur))):
                        cur[i] = rgb
                    try:
                        self._push(d, z)
                        self.oq.put(("log", f"{zname}[{start}:{start+count}] "
                                            f"-> rgb{rgb}"))
                    except Exception as exc:
                        self.oq.put(("log", f"write failed: {exc}"))
                    return

        elif kind == "all":
            rgb = payload
            for d, z, segs, n in self._zones():
                self.state[(d.id, z.name)] = [rgb] * len(z.leds)
            done = set()
            for d, z, segs, n in self._zones():
                if d.id in done:
                    continue
                done.add(d.id)
                try:
                    self._push(d, z)
                except Exception as exc:
                    self.oq.put(("log", f"{d.name}: {exc}"))
            self.oq.put(("log", f"all segments -> rgb{rgb}"))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Control Center")
        root.geometry("980x820")
        OVERRIDE.write_text("control_center")

        self.cq, self.oq = queue.Queue(), queue.Queue()
        self.worker = Worker(self.cq, self.oq)
        self.fan_names = load(FAN_MAP, {})
        self.fw = {}

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.t_seg = ttk.Frame(nb)
        self.t_fan = ttk.Frame(nb)
        self.t_safe = ttk.Frame(nb)
        nb.add(self.t_seg, text="LED segments")
        nb.add(self.t_fan, text="Fan speeds")
        nb.add(self.t_safe, text="Pump safety")

        self._fans()
        self._safety()
        self.segbox = ttk.Frame(self.t_seg)
        self.segbox.pack(fill="both", expand=True)
        ttk.Label(self.segbox, text="connecting...").pack(pady=20)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="All OFF",
                   command=lambda: self.cq.put(("all", (0, 0, 0)))).pack(side="left")
        ttk.Button(bar, text="All WHITE",
                   command=lambda: self.cq.put(("all", (255, 255, 255)))).pack(side="left", padx=6)
        ttk.Label(bar, text="  daemon paused while this window is open"
                  ).pack(side="left", padx=10)

        self.log = tk.Text(root, height=9, wrap="word")
        self.log.pack(fill="both", expand=False, padx=8, pady=8)

        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)

    # ---- fans

    def _fans(self):
        note = ttk.Label(self.t_fan, justify="left", foreground="#a00",
                         text="Only the three NZXT case fans appear here.\n"
                              "The Arctic pump and its radiator fans are on "
                              "motherboard headers and cannot be reached by "
                              "this tool at all.\n"
                              f"Sliders floor at {MIN_SAFE_DUTY}% - below that "
                              "fans can stall.")
        note.pack(anchor="w", padx=10, pady=(10, 4))

        for ch in CHANNELS:
            f = ttk.LabelFrame(self.t_fan,
                               text=f"{ch.upper()}  -  {self.fan_names.get(ch, '')}")
            f.pack(fill="x", padx=10, pady=6)
            top = ttk.Frame(f); top.pack(fill="x", padx=6, pady=4)
            rpm = ttk.Label(top, text="---- rpm", width=11,
                            font=("Consolas", 13, "bold")); rpm.pack(side="left")
            duty = ttk.Label(top, text="--%", width=6,
                             font=("Consolas", 11)); duty.pack(side="left")
            sc = ttk.Scale(top, from_=MIN_SAFE_DUTY, to=100, orient="horizontal")
            sc.set(30); sc.pack(side="left", fill="x", expand=True, padx=8)
            sc.bind("<ButtonRelease-1>", lambda e, c=ch: self.setfan(c, None))
            for v in (25, 40, 60, 80, 100):
                ttk.Button(top, text=str(v), width=4,
                           command=lambda c=ch, x=v: self.setfan(c, x)).pack(side="left", padx=1)
            self.fw[ch] = {"rpm": rpm, "duty": duty, "scale": sc}

    def setfan(self, ch, v):
        if v is None:
            v = int(float(self.fw[ch]["scale"].get()))
        else:
            self.fw[ch]["scale"].set(v)
        self.cq.put(("fan", (ch, v)))

    # ---- safety tab

    def _safety(self):
        txt = tk.Text(self.t_safe, wrap="word", height=30)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("1.0", """PUMP SAFETY

This tool cannot change your pump speed. That is structural, not a guard:

  liquidctl exposes only  fan1, fan2, fan3  on the NZXT controller.
  Those are your three CASE fan channels.

  The Arctic Liquid Freezer III Pro pump, and its three radiator fans, are
  connected to MOTHERBOARD headers (AIO_PUMP / CPU_FAN). liquidctl has no
  access to motherboard PWM headers, so no code here can reach them.

To change the pump you would use BIOS or FanControl (run as administrator).

RECOMMENDED PUMP SETTINGS

  * Run the pump at a FIXED duty, not a temperature curve.
  * Keep it high - 80% to 100% is the usual advice.
  * Never let it stop.

WHY

  A pump running too slow, or at an unstable voltage, can cavitate - vapour
  bubbles forming and collapsing against the impeller. Sustained cavitation
  wears the impeller and is the main way people damage an AIO pump. Low flow
  also lets trapped air collect instead of being carried around the loop.

  Moderate reductions from maximum are fine and quieter. The danger zone is
  very low RPM, stuttering, or stopping - not "slightly below 100%".

  Mounting matters too: the pump should not be the highest point in the loop,
  or air collects in it.

CASE FANS

  The sliders here floor at MINPCT. Below roughly that, fans can stall, stop
  reporting RPM, and sit still while software believes they are spinning.
""".replace("MINPCT", f"{MIN_SAFE_DUTY}%"))
        txt.configure(state="disabled")

    # ---- segments

    def build(self, rows):
        for w in self.segbox.winfo_children():
            w.destroy()
        if not rows:
            ttk.Label(self.segbox, text="No OpenRGB zones. Is the SDK server up?"
                      ).pack(pady=20)
            return
        canvas = tk.Canvas(self.segbox, highlightthickness=0)
        sb = ttk.Scrollbar(self.segbox, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        for did, dname, zname, nleds, segs, dup in rows:
            title = f"[{did}] {dname}"
            if "DRAM" in zname.upper() or "Corsair" in dname:
                title += f"  (stick {dup})"
            f = ttk.LabelFrame(inner, text=f"{title}  /  {zname}   ({nleds} LEDs)")
            f.pack(fill="x", padx=8, pady=5)
            for label, start, count in segs:
                r = ttk.Frame(f); r.pack(fill="x", padx=14, pady=2)
                ttk.Label(r, text=f"{label}", width=24, anchor="w").pack(side="left")
                ttk.Label(r, text=f"idx {start}-{start+count-1}", width=12,
                          foreground="#666").pack(side="left")
                for txt, rgb in (("white", (255, 255, 255)), ("red", (255, 0, 0)),
                                 ("green", (0, 255, 0)), ("blue", (0, 80, 255)),
                                 ("off", (0, 0, 0))):
                    ttk.Button(r, text=txt, width=6,
                               command=lambda di=did, zn=zname, s=start,
                               c=count, g=rgb: self.cq.put(("seg", (di, zn, s, c, g)))
                               ).pack(side="left", padx=1)
                ttk.Button(r, text="pick...", width=7,
                           command=lambda di=did, zn=zname, s=start, c=count:
                           self.pick(di, zn, s, c)).pack(side="left", padx=4)

    def pick(self, did, zname, start, count):
        rgb, _ = colorchooser.askcolor(title=f"{zname} [{start}:{start+count}]")
        if rgb:
            self.cq.put(("seg", (did, zname, start, count,
                                 tuple(int(x) for x in rgb))))

    # ---- plumbing

    def drain(self):
        try:
            while True:
                k, p = self.oq.get_nowait()
                if k == "fans":
                    for ch, w in self.fw.items():
                        i = int(ch[-1])
                        w["rpm"].configure(text=f"{p['speeds'].get(i,'----')} rpm")
                        w["duty"].configure(text=f"{p['duties'].get(i,'--')}%")
                elif k == "zones":
                    self.build(p)
                elif k == "log":
                    self.log.insert("end", p + "\n"); self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        OVERRIDE.unlink(missing_ok=True)
        self.worker.stop_flag.set()
        self.root.after(800, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
