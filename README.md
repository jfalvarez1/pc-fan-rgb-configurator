# pc-fan-rgb-configurator

Open-source replacement for SignalRGB / NZXT CAM / iCUE on one specific
machine: fan curves, pump control and per-LED RGB, driven by Python, tuned
from measured thermal data rather than guesswork.

Everything here was derived by probing the actual hardware. Where a number
appears, it was measured; where something is uncertain, it says so.

---

## The machine

| Part | Detail |
|---|---|
| CPU | Ryzen 7 9800X3D (Tjmax 95 °C) |
| GPU | ZOTAC RTX 5090 SOLID OC, power-capped ~438 W sustained |
| Board | ASUS PRIME B650-PLUS WIFI, Nuvoton NCT6799D SuperIO |
| Cooler | ARCTIC Liquid Freezer III Pro 360 A-RGB (white) |
| Fans | NZXT F360 RGB Core (front), F420 RGB Core (bottom), F single (rear) |
| Controller | NZXT RGB & Fan Controller (3 fan + 6 RGB channels) |
| RAM | 2× Corsair Vengeance RGB DDR5 |

## The stack

| Layer | Owner | Driven by | Elevated? |
|---|---|---|---|
| Pump | `mobo_daemon.py` | fixed 56 % → 2170 rpm, never a curve | yes |
| Radiator fans | `mobo_daemon.py` | CPU Tctl curve | yes |
| Case fans ×3 | `thermal_rgb_loop.py` | GPU core + VRAM curves | no |
| All LEDs | `thermal_rgb_loop.py` | idle dark → synthwave → orange glow | no |

The elevated daemon publishes `sensors.json`; the unelevated one reads it.
That is the whole bridge — it means CPU and VRAM temperatures reach the case-fan
logic without that process needing administrator rights, and it degrades to
GPU-only if the elevated daemon is not running.

## Dependencies

```
pip install openrgb-python liquidctl psutil pythonnet
```

Plus, downloaded separately (not committed):

* **OpenRGB** 1.0rc3 portable — LED control, SDK server on port 6742
* **FanControl** V273 portable — only for the `LibreHardwareMonitorLib.dll`
  it bundles; the GUI is not used
* **NZXT CAM** — needed exactly once, see the accessory-config note below

---

## Hardware findings

Each of these cost real debugging time. They are documented so they do not
have to be rediscovered.

### The NZXT controller silently drops rapid writes

Three back-to-back `set_fixed_speed()` calls: only the first applies. No
exception, no error — liquidctl reports success and the duty never changes.
All writes therefore go through `nzxt_util.set_duty()`, which spaces them
0.4 s apart and reads the value back to confirm.

The same quirk bites RGB: writing zone-by-zone loses updates, so the whole
device buffer is written in a single call.

### NZXT channels need an accessory config that only CAM can write

Symptom: exactly 8 LEDs light per channel, always the same physical fan, no
matter what is sent. OpenRGB *reads* the right counts (24/24/8) but the
controller only *distributes* one accessory's worth of data.

The controller stores how many chained devices sit on each channel. Neither
OpenRGB nor liquidctl can write that. Run CAM once, apply any effect, confirm
every fan lights, close it — the config persists with CAM closed and across
reboots.

Diagnosis that settled it: **liquidctl reproduced the fault identically.**
When two unrelated drivers fail the same way, the driver is not the problem.
Wrongly blamed along the way: write spacing, the openrgb `fast` flag, zone
sizing, daisy-chain length.

### `SetDefault()` does not restore anything

LibreHardwareMonitor's `SetDefault()` releases *software control* but leaves
the last written value in the SuperIO register. A probe raised the pump to
100 %, called `SetDefault()`, printed "released to BIOS" — and the pump stayed
at 100 % across process restarts. The message was a lie.

Correct restore: snapshot the original duty, write it back, *then* release.

### An ARGB header reporting 0 LEDs is not empty

OpenRGB cannot detect LED counts on a motherboard ARGB header, so it reports
zero and sends nothing. The Arctic cooler sat on `Aura Addressable 1` at
indices 0–47 and read as "nothing connected" for hours, because every probe
sized the zone to 24 or 30 — the cooler's fans start at index 12 and run to
47. Probe long (100+) before declaring a header empty.

### Duplicate device names make devices unreachable

Both DDR5 sticks report the identical name. Any lookup by name resolves to the
first, so the second silently never updates. **Always address OpenRGB devices
by `d.id`.**

### This GPU's lighting is not on the GPU

The card's ARGB is cabled into motherboard header `Aura Addressable 2`. The
"ZOTAC GAMING" device OpenRGB exposes has two zones that light nothing — they
are excluded, which also avoids pointless writes to a Static-only device.

### `wmic` is removed in current Windows 11

Use `Get-CimInstance` via PowerShell instead.

### A C rewrite was considered and rejected

Measured first: the two daemons use ~120 MB and ~0.15 % of a 16-core CPU.
A pure-C rewrite would mean reimplementing the HID protocol, an OpenRGB TCP
client, the curve engine and the editor server — and the motherboard half
would still need Python, because SuperIO access requires a signed ring0
driver (LibreHardwareMonitor's). Rewriting the easy 60 % would leave two
runtimes instead of one. MSVC builds fine here if that ever changes.

---

## Confirmed hardware map

### Fan channels (NZXT, PWM)

| Channel | Fan | Idle | @100 % |
|---|---|---|---|
| fan1 | side/front F360 (3×120 mm) | 740 rpm | 2489 rpm |
| fan2 | bottom F420 (3×140 mm) | 668 rpm | 2013 rpm |
| fan3 | rear exhaust (single) | 735 rpm | 1863 rpm |

### Motherboard headers (Nuvoton NCT6799D)

| Header | Device | Notes |
|---|---|---|
| Fan #2 | **AIO pump** | 2830 rpm max. Fixed duty only, never a curve |
| Fan #7 | radiator fans ×3 | 3034 rpm max, one PWM cable |
| Fan #5 | unresponsive to PWM | likely VRM fan, or header in DC mode |

### LED zones

| Zone | LEDs | Contents |
|---|---|---|
| Aura Addressable 1 | 48 | Arctic cooler: pump 12 + 3 rad fans ×12 |
| Aura Addressable 2 | 24 | GPU: LED 0–4 = "ZOTAC" text, 5–7 = logo. 8–23 tested, drive nothing |
| Aura Mainboard | 1 | 4-pin 12 V header, empty |
| Hue 2 Channel 1/2/3 | 24/24/8 | front F360 / bottom F420 / rear fan |
| Corsair DRAM ×2 | 10 each | RAM, device ids 0 and 1 |

---

## Pump configuration

Measured duty → RPM (`data/pump_map.txt`):

```
 80 % → 2795 rpm (99 % of max)      72 % → 2733 rpm (97 %)
 64 % → 2398 rpm (85 %)             56 % → 2163 rpm (76 %)
 48 % → 1971 rpm (70 %)             40 % → 1772 rpm (63 %)
```

**The response saturates early.** Everything from 72 % to 100 % produces
~2750+ rpm — 28 points of duty buying ~80 rpm. The usable control range is
40–72 %.

Set to **56 % → 2163 rpm = 76 % of max**, because:

* **Steady, never a curve.** Repeated speed cycling is a wear mechanism; a
  constant duty removes it. This matters more than the exact value.
* **Not maxed.** Rated for continuous full speed, but that is more bearing
  wear and heat than the loop needs.
* **Well above the low end.** Cavitation — vapour bubbles collapsing on the
  impeller — is the real damage mechanism and lives at low RPM.

A common guideline is "keep a pump above 60–80 % of maximum". That is **RPM,
not duty**; on this pump those are entirely different numbers, and conflating
them put the pump at 99 % of max while appearing conservative.

---

## Curve tuning, measured

Forza Horizon, matched ~94.5 % GPU utilisation, saturated samples only
(baseline n=555, tuned n=329). Baseline = noise-budget curves; tuned =
cooling-first.

```
             BASELINE    TUNED   delta   significance
gpu_core         71.1     67.4    -3.7   CLEAR  (sd 0.7)
gpu_vram         75.2     71.0    -4.2   CLEAR  (sd 1.0)
cpu_tctl         65.2     63.9    -1.3   noise  (sd 2.4)
gpu_clock      2790.0   2791.9    +1.9   noise  (sd 7.5)
gpu_power       439.3    437.7    -1.6   noise  (sd 14.2)

fan rpm   f1 1053→1855   f2 1106→1746   f3 1269→1680
```

**Conclusions**

* GPU core and VRAM are genuinely 3.7–4.2 °C cooler. Real and repeatable.
* **No performance gain.** Clocks and power are unchanged — this card is
  power-capped, so thermal headroom buys margin and VRAM longevity, not
  frames. The lever for performance is the power limit, not cooling.
* **Case airflow does not measurably cool the CPU.** Fan RPM rose ~800 and
  CPU moved −1.3 °C against sd 2.4. The radiator and pump own CPU cooling.
  The case-fan CPU curves were therefore pushed late (70 °C+) and act only as
  a backstop.

### Measurement lessons

1. **Compare only saturated data.** Two warm-ups compared against each other
   showed "no GPU benefit" when the truth was −3.7 °C.
2. **Never conclude from a live snapshot.** One moment in one scene read
   70 vs 69.6 °C = "no gain". Wrong.
3. **Check significance against sample spread.** An early comparison showed
   CPU −6.1 °C; with matched data it is −1.3 °C against sd 2.4 — noise.

---

## Scripts

| Script | Purpose | Admin |
|---|---|---|
| `thermal_rgb_loop.py` | main daemon: case fans + all LEDs | no |
| `mobo_daemon.py` | pump + radiator, publishes `sensors.json` | yes |
| `nzxt_util.py` | verified writes (the dropped-write fix) | no |
| `rgb_effects.py` | synthwave palette + outward glow maths | no |
| `control_center.py` | GUI: per-segment LEDs, case fans | no |
| `mobo_fan_gui.py` | GUI: motherboard headers, raise-only | yes |
| `bench_logger.py` | 1 Hz session logger + analysis | no |
| `pump_map.py` | measures pump duty → RPM | yes |
| `identify_fans.py` / `identify_rgb.py` / `solo_zone.py` / `paint_legend.py` | hardware identification | no |
| `claude_signal.py` | Claude Code hook target | no |

Safety rules encoded in the tools: motherboard sliders **floor at their BIOS
baseline** (raise-only, so a pump can never be slowed), case-fan sliders floor
at 20 % (stall protection), and every script restores headers on exit
including on crash.

---

## Lighting

`RGB_MODE` selects `auto` (default), `off`, `synthwave` or `thermal`.

**auto** — dark at idle, synthwave wave while gaming. Detected from GPU/CPU
utilisation with asymmetric thresholds: on at GPU ≥40 % **or** CPU ≥50 % held
8 s; off only when GPU ≤20 % **and** CPU ≤25 % held 120 s. Eager on, reluctant
off, so a loading screen never blanks the case.

The palette is symmetric (pink, purple, blue, purple) on purpose: a bare
3-stop loop must interpolate blue straight back to pink, crossing the
desaturated middle of the RGB cube — measured `rgb(142,150,209)`, a muddy
grey-lilac. Returning via purple keeps minimum channel spread at 131 instead
of 67.

Devices are driven by capability: Direct where available, `Custom` for Corsair
DRAM (it exposes neither Direct nor Static), and Static-only devices are
updated at most every 10 s to spare their flash.

### Claude Code integration

Claude Code hooks light the case orange while Claude is working:

```json
"UserPromptSubmit": python claude_signal.py on
"Stop":             python claude_signal.py off
"SessionEnd":       python claude_signal.py off
```

One flag file **per session** under `claude_flags/`. A single shared flag was
wrong: with two sessions open, whichever finished first switched the glow off
while the other was still working. Stale flags expire so a crashed session
cannot strand the lights on.

---

## Postmortem: why the browser version looked broken

Kept because the lesson is worth more than the code.

A local web app (HTTP server + HTML) appeared not to animate for many rounds.
Diagnosed afterwards with Playwright: **the animation was working the entire
time** - 2850 frames rendered, colours changing. But "Drive hardware" defaulted
to UNCHECKED, so applyNow hit `if (quiet && !controlling) return;` and did
nothing, silently. The native rewrite defaulted that toggle ON, which is the
only reason it "just worked".

Three real bugs were fixed en route, NONE of which was the cause:

  * server bound to 127.0.0.1 only. "localhost" resolves to ::1 first on
    Windows 11, so every request stalled ~2 s before falling back:
    2050 ms vs 7.8 ms, a 263x penalty.
  * a missing `import socket` crashed startup, caught only by running in the
    foreground instead of trusting a background launch.
  * no cache headers, so the browser could serve stale JS after every fix.

Lessons, in order of how much they would have saved:

  1. Never let a control path no-op SILENTLY. A disabled toggle must say so at
     the moment the action is requested.
  2. Default the obvious toggle ON. A tool that exists to drive hardware should
     drive hardware.
  3. Instrument first. A frame counter and an on-screen error line belong in
     version one, not version six.
  4. Never ask the user to read a browser console. If the UI cannot report its
     own state, that is a design failure - and Playwright can diagnose it in
     one round rather than six.
  5. Do not claim a root cause without proving it.

The web version now lives in `_retired/`. Native Tk is the default for local
tools on this machine.

## LED Studio (native) and spatial effects

`led_studio_native.py` is a standalone Tk app - no browser, no HTTP. The case
drawn to scale, every LED individually clickable, live preview, and hardware
written on a worker thread that only ever sends the NEWEST frame, so a slow
device drops frames instead of building a backlog.

  * click an LED, or a fan's centre for the whole fan
  * drag on empty space to rubber-band select; Shift extends
  * Brush mode paints LEDs directly as you drag
  * pattern generators apply across the selection in click order

### Ring orientation - none of it was right by default

Every fan ring needed a correction, and no two groups needed the same one.
LED chain direction depends on how a fan is mounted and which way its cable
exits; there is no convention. All of these were found by lighting one LED
and looking:

    Rad fan L/M/R      rot +90, mirror T-B
    Side F360 x3       mirror L-R
    Bottom F420 x3     rot -90, mirror L-R
    Rear exhaust       rot -45
    Arctic pump        mirror L-R + T-B

`rot` rotates; `flip` mirrors left-right; `vflip` mirrors top-bottom. Both
mirrors are applied AFTER the rotation so they act on final drawn positions -
that is what makes "left/right are fine, just swap top and bottom" work.

### Spatial rendering

`case_layout.py` is the single source of truth and exposes `led_positions()`,
giving all 132 LEDs a normalised (x, y) in the case. Effects are computed from
PHYSICAL POSITION, not LED index - the old index-based wave scrambled at every
fan boundary.

35 effects in `rgb_effects.SPATIAL`, grouped for the UI in `EFFECT_GROUPS`.
Families follow the conventions the LED community has settled on - WLED's
180+ effect list is the de-facto reference:

    Waves    wave, wave > < ^ v, gradient
    Flow     radial, spiral, plasma, aurora, ripple, snake
    Classic  matrix, scanner (Larson), theater chase, meteor, comet, chaser
    Fill     concentric, fill, stack, wipe, bounce, starburst
    Scatter  rain, twinkle, confetti, juggle, strobe, lightning
    Glow     breathe, pulse, split, spectrum, fire

23 flat buttons was a wall of options AND made the panel taller than the
window. One category is shown at a time in a fixed-height holder, so the panel
stays the same height however many effects exist, and the window now sizes
itself to its content instead of needing a manual resize.

PALETTES holds 12 named palettes plus a custom editor, and the choice is
remembered PER EFFECT. A few effects (matrix, fire, lightning) are
intrinsically coloured and ignore it - the UI says so rather than appearing
broken.

Randomness is HASHED FROM POSITION, never `random()` - an LED must produce the
same value every frame or the effect flickers instead of animating.

Preview them live:

    python effect_demo.py            # cycle all, 12s each
    python effect_demo.py --effect plasma
    python effect_demo.py --list

## OpenRGB is started automatically

OpenRGB must be running for ANY lighting to work, and must be ELEVATED or it
loses PawnIO/SMBus - which means no motherboard ARGB headers, so no cooler
lighting and no GPU logo.

Nothing requires launching it by hand. `openrgb_boot.ensure_running()` is
called by led_studio, effect_demo and the daemon; it triggers the
`HardwareControl-OpenRGB` scheduled task and waits for port 6742.

Triggering a task you own needs NO elevation and raises no UAC prompt, while
the task itself runs with highest privileges - so an unelevated tool can
start an elevated OpenRGB. Verified with `schtasks /Run` returning rc 0 from
a non-admin shell.

Gotcha: run schtasks from PYTHON, not from Git Bash. MSYS path conversion
rewrites `/Run` into `C:/Program Files/Git/Run`. subprocess.run([...]) execs
directly and is unaffected.

## Icon

`scripts/make_icon.py` generates `led_studio.ico` (16-256 px): a ring of lit
LEDs around a dark fan hub, in the app's own synthwave palette. It echoes what
the app actually draws rather than imitating anyone's branding.

Two things it took iterations to get right, both visible in the code comments:

  * the first halo stacked five wide translucent layers and the alpha summed to
    near-white - the whole icon washed out
  * even tightened, adjacent LEDs' halos overlapped and summed, so the pink
    LEDs rendered white. Fixed by drawing the halo in a DIMMED (55%) version of
    the colour, keeping the LED itself the brightest thing

Regenerate with `python scripts/make_icon.py`.

## UI notes

Layout canvas is 820x680. Elements are spread with clearance, and labels are
placed by element type - below rings past their radius, below vertical strips,
ABOVE horizontal strips - so text never lands on an LED.

The GPU run is right-adjusted on the card body, because that is where the lit
strip physically sits: LED 0-4 are the "ZOTAC" text, 5-7 the logo.

Unlit LEDs are drawn #242c3a with a lighter rim. They used to be #11151c
against a #0d0f14 background - effectively invisible, so a dark case read as a
field of empty holes.

Buttons are flat tk.Labels with hover and an accent active state. Tk's default
3D relief looks like Windows XP. Active state is used to SHOW state: the
running effect, Take control, Drive hardware and Brush all light up.

### Taskbar icon

Windows groups taskbar buttons by AppUserModelID. A script launched through
pythonw.exe inherits PYTHON'S id, so the taskbar showed the Python icon even
though the title bar and Start Menu showed ours. Fixed by calling
SetCurrentProcessExplicitAppUserModelID("HardwareControl.LEDStudio") BEFORE
any window is created.

## Effect space is normalised PER GROUP

The keyboard sits below the case on screen. If effect coordinates came from
one shared bounding box, every key would land in the bottom tenth of the
space - a vertical wave or a "stack" would treat the whole keyboard as one
bottom row.

`led_positions()` normalises each `fx_group` separately, so the case and the
keyboard each span the full 0..1 range. Verified: the topmost radiator LED and
the top keyboard row are both ny=0.040; the lowest bottom-fan LED and the
bottom key row are both ny=0.960. Effects therefore run side by side across
both surfaces rather than treating the keyboard as a footnote.

## VU meter

`vu` (classic green/amber/red, scaled by height like a real meter, with
falling peak markers) and `vu pal` (same shape, palette-coloured). Bar count
is user-tweakable at runtime from the panel - the slider writes
`rgb_effects.VU_BARS`.

`audio_levels.py` taps the default speaker's WASAPI loopback (via
`soundcard`), FFTs it, and buckets into LOGARITHMIC bands - pitch is
logarithmic, so linear bins would cram nearly every musical note into the
first two bars. Attack is fast, release slow, so bars fall like a real meter.

If capture is unavailable the meters fall back to synthesised motion, and the
panel says which is in use: "live audio", "live audio (silent)", or
"SIMULATED (no audio capture)". A meter that bounces to nothing is worse than
no meter, so it is never left ambiguous.

### Why the bottom rows used to stay lit

The bottom fans and the bottom keyboard row were permanently filled whenever
audio played. Four separate causes, each found by measurement:

1. **The FFT was never normalised.** `np.fft.rfft` scales with block size, so
   magnitudes read ~1000x high and every band sat far above the dB floor.
   Dividing by `window.sum()/2` makes a bin read as a real amplitude, so the
   dB constants in `audio_levels.py` are now genuine dBFS.
2. **Some bands had no FFT bins at all.** At 2048 samples the bin spacing is
   23.4 Hz, and the 46-53 Hz log band was empty - a permanent 0.00 rather than
   a quiet band. Now 4096 samples plus a nearest-bin fallback.
3. **A fixed dB floor cannot work.** Steady content - a game, a dense mix -
   parks every band near the top and the lower rows never go out. Each band is
   now normalised into its own rolling min/max over `WINDOW_SEC`, so it reads
   1.0 when loudest in the last few seconds and 0.0 when quietest. A first
   attempt used a slowly-rising baseline instead; at 0.0008 per block its time
   constant was ~100 s, so across a 15 s capture it never moved and the bottom
   row was dark 0% of the time.
4. **A continuous fill lights the bottom row at any level.** The lowest LED
   row sits at height ~0.04 in effect space, so "lit if height <= level" lights
   it for anything above 4%. The meter is now quantised into `VU_ROWS`
   segments with thresholds spread across `VU_LO`..`VU_HI`, so the bottom row
   needs a real level and the top row stays reachable.

With genuinely continuous audio a bottom segment is still legitimately lit
most of the time - that is what a real meter does, and no threshold makes it
blink without also making the meter twitchy. So a lit segment is additionally
dimmed by the current level (`VU_DIM`): measured over live audio the bottom
row now swings between 0.25 and 0.79 brightness instead of sitting at a
constant full green, and the case and keyboard track each other exactly.

Tunables in `rgb_effects.py`: `VU_ROWS`, `VU_LO`, `VU_HI`, `VU_DIM`,
`VU_PEAK_FALL`, `VU_GAIN` (the panel's **VU sensitivity** slider, 0.3x-3.0x,
since a game sits near the top of the range where a quiet track barely leaves
the bottom). In `audio_levels.py`: `WINDOW_SEC`, `GATE`, `EXPO`, `MIN_RANGE`,
`SMOOTH_UP`/`SMOOTH_DN`.

`fx_vu` is called once per LED - 207 times a frame - so band levels are
computed once per frame and cached, which also makes a real falling peak-hold
possible. Measured: 207 level fetches per frame down to 1.

## Autostart

| Component | Mechanism |
|---|---|
| `thermal_rgb_loop` | `ThermalRGBLoop.lnk` in the Startup folder |
| `mobo_daemon` | Scheduled Task `HardwareControl-MoboDaemon`, AtLogon, Highest |
| OpenRGB | Scheduled Task `HardwareControl-OpenRGB`, AtLogon +15 s, Highest |

Install the elevated tasks with `Install Autostart.bat` and
`Install OpenRGB Autostart.bat`. OpenRGB **must** run elevated or it loses
PawnIO and therefore SMBus, which means no motherboard ARGB headers — no
cooler lighting and no GPU logo.

To remove:

```powershell
Unregister-ScheduledTask -TaskName 'HardwareControl-MoboDaemon' -Confirm:$false
Unregister-ScheduledTask -TaskName 'HardwareControl-OpenRGB'    -Confirm:$false
```

---

## Conflicts

Only one program may own a device. Known contenders on this machine:

* **NZXT CAM** — keep installed for the accessory config, but do not autostart
* **SignalRGB** — takes exclusive control; cannot coexist with OpenRGB
* **Razer Synapse / Logitech G Hub** — own their own peripherals; the keyboard
  is deliberately excluded from all effects here
* **FanControl** — do not run it while `mobo_daemon` is active; both write
  SuperIO

## Data

`data/` holds the raw measurement CSVs behind the tuning, the pump map, and
the confirmed hardware maps. They are machine-specific but are the evidence
for every number in this document.
