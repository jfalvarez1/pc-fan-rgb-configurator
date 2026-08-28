# LED Studio — User Guide

Everything this stack does, and how to drive it. For *why* it is built the way
it is — the hardware quirks, the measurements, the bugs — see `README.md`.

---

## Contents

- [The three programs](#the-three-programs)
- [Starting it](#starting-it)
- [The Lighting tab](#the-lighting-tab)
  - [Selecting LEDs](#selecting-leds)
  - [Painting colours](#painting-colours)
  - [Running an effect](#running-an-effect)
  - [Palettes](#palettes)
  - [Effect reference](#effect-reference)
  - [VU meters](#vu-meters)
- [Effect layers](#effect-layers)
  - [What blend does](#what-blend-does)
- [The Fans tab](#the-fans-tab)
  - [Reading a curve chart](#reading-a-curve-chart)
  - [Curve trim](#curve-trim)
  - [The pump](#the-pump)
- [Autostart](#autostart)
- [Troubleshooting](#troubleshooting)
- [File reference](#file-reference)

---

## The three programs

| Program | Runs as | Owns | Started by |
|---|---|---|---|
| `led_studio_native.py` | you | the editor UI; takes LEDs only while "Take control" is on | you, or the desktop shortcut |
| `thermal_rgb_loop.py` | you | the 3 NZXT case fans, all LEDs when the editor is not driving | Startup shortcut |
| `mobo_daemon.py` | **administrator** | AIO pump + radiator fans (motherboard headers) | Scheduled Task at logon |

The elevated daemon needs administrator rights because motherboard fan headers
are reached through a signed kernel driver. It publishes `sensors.json`; the
other two read it. That file is the whole bridge between them.

Only one program may own a device at a time. If two try, one silently loses —
see [Troubleshooting](#troubleshooting).

---

## Starting it

Double-click **LED Studio** (desktop shortcut), or:

```
pythonw C:\HardwareControl\scripts\led_studio_native.py
```

OpenRGB is started automatically if it is not already running.

It **starts with Windows**, **takes control automatically**, and **restores
whatever you had running** last time — effect, palette, speed, VU settings,
layers and any painted colours.

Two buttons at the top of the panel control whether anything reaches hardware:

- **Take control** — already on at launch. Takes LED ownership from
  `thermal_rgb_loop`, which stands down for lighting while the flag exists.
  Released automatically when you close the app, and the daemon's own
  temperature-reactive lighting resumes.
- **Drive hardware** — actually writes colours. With this off you get a live
  preview in the window and nothing else. Both must be on to change your case.

**Keep lighting on exit** (on by default) means your lighting stays exactly as
you left it when you close the window. Without it, closing hands the LEDs back
to `thermal_rgb_loop`, which blanks them at idle - so you would set up your
lighting, close the app, and watch it go dark.

It works by leaving `hold=1` in the flag. That is the one marking which
survives both the one-hour expiry and the dead-owner check, because it means
"the user chose this deliberately" rather than "a program is currently running".
Turn it off and closing behaves as it used to: the daemon takes the LEDs back
and resumes its own temperature-reactive lighting.

Because the editor now holds control for as long as it is open, the flag is
**scoped**: it says `scope=leds`, so the daemon keeps running your **case fan
curves** the whole time. It also carries the editor's process id, so if the
app ever crashes the daemon takes lighting back on its next poll rather than
standing down for the rest of the hour.

To stop it starting with Windows, delete `LEDStudio.lnk` from your Startup
folder (`Win+R` → `shell:startup`). To stop it taking control automatically,
set `AUTO_CONTROL = False` near the top of `led_studio_native.py`.

Your setup is saved to `led_studio_state.json` on close and every 20 seconds,
so a crash costs at most the last edit.

---

## The Lighting tab

The canvas is your case seen from the side, plus the keyboard below it. Every
dot is one addressable LED, in its real physical position.

### Selecting LEDs

| Action | Result |
|---|---|
| click an LED | select just that one |
| click a fan's centre | select the whole fan |
| shift-click | add to the selection |
| drag on empty space | rubber-band select |
| **All / None / Invert** | as named |

Selected LEDs get a white ring.

### Painting colours

Pick a swatch or **Pick…** for the full colour picker, then **Paint** to apply
it to the selection. **Blank selection** sets it black.

**Brush** mode paints continuously as you drag — no selection needed.

Painted colours are remembered as the *background*: if you then run layers
without a global effect, the layers composite on top of your painting rather
than erasing it.

### Intensity

Two sliders, multiplied together:

- **Master intensity** scales every LED in the case and on the keyboard.
- **Selected LEDs** sets a per-LED level for whatever is currently selected,
  so one fan can sit dimmer than the rest.

Both apply to painted colours and to running effects alike. Dimming is
lossless: intensity scales what is EMITTED and never the intended colour, so
turning brightness down and back up returns exactly the colour you set rather
than one that has been quantised away a little more each time.

**Reset intensities** returns everything to 100%.

### Running an effect

Effects are grouped into categories — **Waves, Flow, Classic, Fill, Scatter,
Glow**. Click a category, then an effect. It starts immediately in the preview.

**Speed** scales time for every animation, from 0.1× to 8×.

### Palettes

The strip shows the active palette. `<` and `>` cycle through twelve built-ins
plus **custom**, and **Edit palette…** opens the colour editor.

Each effect remembers its own palette. A few effects ignore palettes entirely
because their colours are the point — `matrix` (green), `fire`, `lightning`.

### Effect reference

| Category | Effects |
|---|---|
| **Waves** | wave, wave >, wave <, wave ^, wave v, gradient |
| **Flow** | radial, spiral, plasma, aurora, ripple, snake |
| **Classic** | matrix, scanner, theater, meteor, comet, chaser |
| **Fill** | concentric, fill, stack, wipe, bounce, starburst, vu, vu pal |
| **Scatter** | rain, twinkle, confetti, juggle, strobe, lightning |
| **Glow** | breathe, pulse, split, spectrum, fire |

`matrix` behaves differently on the keyboard: because that is a real 15×5
grid, the rain snaps to it — one key wide, falling a row at a time. On fan
rings, which have no rows or columns, it keeps its smooth spatial form.

### System usage gradient

Two effects in the **System** category turn the case into a load readout
rather than decoration.

**Blue** when a component is idle, **green** under light load, through yellow
and orange to **red** when it is pinned. Each run of LEDs reports a different
resource, so you can see at a glance which part of the machine is busy:

| Lights | Reports |
|---|---|
| AIO pump + the three radiator fans | **CPU** usage |
| GPU lighting (ZOTAC text and logo) + bottom F420 intake | **GPU** usage |
| RAM sticks | **RAM** usage |
| Side F360 + rear exhaust + keyboard | **overall** system load |

The pairing is physical: the parts cooling the CPU show the CPU, and the
bottom intake that feeds the graphics card shows the graphics card.

- **usage** - each run a flat colour, so two fans at 40% and 55% are directly
  comparable. It is an instrument, not an animation.
- **usage bar** - the same colours, but each run also fills in proportion to
  its load, readable from across the room without judging hue.

The colour stops are deliberately not evenly spaced. Load spends most of its
life under 50%, so an even ramp would leave the case green nearly always and
waste the top half of the scale.

**Overall load weights RAM low** (45% CPU, 45% GPU, 10% RAM). This machine
idles around 80-96% RAM once Windows has filled it with cache; weighting it
equally would pin the side and rear fans red permanently and tell you nothing.
RAM still gets its own honest readout on the DIMMs.

### VU meters

`vu` (green→amber→red, like a real meter) and `vu pal` (palette-coloured) are
driven by whatever Windows is playing, captured from the speaker's loopback.

- **VU bars** — how many frequency bands, 2 to 20.
- **VU sensitivity** — 0.3× to 3.0×. Content loudness varies enormously; a
  game sits near the top of the range where a quiet track barely leaves the
  bottom. Turn it down if the meter is pinned, up if it barely moves.

The label says which source is live: `live audio`, `live audio (silent)`, or
`SIMULATED (no audio capture)`. A meter that bounces to nothing is worse than
no meter, so it never leaves this ambiguous.

Levels are measured *relative to the band's own recent range*, so the meter
keeps moving during steady loud content instead of pinning every bar to the
top. Lit segments also dim with the level, so the bottom row breathes rather
than sitting at a constant green.

---

## Effect layers

A layer is a box that applies its effect **only to the LEDs it covers** — the
same idea as SignalRGB's effect blocks.

Turn on **Layer mode**, then **+ Add**. The canvas now edits boxes rather than
LEDs:

| Action | Result |
|---|---|
| drag inside a box | move it |
| drag a corner | resize; the opposite corner stays pinned |
| drag the handle above the top edge | rotate |
| arrow keys / shift-arrows | nudge 1 px / 10 px |
| Delete | remove the selected box |

**Changing a layer's effect** — three ways:

1. the **Effect: …** button in the layers section (opens a chooser),
2. double-click the layer's row in the list,
3. the normal effect grid — while a layer is selected it retargets to that
   layer, and says so: the header reads `ANIMATIONS → LAYER 1`.

Layers stack. Later ones paint over earlier ones; **Raise** / **Lower**
reorder. Each has its own opacity, blend mode and palette.

The effect runs in the box's *own* coordinate space: an LED at the box's
top-left is the effect's (0,0) and the bottom-right is (1,1). So a wave runs
across the box wherever you put it and however far you turn it.

### What blend does

Blend decides how a layer's colour combines with whatever is already
underneath it — the global effect, a lower layer, or your painted background.
Opacity (`a`) scales the layer first.

| Mode | Formula per channel | Looks like |
|---|---|---|
| **normal** | `under × (1−a) + layer × a` | a cross-fade. At 100% opacity the layer replaces what is beneath; at 50% it is half-and-half. This is the one you want by default. |
| **add** | `under + layer × a`, capped at 255 | light piling on light. Overlaps get brighter and drift toward white. Good for glows and sparks over a dim base; will wash out over a bright one. |
| **max** | `max(under, layer × a)` per channel | the brighter of the two wins, channel by channel. Overlays without the washing-out that `add` causes — useful when you want a layer to show through only where it is brighter than the background. |

Concrete: layer colour `(200,100,50)` at 50% opacity over a background of
`(100,0,0)` gives **normal** `(150,50,25)`, **add** `(200,50,25)`, **max**
`(150,50,25)` — and over black, **normal** and **max** both give `(100,50,25)`
while **add** gives the same, because there is nothing to add to.

---

## The Fans tab

Switch with the **Fans** tab above the canvas. The lighting controls are
replaced by fan controls, since none of them apply here.

**Left:** a card per fan showing the curves it actually runs, with the live
operating point marked. **Right:** live readouts and safe adjustments.

Readouts include CPU Tctl and CCD1, GPU core and VRAM, all six case-fan and
radiator RPMs, pump duty and RPM, and — from `nvidia-smi` — GPU power against
its limit, core and memory clocks, utilisation and VRAM used.

Nothing on this tab talks to hardware. It reads the files the daemons publish,
so it can never fight them for a device.

### Reading a curve chart

X axis is temperature, Y axis is fan duty. Each coloured line is one
temperature source:

- **cyan** GPU core  **purple** GPU VRAM  **orange** CPU Tctl

Every channel carries one curve *per sensor* and runs at whichever demands the
most duty — the thick line is the one currently leading, and the white dot is
where that fan is right now. The dashed grey line is the minimum duty, which no
curve or trim can go below.

Why per-sensor rather than "hottest sensor wins": 80 °C is hot for a GPU core,
unremarkable for a 9800X3D and cool for GDDR7. Converting each sensor to a duty
*first*, then taking the maximum, compares like with like.

### Curve trim

Four sliders shift a whole curve up or down by up to **15 duty points**. They
apply live — within one poll, no restart.

They deliberately cannot *reshape* a curve. The shapes came from measured
thermal data (Forza Horizon, matched GPU load, n=555 vs n=329: GPU core
−3.7 °C, VRAM −4.2 °C, CPU −1.3 °C which was inside the noise), and a
drag-the-curve editor would make that easy to throw away by accident. The hard
min/max clamps still apply on top, so no trim can stall a fan.

**Reset trims** returns all four to zero.

### Restart daemons

A button at the bottom of the Fans tab. It stops and restarts both daemons,
which re-pins the pump and reloads both curves, then waits and **checks the
pump actually landed** rather than reporting that it tried. The result appears
in the status line.

Use it when a header stops matching its commanded duty. If it still mismatches
after a restart, another program owns that header and only an elevated process
can clear it - run `Fix Cooling.bat`.

The daemon also repairs this by itself now: if the pump drifts more than 5
points from target for 3 consecutive polls it writes the value back, up to 5
times, then stops fighting and says another program owns the header.

### The pump

The pump is a **fixed duty and never a curve** — repeated speed cycling is a
wear mechanism, and avoiding it is the single biggest durability factor here.

Choose from the measured duty→rpm points:

| duty | rpm | % of max |
|---|---|---|
| 40% | 1772 | 63% |
| 48% | 1971 | 70% |
| 56% | 2163 | 76% |
| 64% | 2398 | 85% |
| 72% | 2733 | 97% |
| 80% | 2795 | 99% |

The response **saturates early** — everything from 72% up produces ~2750+ rpm,
so 28 points of duty buy about 80 rpm. The usable range is 40–72%.

Default is **56% → 2163 rpm = 76% of maximum**: inside the usual "keep a pump
at 60–80% of maximum" guidance, well clear of the low-RPM cavitation that
actually damages pumps, and below the saturation zone that only adds wear.

That guidance is about **RPM, not duty**. On this pump those are very different
numbers — reading it as duty would put the pump at 99% of maximum while looking
conservative.

The pump is **not trimmable**, and the daemon clamps anything below its safety
floor. Pump changes take effect when `mobo_daemon` restarts.

---

## Autostart

| Component | Mechanism |
|---|---|
| `thermal_rgb_loop` | shortcut in the Startup folder |
| `mobo_daemon` | Scheduled Task at logon, highest privileges |
| OpenRGB | started on demand by whatever needs it |

The Scheduled Task runs elevated without a UAC prompt because triggering a task
you own needs no elevation, even though the task itself runs elevated.

---

## Troubleshooting

**A fan or the pump ignores its curve.** The Fans tab flags this directly:
`pump commanded 56% but hardware reports 25%`.

**Run `Fix Cooling.bat`** (it will ask for administrator rights). It stops
duplicate daemons, stops any program holding the same chip, restarts both
daemons, and tells you whether the pump landed on its commanded duty.

Two things cause this. The first is a **duplicate daemon** - two copies
driving the same chip and overwriting each other. `schtasks /End` does not
always clear one: if the Task Scheduler has lost track of the process it
started, it reports success and the orphan keeps running. The daemons now
refuse to start a second driving copy, so this cannot recur.

The second is **another program owning the header**. Only one may:

- **FanControl** — writes the same SuperIO chip as `mobo_daemon`. Do not run
  both. This is the most common cause.
- **NZXT CAM** — keep it installed (see below) but do not let it autostart.
- **BIOS Q-Fan** — will reclaim a header if software control is released.

Close the offending program, then restart `mobo_daemon` (elevated).

**Colours do not change.** Check **Take control** and **Drive hardware** are
both on. If they are, something else owns the LEDs — SignalRGB takes exclusive
control and cannot coexist with OpenRGB.

**Only 8 LEDs light on an NZXT channel.** The controller stores how many
accessories are chained per channel, and only CAM can write that. Run CAM once,
apply any effect, confirm every fan lights, and close it. The setting persists
with CAM closed and across reboots.

**The keyboard does not respond.** Razer Synapse owns it while running.

**VU meter shows `SIMULATED`.** Audio capture is unavailable — check the
default playback device exists. The effect still animates, it just is not
listening to anything.

**An LED stays one colour when everything else changes.** Almost certainly the
device's own onboard profile, not this software.

---

## What is saved, and where

Everything survives a restart. Nothing needs exporting by hand.

| What | File | Written when |
|---|---|---|
| effect, palette per effect, custom palette, speed | `led_studio_state.json` | on close and every 20 s |
| VU bars and sensitivity | `led_studio_state.json` | " |
| all effect layers - position, size, angle, blend, opacity | `led_studio_state.json` | " |
| painted colours, per-LED intensity, master intensity | `led_studio_state.json` | " |
| whether the keyboard is lit | `led_studio_state.json` | " |
| fan curve trims | `fan_tuning.json` | the moment you move a slider |
| pump duty | `pump_config.json` | the moment you pick one |
| LED counts per zone | `rgb_zone_sizes.json` | set once during mapping |

**Orientation is different.** Which way each fan ring is rotated or mirrored
lives in `case_layout.py` as code, not in a settings file - every one of those
transforms was derived by lighting single LEDs and checking them against the
physical case. It persists because it is committed, and it is not editable
from the UI, deliberately: there is one correct answer per ring and it is
already recorded.

## Checking nothing has broken

`Run Tests.bat` runs 151 automated checks in about a minute - every clamp, the
control handover, the layout, all 37 effects, and the editor's state handling.
It touches no hardware and snapshots any file it writes, so it is safe while
everything is running. See [TEST_PLAN.md](TEST_PLAN.md), which also lists the
handful of checks that need a reboot or a pair of eyes.

## File reference

Everything under `C:\HardwareControl\scripts`:

| File | Purpose |
|---|---|
| `led_studio_native.py` | the editor |
| `fan_panel.py` | Fans tab charts |
| `fan_side.py` | Fans tab controls and readouts |
| `fan_tuning.py` | curve trim, clamped; read live by both daemons |
| `rgb_effects.py` | all 37 effects, palettes, VU |
| `fx_layers.py` | layer geometry — `python fx_layers.py` runs 68 self-tests |
| `case_layout.py` | physical LED positions, the single source of truth |
| `thermal_rgb_loop.py` | case fans + LEDs daemon |
| `mobo_daemon.py` | pump + radiator daemon (elevated) |
| `audio_levels.py` | speaker loopback capture for the VU meters |
| `nzxt_util.py` | spaced, verified writes — the NZXT controller drops rapid ones |
| `single_instance.py` | named-mutex guard so two daemons cannot drive one chip |
| `selftest.py` | the regression suite behind `Run Tests.bat` |

State files written at runtime: `sensors.json` (elevated daemon),
`fan_state.json` (case daemon), `fan_tuning.json` (your trims),
`pump_config.json` (pump duty + its measured map).
