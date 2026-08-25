"""The Fans tab's own side controls, replacing the lighting ones.

Editable, but only within limits that cannot undo the measured tuning: curve
trims are capped at +/-15 duty points by `fan_tuning`, and the pump is a pick
from its own measured duty->rpm map rather than a free slider. There is
deliberately no free-form curve editor - the curves came from thermal
measurement, not from taste, and a drag handle would make them trivial to
throw away by accident.

GPU telemetry comes from nvidia-smi on a background thread. It takes a couple
of hundred milliseconds, which is fine every few seconds and completely
unacceptable inside a 30 fps UI callback.
"""
import json
import subprocess
import sys
import threading
import time
import tkinter as tk

import fan_tuning
from fan_panel import (ACCENT, BAD, BASE, GOOD, INK, LINE, MUTED,
                       PUMP_TOLERANCE, STALE_AFTER, WARN, _load)

FONT_S = ("Segoe UI", 9)
FONT_M = ("Segoe UI", 10)
BTN = "#232b39"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MOBO_TASK = "HardwareControl-MoboDaemon"


class GpuTelemetry:
    """nvidia-smi, polled on a thread and cached."""

    FIELDS = ("temperature.gpu", "power.draw", "power.limit", "clocks.sm",
              "clocks.mem", "utilization.gpu", "memory.used", "memory.total")
    PERIOD = 4.0

    def __init__(self):
        self.data = {}
        self.ok = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=" + ",".join(self.FIELDS),
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                vals = [v.strip() for v in out.stdout.strip().split(",")]
                got = {}
                for k, v in zip(self.FIELDS, vals):
                    try:
                        got[k] = float(v)
                    except ValueError:
                        got[k] = None       # "N/A" - VRAM temp reports this
                self.data = got
                self.ok = True
            except Exception:
                self.ok = False
            self._stop.wait(self.PERIOD)


GPU = GpuTelemetry()


class FanSidePanel:
    def __init__(self, parent, panel_bg, on_change=None):
        self.on_change = on_change
        self.bg = panel_bg
        p = parent

        def head(t):
            tk.Label(p, text=t, bg=self.bg, fg=MUTED,
                     font=("Segoe UI", 9, "bold"), anchor="w"
                     ).pack(fill="x", padx=16, pady=(14, 6))

        tk.Label(p, text="FANS & COOLING", bg=self.bg, fg=INK,
                 font=("Segoe UI Semibold", 15), anchor="w"
                 ).pack(fill="x", padx=16, pady=(16, 0))
        tk.Label(p, text="live sensors, the curves, and safe trim",
                 bg=self.bg, fg=MUTED, font=FONT_S, anchor="w"
                 ).pack(fill="x", padx=16)

        head("TEMPERATURES")
        self.temp_box = tk.Frame(p, bg=self.bg)
        self.temp_box.pack(fill="x", padx=16)

        head("FANS & PUMP")
        self.fan_box = tk.Frame(p, bg=self.bg)
        self.fan_box.pack(fill="x", padx=16)

        head("GPU")
        self.gpu_box = tk.Frame(p, bg=self.bg)
        self.gpu_box.pack(fill="x", padx=16)

        head("CURVE TRIM")
        tk.Label(p, text="Shifts a whole curve by up to "
                         + str(int(fan_tuning.TRIM_LIMIT))
                         + " duty points.\nApplied live, within one poll. The "
                           "hard min/max\nclamps still apply, so a trim cannot "
                           "stall a fan.",
                 bg=self.bg, fg=MUTED, font=FONT_S, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(0, 4))
        self.trims = {}
        self.trim_lbls = {}
        cur = fan_tuning.load_trims()
        for key, label in (("fan1", "side intake F360"),
                           ("fan2", "bottom intake F420"),
                           ("fan3", "rear exhaust"),
                           ("rad", "radiator x3")):
            lb = tk.Label(p, text=f"{label}: {cur[key]:+.0f}", bg=self.bg,
                          fg=INK, font=FONT_S, anchor="w")
            lb.pack(fill="x", padx=16, pady=(6, 0))
            var = tk.DoubleVar(value=cur[key])
            tk.Scale(p, from_=-fan_tuning.TRIM_LIMIT, to=fan_tuning.TRIM_LIMIT,
                     resolution=1, orient="horizontal", variable=var,
                     bg=self.bg, fg=INK, troughcolor=BTN,
                     highlightthickness=0, bd=0, sliderrelief="flat",
                     activebackground=ACCENT, font=FONT_S, showvalue=False,
                     command=lambda _v, k=key: self._trim_changed(k)
                     ).pack(fill="x", padx=14)
            self.trims[key] = var
            self.trim_lbls[key] = (lb, label)
        b = tk.Label(p, text="Reset trims", bg=BTN, fg=INK, font=FONT_M,
                     padx=10, pady=7, cursor="hand2", highlightthickness=1,
                     highlightbackground=LINE)
        b.pack(fill="x", padx=16, pady=(8, 2))
        b.bind("<Button-1>", lambda e: self.reset_trims())

        head("PUMP")
        self.pump_lbl = tk.Label(p, text="", bg=self.bg, fg=INK, font=FONT_M,
                                 anchor="w", justify="left")
        self.pump_lbl.pack(fill="x", padx=16)
        tk.Label(p, text="Fixed duty, never a curve - speed cycling is a\n"
                         "wear mechanism. Choices are the MEASURED duty->rpm\n"
                         "points; the daemon clamps below its safety floor.\n"
                         "Takes effect when mobo_daemon restarts.",
                 bg=self.bg, fg=MUTED, font=FONT_S, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(4, 4))
        self.pump_choice = tk.Frame(p, bg=self.bg)
        self.pump_choice.pack(fill="x", padx=14)

        head("STATUS")
        self.status_box = tk.Frame(p, bg=self.bg)
        self.status_box.pack(fill="x", padx=16, pady=(0, 6))
        self._busy = False
        self.restart_btn = tk.Label(
            p, text="Restart daemons", bg=ACCENT, fg="#ffffff", font=FONT_M,
            padx=10, pady=8, cursor="hand2", highlightthickness=1,
            highlightbackground=ACCENT)
        self.restart_btn.pack(fill="x", padx=16, pady=(2, 2))
        self.restart_btn.bind("<Button-1>", lambda e: self.restart_daemons())
        tk.Label(p, text="Re-pins the pump and reloads both curves. Use this\n"
                         "if a header stops matching its commanded duty.\n"
                         "If it still mismatches afterwards, another program\n"
                         "owns the header - run Fix Cooling.bat (elevated).",
                 bg=self.bg, fg=MUTED, font=FONT_S, anchor="w", justify="left"
                 ).pack(fill="x", padx=16, pady=(0, 18))

    # ---- edits

    def _trim_changed(self, key):
        written = fan_tuning.save_trims(
            {k: v.get() for k, v in self.trims.items()})
        lb, label = self.trim_lbls[key]
        lb.config(text=f"{label}: {written[key]:+.0f}",
                  fg=ACCENT if written[key] else INK)
        if self.on_change:
            self.on_change(f"{label} trim {written[key]:+.0f} duty points "
                           f"(live within one poll)")

    def reset_trims(self):
        for v in self.trims.values():
            v.set(0)
        written = fan_tuning.save_trims({k: 0 for k in self.trims})
        for key, (lb, label) in self.trim_lbls.items():
            lb.config(text=f"{label}: {written[key]:+.0f}", fg=INK)
        if self.on_change:
            self.on_change("curve trims reset to zero")

    def _set_pump(self, duty):
        cfg = _load("pump_config.json") or {}
        cfg["pump_duty"] = float(duty)
        (BASE / "pump_config.json").write_text(json.dumps(cfg, indent=2))
        for w in self.pump_choice.winfo_children():
            w.destroy()
        self._pump_buttons(float(duty))
        if self.on_change:
            self.on_change(f"pump set to {duty:.0f}% - restart mobo_daemon "
                           f"(elevated) for it to take effect")

    # ---- restart

    def restart_daemons(self):
        """Stop and restart both daemons, then check the pump actually landed.

        Done on a thread: schtasks plus the settle wait is several seconds,
        and blocking the UI thread would freeze the whole editor.
        """
        if self._busy:
            return
        self._busy = True
        self.restart_btn.config(text="Restarting...", bg=BTN, fg=INK)
        threading.Thread(target=self._restart_worker, daemon=True).start()

    def _restart_worker(self):
        msgs = []

        def run(args):
            return subprocess.run(args, capture_output=True, text=True,
                                  timeout=60, creationflags=NO_WINDOW)

        # elevated daemon: the scheduled task owns it, and triggering a task
        # you own needs no elevation even though the task runs elevated
        try:
            run(["schtasks", "/End", "/TN", MOBO_TASK])
            time.sleep(2.0)
            r = run(["schtasks", "/Run", "/TN", MOBO_TASK])
            msgs.append("mobo_daemon restarted" if r.returncode == 0
                        else f"mobo task: {(r.stdout or r.stderr).strip()[:50]}")
        except Exception as exc:
            msgs.append(f"mobo task failed: {type(exc).__name__}")

        # case-fan daemon: stop any copy, then start one clean
        try:
            stopped = 0
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cl = " ".join(proc.info.get("cmdline") or [])
                        if "thermal_rgb_loop" in cl:
                            proc.terminate()
                            stopped += 1
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(1.5)
            exe = sys.executable
            if exe.lower().endswith("python.exe"):
                exe = exe[:-len("python.exe")] + "pythonw.exe"
            subprocess.Popen([exe, "thermal_rgb_loop.py", "--apply", "--log",
                              "--csv"], cwd=str(BASE), creationflags=NO_WINDOW)
            msgs.append(f"thermal_rgb_loop restarted ({stopped} stopped)")
        except Exception as exc:
            msgs.append(f"thermal loop failed: {type(exc).__name__}")

        # verify rather than assume - the whole point of this panel
        time.sleep(11.0)
        verdict = "could not read sensors.json"
        try:
            sens = _load("sensors.json") or {}
            cfg = _load("pump_config.json") or {}
            want, got = cfg.get("pump_duty"), sens.get("pump_duty")
            rpm = sens.get("pump_rpm")
            if want is not None and got is not None:
                if abs(want - got) <= PUMP_TOLERANCE:
                    verdict = f"OK - pump {got:.0f}% ({rpm:.0f} rpm)"
                else:
                    verdict = (f"STILL WRONG - told {want:.0f}%, reads "
                               f"{got:.0f}%. Another program owns the header; "
                               f"run Fix Cooling.bat")
        except Exception as exc:
            verdict = f"verify failed: {type(exc).__name__}"
        msgs.append(verdict)
        try:
            self.restart_btn.after(0, self._restart_done, msgs)
        except Exception:
            pass

    def _restart_done(self, msgs):
        self._busy = False
        self.restart_btn.config(text="Restart daemons", bg=ACCENT,
                                fg="#ffffff")
        if self.on_change:
            self.on_change("  |  ".join(msgs))

    # ---- readouts

    def _rows(self, box, items):
        """Rebuild a readout block. Values change every second, so the labels
        are recreated rather than each being tracked individually."""
        for w in box.winfo_children():
            w.destroy()
        for name, value, colour in items:
            r = tk.Frame(box, bg=self.bg)
            r.pack(fill="x")
            tk.Label(r, text=name, bg=self.bg, fg=MUTED, font=FONT_S,
                     anchor="w").pack(side="left")
            tk.Label(r, text=value, bg=self.bg, fg=colour or INK,
                     font=FONT_M, anchor="e").pack(side="right")

    @staticmethod
    def _colour(v, warm, hot):
        if v is None:
            return MUTED
        return BAD if v >= hot else (WARN if v >= warm else GOOD)

    def _pump_buttons(self, current):
        if self.pump_choice.winfo_children():
            return                          # static; built once
        table = (_load("pump_config.json") or {}).get("map") or []
        for e in sorted(table, key=lambda x: x["duty"]):
            duty, rpm = float(e["duty"]), float(e["rpm"])
            sel = current is not None and abs(duty - current) < 0.5
            b = tk.Label(self.pump_choice,
                         text=f"{duty:.0f}%   ·   {rpm:.0f} rpm",
                         bg=ACCENT if sel else BTN,
                         fg="#ffffff" if sel else INK, font=FONT_S,
                         padx=8, pady=6, cursor="hand2", highlightthickness=1,
                         highlightbackground=ACCENT if sel else LINE)
            b.pack(fill="x", pady=1)
            b.bind("<Button-1>", lambda ev, dd=duty: self._set_pump(dd))

    def refresh(self, d):
        t, sens = d["temps"], d["sens"]

        items = []
        for key, label, warm, hot in (("cpu_tctl", "CPU Tctl", 75, 88),
                                      ("cpu_ccd1", "CPU CCD1", 70, 85),
                                      ("gpu_core", "GPU core", 70, 83),
                                      ("gpu_vram", "GPU VRAM", 80, 92)):
            v = t.get(key, sens.get(key))
            items.append((label, f"{v:.1f} °C" if v is not None else "--",
                          self._colour(v, warm, hot)))
        self._rows(self.temp_box, items)

        fans = []
        for ch in ("fan1", "fan2", "fan3"):
            f = d["fans"].get(ch) or {}
            if f.get("rpm") is not None:
                fans.append((f.get("label", ch),
                             f"{f['duty']}%   {f['rpm']} rpm", INK))
            else:
                fans.append((f.get("label", ch), "--", MUTED))
        for tag, dk, rk in (("radiator x3", "rad_duty", "rad_rpm"),
                            ("pump", "pump_duty", "pump_rpm")):
            dv, rv = sens.get(dk), sens.get(rk)
            fans.append((tag, f"{dv:.0f}%   {rv:.0f} rpm"
                         if dv is not None and rv is not None else "--",
                         INK if dv is not None else MUTED))
        self._rows(self.fan_box, fans)

        g = GPU.data or {}
        if not g:
            self._rows(self.gpu_box, [("nvidia-smi", "unavailable"
                                       if GPU.ok is False else "reading...",
                                       MUTED)])
        else:
            pw, lim = g.get("power.draw"), g.get("power.limit")
            used, tot = g.get("memory.used"), g.get("memory.total")
            self._rows(self.gpu_box, [
                ("core temp",
                 f"{g['temperature.gpu']:.0f} °C"
                 if g.get("temperature.gpu") is not None else "--",
                 self._colour(g.get("temperature.gpu"), 70, 83)),
                ("power",
                 f"{pw:.0f} / {lim:.0f} W" if pw is not None and lim else "--",
                 INK),
                ("core clock", f"{g['clocks.sm']:.0f} MHz"
                 if g.get("clocks.sm") is not None else "--", INK),
                ("memory clock", f"{g['clocks.mem']:.0f} MHz"
                 if g.get("clocks.mem") is not None else "--", INK),
                ("utilisation", f"{g['utilization.gpu']:.0f} %"
                 if g.get("utilization.gpu") is not None else "--", INK),
                ("VRAM used", f"{used/1024:.1f} / {tot/1024:.0f} GB"
                 if used is not None and tot else "--", INK),
            ])

        cmd, act, rpm = d["pump_cmd"], sens.get("pump_duty"), sens.get("pump_rpm")
        txt = []
        if act is not None:
            txt.append(f"measured {act:.0f}%"
                       + (f"   {rpm:.0f} rpm" if rpm is not None else ""))
        if cmd is not None:
            txt.append(f"commanded {cmd:.0f}%")
        bad = (cmd is not None and act is not None
               and abs(cmd - act) > PUMP_TOLERANCE)
        self.pump_lbl.config(text="\n".join(txt) or "no data",
                             fg=BAD if bad else INK)
        self._pump_buttons(cmd)

        notes = self.notes_for(d)
        self._rows(self.status_box,
                   [(n, "", WARN) for n in notes]
                   or [("all headers obeying their curves", "", GOOD)])

    @staticmethod
    def notes_for(d):
        out = []
        if d["case_age"] is None or d["case_age"] > STALE_AFTER:
            out.append("thermal_rgb_loop not running")
        if d["mobo_age"] is None or d["mobo_age"] > STALE_AFTER:
            out.append("mobo_daemon not running")
        cmd, act = d["pump_cmd"], d["sens"].get("pump_duty")
        if cmd is not None and act is not None and abs(cmd - act) > PUMP_TOLERANCE:
            out.append(f"pump told {cmd:.0f}%, hardware at {act:.0f}%")
        return out
