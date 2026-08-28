"""Keeps the animation running after the editor is closed.

    pythonw led_player.py

LED Studio held its last frame on exit, so closing the window froze whatever
was on screen. This carries on rendering it instead - same effects, same
layers, same palette and intensity - reading the state file the editor writes.

It reuses the editor's own Hardware thread rather than reimplementing device
resolution. That class is plain threading and touches no Tk, so importing it
here costs nothing and there is only one piece of code deciding which LED on
which device an element maps to.

It stands down the moment the editor comes back: the editor stamps its own pid
into the override flag on launch, and seeing a different owner there is the
signal to exit. Two processes writing the same LEDs is the failure this
project has already been bitten by twice.
"""
import json
import os
import pathlib
import queue
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import case_layout                                  # noqa: E402
import fx_layers                                    # noqa: E402
import rgb_effects as fx                            # noqa: E402
import single_instance                              # noqa: E402
import usage_levels                                 # noqa: E402
from led_studio_native import Hardware, OVERRIDE, STATE   # noqa: E402

FPS = 20.0                  # the hardware write rate; no point going faster


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def own_flag():
    try:
        OVERRIDE.write_text("led_player\npid=%d\nscope=leds\nhold=1\n"
                            % os.getpid())
    except OSError:
        pass


def _alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


def should_stop():
    """True once a LIVE editor owns the flag.

    Not simply "the flag is not ours". The editor stamps the flag on close and
    the player stamps its own a moment later, so a plain ownership test made
    the player quit during that handover - and quit again every time it lost a
    race with the editor's twenty-second heartbeat. Only a pid that belongs to
    a process still running means someone else is really driving.
    """
    try:
        body = OVERRIDE.read_text()
    except OSError:
        return True                   # flag gone entirely: stand down
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                pid = int(line[4:])
            except ValueError:
                return False
            if pid == os.getpid():
                return False
            return _alive(pid)
    return False                      # no pid recorded: nobody live owns it


def build_leds():
    leds = []
    for el, i, nx, ny in case_layout.led_positions():
        x, y = case_layout._ring_xy(el, i)
        leds.append({"el": el, "i": i, "x": x, "y": y, "nx": nx, "ny": ny,
                     "rgb": (0, 0, 0), "manual": (0, 0, 0),
                     "cell": case_layout.cell_of(el, i),
                     "usrc": case_layout.usage_source(el), "gain": 1.0})
    return leds


def main():
    if not single_instance.claim("LedPlayer"):
        return 0
    st = load_state()
    effect = st.get("effect")
    layers = []
    for d in st.get("layers") or []:
        try:
            lay = fx_layers.Layer(d["effect"], d["x"], d["y"], d["w"], d["h"],
                                  angle=d.get("angle", 0.0),
                                  palette=d.get("palette"),
                                  opacity=d.get("opacity", 1.0),
                                  blend=d.get("blend", "normal"),
                                  speed=d.get("speed", 1.0))
            lay.on = bool(d.get("on", True))
            layers.append(lay)
        except Exception:
            continue
    if not effect and not any(l.on for l in layers):
        return 0                      # nothing was animating; nothing to do

    leds = build_leds()
    man = st.get("manual")
    if isinstance(man, list) and len(man) == len(leds):
        for r, c in zip(leds, man):
            r["manual"] = tuple(c)
    gains = st.get("gains")
    if isinstance(gains, list) and len(gains) == len(leds):
        for r, g in zip(leds, gains):
            try:
                r["gain"] = max(0.0, min(1.0, float(g)))
            except (TypeError, ValueError):
                pass
    for lay in layers:
        lay.reindex(leds)

    bright = float(st.get("brightness", 100)) / 100.0
    speed = float(st.get("speed", 1.0))
    light_kb = st.get("light_keyboard", True)
    custom = [tuple(c) for c in (st.get("custom") or [])] or None
    pal_names = st.get("palettes") or {}
    default_pal = st.get("palette_name", "synthwave")

    def palette_for(name):
        name = name or default_pal
        if name == "custom" and custom:
            return custom
        return fx.PALETTES.get(name, fx.SYNTHWAVE)

    if effect:
        fx.set_vu_gain(float(st.get("gain", 10)) / 10.0)
    if st.get("wpm_cap"):
        usage_levels.SHARED.set_cap(st["wpm_cap"])
    if st.get("bars"):
        fx.VU_BARS = int(st["bars"])

    out = queue.Queue()
    hw = Hardware(out)
    hw.start()
    own_flag()

    live = [l for l in layers if l.on and l.effect in fx.SPATIAL]
    needs_usage = (effect in fx.USAGE_AWARE
                   or any(l.effect in fx.USAGE_AWARE for l in live))
    if needs_usage:
        usage_levels.SHARED.start()

    base_fn = fx.SPATIAL.get(effect) if effect else None
    base_cells = effect in fx.CELL_AWARE
    base_usage = effect in fx.USAGE_AWARE
    pal = palette_for(pal_names.get(effect))
    period = 1.0 / FPS
    t0 = time.monotonic()
    beat = 0.0

    try:
        while True:
            if should_stop():
                break                 # a live editor is back; it owns the LEDs
            now = time.monotonic()
            t = (now - t0) * speed
            lv = None
            if needs_usage:
                lv = {k: usage_levels.SHARED.value(k)
                      for k in set(case_layout.USAGE_SOURCES.values())}
            for r in leds:
                if base_fn is None:
                    c = r["manual"]
                elif base_usage:
                    c = base_fn(r["nx"], r["ny"], t, pal, usage=lv[r["usrc"]])
                elif base_cells and r["cell"]:
                    c = base_fn(r["nx"], r["ny"], t, pal, cell=r["cell"])
                else:
                    c = base_fn(r["nx"], r["ny"], t, pal)
                r["rgb"] = c
            for lay in live:
                lfn = fx.SPATIAL[lay.effect]
                lpal = palette_for(lay.palette)
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
                    r["rgb"] = lay.apply(r["rgb"], col)

            frame = {}
            for r in leds:
                f = bright * r["gain"]
                c = r["rgb"]
                if f < 0.999:
                    c = tuple(max(0, min(255, int(v * f))) for v in c)
                frame.setdefault(r["el"]["id"], []).append(c)
            if not light_kb:
                for el in case_layout.LAYOUT:
                    if el.get("fx_group") == "keyboard":
                        frame.pop(el["id"], None)
            hw.post(frame)

            if now - beat > 5.0:      # keep the flag fresh and ours
                beat = now
                own_flag()
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        hw.stop_flag.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
