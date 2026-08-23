"""Interactive fan tester GUI - work out which channel is which fan.

Per channel you get: live RPM, a duty slider, quick-set buttons, a PULSE mode
that swings the fan up and down so it is obvious by ear, a SOLO button that
quiets every other channel, and a name box to record what you found.

Names are saved to fan_map.json next to this script, and thermal_rgb_loop.py
reads them for labelling.

All device I/O happens on ONE worker thread. liquidctl handles are not
thread-safe, and the controller silently drops rapid writes, so every command
is queued and spaced by nzxt_util.set_duty().

Run:  python fan_tester.py
Original duties are restored when you close the window.
"""
import json
import pathlib
import queue
import threading
import tkinter as tk
from tkinter import ttk

import nzxt_util as nz

CHANNELS = ["fan1", "fan2", "fan3"]
MAP_FILE = pathlib.Path(__file__).with_name("fan_map.json")

POLL_INTERVAL = 1.0     # seconds between status reads
PULSE_LOW = 25
PULSE_HIGH = 100
PULSE_PERIOD = 3.0      # seconds at each level
QUIET_DUTY = 25         # what "solo" drops the other channels to


class Worker(threading.Thread):
    """Owns the liquidctl handle. Commands in, status out."""

    def __init__(self, cmd_q, out_q):
        super().__init__(daemon=True)
        self.cmd_q = cmd_q
        self.out_q = out_q
        self.stop_flag = threading.Event()
        self.pulse = {}          # channel -> next toggle deadline
        self.pulse_state = {}    # channel -> bool (currently high)
        self.original = {}

    def run(self):
        import time
        dev = nz.find_nzxt()
        if dev is None:
            self.out_q.put(("error", "No NZXT controller found."))
            return

        try:
            with dev.connect():
                dev.initialize()
                self.original = nz.read_duties(dev)
                self.out_q.put(("original", dict(self.original)))
                self.out_q.put(("log", f"Connected: {dev.description}"))
                self.out_q.put(("log", f"Original duties: {self.original}"))

                next_poll = 0.0
                while not self.stop_flag.is_set():
                    now = time.monotonic()

                    # ---- commands
                    try:
                        while True:
                            kind, payload = self.cmd_q.get_nowait()
                            self._handle(dev, kind, payload, now)
                    except queue.Empty:
                        pass

                    # ---- pulse toggles
                    for ch, deadline in list(self.pulse.items()):
                        if now >= deadline:
                            high = not self.pulse_state.get(ch, False)
                            self.pulse_state[ch] = high
                            duty = PULSE_HIGH if high else PULSE_LOW
                            ok = nz.set_duty(dev, ch, duty)
                            self.out_q.put(
                                ("log", f"{ch} pulse -> {duty}%"
                                        f"{'' if ok else '  [WRITE DROPPED]'}"))
                            self.pulse[ch] = time.monotonic() + PULSE_PERIOD

                    # ---- status
                    if now >= next_poll:
                        try:
                            self.out_q.put(("status", {
                                "duties": nz.read_duties(dev),
                                "speeds": nz.read_speeds(dev),
                            }))
                        except Exception as exc:
                            self.out_q.put(("log", f"status error: {exc}"))
                        next_poll = time.monotonic() + POLL_INTERVAL

                    time.sleep(0.05)

                # ---- restore
                for ch in CHANNELS:
                    idx = int(ch[-1])
                    if idx in self.original:
                        nz.set_duty(dev, ch, self.original[idx], verify=False)
                self.out_q.put(("log", f"Restored to {self.original}"))
        except Exception as exc:
            self.out_q.put(("error", str(exc)))
        finally:
            self.out_q.put(("closed", None))

    def _handle(self, dev, kind, payload, now):
        import time
        if kind == "set":
            ch, duty = payload
            self.pulse.pop(ch, None)
            ok = nz.set_duty(dev, ch, duty)
            self.out_q.put(("log", f"{ch} -> {duty}%"
                                   f"{'' if ok else '  [WRITE DROPPED]'}"))
        elif kind == "pulse_on":
            ch = payload
            self.pulse[ch] = now
            self.pulse_state[ch] = False
            self.out_q.put(("log", f"{ch} PULSE started "
                                   f"({PULSE_LOW}%<->{PULSE_HIGH}%)"))
        elif kind == "pulse_off":
            ch = payload
            self.pulse.pop(ch, None)
            self.out_q.put(("log", f"{ch} pulse stopped"))
        elif kind == "solo":
            ch = payload
            self.pulse.clear()
            for other in CHANNELS:
                if other != ch:
                    nz.set_duty(dev, other, QUIET_DUTY)
            nz.set_duty(dev, ch, PULSE_HIGH)
            self.out_q.put(("log", f"SOLO {ch}: others at {QUIET_DUTY}%, "
                                   f"{ch} at {PULSE_HIGH}%"))
        elif kind == "restore":
            self.pulse.clear()
            for other in CHANNELS:
                idx = int(other[-1])
                if idx in self.original:
                    nz.set_duty(dev, other, self.original[idx])
            self.out_q.put(("log", "All channels restored"))


class App:
    def __init__(self, root):
        self.root = root
        root.title("NZXT Fan Tester")
        root.geometry("640x560")

        self.cmd_q = queue.Queue()
        self.out_q = queue.Queue()
        self.worker = Worker(self.cmd_q, self.out_q)

        self.names = {}
        if MAP_FILE.exists():
            try:
                self.names = json.loads(MAP_FILE.read_text())
            except Exception:
                self.names = {}

        self.rpm_labels = {}
        self.duty_labels = {}
        self.name_vars = {}
        self.sliders = {}
        self.pulsing = {}

        for ch in CHANNELS:
            self._build_channel(root, ch)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=10, pady=6)
        ttk.Button(bar, text="Restore all",
                   command=lambda: self.cmd_q.put(("restore", None))
                   ).pack(side="left")
        ttk.Button(bar, text="Save names",
                   command=self.save_names).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="connecting...")
        self.status.pack(side="right")

        self.logbox = tk.Text(root, height=9, wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.drain)

    def _build_channel(self, root, ch):
        frame = ttk.LabelFrame(root, text=ch.upper())
        frame.pack(fill="x", padx=10, pady=6)

        top = ttk.Frame(frame)
        top.pack(fill="x", padx=6, pady=4)

        rpm = ttk.Label(top, text="---- rpm", width=12,
                        font=("Consolas", 13, "bold"))
        rpm.pack(side="left")
        self.rpm_labels[ch] = rpm

        duty = ttk.Label(top, text="--%", width=6, font=("Consolas", 11))
        duty.pack(side="left")
        self.duty_labels[ch] = duty

        ttk.Label(top, text="name:").pack(side="left", padx=(10, 2))
        var = tk.StringVar(value=self.names.get(ch, ""))
        ttk.Entry(top, textvariable=var, width=22).pack(side="left")
        self.name_vars[ch] = var

        mid = ttk.Frame(frame)
        mid.pack(fill="x", padx=6, pady=4)

        slider = ttk.Scale(mid, from_=20, to=100, orient="horizontal")
        slider.set(25)
        slider.pack(side="left", fill="x", expand=True)
        slider.bind("<ButtonRelease-1>",
                    lambda e, c=ch: self.slider_release(c))
        self.sliders[ch] = slider

        for label, val in (("25%", 25), ("50%", 50), ("100%", 100)):
            ttk.Button(mid, text=label, width=5,
                       command=lambda c=ch, v=val: self.set_duty(c, v)
                       ).pack(side="left", padx=2)

        btn = ttk.Button(mid, text="PULSE", width=7,
                         command=lambda c=ch: self.toggle_pulse(c))
        btn.pack(side="left", padx=(8, 2))
        self.pulsing[ch] = {"on": False, "btn": btn}

        ttk.Button(mid, text="SOLO", width=6,
                   command=lambda c=ch: self.cmd_q.put(("solo", c))
                   ).pack(side="left", padx=2)

    def slider_release(self, ch):
        self.set_duty(ch, int(float(self.sliders[ch].get())))

    def set_duty(self, ch, duty):
        state = self.pulsing[ch]
        if state["on"]:
            state["on"] = False
            state["btn"].configure(text="PULSE")
        self.sliders[ch].set(duty)
        self.cmd_q.put(("set", (ch, duty)))

    def toggle_pulse(self, ch):
        state = self.pulsing[ch]
        state["on"] = not state["on"]
        if state["on"]:
            state["btn"].configure(text="STOP")
            self.cmd_q.put(("pulse_on", ch))
        else:
            state["btn"].configure(text="PULSE")
            self.cmd_q.put(("pulse_off", ch))

    def save_names(self):
        data = {ch: var.get().strip()
                for ch, var in self.name_vars.items() if var.get().strip()}
        MAP_FILE.write_text(json.dumps(data, indent=2))
        self.log(f"Saved {MAP_FILE.name}: {data}")

    def log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")

    def drain(self):
        try:
            while True:
                kind, payload = self.out_q.get_nowait()
                if kind == "status":
                    for ch in CHANNELS:
                        i = int(ch[-1])
                        rpm = payload["speeds"].get(i)
                        duty = payload["duties"].get(i)
                        self.rpm_labels[ch].configure(
                            text=f"{rpm if rpm is not None else '----'} rpm")
                        self.duty_labels[ch].configure(
                            text=f"{duty if duty is not None else '--'}%")
                    self.status.configure(text="connected")
                elif kind == "log":
                    self.log(payload)
                elif kind == "original":
                    self.status.configure(text="connected")
                elif kind == "error":
                    self.log(f"ERROR: {payload}")
                    self.status.configure(text="error")
                elif kind == "closed":
                    self.status.configure(text="disconnected")
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def on_close(self):
        self.save_names()
        self.worker.stop_flag.set()
        self.root.after(1500, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
