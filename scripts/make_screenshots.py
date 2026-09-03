"""Capture a screenshot of every LED Studio feature, for the docs.

    python make_screenshots.py

Writes PNGs to docs/screenshots/ and prints a markdown block for the guide.

Two things it deliberately does NOT do:

  * touch the live settings. It points LED_STUDIO_DATA at a throwaway copy
    before importing anything, exactly as the test suite does. Screenshots are
    a documentation chore; they must not be able to change what the user's
    lighting does.
  * touch the hardware. Writing is switched off and control released before
    the first frame, so this can run while the real editor and the daemons are
    live without two processes fighting over the same LEDs.

Capture is by window rectangle rather than a full-screen grab, so whatever
else is on the desktop stays out of the picture.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time

LIVE = pathlib.Path(__file__).resolve().parent
ROOT = LIVE.parent
OUT = ROOT / "docs" / "screenshots"

# Isolate BEFORE any project module is imported.
_scratch = pathlib.Path(tempfile.mkdtemp(prefix="ledstudio-shots-"))
for _n in ("pump_config.json", "fan_tuning.json", "rgb_zone_sizes.json",
           "led_studio_state.json", "sensors.json", "fan_state.json",
           "rgb_labels.json"):
    if (LIVE / _n).exists():
        shutil.copy2(LIVE / _n, _scratch / _n)
os.environ["LED_STUDIO_DATA"] = str(_scratch)

sys.path.insert(0, str(LIVE))

import tkinter as tk                                       # noqa: E402
from PIL import ImageGrab                                  # noqa: E402

import led_studio_native as ls                             # noqa: E402


def settle(root, ms=700):
    """Let Tk lay out, let the effect clock produce a frame, then repaint.

    A screenshot taken immediately after changing state catches the previous
    frame, which is how you end up with a gallery of near-identical pictures
    that do not show the thing they are captioned with.
    """
    end = time.time() + ms / 1000.0
    while time.time() < end:
        root.update()
        time.sleep(0.02)
    try:
        app.repaint(force=True)
    except Exception:
        pass
    root.update()


def shot(root, name, caption, widget=None):
    w = widget or root
    root.update_idletasks()
    root.update()
    x, y = w.winfo_rootx(), w.winfo_rooty()
    bb = (x, y, x + w.winfo_width(), y + w.winfo_height())
    img = ImageGrab.grab(bbox=bb, all_screens=True)
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.png"
    img.save(p)
    SHOTS.append((name, caption))
    print(f"  {p.relative_to(ROOT).as_posix():44} {img.width}x{img.height}")
    return p


SHOTS = []

root = tk.Tk()
app = ls.App(root)

# hardware off, control released - never fight the live editor or the daemon
try:
    if app.hw_var.get():
        app.toggle_hw()
    if app.controlling:
        app.toggle_ctl()
except Exception:
    pass

# The saved state may have the master intensity turned right down, which is
# fine on real LEDs in a dark case and useless in a screenshot - the effects
# come out as barely-visible smudges. Documentation shots run at full.
try:
    app.bright.set(100)
    app.set_bright()
except Exception:
    pass

root.update()
root.lift()
root.attributes("-topmost", True)
root.update()
settle(root, 1200)


def refresh_live_telemetry():
    """Re-copy the daemons' output into the scratch dir.

    The Fans tab reports a daemon as down when its state file has gone stale.
    Working from a copy taken at startup, that ages as the run goes on and the
    tab ends up documenting a fault that is not happening. Copying the current
    files just before the shot shows the real machine.
    """
    for n in ("sensors.json", "fan_state.json"):
        try:
            shutil.copy2(LIVE / n, _scratch / n)
        except OSError:
            pass

print("capturing...")

# --- the editor itself -----------------------------------------------------
app.show_tab("Lighting")
app.start_fx("wave")
settle(root, 1200)
shot(root, "01-main-window",
     "The editor. Every LED in the case is drawn where it physically sits, so "
     "an effect is aimed at the machine rather than at a list of channels.")

# --- effect categories -----------------------------------------------------
app.show_cat("Flow")
settle(root, 900)
shot(root, "02-effect-categories",
     "39 effects in seven categories. Picking a category swaps the row of "
     "effect buttons underneath it.")

# --- one screenshot per effect family --------------------------------------
FAMILY = [
    ("plasma", "Flow", "03-effect-plasma",
     "**Flow** - plasma, aurora, ripple, spiral, radial, snake. Continuous "
     "fields computed from each LED's real position."),
    ("matrix", "Classic", "04-effect-matrix",
     "**Classic** - matrix rain falling as vertical strands down the "
     "keyboard's real key grid, plus scanner, theater, meteor, comet, chaser."),
    ("vu", "Fill", "05-effect-vu",
     "**Fill** - the VU meter reads live desktop audio over WASAPI loopback "
     "and lights rows bottom-up per band. Also concentric, fill, stack, wipe, "
     "bounce, starburst."),
    ("twinkle", "Scatter", "06-effect-scatter",
     "**Scatter** - rain, twinkle, confetti, juggle, strobe, lightning. "
     "Per-LED random events rather than a moving field."),
    ("fire", "Glow", "07-effect-glow",
     "**Glow** - breathe, pulse, split, spectrum, fire."),
    ("usage", "System", "08-effect-usage",
     "**System** - the resource gradient. CPU drives the AIO and pump, GPU "
     "drives the card's logo and the bottom fans, RAM and overall load drive "
     "the front and rear, and the keyboard tracks your typing speed. Green "
     "through yellow and orange to red."),
]
for eff, cat, name, cap in FAMILY:
    app.show_cat(cat)
    app.start_fx(eff)
    settle(root, 1400)
    shot(root, name, cap)

# --- palettes --------------------------------------------------------------
app.show_cat("Waves")
app.start_fx("gradient")
settle(root, 1000)
shot(root, "09-palettes",
     "Palettes. Each effect remembers the palette you last used with it, so "
     "switching effects does not throw away the colour you chose.")

# --- layers ----------------------------------------------------------------
app.start_fx("wave")
if not app.layer_mode:
    app.toggle_layers()
app.add_layer()
settle(root, 1200)
shot(root, "10-effect-layers",
     "Effect layers, SignalRGB style. Drag the box to move it, the corners to "
     "resize, the top handle to rotate - only the LEDs it covers get its "
     "effect, blended over whatever is underneath.")

# --- the layer effect picker ----------------------------------------------
try:
    app.choose_effect()
    settle(root, 900)
    dlg = [w for w in app.root.winfo_children()
           if isinstance(w, tk.Toplevel) and w.winfo_viewable()]
    if dlg:
        d = dlg[-1]
        d.lift()
        d.attributes("-topmost", True)
        settle(root, 500)
        shot(root, "11-layer-effect-picker",
             "Choosing a layer's effect - the same categorised grid, without "
             "leaving the layers section.", widget=d)
        d.destroy()
except Exception as exc:
    print(f"  (layer picker skipped: {type(exc).__name__}: {exc})")

# --- fans tab --------------------------------------------------------------
settle(root, 400)
refresh_live_telemetry()
app.show_tab("Fans")
settle(root, 2200)
refresh_live_telemetry()
try:
    app.fans.refresh()
except Exception as exc:
    print(f"  (fan refresh: {type(exc).__name__}: {exc})")
settle(root, 1200)
shot(root, "12-fans-tab",
     "The Fans tab. Every channel's curve, what the daemon is commanding "
     "against what the fan is actually doing, the pump, and as many "
     "temperature readouts as the hardware exposes.")

print("\nmarkdown for the guide:\n")
for name, cap in SHOTS:
    print(f"![{cap.split('.')[0][:60]}](docs/screenshots/{name}.png)\n")
    print(f"{cap}\n")

try:
    app.close()
except Exception:
    pass
try:
    root.destroy()
except Exception:
    pass
shutil.rmtree(_scratch, ignore_errors=True)
print(f"\n{len(SHOTS)} screenshots in {OUT}")
