"""Motherboard fan GUI - identify and label every SuperIO header.

MUST RUN AS ADMINISTRATOR. LibreHardwareMonitor loads a signed kernel driver
to reach the Nuvoton SuperIO chip; without elevation nothing is readable.

PUMP SAFETY - enforced in code, not by convention:
  * Each slider FLOORS at that header's baseline duty (what BIOS had set).
    You can raise a header, never lower it below where it started.
  * One of these headers is the AIO pump. Raising a pump is harmless.
    Slowing one risks cavitation, so there is no code path here that lowers
    a duty below baseline.
  * "Release to BIOS" hands a header straight back to firmware control.
  * Closing the window releases EVERY header back to BIOS.

Labels are saved to mobo_fan_map.json.
"""
import atexit
import ctypes
import json
import os
import pathlib
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

FANCONTROL = r"C:\HardwareControl\FanControl"
BASE = pathlib.Path(__file__).resolve().parent
MAP = BASE / "mobo_fan_map.json"

_restore = []


def restore_all():
    """Write the ORIGINAL duty back, then release. SetDefault() alone leaves
    the last written value in the register - measured, not theoretical."""
    for c, base in _restore:
        try:
            if base is not None:
                c.SetSoftware(float(base))
        except Exception:
            pass
        try:
            c.SetDefault()
        except Exception:
            pass
    _restore.clear()


atexit.register(restore_all)


class Worker(threading.Thread):
    def __init__(self, cq, oq):
        super().__init__(daemon=True)
        self.cq, self.oq = cq, oq
        self.stop_flag = threading.Event()
        self.sio = None
        self.computer = None
        self.baseline = {}

    def run(self):
        import time
        try:
            from pythonnet import load
            load("coreclr")
            import clr
            sys.path.insert(0, FANCONTROL)
            os.chdir(FANCONTROL)
            clr.AddReference("LibreHardwareMonitorLib")
            from LibreHardwareMonitor.Hardware import Computer

            c = Computer()
            c.IsMotherboardEnabled = True
            c.IsControllerEnabled = True
            c.IsCpuEnabled = True
            c.Open()
            self.computer = c
            for hw in c.Hardware:
                hw.Update()
                for sub in hw.SubHardware:
                    sub.Update()
                    if "Nuvoton" in sub.Name:
                        self.sio = sub
            if self.sio is None:
                self.oq.put(("fatal", "No SuperIO chip found"))
                return
        except Exception as exc:
            self.oq.put(("fatal", f"LHM init failed: {exc}"))
            return

        self.sio.Update()
        self.ctl = {s.Name: s for s in self.sio.Sensors
                    if str(s.SensorType) == "Control"}
        self.fan = {s.Name: s for s in self.sio.Sensors
                    if str(s.SensorType) == "Fan"}
        for n, s in self.ctl.items():
            self.baseline[n] = float(s.Value or 0)

        self.oq.put(("init", {
            "chip": self.sio.Name,
            "headers": [(n, self.baseline[n],
                         float(self.fan[n].Value or 0) if n in self.fan else 0.0)
                        for n in sorted(self.ctl)],
        }))

        nxt = 0.0
        while not self.stop_flag.is_set():
            now = time.monotonic()
            try:
                while True:
                    self._handle(*self.cq.get_nowait())
            except queue.Empty:
                pass
            if now >= nxt:
                try:
                    self.sio.Update()
                    self.oq.put(("tick", {
                        n: (float(self.fan[n].Value or 0) if n in self.fan else 0.0,
                            float(self.ctl[n].Value or 0))
                        for n in self.ctl}))
                except Exception as exc:
                    self.oq.put(("log", f"poll: {exc}"))
                nxt = now + 1.0
            time.sleep(0.05)
        restore_all()
        try:
            self.computer.Close()
        except Exception:
            pass

    def _handle(self, kind, payload):
        if kind == "set":
            name, duty = payload
            floor = self.baseline.get(name, 0)
            safe = max(floor, min(100.0, float(duty)))
            if safe != duty:
                self.oq.put(("log", f"{name}: {duty:.0f}% floored to "
                                    f"{safe:.0f}% (baseline)"))
            try:
                s = self.ctl[name]
                if not any(c is s.Control for c, _b in _restore):
                    _restore.append((s.Control, self.baseline.get(name)))
                s.Control.SetSoftware(safe)
                self.oq.put(("log", f"{name} -> {safe:.0f}%"))
            except Exception as exc:
                self.oq.put(("log", f"{name} set failed: {exc}"))
        elif kind == "release":
            name = payload
            try:
                base = self.baseline.get(name)
                if base is not None:
                    self.ctl[name].Control.SetSoftware(float(base))
                self.ctl[name].Control.SetDefault()
                self.oq.put(("log", f"{name} restored to {base:.0f}% and released"))
            except Exception as exc:
                self.oq.put(("log", f"{name} release failed: {exc}"))
        elif kind == "release_all":
            restore_all()
            self.oq.put(("log", "ALL headers released to BIOS"))


class App:
    def __init__(self, root):
        self.root = root
        root.title("Motherboard Fan Headers (elevated)")
        root.geometry("880x720")
        self.cq, self.oq = queue.Queue(), queue.Queue()
        self.worker = Worker(self.cq, self.oq)
        self.names = {}
        self.rows = {}
        try:
            self.names = json.loads(MAP.read_text())
        except Exception:
            self.names = {}

        hdr = ttk.Label(root, justify="left", foreground="#a00",
                        text="Sliders CANNOT go below each header's BIOS "
                             "baseline - a pump can be raised, never slowed.\n"
                             "Closing this window releases every header back "
                             "to BIOS.")
        hdr.pack(anchor="w", padx=10, pady=(10, 4))

        self.box = ttk.Frame(root)
        self.box.pack(fill="both", expand=True, padx=6)
        ttk.Label(self.box, text="loading LibreHardwareMonitor...").pack(pady=20)

        bar = ttk.Frame(root); bar.pack(fill="x", padx=10, pady=4)
        ttk.Button(bar, text="Release ALL to BIOS",
                   command=lambda: self.cq.put(("release_all", None))).pack(side="left")
        ttk.Button(bar, text="Save labels", command=self.save).pack(side="left", padx=6)

        self.log = tk.Text(root, height=9, wrap="word")
        self.log.pack(fill="both", expand=False, padx=10, pady=8)

        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)

    def build(self, info):
        for w in self.box.winfo_children():
            w.destroy()
        ttk.Label(self.box, text=f"SuperIO: {info['chip']}",
                  font=("", 10, "bold")).pack(anchor="w", padx=6, pady=4)
        for name, base, rpm in info["headers"]:
            f = ttk.LabelFrame(self.box, text=name)
            f.pack(fill="x", padx=6, pady=3)
            r = ttk.Frame(f); r.pack(fill="x", padx=6, pady=3)
            lr = ttk.Label(r, text="---- rpm", width=10,
                           font=("Consolas", 12, "bold")); lr.pack(side="left")
            ld = ttk.Label(r, text="--%", width=6,
                           font=("Consolas", 10)); ld.pack(side="left")
            ttk.Label(r, text=f"floor {base:.0f}%", width=10,
                      foreground="#666").pack(side="left")
            sc = ttk.Scale(r, from_=base, to=100, orient="horizontal")
            sc.set(max(base, 50)); sc.pack(side="left", fill="x", expand=True, padx=6)
            sc.bind("<ButtonRelease-1>",
                    lambda e, n=name: self.cq.put(("set", (n, float(self.rows[n]['sc'].get())))))
            ttk.Button(r, text="100%", width=5,
                       command=lambda n=name: self.cq.put(("set", (n, 100.0)))).pack(side="left", padx=2)
            ttk.Button(r, text="BIOS", width=5,
                       command=lambda n=name: self.cq.put(("release", n))).pack(side="left", padx=2)
            v = tk.StringVar(value=self.names.get(name, ""))
            ttk.Entry(r, textvariable=v, width=22).pack(side="left", padx=6)
            self.rows[name] = {"rpm": lr, "duty": ld, "sc": sc, "name": v,
                               "base": base}

    def save(self):
        d = {n: r["name"].get().strip() for n, r in self.rows.items()
             if r["name"].get().strip()}
        MAP.write_text(json.dumps(d, indent=2))
        self.logline(f"saved {len(d)} labels to {MAP.name}")

    def logline(self, m):
        self.log.insert("end", m + "\n"); self.log.see("end")

    def drain(self):
        try:
            while True:
                k, p = self.oq.get_nowait()
                if k == "init":
                    self.build(p)
                elif k == "tick":
                    for n, (rpm, duty) in p.items():
                        if n in self.rows:
                            self.rows[n]["rpm"].configure(text=f"{rpm:.0f} rpm")
                            self.rows[n]["duty"].configure(text=f"{duty:.0f}%")
                elif k in ("log", "fatal"):
                    self.logline(p)
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def close(self):
        self.save()
        self.cq.put(("release_all", None))
        self.worker.stop_flag.set()
        self.root.after(1200, self.root.destroy)


if __name__ == "__main__":
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("Run as administrator")
            sys.exit(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    finally:
        restore_all()
