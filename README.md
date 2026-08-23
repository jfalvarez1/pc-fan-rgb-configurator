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
| Aura Addressable 1 | 48 | Arctic cooler: pump 12 + 3 rad fans ×12. **Chain runs right→left** |
| Aura Addressable 2 | 24 | GPU ZOTAC logo |
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
