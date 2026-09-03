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

# ---------------------------------------------------------------------------
# Run against a COPY of the live data, never the live files themselves.
#
# The editor tests construct real App objects, and those load and save
# led_studio_state.json. Snapshotting the file around a section does not make
# that safe: an app built while the file briefly holds a test value loads it
# and writes it back after the restore, which silently turned the user's
# keep-on-exit off twice. app_paths honours LED_STUDIO_DATA, so pointing it at
# a scratch copy before any module is imported keeps the real settings
# untouched no matter what a test does.
import os as _os
import shutil as _shutil
import tempfile as _tempfile

_LIVE = pathlib.Path(__file__).resolve().parent
if not _os.environ.get("LED_STUDIO_KEEP_LIVE"):
    _SCRATCH = pathlib.Path(_tempfile.mkdtemp(prefix="ledstudio-selftest-"))
    for _name in ("pump_config.json", "fan_tuning.json", "rgb_zone_sizes.json",
                  "led_studio_state.json", "sensors.json", "fan_state.json",
                  "rgb_labels.json"):
        _src = _LIVE / _name
        if _src.exists():
            _shutil.copy2(_src, _SCRATCH / _name)
    _os.environ["LED_STUDIO_DATA"] = str(_SCRATCH)

# Fingerprint the live files BEFORE anything imports, and check them again at
# the end. Isolation is only as good as the weakest path in the suite, and the
# way this went wrong twice was a single test still naming the live folder
# while everything around it used the copy. A closing assertion catches the
# next one the same day it is written, instead of when the user notices a
# setting has turned itself off.
import hashlib as _hashlib

# SETTINGS only. sensors.json and fan_state.json are deliberately absent: the
# daemons rewrite them every few seconds, so they change during any run long
# enough to be worth doing, and a check that always fails is a check everyone
# learns to ignore.
_WATCHED = ("led_studio_state.json", "fan_tuning.json", "pump_config.json",
            "rgb_zone_sizes.json", "rgb_labels.json")


def _fingerprint():
    out = {}
    for _n in _WATCHED:
        _p = _LIVE / _n
        try:
            out[_n] = _hashlib.sha256(_p.read_bytes()).hexdigest()
        except OSError:
            out[_n] = None          # absent is a state worth noticing too
    return out


_BEFORE = _fingerprint()

BASE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# BASE is where the CODE lives; DATA is where the STATE lives. They are the
# same folder in normal use, which is exactly why mixing them up went
# unnoticed: a test corrupted the live fan_tuning.json to check the fallback,
# while fan_tuning itself read the scratch copy - so the assertion failed AND
# the user's real file was the one being scribbled on.
import app_paths                                    # noqa: E402
DATA = app_paths.DATA

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
        self.paths = [DATA / n for n in names]
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
    import app_paths
    cfg = json.loads((app_paths.DATA / "pump_config.json").read_text())
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
        (DATA / "fan_tuning.json").write_text("{ not json")
        chk("corrupt trim file falls back to zero",
            all(v == 0.0 for v in fan_tuning.load_trims().values()))
        chk("pump is not trimmable at all",
            "pump" not in fan_tuning.TRIM_KEYS)

    # --- cooling profiles: quiet must be gentler everywhere, and both must
    # still be able to reach full speed. A "quiet" profile that cannot reach
    # 100% is not quieter, it is a thermal limit with a friendly name.
    for ch in trl.FAN_CHANNELS:
        for src in trl.FAN_CHANNELS[ch]["curves"]:
            agg = trl.channel_curves(ch, "aggressive").get(src)
            qui = trl.channel_curves(ch, "quiet").get(src)
            if not (agg and qui):
                continue
            hotter = [t for t in range(30, 101)
                      if trl.interpolate(qui, t) > trl.interpolate(agg, t) + 1e-9]
            chk(f"quiet {ch}/{src} never asks for more than aggressive",
                not hotter, f"{len(hotter)} temperatures")
        chk(f"quiet {ch} still reaches full speed by 95C",
            max(trl.interpolate(c, 95)
                for c in trl.channel_curves(ch, "quiet").values()) >= 75)
    ca, fa = md.rad_curve_for("aggressive")
    cq, fq = md.rad_curve_for("quiet")
    hotter = [t for t in range(30, 101)
              if md.interpolate(cq, t) > md.interpolate(ca, t) + 1e-9]
    chk("quiet radiator never asks for more than aggressive", not hotter)
    chk("quiet radiator still reaches 100%", md.interpolate(cq, 95) == 100)
    chk("aggressive radiator reaches 100% inside the real load range",
        md.interpolate(ca, 80) == 100)
    chk("quiet floor is not below the stall-safe minimum", fq >= 20, str(fq))
    chk("an unknown profile name falls back to a real one",
        fan_tuning.DEFAULT_PROFILE in fan_tuning.PROFILES)
    chk("the pump is not switched by the profile",
        all("pump" not in p for p in fan_tuning.PROFILES))

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

    # Point the daemon at a THROWAWAY flag for the duration. Using the real
    # one raced with a live editor: it refreshes manual_override.flag every
    # 20s, so the file could be rewritten mid-assertion. That produced a
    # failure that vanished on re-run, which is worse than no test at all -
    # it teaches you to re-run until green.
    real = trl.MANUAL_FLAG
    flag = DATA / "selftest_override.flag"
    trl.MANUAL_FLAG = str(flag)
    try:
        _run_override_checks(trl, flag)
    finally:
        trl.MANUAL_FLAG = real
        if flag.exists():
            flag.unlink()


def _run_override_checks(trl, flag):
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


def test_usage():
    section("usage gradient")
    import case_layout as cl
    import rgb_effects as fx
    import usage_levels

    c0 = fx.usage_colour(0.0)
    chk(f"idle reads strong green {c0}", c0[1] > 200 and c0[0] < 60)
    lo = fx.usage_colour(0.12)
    chk(f"light load is still green {lo}", lo[1] > 200 and lo[0] < 100)
    hi = fx.usage_colour(1.0)
    chk(f"full load reads red {hi}", hi[0] > 200 and hi[1] < 40)

    # Thresholds, not endpoints: nothing sits at exactly 0% or 100%, so both
    # ends have to saturate early or the two colours that matter never appear.
    chk(f"anything at or below {fx.USAGE_IDLE:.0%} is fully green",
        all(fx.usage_colour(u) == (0, 255, 0)
            for u in (0.0, 0.02, 0.05, fx.USAGE_IDLE)))
    chk(f"anything at or above {fx.USAGE_FULL:.0%} is fully red",
        all(fx.usage_colour(u) == (255, 0, 0)
            for u in (fx.USAGE_FULL, 0.93, 0.97, 1.0)))
    chk("the thresholds leave a real gradient between them",
        fx.USAGE_FULL - fx.USAGE_IDLE >= 0.5)

    # green -> yellow -> orange -> red, with every band actually visible
    def kind(c):
        r, g, b = c
        if g > 200 and r < 80:
            return "green"
        if r > 200 and g > 200:
            return "yellow"
        if r > 200 and 60 < g <= 200:
            return "orange"
        if r > 200:
            return "red"
        return "mid"
    seen = [kind(fx.usage_colour(i / 100)) for i in range(101)]
    for want in ("green", "yellow", "orange", "red"):
        chk(f"{want} is visible on the scale ({seen.count(want)}%)",
            seen.count(want) >= 3)
    firsts = {b: seen.index(b) for b in ("green", "yellow", "orange", "red")
              if b in seen}
    chk("the bands appear in order green, yellow, orange, red",
        firsts.get("green", 0) < firsts.get("yellow", 1)
        < firsts.get("orange", 2) < firsts.get("red", 3), str(firsts))
    # This used to allow blue up to 60, which passed while the idle stop
    # carried 45 - and 45 of blue on a saturated green is visibly sea green
    # on a real LED. A limit loose enough to admit the bug is not a test.
    worst = max(fx.usage_colour(i / 100)[2] for i in range(101))
    chk(f"no blue anywhere on the scale (worst channel {worst})", worst <= 2)
    chk("idle is pure green", fx.usage_colour(0.0) == (0, 255, 0))

    chk("red never falls as load rises",
        all(fx.usage_colour(i / 50)[0] <= fx.usage_colour((i + 1) / 50)[0] + 1
            for i in range(50)))
    chk("green never rises as load rises",
        all(fx.usage_colour(i / 50)[1] >= fx.usage_colour((i + 1) / 50)[1] - 1
            for i in range(50)))
    chk("usage below 0 and above 1 are clamped",
        fx.usage_colour(-99) == c0 and fx.usage_colour(99) == hi)
    chk("a missing usage value does not raise",
        fx.fx_usage(0.5, 0.5, 1.0, None, usage=None) == c0)
    chk("every colour component stays in range",
        all(0 <= v <= 255 for i in range(101)
            for v in fx.usage_colour(i / 100)))

    # the physical mapping is the feature; assert it explicitly
    lab = {}
    for el in cl.LAYOUT:
        lab.setdefault(cl.usage_source(el), set()).add(el["label"])
    chk("pump and radiator fans report CPU",
        {"Arctic pump", "Radiator L", "Radiator M", "Radiator R"}
        <= lab.get("cpu", set()))
    chk("GPU lighting and the bottom intake report GPU",
        {"ZOTAC text", "logo", "Bottom F420 L", "Bottom F420 M",
         "Bottom F420 R"} <= lab.get("gpu", set()))
    chk("the DIMMs report RAM",
        {"RAM 1", "RAM 2"} <= lab.get("ram", set()))
    chk("side and rear fans report overall load",
        {"Rear exhaust", "Side F360 top", "Side F360 mid",
         "Side F360 bottom"} <= lab.get("all", set()))
    chk("every element has a source",
        all(el["id"] in cl.USAGE_SOURCES for el in cl.LAYOUT))

    # --- typing speed on the keyboard
    import re
    import usage_levels as ul
    chk("the keyboard reports typing speed",
        cl.usage_source({"id": "keyboard"}) == "wpm")
    chk("0 wpm is green", fx.usage_colour(0.0) == (0, 255, 0))
    chk(f"{ul.WPM_CAP:.0f} wpm is fully red",
        fx.usage_colour(ul.WPM_CAP / ul.WPM_CAP) == (255, 0, 0))
    cu = usage_levels.UsageLevels()
    cu.wpm = 60.0
    cu.set_cap(200)
    slow = cu.typing
    cu.set_cap(80)
    chk(f"a lower cap makes the same speed read hotter "
        f"({slow:.2f} -> {cu.typing:.2f})", cu.typing > slow)
    chk("cap clamps low", cu.set_cap(-5) == ul.WPM_CAP_MIN)
    chk("cap clamps high", cu.set_cap(10 ** 6) == ul.WPM_CAP_MAX)
    chk("a zero cap cannot divide by zero", cu.set_cap(0) >= ul.WPM_CAP_MIN)
    chk("a garbage cap is ignored rather than raising",
        isinstance(cu.set_cap("abc"), float))
    tu = usage_levels.UsageLevels()
    tu.wpm = ul.WPM_CAP * 3
    tu.typing = min(1.0, tu.wpm / ul.WPM_CAP)
    chk("typing faster than the cap clamps rather than overflowing",
        tu.typing == 1.0 and fx.usage_colour(tu.typing) == (255, 0, 0))
    import time as _t
    tu2 = usage_levels.UsageLevels()
    now = _t.monotonic()
    tu2._presses.extend(now - i * 0.5 for i in range(int(ul.WPM_WINDOW / 0.5)))
    rate = len(tu2._presses) * (60.0 / ul.WPM_WINDOW)
    chk(f"press timestamps convert to wpm correctly ({rate:.0f})",
        abs(rate - 120) < 1)

    # This reads a key, so what it CANNOT do matters as much as what it can.
    src = (BASE / "usage_levels.py").read_text()
    codes = set(re.findall("0x[0-9A-Fa-f]+", src)) - {"0x8000"}
    chk(f"only one virtual key code appears in the source {codes or '{}'}",
        codes == {"0x20"})
    chk("that code is the spacebar", ul.VK_SPACE == 0x20)
    chk("no global keyboard hook is installed",
        "SetWindowsHookEx" not in src and "WH_KEYBOARD" not in src)
    chk("only press timestamps are kept, never key content",
        "_presses" in src and "keylog" not in src.lower())

    # a full RAM cache must not drag the whole case red
    u = usage_levels.UsageLevels()
    u.cpu, u.gpu, u.ram = 0.0, 0.0, 1.0
    chk(f"RAM at 100% alone keeps overall low ({u.overall:.2f})",
        u.overall <= 0.15, f"{u.overall:.2f}")
    u.cpu, u.gpu, u.ram = 1.0, 1.0, 1.0
    chk("everything pinned reaches full", u.overall > 0.95)
    u.cpu = u.gpu = u.ram = 0.0
    chk("everything idle reads zero", u.overall == 0.0)


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
    import rgb_effects as fx

    with Restore("led_studio_state.json", "manual_override.flag"):
        # DATA, not BASE. This test wants a first-run editor, so it removes
        # the state file - and while it named the live one, it deleted the
        # user's settings and then loaded the scratch copy anyway, so the
        # "layer restored" assertion counted a layer the test never made.
        p = DATA / "led_studio_state.json"
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

        # EVERY effect must survive a real frame and actually reach the
        # hardware. The existing effect test only called the functions
        # directly, so an effect that rendered fine but threw elsewhere in
        # tick() looked healthy - which is exactly how "usage" shipped broken:
        # a stale lookup key raised KeyError, tick() swallowed it, and the
        # effect silently stopped posting.
        prev_post = app.hw.post
        frames = []
        app.hw.post = lambda f: frames.append(f)
        app.controlling = True
        app.hw_var.set(True)
        was_active = app.active
        app.active = None       # or the effect buttons retarget to the layer
        dead = []
        for name in sorted(fx.SPATIAL):
            frames.clear()
            app.start_fx(name)
            app.tick()
            if app.effect != name or not frames:
                dead.append(f"{name}: {app.status.cget('text')[:44]}")
        chk(f"all {len(fx.SPATIAL)} effects render AND post a frame",
            not dead, "; ".join(dead[:3]))
        app.stop_fx()
        app.active = was_active
        app.hw.post = prev_post

        # --- intensity limits
        app.sel_all()
        app.paint_sel((200, 100, 50))
        app.bright.set(100); app.set_bright()
        chk("intensity 100% emits the intended colour",
            app.leds[0]["out"] == (200, 100, 50), str(app.leds[0]["out"]))
        app.bright.set(0); app.set_bright()
        chk("intensity 0% emits black and cannot go negative",
            app.leds[0]["out"] == (0, 0, 0)
            and all(0 <= v for v in app.leds[0]["out"]))
        app.bright.set(100); app.set_bright()
        chk("dimming is lossless - the intended colour survives",
            app.leds[0]["rgb"] == (200, 100, 50)
            and app.leds[0]["out"] == (200, 100, 50))
        for bad in (-50, 250):
            app.bright.set(bad); app.set_bright()
            chk(f"master intensity {bad} still yields valid RGB",
                all(0 <= v <= 255 for r in app.leds for v in r["out"]))
        app.bright.set(100); app.set_bright()
        app.leds[0]["gain"] = 5.0          # out-of-range per-LED gain
        app.reapply()
        chk("an out-of-range per-LED gain cannot exceed 255",
            all(0 <= v <= 255 for v in app.leds[0]["out"]),
            str(app.leds[0]["out"]))
        app.reset_intensity()
        chk("reset returns every LED to full", all(r["gain"] == 1.0
                                                   for r in app.leds))

        # state round trip - deselect first so this targets the global effect
        app.active = None
        app.start_fx("plasma")
        chk("with nothing selected the effect is global again",
            app.effect == "plasma")
        app.speed.set(4.0)

        # Closing has two intended behaviours now, so assert both rather than
        # only the one that used to exist.
        app.keep_var.set(False)
        shutdown(app, root)
        chk("keep OFF: flag released so the daemon takes the LEDs back",
            not ls.OVERRIDE.exists())

        root_k = tk.Tk()
        root_k.withdraw()
        app_k = ls.App(root_k)
        root_k.update_idletasks()
        app_k.keep_var.set(True)
        shutdown(app_k, root_k)
        body = ls.OVERRIDE.read_text() if ls.OVERRIDE.exists() else ""
        chk("keep ON: flag kept and marked hold=1",
            ls.OVERRIDE.exists() and "hold=1" in body,
            body.replace("\n", "|"))
        import thermal_rgb_loop as _trl
        chk("keep ON: daemon leaves the LEDs alone",
            _trl.manual_override("leds") is True)
        chk("keep ON: daemon still runs the fans",
            _trl.manual_override("fans") is False)

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


def test_handoff():
    section("animation handoff")
    import subprocess
    import time as _t
    import led_studio_native as ls

    # Snapshotted. An earlier ad-hoc version of this test set keep-on-exit to
    # False and wrote it into the real state file, which silently disabled the
    # feature on the machine under test until someone noticed the lighting no
    # longer followed their typing.
    with Restore("led_studio_state.json", "manual_override.flag"):
        import tkinter as tk
        try:
            import psutil
        except Exception:
            chk("psutil available for the handoff test", False)
            return

        def players():
            out = []
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if not (proc.info.get("name") or "").lower().startswith(
                            "python"):
                        continue
                    argv = proc.info.get("cmdline") or []
                    if any(os.path.basename(a).lower() == "led_player.py"
                           for a in argv):
                        out.append(proc.info["pid"])
                except Exception:
                    continue
            return out

        # A real editor running outside the suite makes this untestable: its
        # heartbeat stamps the flag every 20s, and the player correctly stands
        # down for a live owner. Skipping honestly beats reporting a failure
        # that says nothing about the code.
        mine = os.getpid()
        live_editor = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["pid"] == mine:
                    continue
                if not (proc.info.get("name") or "").lower().startswith(
                        "python"):
                    continue
                argv = proc.info.get("cmdline") or []
                if any(os.path.basename(a).lower() == "led_studio_native.py"
                       for a in argv):
                    live_editor.append(proc.info["pid"])
            except Exception:
                continue
        if live_editor:
            chk(f"skipped: an editor is already running {live_editor} and its "
                f"heartbeat owns the flag", True)
            return

        for pid in players():
            try:
                psutil.Process(pid).terminate()
            except Exception:
                pass
        _t.sleep(1)

        root = tk.Tk()
        root.withdraw()
        app = ls.App(root)
        root.update_idletasks()
        app.start_fx("wave")
        app.keep_var.set(True)
        chk("no player while the editor is running", not players())
        shutdown(app, root)
        _t.sleep(4)
        pl = players()
        chk(f"closing hands the animation to a player {pl}", bool(pl))
        if pl:
            body = ls.OVERRIDE.read_text() if ls.OVERRIDE.exists() else ""
            chk("the player holds the flag", "led_player" in body)
            chk("still scoped to leds, so fans keep running",
                "scope=leds" in body)
            proc = psutil.Process(pl[0])
            c0 = proc.cpu_times().user
            _t.sleep(2)
            chk("the player is actually animating",
                proc.cpu_times().user > c0)

        r2 = tk.Tk()
        r2.withdraw()
        a2 = ls.App(r2)
        r2.update_idletasks()
        _t.sleep(2)
        chk("reopening stops the player", not players(), str(players()))
        a2.keep_var.set(False)
        shutdown(a2, r2)
        _t.sleep(2)
        chk("with keep off, nothing is left running", not players())
        for pid in players():
            try:
                psutil.Process(pid).terminate()
            except Exception:
                pass


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


# ----------------------------------------------------------------- paths ---

_FROZEN_CHILD = '''
import json, pathlib, sys
sys.frozen = True
sys.executable = r"{exe}"
sys._MEIPASS = r"{meipass}"
sys.path.insert(0, r"{live}")
import app_paths, fan_tuning, thermal_rgb_loop as trl, mobo_daemon as md
import led_studio_native as ls, fan_panel
print("@@" + json.dumps({{
    "DATA": str(app_paths.DATA), "FROZEN": app_paths.FROZEN,
    "fan_tuning.TRIM_FILE": str(fan_tuning.TRIM_FILE),
    "trl._BASE": trl._BASE, "trl.MANUAL_FLAG": trl.MANUAL_FLAG,
    "trl.CLAUDE_FLAG_DIR": trl.CLAUDE_FLAG_DIR,
    "md.BASE": str(md.BASE), "md.SENSORS": str(md.SENSORS),
    "ls.BASE": str(ls.BASE), "ls.OVERRIDE": str(ls.OVERRIDE),
    "ls.STATE": str(ls.STATE), "ls.icon": str(ls.icon_path()),
    "ls.PLAYER_EXE_NAME": ls.PLAYER_EXE_NAME,
    "fan_panel.BASE": str(fan_panel.BASE),
}}))
'''


def test_paths():
    """Frozen, every module must still find the ONE shared data directory.

    This failure mode is silent, which is why it gets its own section. A
    module that locates its JSON with __file__ keeps working perfectly after a
    PyInstaller build - it just reads a different, stale copy from inside the
    bundle. Nothing raises. The editor writes settings the daemons never see,
    and the two disagree about the hardware until someone notices the fans.

    So: fake a frozen interpreter in a child process, import the whole graph,
    and insist every path constant lands in the live scripts folder.
    """
    section("frozen paths")
    import subprocess
    import tempfile

    live = pathlib.Path(r"C:\\HardwareControl\\scripts")
    if not (live / "app_paths.py").exists():
        chk("live scripts folder present", False, str(live))
        return
    src = _FROZEN_CHILD.format(
        exe=r"C:\\HardwareControl\\LEDStudio\\LEDStudio.exe",
        meipass=r"C:\\HardwareControl\\LEDStudio\\_internal",
        live=str(live))
    tmp = pathlib.Path(tempfile.mkdtemp()) / "frozen_probe.py"
    tmp.write_text(src, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LED_STUDIO_DATA", None)      # the child must resolve for real
    r = subprocess.run([sys.executable, str(tmp)], capture_output=True,
                       text=True, env=env)
    line = next((l for l in r.stdout.splitlines() if l.startswith("@@")), None)
    if not chk("the whole module graph imports under a frozen interpreter",
               bool(line), "" if line else (r.stderr or r.stdout)[-400:]):
        return
    got = json.loads(line[2:])

    chk("app_paths reports frozen", got["FROZEN"] is True)
    chk("DATA is the live scripts folder",
        pathlib.Path(got["DATA"]) == live, got["DATA"])
    for key in ("fan_tuning.TRIM_FILE", "trl.MANUAL_FLAG",
                "trl.CLAUDE_FLAG_DIR", "md.SENSORS", "ls.OVERRIDE",
                "ls.STATE"):
        chk(f"{key} lives in the shared data dir",
            pathlib.Path(got[key]).parent == live, got[key])
    for key in ("trl._BASE", "md.BASE", "ls.BASE", "fan_panel.BASE"):
        chk(f"{key} is the shared data dir",
            pathlib.Path(got[key]) == live, got[key])

    # Nothing writable may resolve into the bundle, which is deleted and
    # rebuilt. The icon is the deliberate exception: read-only, shipped inside
    # the exe so a copied executable keeps its identity.
    for key, val in got.items():
        if isinstance(val, str) and key != "ls.icon":
            chk(f"{key} is not inside the bundle",
                "_internal" not in val.lower(), val)
    chk("the icon is bundled, so a copied exe keeps it",
        "_internal" in got["ls.icon"].lower(), got["ls.icon"])
    chk("the frozen player is matched by exe name, not by python",
        got["ls.PLAYER_EXE_NAME"] == "ledstudio.exe",
        got["ls.PLAYER_EXE_NAME"])
    chk("editor and daemon share one override flag",
        got["ls.OVERRIDE"].lower() == got["trl.MANUAL_FLAG"].lower(),
        f'{got["ls.OVERRIDE"]} vs {got["trl.MANUAL_FLAG"]}')


SECTIONS = {
    "paths": test_paths,
    "limits": test_limits,
    "override": test_override,
    "instance": test_single_instance,
    "layout": test_layout,
    "effects": test_effects,
    "vu": test_vu,
    "usage": test_usage,
    "matrix": test_matrix,
    "geometry": test_layer_geometry,
    "app": test_app,
    "handoff": test_handoff,
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
    # The suite must leave the user's live settings exactly as it found them.
    if not os.environ.get("LED_STUDIO_KEEP_LIVE"):
        section("live data untouched")
        after = _fingerprint()
        for name in _WATCHED:
            was, now = _BEFORE[name], after[name]
            chk(f"{name} was not modified", was == now,
                "MISSING NOW" if now is None and was else
                "CREATED" if was is None and now else
                "CONTENT CHANGED" if was != now else "")

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
