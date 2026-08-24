# Test plan

Two halves: what a machine can check, and what only a human or a reboot can.
The automated half runs in about a minute and touches no hardware.

```
Run Tests.bat                      (or)
python C:\HardwareControl\scripts\selftest.py
python C:\HardwareControl\scripts\selftest.py limits override
```

Safe to run while the daemons and the editor are live: every file a test has
to write is snapshotted and restored, and nothing talks to a device.

Exit code is 0 when everything passes, 1 otherwise, so it can gate a commit.

---

## Automated — 151 checks

| Section | What it proves |
|---|---|
| `limits` | every clamp holds at and beyond its edge |
| `override` | the daemon hands over lighting without losing the fans |
| `instance` | two daemons cannot drive the same chip |
| `layout` | all 207 LEDs map once, and the two surfaces align |
| `effects` | all 37 effects return valid colour everywhere |
| `vu` | the meter uses its whole range and the bottom row can go dark |
| `matrix` | the rain reads as a strand on a real grid |
| `geometry` | layer move/resize/rotate maths (68 sub-checks) |
| `app` | control handover, frame lengths, state round-trip, hostile input |
| `fans` | the fan view gathers and renders |

### The limits, specifically

These are the ones that matter, so they are tested past their edges rather
than in the comfortable middle.

**Pump.** Duty is clamped into `[PUMP_MIN_SAFE, 100]`. Tested with −1000, −1,
0, 10, 39.9, 40, 56, 80, 100, 101 and 1e6. Also asserted: the floor is at or
above 40%, every duty offered in the UI is at or above the floor, the
configured duty is one of the *measured* map points, and its measured RPM
clears the 1500 rpm abort floor. The pump is not trimmable at all — asserted,
not assumed.

**Curve trim.** Clamped to ±15 duty points on both load and save. Tested with
±10⁶, with `"abc"`, `None`, `NaN` and `[]`, and with a corrupt JSON file.
Then the important one: for **every** curve, at **every** temperature from 0
to 120 °C, at the trim extremes, the resulting duty is still inside the
daemon's own `[MIN_DUTY, MAX_DUTY]` — so no trim can command a stall.

**Curve shape.** Temperatures ascend, duties never decrease, duties are
percentages. Below the first knee holds the first duty; above the last holds
the last.

**One interpolation.** `thermal_rgb_loop`, `mobo_daemon` and `fan_panel` each
have an `interpolate()`. All three are asserted to agree exactly across every
curve at every integer temperature — the fan view must not draw a curve the
daemon would not run.

**Override flag.** `scope=leds` pauses lighting and leaves fan control
running; a legacy flag with no scope still pauses both; a dead `pid=` releases
immediately; a malformed or empty flag fails *safe* (stays paused).

**Blending.** Opacity above 1 and below 0, and every blend mode at full white,
all stay inside 0–255.

**Frame length.** The frame posted to hardware carries exactly the declared
LED count for every element — the guard against the shifted-tail bug.

**Hostile state files.** `{ not json`, `[]`, `null`, a layer with an unknown
effect, a non-list where a list belongs, a speed of `"fast"`. The editor must
start every time.

---

## Manual — needs hardware, elevation, or a reboot

Automation cannot see light, and cannot elevate. These are quick.

### After `Cleanup Startup.bat`

- [ ] Its own VERIFY block prints **ALL CLEAN** (it re-reads every service, the
      registry entry and the task rather than reporting what it intended).
- [ ] LED Studio still drives lighting afterwards.
- [ ] Re-test the white **O** key with the Razer services stopped. If it still
      differs from its neighbours with everything else off, it is the
      keyboard's own onboard profile or a dead LED, not this software.

### After a reboot — the one thing never yet verified end to end

- [ ] LED Studio appears on its own.
- [ ] Fans tab: both daemons green, neither says "NOT RUNNING".
- [ ] Fans tab: pump reads its commanded duty, no red mismatch line.
- [ ] Case fans are on curve values, not stuck at one number.
- [ ] `mobo_daemon.log` shows exactly **one** `===== started pid` for this
      boot. More than one means the single-instance guard failed.

### Under load

- [ ] Run a game for ten minutes. Fan duties rise with GPU core/VRAM.
- [ ] Pump stays flat at its commanded duty — it is fixed, never a curve.
- [ ] No `WARNING: pump at ...` lines in `mobo_daemon.log`.

### Lighting by eye

- [ ] Every fan ring lights (all three NZXT channels, not just 8 LEDs each).
- [ ] `matrix` on the keyboard falls as narrow vertical strands, one key wide.
- [ ] `vu` with music: bottom row visibly changes rather than sitting lit.
- [ ] Add a layer, drag/resize/rotate it — only LEDs inside it change.
- [ ] Close the app: the daemon's own lighting resumes within a few seconds.
- [ ] Reopen: the effect, palette and layers come back as they were.

---

## Known gaps

Stated rather than papered over:

- The **elevated** paths in `mobo_daemon` — the pump re-assert and the SuperIO
  writes — cannot run under the unelevated test process. Their arithmetic is
  covered; the actual `SetSoftware` call is exercised only by the daemon
  running for real, and observed through `sensors.json` and the Fans tab.
- **Audio capture** is not asserted against real sound. The VU maths is tested
  with injected levels; whether the loopback device is present is a runtime
  condition the UI reports rather than a test.
- Running the `app` section repeatedly builds and destroys several Tk roots
  in quick succession, which leaves a few `invalid command name ...tick`
  lines from Tcl's background error handler. That is the harness tearing down
  faster than Tk's timers unwind — the application itself closes cleanly, and
  the suite still reports 0 failures.
