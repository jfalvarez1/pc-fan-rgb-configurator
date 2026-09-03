"""Fans view for LED Studio - the curves each fan actually runs, plotted.

    from fan_panel import FanPanel
    fp = FanPanel(canvas)
    fp.refresh()        # re-read live state and redraw

Read-only on purpose. The curves here were tuned from measured thermal data
and the pump is deliberately hard to set to an unsafe value, so this shows
what is running rather than offering a drag-the-curve editor that would make
it easy to undo that work by accident.

Sources, all files already published by the daemons - nothing here talks to
hardware, so the view cannot fight the daemons for a device:

    fan_state.json   case fan duty/rpm + temps      (thermal_rgb_loop)
    sensors.json     cpu/gpu temps, pump + radiator (mobo_daemon, elevated)
    thermal_rgb_loop.FAN_CHANNELS   the case fan curves
    mobo_daemon.RAD_CURVE           the radiator curve
    pump_config.json                the measured pump duty actually used

It also reports when a header is NOT doing what it was told. The daemon logs
"pump pinned at 56%" and moves on; if something else then takes the header,
nothing notices. Comparing commanded against measured is the only way that
shows up, so this view does that comparison and says so plainly.
"""
import json
import math
import pathlib
import time

# Shared with the daemons, and stable when frozen - see app_paths.
from app_paths import DATA as BASE

BG      = "#0d0f14"
CARD    = "#151922"
LINE    = "#2a3140"
INK     = "#e6e9ef"
MUTED   = "#7d8697"
ACCENT  = "#ff3aa2"
GOOD    = "#00e08a"
WARN    = "#ffb020"
BAD     = "#ff4d4d"

FONT   = ("Segoe UI", 11)
FONT_H = ("Segoe UI", 12, "bold")
FONT_S = ("Segoe UI", 9)
FONT_M = ("Segoe UI", 10)

# one colour per temperature source, used for both curve and legend
SRC_COLOUR = {
    "gpu_core": "#00e9ff",
    "gpu_vram": "#b26bff",
    "cpu_tctl": "#ff9d3a",
}
SRC_LABEL = {
    "gpu_core": "GPU core",
    "gpu_vram": "GPU VRAM",
    "cpu_tctl": "CPU Tctl",
}

STALE_AFTER = 20.0          # a state file older than this means "not running"

# Two different comparisons, two different tolerances - using one number for
# both cried wolf on the radiator every time it sat inside its own deadband.
#
# The pump is a FIXED commanded value with no deadband and no ramping, so any
# real deviation is a fault. The curve-driven headers legitimately lag: the
# daemons ignore changes smaller than their deadband (6 for the radiator, 5
# for the case fans) and hold a falling target for 45-60 s before acting. A
# warning that fires during normal operation trains you to ignore it, which is
# how the pump managed to sit at the wrong speed unnoticed in the first place.
PUMP_TOLERANCE = 5.0        # commanded vs measured, fixed value
CURVE_TOLERANCE = 14.0      # curve target vs measured, allows deadband + lag
DUTY_TOLERANCE = PUMP_TOLERANCE      # kept for callers that import it

T_MIN, T_MAX = 30.0, 95.0   # chart x range, degrees C


def _load(name):
    try:
        return json.loads((BASE / name).read_text())
    except Exception:
        return None


def interpolate(curve, temp):
    """Duty at `temp` on a [(temp, duty), ...] curve. Mirrors the daemon."""
    if not curve:
        return 0.0
    if temp <= curve[0][0]:
        return float(curve[0][1])
    if temp >= curve[-1][0]:
        return float(curve[-1][1])
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return float(d1)
            f = (temp - t0) / (t1 - t0)
            return float(d0 + (d1 - d0) * f)
    return float(curve[-1][1])


class FanPanel:
    """Draws the fan curves and live operating points onto a Tk canvas."""

    def __init__(self, canvas, width, height):
        self.cv = canvas
        self.w, self.h = width, height
        self.notes = []          # warnings raised by the last refresh

    # ---- data

    def gather(self):
        """Everything the view needs, with each source's staleness."""
        now = time.time()
        st = _load("fan_state.json") or {}
        sens = _load("sensors.json") or {}
        pump_cfg = _load("pump_config.json") or {}

        try:
            import thermal_rgb_loop as trl
            channels = trl.FAN_CHANNELS
            min_duty, max_duty = trl.MIN_DUTY, trl.MAX_DUTY
        except Exception:
            channels, min_duty, max_duty = {}, 20, 100
        try:
            import mobo_daemon as md
            rad_curve = md.RAD_CURVE
            rad_min = getattr(md, "RAD_MIN_DUTY", 0)
            pump_cfg_duty = getattr(md, "PUMP_DUTY", None)
            pump_floor = getattr(md, "PUMP_MIN_SAFE", None)
        except Exception:
            rad_curve, rad_min, pump_cfg_duty, pump_floor = [], 0, None, None

        # the daemon prefers the measured value in pump_config.json over the
        # constant in the source, so report what it will actually use
        pump_duty_cmd = pump_cfg.get("pump_duty", pump_cfg_duty)

        temps = dict(st.get("temps") or {})
        if "cpu_tctl" in sens:
            temps.setdefault("cpu_tctl", sens["cpu_tctl"])
        for k in ("gpu_core", "gpu_vram", "cpu_tctl"):
            if k in sens and k not in temps:
                temps[k] = sens[k]

        return {
            "now": now,
            "case_age": now - st.get("ts", 0) if st else None,
            "mobo_age": now - sens.get("ts", 0) if sens else None,
            "fans": st.get("fans") or {},
            "temps": temps,
            "channels": channels,
            "min_duty": min_duty,
            "max_duty": max_duty,
            "rad_curve": rad_curve,
            "rad_min": rad_min,
            "pump_cmd": pump_duty_cmd,
            "pump_floor": pump_floor,
            "sens": sens,
            "rgb_mode": st.get("rgb_mode"),
        }

    # ---- drawing helpers

    def _card(self, x, y, w, h, title, subtitle=None):
        c = self.cv
        c.create_rectangle(x, y, x + w, y + h, fill=CARD, outline=LINE)
        c.create_text(x + 14, y + 16, text=title, fill=INK, font=FONT_H,
                      anchor="w")
        if subtitle:
            c.create_text(x + w - 14, y + 16, text=subtitle, fill=MUTED,
                          font=FONT_S, anchor="e")

    def _chart(self, x, y, w, h, curves, live_temp=None, live_duty=None,
               lead=None, min_duty=None):
        """Plot duty-vs-temperature curves inside the given box."""
        c = self.cv
        c.create_rectangle(x, y, x + w, y + h, outline=LINE, fill=BG)

        def px(t):
            return x + (t - T_MIN) / (T_MAX - T_MIN) * w

        def py(d):
            return y + h - (d / 100.0) * h

        # grid
        for d in (0, 25, 50, 75, 100):
            yy = py(d)
            c.create_line(x, yy, x + w, yy, fill="#1d2330")
            c.create_text(x - 6, yy, text=f"{d}", fill=MUTED, font=FONT_S,
                          anchor="e")
        for t in (40, 50, 60, 70, 80, 90):
            xx = px(t)
            c.create_line(xx, y, xx, y + h, fill="#1d2330")
            c.create_text(xx, y + h + 10, text=f"{t}", fill=MUTED,
                          font=FONT_S)
        c.create_text(x + w / 2, y + h + 24, text="temperature  °C",
                      fill=MUTED, font=FONT_S)

        if min_duty:
            c.create_line(x, py(min_duty), x + w, py(min_duty),
                          fill="#3a4356", dash=(3, 3))
            c.create_text(x + 4, py(min_duty) - 8, text=f"min {min_duty}%",
                          fill="#55617a", font=FONT_S, anchor="w")

        for src, curve in curves.items():
            col = SRC_COLOUR.get(src, ACCENT)
            pts = []
            # flat before the first knee and after the last, as the daemon does
            pts += [px(T_MIN), py(curve[0][1])]
            for t, d in curve:
                pts += [px(t), py(d)]
            pts += [px(T_MAX), py(curve[-1][1])]
            c.create_line(pts, fill=col, width=3 if src == lead else 2,
                          smooth=False)
            for t, d in curve:
                c.create_oval(px(t) - 2.5, py(d) - 2.5, px(t) + 2.5,
                              py(d) + 2.5, fill=col, outline="")

        # live operating point
        if live_temp is not None and live_duty is not None:
            xx, yy = px(live_temp), py(live_duty)
            c.create_line(xx, y, xx, y + h, fill="#ffffff", dash=(2, 4))
            c.create_oval(xx - 6, yy - 6, xx + 6, yy + 6, fill="#ffffff",
                          outline=ACCENT, width=2)

    def _legend(self, x, y, curves, lead=None):
        c = self.cv
        for i, src in enumerate(curves):
            col = SRC_COLOUR.get(src, ACCENT)
            yy = y + i * 15
            c.create_line(x, yy, x + 16, yy, fill=col, width=3)
            txt = SRC_LABEL.get(src, src)
            if src == lead:
                txt += "  (leading)"
            c.create_text(x + 22, yy, text=txt,
                          fill=INK if src == lead else MUTED,
                          font=FONT_S, anchor="w")

    # ---- the view

    def refresh(self):
        c = self.cv
        c.delete("all")
        self.notes = []
        d = self.gather()

        c.create_text(24, 22, text="FAN CURVES", fill=INK,
                      font=("Segoe UI Semibold", 15), anchor="w")
        c.create_text(24, 44,
                      text="What each fan is actually running. Read-only - "
                           "curves were tuned from measured thermal data.",
                      fill=MUTED, font=FONT_M, anchor="w")

        y = 70
        y = self._status_bar(24, y, self.w - 48, d)

        cw = (self.w - 48 - 16) / 2
        chh = 300
        col_x = (24, 24 + cw + 16)

        # Every card placed by the same rule. Threading the motherboard cards
        # in after a variable number of case fans by hand produced placement
        # arithmetic that was wrong for any count except three.
        cards = [
            (lambda x, y_, w, h, i=ch_id, cf=cfg:
                self._case_fan(x, y_, w, h, i, cf, d))
            for ch_id, cfg in d["channels"].items()
        ]
        cards.append(lambda x, y_, w, h: self._pump(x, y_, w, h, d))
        cards.append(lambda x, y_, w, h: self._radiator(x, y_, w, h, d))

        for i, draw in enumerate(cards):
            draw(col_x[i % 2], y + (i // 2) * (chh + 14), cw, chh)
        return self.notes

    def _status_bar(self, x, y, w, d):
        c = self.cv
        c.create_rectangle(x, y, x + w, y + 34, fill=CARD, outline=LINE)
        parts = []
        case_ok = d["case_age"] is not None and d["case_age"] < STALE_AFTER
        mobo_ok = d["mobo_age"] is not None and d["mobo_age"] < STALE_AFTER
        parts.append(("thermal_rgb_loop", case_ok, d["case_age"]))
        parts.append(("mobo_daemon", mobo_ok, d["mobo_age"]))
        cx = x + 14
        for name, ok, age in parts:
            col = GOOD if ok else BAD
            c.create_oval(cx, y + 14, cx + 8, y + 22, fill=col, outline="")
            txt = (f"{name}  live ({age:.0f}s ago)" if ok
                   else f"{name}  NOT RUNNING"
                   + (f" (last seen {age/60:.0f} min ago)"
                      if age is not None else ""))
            c.create_text(cx + 14, y + 18, text=txt, fill=INK if ok else BAD,
                          font=FONT_M, anchor="w")
            if not ok:
                self.notes.append(f"{name} is not running")
            cx += 300
        t = d["temps"]
        readout = "   ".join(
            f"{SRC_LABEL.get(k, k)} {t[k]:.0f}°C"
            for k in ("cpu_tctl", "gpu_core", "gpu_vram") if k in t)
        c.create_text(x + w - 14, y + 18, text=readout, fill=MUTED,
                      font=FONT_M, anchor="e")
        return y + 46

    def _case_fan(self, x, y, w, h, ch_id, cfg, d):
        c = self.cv
        live = d["fans"].get(ch_id) or {}
        duty, rpm = live.get("duty"), live.get("rpm")
        curves = cfg.get("curves", {})
        temps = d["temps"]

        # which sensor is asking for the most duty - that is what runs
        lead, lead_duty, lead_temp = None, -1.0, None
        for src, curve in curves.items():
            if src not in temps:
                continue
            want = interpolate(curve, temps[src])
            if want > lead_duty:
                lead, lead_duty, lead_temp = src, want, temps[src]

        sub = f"NZXT {ch_id}"
        self._card(x, y, w, h, cfg.get("label", ch_id), sub)

        line = []
        if duty is not None:
            line.append(f"{duty}%")
        if rpm is not None:
            line.append(f"{rpm} rpm")
        if lead:
            line.append(f"driven by {SRC_LABEL.get(lead, lead)} "
                        f"{lead_temp:.0f}°C -> {lead_duty:.0f}%")
        c.create_text(x + 14, y + 40, text="   ".join(line) or "no data",
                      fill=INK, font=FONT, anchor="w")

        # commanded vs measured: the only way a hijacked header shows up
        if duty is not None and lead is not None:
            want = max(d["min_duty"], min(d["max_duty"], round(lead_duty)))
            if abs(want - duty) > CURVE_TOLERANCE:
                msg = (f"{cfg.get('label', ch_id)}: curve asks {want}%, "
                       f"hardware reports {duty}%")
                c.create_text(x + 14, y + 62, text="! " + msg, fill=WARN,
                              font=FONT_S, anchor="w")
                self.notes.append(msg)

        self._chart(x + 46, y + 86, w - 120, h - 130, curves,
                    live_temp=lead_temp, live_duty=duty, lead=lead,
                    min_duty=d["min_duty"])
        self._legend(x + 46, y + h - 34, curves, lead)

    def _pump(self, x, y, w, h, d):
        c = self.cv
        sens = d["sens"]
        cmd = d["pump_cmd"]
        act = sens.get("pump_duty")
        rpm = sens.get("pump_rpm")
        self._card(x, y, w, h, "AIO pump", "motherboard Fan #2")

        line = []
        if act is not None:
            line.append(f"{act:.0f}%")
        if rpm is not None:
            line.append(f"{rpm:.0f} rpm")
        if cmd is not None:
            line.append(f"commanded {cmd:.0f}%")
        c.create_text(x + 14, y + 40, text="   ".join(line) or "no data",
                      fill=INK, font=FONT, anchor="w")

        c.create_text(x + 14, y + 62,
                      text="Fixed duty, never a curve - speed cycling is a "
                           "wear mechanism.",
                      fill=MUTED, font=FONT_S, anchor="w")

        bx, by = x + 46, y + 92
        bw, bh = w - 120, h - 140
        c.create_rectangle(bx, by, bx + bw, by + bh, outline=LINE, fill=BG)

        def py(v):
            return by + bh - (v / 100.0) * bh

        for v in (0, 25, 50, 75, 100):
            c.create_line(bx, py(v), bx + bw, py(v), fill="#1d2330")
            c.create_text(bx - 6, py(v), text=f"{v}", fill=MUTED,
                          font=FONT_S, anchor="e")
        if d["pump_floor"]:
            c.create_line(bx, py(d["pump_floor"]), bx + bw,
                          py(d["pump_floor"]), fill=BAD, dash=(4, 3))
            c.create_text(bx + 4, py(d["pump_floor"]) - 9,
                          text=f"safety floor {d['pump_floor']:.0f}%",
                          fill=BAD, font=FONT_S, anchor="w")
        if cmd is not None:
            c.create_line(bx, py(cmd), bx + bw, py(cmd), fill=GOOD, width=3)
            c.create_text(bx + bw - 4, py(cmd) - 9,
                          text=f"commanded {cmd:.0f}%", fill=GOOD,
                          font=FONT_S, anchor="e")
        if act is not None:
            c.create_line(bx, py(act), bx + bw, py(act), fill="#ffffff",
                          width=2, dash=(6, 3))
            c.create_text(bx + bw - 4, py(act) + 10,
                          text=f"measured {act:.0f}%", fill="#ffffff",
                          font=FONT_S, anchor="e")

        if cmd is not None and act is not None and abs(cmd - act) > PUMP_TOLERANCE:
            msg = (f"pump commanded {cmd:.0f}% but hardware reports "
                   f"{act:.0f}% - another program owns this header")
            c.create_text(x + 14, y + h - 30, text="! " + msg, fill=BAD,
                          font=FONT_S, anchor="w")
            c.create_text(x + 14, y + h - 15,
                          text="  FanControl, NZXT CAM or BIOS Q-Fan will do "
                               "this. Close it and restart mobo_daemon.",
                          fill=WARN, font=FONT_S, anchor="w")
            self.notes.append(msg)

    def _radiator(self, x, y, w, h, d):
        c = self.cv
        sens = d["sens"]
        act = sens.get("rad_duty")
        rpm = sens.get("rad_rpm")
        cpu = d["temps"].get("cpu_tctl", sens.get("cpu_tctl"))
        curve = d["rad_curve"]
        want = interpolate(curve, cpu) if (curve and cpu is not None) else None
        if want is not None and d["rad_min"]:
            want = max(d["rad_min"], want)

        self._card(x, y, w, h, "Radiator fans x3", "motherboard Fan #7")
        line = []
        if act is not None:
            line.append(f"{act:.0f}%")
        if rpm is not None:
            line.append(f"{rpm:.0f} rpm")
        if want is not None:
            line.append(f"curve asks {want:.0f}% at {cpu:.0f}°C")
        c.create_text(x + 14, y + 40, text="   ".join(line) or "no data",
                      fill=INK, font=FONT, anchor="w")
        c.create_text(x + 14, y + 62,
                      text="The CPU's only cooling, so this ramps earlier and "
                           "harder than the case fans.",
                      fill=MUTED, font=FONT_S, anchor="w")

        self._chart(x + 46, y + 86, w - 120, h - 130, {"cpu_tctl": curve},
                    live_temp=cpu, live_duty=act, lead="cpu_tctl",
                    min_duty=d["rad_min"])
        self._legend(x + 46, y + h - 34, {"cpu_tctl": curve}, "cpu_tctl")

        if want is not None and act is not None and abs(want - act) > CURVE_TOLERANCE:
            msg = (f"radiator curve asks {want:.0f}% but hardware reports "
                   f"{act:.0f}%")
            self.notes.append(msg)


if __name__ == "__main__":
    import tkinter as tk
    root = tk.Tk()
    root.title("Fan curves")
    root.configure(bg=BG)
    W, H = 1130, 1120
    cv = tk.Canvas(root, width=W, height=H, bg=BG, highlightthickness=0)
    cv.pack(fill="both", expand=True)
    fp = FanPanel(cv, W, H)

    def tick():
        for n in fp.refresh():
            print("note:", n)
        root.after(1000, tick)

    tick()
    root.mainloop()
