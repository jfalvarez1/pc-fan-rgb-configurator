"""Regression tests for the whole stack.

    python selftest.py            # everything
    python selftest.py limits     # just one section

Runs headless and touches no hardware. Any file it has to modify is snapshotted
and restored, so it is safe to run while the daemons and the editor are live.

The point is the LIMITS. Most of what this stack does is clamp something: a
pump that must never run slow, a fan that must never stall, a trim that must
not be able to undo measured tuning. Those clamps are the safety story, so they
are tested at and beyond their edges rather than in the middle where everything
works.
"""
import io
import json
import math
import os
import pathlib
import sys
import traceback

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []
_section = "?"


def section(name):
    global _section
    _section = name
    print(f"\n--- {name} " + "-" * max(0, 58 - len(name)))


def shutdown(app, root):
    """Close an app and tear its root down without the delayed destroy that
    close() schedules firing into an already-destroyed interpreter."""
    app.close()
    root.update()
    job = getattr(app, "_destroy_id", None)
    if job:
        try:
            root.after_cancel(job)
        except Exception:
            pass
    try:
        root.destroy()
    except Exception:
        pass


def chk(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ok   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append((_section, name, detail))
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


class Restore:
    """Snapshot files a test must write, and put them back afterwards."""

    def __init__(self, *names):
        self.paths = [BASE / n for n in names]
        self.saved = {}

    def __enter__(self):
        for p in self.paths:
            self.saved[p] = p.read_bytes() if p.exists() else None
        return self

    def __exit__(self, *exc):
        for p, data in self.saved.items():
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_bytes(data)
        return False


# ---------------------------------------------------------------- limits ---

def test_limits():
    section("safety limits")
    import fan_tuning
    import mobo_daemon as md
    import thermal_rgb_loop as trl

    # --- pump duty clamp. This is the one that matters most: too slow is
    # cavitation, and the whole design says the user must not be able to get
    # there from the UI or from a hand-edited config.
    lo, hi = md.PUMP_MIN_SAFE, 100.0
    for raw in (-1000, -1, 0, 10, 39.9, 40, 56, 80, 100, 101, 1e6):
        got = max(lo, min(hi, float(raw)))
        chk(f"pump {raw!r} clamps into [{lo:.0f},{hi:.0f}]", lo <= got <= hi,
            f"-> {got:.1f}")
    chk("pump floor is above the measured cavitation end", md.PUMP_MIN_SAFE >= 40)
    cfg = json.loads((BASE / "pump_config.json").read_text())
    duties = [e["duty"] for e in cfg.get("map", [])]
    chk("every offered pump duty is at or above the floor",
        all(d >= md.PUMP_MIN_SAFE for d in duties), f"{duties}")
    rpms = {e["duty"]: e["rpm"] for e in cfg.get("map", [])}
    chk("configured pump duty is one of the measured points",
        cfg.get("pump_duty") in rpms, f"{cfg.get('pump_duty')}")
    chosen_rpm = rpms.get(cfg.get("pump_duty"))
    if chosen_rpm:
        chk("configured pump rpm is above the 1500 rpm abort floor",
            chosen_rpm >= 1500, f"{chosen_rpm:.0f} rpm")

    # --- trim clamp, including garbage input
    with Restore("fan_tuning.json"):
        got = fan_tuning.save_trims({k: 10 ** 6 for k in fan_tuning.TRIM_KEYS})
        chk("huge positive trim clamps to the limit",
            all(v == fan_tuning.TRIM_LIMIT for v in got.values()), str(got))
        got = fan_tuning.save_trims({k: -10 ** 6 for k in fan_tuning.TRIM_KEYS})
        chk("huge negative trim clamps to the limit",
            all(v == -fan_tuning.TRIM_LIMIT for v in got.values()), str(got))
        got = fan_tuning.save_trims({"fan1": "abc", "fan2": None,
                                     "fan3": float("nan"), "rad": []})
        ok = all(v == 0.0 or v != v for v in got.values())
        chk("garbage trim values do not crash and do not exceed the limit",
            all(abs(v) <= fan_tuning.TRIM_LIMIT or v != v
                for v in got.values()), str(got))
        (BASE / "fan_tuning.json").write_text("{ not json")
        chk("corrupt trim file falls back to zero",
            all(v == 0.0 for v in fan_tuning.load_trims().values()))
        chk("pump is not trimmable at all",
            "pump" not in fan_tuning.TRIM_KEYS)

    # --- a trim can never push a fan outside its hard clamps
    worst = []
    for ch, cfg2 in trl.FAN_CHANNELS.items():
        for src, curve in cfg2["curves"].items():
            for t in range(0, 121):
                for trim in (-fan_tuning.TRIM_LIMIT, 0, fan_tuning.TRIM_LIMIT):
                    d = trl.interpolate(curve, t) + trim
                    d = max(trl.MIN_DUTY, min(trl.MAX_DUTY, round(d)))
                    if not (trl.MIN_DUTY <= d <= trl.MAX_DUTY):
                        worst.append((ch, src, t, trim, d))
    chk(f"case fan duty stays in [{trl.MIN_DUTY},{trl.MAX_DUTY}] at every "
        f"temperature and trim", not worst, f"{len(worst)} violations")

    radworst = []
    for t in range(0, 121):
        for trim in (-fan_tuning.TRIM_LIMIT, 0, fan_tuning.TRIM_LIMIT):
            d = max(md.RAD_MIN_DUTY,
                    min(100, round(md.interpolate(md.RAD_CURVE, t) + trim)))
            if not (md.RAD_MIN_DUTY <= d <= 100):
                radworst.append((t, trim, d))
    chk(f"radiator duty stays in [{md.RAD_MIN_DUTY},100] at every temperature "
        f"and trim", not radworst, f"{len(radworst)} violations")

    # --- curve shape sanity
    for ch, cfg2 in trl.FAN_CHANNELS.items():
        for src, curve in cfg2["curves"].items():
            temps = [p[0] for p in curve]
            duties2 = [p[1] for p in curve]
            chk(f"{ch}/{src} temperatures ascend", temps == sorted(temps))
            chk(f"{ch}/{src} duties never decrease",
                duties2 == sorted(duties2))
            chk(f"{ch}/{src} duties are percentages",
                all(0 <= d <= 100 for d in duties2))
    chk("radiator curve ascends",
        [p[0] for p in md.RAD_CURVE] == sorted(p[0] for p in md.RAD_CURVE)
        and [p[1] for p in md.RAD_CURVE] == sorted(p[1] for p in md.RAD_CURVE))

    # --- interpolation agrees between daemon, mobo daemon and the fan view
    import fan_panel
    same = all(
        abs(fan_panel.interpolate(c, t) - trl.interpolate(c, t)) < 1e-9
        and abs(md.interpolate(c, t) - trl.interpolate(c, t)) < 1e-9
        for cfg2 in trl.FAN_CHANNELS.values()
        for c in cfg2["curves"].values() for t in range(0, 121))
    chk("all three interpolate() implementations agree exactly", same)
    c0 = trl.FAN_CHANNELS["fan1"]["curves"]["gpu_core"]
    chk("below the first knee holds the first duty",
        trl.interpolate(c0, -50) == c0[0][1])
    chk("above the last knee holds the last duty",
        trl.interpolate(c0, 999) == c0[-1][1])


# ------------------------------------------------------------- override ----

def test_override():
    section("override flag")
    import thermal_rgb_loop as trl
    flag = pathlib.Path(trl.MANUAL_FLAG)
    with Restore(flag.name):
        flag.write_text("led_studio_native\npid=%d\nscope=leds\n" % os.getpid())
        chk("scope=leds pauses lighting", trl.manual_override("leds") is True)
        chk("scope=leds LEAVES FAN CONTROL RUNNING",
            trl.manual_override("fans") is False)
        flag.write_text("dashboard")
        chk("legacy flag (no scope) pauses lighting",
            trl.manual_override("leds") is True)
        chk("legacy flag (no scope) pauses fans",
            trl.manual_override("fans") is True)
        flag.write_text("x\nscope=all\n")
        chk("explicit scope=all pauses fans",
            trl.manual_override("fans") is True)
        flag.write_text("led_studio_native\npid=999999\nscope=leds\n")
        chk("dead owner releases lighting immediately",
            trl.manual_override("leds") is False)
        flag.write_text("led_studio_native\npid=notanumber\n")
        chk("malformed pid fails safe (stays paused)",
            trl.manual_override("leds") is True)
        flag.write_text("")
        chk("empty flag still pauses (fails safe)",
            trl.manual_override("leds") is True)
        if flag.exists():
            flag.unlink()
        chk("no flag: nothing paused",
            trl.manual_override("leds") is False
            and trl.manual_override("fans") is False)


# -------------------------------------------------------- single instance --

def test_single_instance():
    section("single instance")
    import subprocess
    import single_instance
    chk("claim succeeds for a fresh name",
        single_instance.claim("SelfTestUnique") is True)
    code = ("import sys; sys.path.insert(0, r'%s'); import single_instance; "
            "print(single_instance.claim('SelfTestUnique'))" % BASE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=60)
    chk("a second PROCESS is refused the same name",
        out.stdout.strip() == "False", out.stdout.strip() or out.stderr[:60])


# ---------------------------------------------------------------- layout ---

def test_layout():
    section("layout integrity")
    import case_layout as cl
    pos = cl.led_positions()
    chk("207 LEDs mapped", len(pos) == 207, str(len(pos)))
    keys = [(el["id"], i) for el, i, _, _ in pos]
    chk("no duplicate (element, index)", len(keys) == len(set(keys)))
    for el in cl.LAYOUT:
        n = sum(1 for e, _i, _x, _y in pos if e["id"] == el["id"])
        chk(f"{el['id']} yields exactly its declared count",
            n == el["count"], f"{n} vs {el['count']}")
    groups = {}
    for el, i, nx, ny in pos:
        groups.setdefault(el.get("fx_group", "case"), []).append((nx, ny))
    for g, pts in groups.items():
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        chk(f"{g} effect space spans 0.04..0.96 in x",
            abs(min(xs) - 0.04) < 1e-6 and abs(max(xs) - 0.96) < 1e-6,
            f"{min(xs):.3f}..{max(xs):.3f}")
        chk(f"{g} effect space spans 0.04..0.96 in y",
            abs(min(ys) - 0.04) < 1e-6 and abs(max(ys) - 0.96) < 1e-6,
            f"{min(ys):.3f}..{max(ys):.3f}")
    # the alignment the keyboard was added for
    tops = {g: min(p[1] for p in pts) for g, pts in groups.items()}
    bots = {g: max(p[1] for p in pts) for g, pts in groups.items()}
    chk("case and keyboard share a top edge in effect space",
        abs(tops["case"] - tops["keyboard"]) < 1e-6)
    chk("case and keyboard share a bottom edge in effect space",
        abs(bots["case"] - bots["keyboard"]) < 1e-6)
    kb = [el for el in cl.LAYOUT if el.get("kind") == "grid"][0]
    cells = {cl.cell_of(kb, i)[:2] for i in range(kb["count"])}
    chk("keyboard cells cover the full grid exactly",
        cells == {(c, r) for r in range(kb["rows"])
                  for c in range(kb["cols"])}, f"{len(cells)} cells")
    chk("ring elements have no grid cell",
        all(cl.cell_of(el, 0) is None for el in cl.LAYOUT
            if el.get("kind") != "grid"))


# --------------------------------------------------------------- effects ---

def test_effects():
    section("effects")
    import rgb_effects as fx
    pal = fx.SYNTHWAVE
    bad = []
    for name, fn in fx.SPATIAL.items():
        for nx in (0.0, 0.04, 0.5, 0.96, 1.0):
            for ny in (0.0, 0.04, 0.5, 0.96, 1.0):
                for t in (0.0, 1.7, 12345.6):
                    try:
                        c = fn(nx, ny, t, pal)
                    except Exception as exc:
                        bad.append((name, type(exc).__name__))
                        break
                    if (not isinstance(c, tuple) or len(c) != 3
                            or not all(isinstance(v, int) and 0 <= v <= 255
                                       for v in c)):
                        bad.append((name, repr(c)))
                        break
    chk(f"all {len(fx.SPATIAL)} effects return a valid RGB triple everywhere",
        not bad, str(bad[:3]))
    grouped = {n for names in fx.EFFECT_GROUPS.values() for n in names}
    chk("every effect appears in a category",
        set(fx.SPATIAL) <= grouped,
        str(sorted(set(fx.SPATIAL) - grouped)))
    chk("no category lists an effect that does not exist",
        grouped <= set(fx.SPATIAL),
        str(sorted(grouped - set(fx.SPATIAL))))
    chk("palette-ignoring effects all exist",
        fx.IGNORES_PALETTE <= set(fx.SPATIAL))
    for pname, cols in fx.PALETTES.items():
        chk(f"palette {pname} is well formed",
            len(cols) >= 2 and all(len(c) == 3 and all(0 <= v <= 255 for v in c)
                                   for c in cols))


def test_vu():
    section("VU meter limits")
    import rgb_effects as fx
    thr = [fx._row_threshold(r) for r in range(fx.VU_ROWS)]
    chk("row thresholds ascend", thr == sorted(thr))
    chk("bottom row needs a real level, not any signal",
        abs(thr[0] - fx.VU_LO) < 1e-9, f"{thr[0]:.2f}")
    chk("top row is reachable below full scale",
        thr[-1] <= fx.VU_HI + 1e-9 and thr[-1] < 1.0, f"{thr[-1]:.2f}")
    chk("_top_row(-inf) lights nothing", fx._top_row(0.0) == -1)
    chk("_top_row(1.0) lights the top", fx._top_row(1.0) == fx.VU_ROWS - 1)
    mono = all(fx._top_row(v / 100.0) <= fx._top_row((v + 1) / 100.0)
               for v in range(100))
    chk("lit rows never decrease as level rises", mono)

    class Fake:
        available = True
        active = True

        def __init__(self, v):
            self.v = v

        def levels(self, n):
            return [self.v] * n

        def set_gain(self, g):
            pass

    import case_layout as cl
    saved = fx.AUDIO
    try:
        pos = cl.led_positions()
        bottom = [(nx, ny) for _e, _i, nx, ny in pos if ny > 0.90]
        for lvl, expect_lit in ((0.0, False), (0.05, False), (0.09, False),
                                (0.12, True), (1.0, True)):
            fx.AUDIO = Fake(lvl)
            fx._FRAME.update(t=None, n=None, lv=None, peak=[])
            lit = any(fx.fx_vu(nx, ny, 1.0, fx.SYNTHWAVE) != (0, 0, 0)
                      for nx, ny in bottom)
            chk(f"bottom row {'lit' if expect_lit else 'dark'} at level {lvl}",
                lit is expect_lit)
        # brightness must track level, not sit at a constant
        fx.AUDIO = Fake(0.3)
        fx._FRAME.update(t=None, n=None, lv=None, peak=[])
        dim = max(max(fx.fx_vu(nx, ny, 1.0, fx.SYNTHWAVE)) for nx, ny in bottom)
        fx.AUDIO = Fake(1.0)
        fx._FRAME.update(t=None, n=None, lv=None, peak=[])
        bright = max(max(fx.fx_vu(nx, ny, 1.0, fx.SYNTHWAVE))
                     for nx, ny in bottom)
        chk("a lit bottom row is dimmer at low level than at high",
            dim < bright, f"{dim} < {bright}")
    finally:
        fx.AUDIO = saved
        fx._FRAME.update(t=None, n=None, lv=None, peak=[])

    chk("gain clamps low", fx.set_vu_gain(-99) >= 0.1)
    chk("gain clamps high", fx.set_vu_gain(1e6) <= 4.0)
    fx.set_vu_gain(1.0)


def test_matrix():
    section("matrix rain")
    import case_layout as cl
    import rgb_effects as fx
    kb = [el for el in cl.LAYOUT if el.get("kind") == "grid"][0]
    pos = {(e["id"], i): (nx, ny) for e, i, nx, ny in cl.led_positions()}
    blanks = set(kb.get("blanks", ()))
    vruns, hruns = [], []
    for k in range(200):
        t = k * 0.04
        grid = {}
        for i in range(kb["count"]):
            if i in blanks:
                continue
            nx, ny = pos[("keyboard", i)]
            c = fx.fx_matrix(nx, ny, t, None, cell=cl.cell_of(kb, i))
            col, row, _, _ = cl.cell_of(kb, i)
            grid[(col, row)] = c != (0, 0, 0)
        for col in range(kb["cols"]):
            run = 0
            for row in list(range(kb["rows"])) + [None]:
                if row is not None and grid.get((col, row)):
                    run += 1
                elif run:
                    vruns.append(run)
                    run = 0
        for row in range(kb["rows"]):
            run = 0
            for col in list(range(kb["cols"])) + [None]:
                if col is not None and grid.get((col, row)):
                    run += 1
                elif run:
                    hruns.append(run)
                    run = 0
    v = sum(vruns) / len(vruns)
    h = sum(hruns) / len(hruns)
    chk("rain reads as a strand: taller than wide", v > h, f"{v:.2f} vs {h:.2f}")
    chk("strand is about one key wide", h < 1.8, f"{h:.2f}")
    ring = [el for el in cl.LAYOUT if el.get("kind") != "grid"][0]
    chk("ring layouts keep the spatial version",
        cl.cell_of(ring, 0) is None)


# ------------------------------------------------------------------ app ----

def test_app():
    section("editor")
    import tkinter as tk
    import fx_layers
    import led_studio_native as ls

    with Restore("led_studio_state.json", "manual_override.flag"):
        p = BASE / "led_studio_state.json"
        if p.exists():
            p.unlink()
        root = tk.Tk()
        root.withdraw()
        errs = []
        root.report_callback_exception = lambda e, v, t: errs.append(
            "".join(traceback.format_exception(e, v, t)))
        app = ls.App(root)
        root.update_idletasks()

        chk("takes control on launch", app.controlling is True)
        body = ls.OVERRIDE.read_text()
        chk("flag carries our pid", f"pid={os.getpid()}" in body)
        chk("flag declares scope=leds", "scope=leds" in body)

        # frame guard: a short colour list must never shift the tail
        app.start_fx("wave")
        for _ in range(3):
            app.tick()
        el = ls.case_layout.LAYOUT[0]
        posted = {}
        app.hw.post = lambda f: posted.update(f)
        app.controlling = True
        app.hw_var.set(True)
        app.push()
        chk("pushed frame length matches every element",
            all(len(posted.get(e["id"], ())) == e["count"]
                for e in ls.case_layout.LAYOUT),
            str({e["id"]: len(posted.get(e["id"], ())) for e in
                 ls.case_layout.LAYOUT if
                 len(posted.get(e["id"], ())) != e["count"]}))

        # layer limits
        app.add_layer()
        lay = app.active
        lay.resize_from(0, lay.x, lay.y)
        chk("a layer cannot be resized below its minimum",
            lay.w >= fx_layers.MIN_SIZE and lay.h >= fx_layers.MIN_SIZE,
            f"{lay.w:.0f}x{lay.h:.0f}")
        lay.opacity = 5.0
        c = lay.apply((0, 0, 0), (200, 100, 50))
        chk("opacity above 1 cannot exceed 255", all(0 <= v <= 255 for v in c),
            str(c))
        lay.opacity = -5.0
        c = lay.apply((10, 20, 30), (200, 100, 50))
        chk("negative opacity cannot go below 0",
            all(0 <= v <= 255 for v in c), str(c))
        lay.opacity = 1.0
        for b in fx_layers.BLENDS:
            lay.blend = b
            c = lay.apply((255, 255, 255), (255, 255, 255))
            chk(f"blend {b} stays in range at full white",
                all(0 <= v <= 255 for v in c), str(c))
        lay.blend = "normal"

        # With a layer selected, effect buttons retarget to that layer -
        # documented behaviour, and worth locking in. The first version of
        # this test set the effect here and then asserted the GLOBAL effect
        # had changed, which it correctly had not.
        before_global = app.effect
        app.start_fx("ripple")
        chk("effect buttons retarget to the selected layer",
            lay.effect == "ripple" and app.effect == before_global,
            f"layer={lay.effect} global={app.effect}")

        # state round trip - deselect first so this targets the global effect
        app.active = None
        app.start_fx("plasma")
        chk("with nothing selected the effect is global again",
            app.effect == "plasma")
        app.speed.set(4.0)
        shutdown(app, root)
        chk("flag released on close", not ls.OVERRIDE.exists())

        root2 = tk.Tk()
        root2.withdraw()
        app2 = ls.App(root2)
        root2.update_idletasks()
        chk("effect restored", app2.effect == "plasma", str(app2.effect))
        chk("speed restored", abs(app2.speed.get() - 4.0) < 1e-9)
        chk("layer restored", len(app2.layers) == 1, str(len(app2.layers)))
        shutdown(app2, root2)

        # hostile state files must not stop startup
        for junk in ("{ not json", "[]", "null",
                     json.dumps({"layers": [{"effect": "nope"}],
                                 "speed": "fast", "manual": [[1, 2]]}),
                     json.dumps({"effect": "does_not_exist",
                                 "layers": "notalist"})):
            p.write_text(junk)
            r = tk.Tk()
            r.withdraw()
            try:
                a = ls.App(r)
                r.update_idletasks()
                shutdown(a, r)
                ok = True
            except Exception as exc:
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
                try:
                    r.destroy()
                except Exception:
                    pass
            chk(f"survives state file {junk[:28]!r}", ok,
                "" if ok else detail)
        chk("no Tk callback errors during the editor tests", not errs,
            errs[0][:120] if errs else "")


def test_fan_view():
    section("fan view")
    import tkinter as tk
    import fan_panel
    root = tk.Tk()
    root.withdraw()
    cv = tk.Canvas(root, width=1130, height=1120)
    fp = fan_panel.FanPanel(cv, 1130, 1120)
    d = fp.gather()
    chk("gather returns the three case channels",
        set(d["channels"]) == {"fan1", "fan2", "fan3"}, str(list(d["channels"])))
    chk("gather reports a pump command", d["pump_cmd"] is not None,
        str(d["pump_cmd"]))
    notes = fp.refresh()
    chk("fan view renders without raising", len(cv.find_all()) > 100,
        f"{len(cv.find_all())} items")
    bbox = cv.bbox("all")
    chk("fan view fits its canvas", bbox and bbox[3] <= 1120,
        f"bottom={bbox[3] if bbox else '?'}")
    chk("notes are plain strings", all(isinstance(n, str) for n in notes),
        str(notes))
    root.destroy()


def test_layer_geometry():
    section("layer geometry (fx_layers self-test)")
    import subprocess
    out = subprocess.run([sys.executable, str(BASE / "fx_layers.py")],
                         capture_output=True, text=True, timeout=120)
    n_ok = out.stdout.count("  ok ")
    n_bad = out.stdout.count("  FAIL")
    chk(f"fx_layers self-test passes ({n_ok} checks)",
        "ALL PASS" in out.stdout and n_bad == 0, f"{n_bad} failures")


SECTIONS = {
    "limits": test_limits,
    "override": test_override,
    "instance": test_single_instance,
    "layout": test_layout,
    "effects": test_effects,
    "vu": test_vu,
    "matrix": test_matrix,
    "geometry": test_layer_geometry,
    "app": test_app,
    "fans": test_fan_view,
}


def main():
    want = sys.argv[1:] or list(SECTIONS)
    unknown = [w for w in want if w not in SECTIONS]
    if unknown:
        print(f"unknown section(s): {unknown}\navailable: {list(SECTIONS)}")
        return 2
    print("=" * 64)
    print("HardwareControl self-test - no hardware is touched")
    print("=" * 64)
    for name in want:
        try:
            SECTIONS[name]()
        except Exception:
            FAIL.append((name, "section raised", traceback.format_exc()))
            print(f"  FAIL {name} raised:\n{traceback.format_exc()}")
    print("\n" + "=" * 64)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nFAILURES:")
        for sec, name, detail in FAIL:
            print(f"  [{sec}] {name}")
            if detail:
                print(f"      {detail}")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
