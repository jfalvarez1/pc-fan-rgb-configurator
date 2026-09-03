"""Thermal-reactive fan + RGB daemon.

Reads temperatures, applies interpolated fan curves to the NZXT controller,
and pushes a matching colour to OpenRGB devices.

SAFETY: dry-run by default. It prints what it *would* do and touches nothing.
Pass --apply to actually drive hardware.

    python thermal_rgb_loop.py              # dry run, safe
    python thermal_rgb_loop.py --once       # single pass, no loop
    python thermal_rgb_loop.py --apply      # for real
    python thermal_rgb_loop.py --apply --no-rgb    # fans only

Ctrl+C restores every channel to SAFE_EXIT_DUTY.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import threading
import urllib.request

import case_layout
import fan_tuning
import single_instance
import nzxt_util
import openrgb_boot
import rgb_effects

# ---------------------------------------------------------------- CONFIG ---

POLL_SECONDS = 3.0

# CASE FAN CURVES - each channel carries one curve PER SENSOR and runs at
# whichever demands the most duty.
#
# Why per-sensor rather than max(temperature): 80 C is hot for a GPU core,
# unremarkable for a 9800X3D, and cool for GDDR7. Converting each sensor to a
# duty FIRST, then taking the max, compares like with like.
#
# HARDWARE (identified one channel at a time, measured at 100%):
#   fan1  side intake F360    3x120mm, 2489 rpm max
#   fan2  bottom intake F420  3x140mm, 2013 rpm max, feeds the GPU directly
#   fan3  rear exhaust        single fan, 1863 rpm max
# The Arctic pump and radiator fans are NOT here - they are on motherboard
# headers, owned by mobo_daemon.
#
# TUNED FROM MEASUREMENT, not from principle. Forza Horizon, matched ~94.5%
# GPU util, baseline n=555 vs tuned n=329 saturated samples:
#
#     gpu_core   71.1 -> 67.4   -3.7 C   CLEAR (sd 0.7)
#     gpu_vram   75.2 -> 71.0   -4.2 C   CLEAR (sd 1.0)
#     cpu_tctl   65.2 -> 63.9   -1.3 C   NOISE (sd 2.4)
#     gpu_clock  2790 -> 2792   +1.9     NOISE - the card is power-capped at
#                                        ~438 W, so cooling buys margin, not
#                                        frames.
#
# Consequences, and they drive the curve shapes below:
#   * GPU core / VRAM curves are aggressive - that is where fan speed
#     measurably converts into lower temperatures.
#   * CPU curves are deliberately LATE and mild. Driving case fans on CPU
#     temperature was measured to do nothing (-1.3 C against sd 2.4); the
#     radiator and pump own CPU cooling. They remain only as a backstop for
#     a genuinely hot CPU.
#   * Idle floors sit near 28-32%. Above ~46 C the ramp is steep. There is
#     nothing to cool at 42 C idle, and running six intakes hard there only
#     pulls dust and adds bearing hours.
#
# Sensors come from sensors.json (elevated mobo_daemon). gpu_core falls back
# to nvidia-smi, so the GPU curves still work with no daemon running.
FAN_CHANNELS = {
    "fan1": {
        "label": "side intake F360",
        "curves": {
            # GPU core and VRAM: MEASURED effective. -3.7 C / -4.2 C when
            # these were driven hard. Keep them aggressive.
            "gpu_core": [(46, 28), (55, 45), (62, 65), (70, 88), (76, 100)],
            "gpu_vram": [(52, 28), (63, 45), (70, 65), (78, 88), (86, 100)],
            # CPU: MEASURED INEFFECTIVE. Case fan rpm rose ~800 and CPU moved
            # -1.3 C against sd 2.4 - noise. Kept only as a mild backstop for
            # a genuinely hot CPU, not as a normal driver.
            "cpu_tctl": [(70, 28), (80, 45), (88, 70), (93, 90)],
        },
    },
    "fan2": {
        "label": "bottom intake F420",
        # feeds the GPU directly - the highest-leverage fan in the case
        "curves": {
            "gpu_core": [(46, 30), (55, 50), (62, 72), (70, 92), (76, 100)],
            "gpu_vram": [(52, 30), (63, 50), (70, 72), (78, 92), (86, 100)],
            "cpu_tctl": [(70, 30), (80, 48), (88, 72), (93, 92)],
        },
    },
    "fan3": {
        "label": "rear exhaust",
        # single fan and the exit path for CPU + VRM heat, so it keeps a
        # slightly stronger CPU term than the intakes - but still well below
        # what it was, since the effect never showed up in measurement.
        "curves": {
            "gpu_core": [(46, 30), (55, 50), (62, 72), (70, 92), (76, 100)],
            "gpu_vram": [(52, 30), (63, 50), (70, 72), (78, 92), (86, 100)],
            "cpu_tctl": [(65, 32), (75, 50), (84, 75), (91, 95)],
        },
    },
}

# QUIET profile. Every knee later and lower than the tuned curves above -
# the same shape, shifted. This is the trade the measured tuning deliberately
# refused (it cost 3.7 C on GPU core and 4.2 C on VRAM), offered here as a
# choice rather than a default.
QUIET_CURVES = {
    "fan1": {
        "gpu_core": [(52, 22), (62, 32), (70, 48), (78, 68), (86, 90)],
        "gpu_vram": [(58, 22), (68, 32), (76, 48), (84, 68), (92, 90)],
        "cpu_tctl": [(76, 22), (85, 35), (91, 55), (95, 75)],
    },
    "fan2": {
        "gpu_core": [(52, 24), (62, 34), (70, 50), (78, 70), (86, 92)],
        "gpu_vram": [(58, 24), (68, 34), (76, 50), (84, 70), (92, 92)],
        "cpu_tctl": [(76, 24), (85, 36), (91, 56), (95, 76)],
    },
    "fan3": {
        "gpu_core": [(52, 24), (62, 34), (70, 50), (78, 70), (86, 92)],
        "gpu_vram": [(58, 24), (68, 34), (76, 50), (84, 70), (92, 92)],
        "cpu_tctl": [(72, 26), (82, 38), (89, 58), (94, 78)],
    },
}


def channel_curves(ch, profile):
    """The curve set this channel should run under the given profile."""
    if profile == "quiet" and ch in QUIET_CURVES:
        return QUIET_CURVES[ch]
    return FAN_CHANNELS[ch]["curves"]


# Where sensors.json lives (written by the elevated mobo_daemon)
SENSOR_FILE = "sensors.json"
SENSOR_MAX_AGE = 30.0      # ignore stale data if the mobo daemon stops

# Response dynamics, tuned for multi-hour sessions. Fans audibly hunting up
# and down is more irritating than a steady slightly-higher speed, so these
# are deliberately sluggish.
MIN_DUTY = 20               # never command below this (stall protection)
MAX_DUTY = 100
DUTY_DEADBAND = 5           # ignore small changes; stops 1 C wobble ramping
FALL_DELAY_SECONDS = 60.0   # a 1-minute lull (loading screen, menu, cutscene)
                            # must NOT spin the fans down mid-session
EMA_ALPHA = 0.12            # heavy smoothing: ~30 s to track a real change,
                            # so transient spikes are ignored entirely
SAFE_EXIT_DUTY = 60         # applied to all channels on clean exit

RGB_SOURCE = "gpu"          # which sensor drives the lighting
# Stops span the range this card ACTUALLY occupies, so the colour is
# informative at a glance: blue idle -> teal gaming -> amber warm -> red hot.
RGB_STOPS = [               # (temp_C, (r, g, b))
    (50, (0, 90, 255)),     # idle / desktop
    (65, (0, 220, 180)),    # normal sustained gaming
    (75, (255, 180, 0)),    # working hard
    (85, (255, 0, 0)),      # hot
]
OPENRGB_HOST = "127.0.0.1"
OPENRGB_PORT = 6742

# "auto"      - DARK at idle, synthwave wave while gaming (default)
# "off"       - LEDs actively blanked, always
# "synthwave" - wave always on
# "thermal"   - one colour everywhere, mapped from temperature
# Override per run with --rgb-mode, e.g. --rgb-mode synthwave
RGB_MODE = "auto"

# Which spatial effect the wave uses. All are rendered by PHYSICAL POSITION
# using case_layout, so they sweep across the case correctly instead of
# scrambling at fan boundaries the way index-based rendering did.
#   wave radial spiral comet rain plasma breathe fire
WAVE_EFFECT = "wave"
# Cycle through several instead of sitting on one. Empty list = no cycling.
WAVE_CYCLE = ["wave", "aurora", "plasma", "radial", "spiral", "matrix"]
WAVE_CYCLE_SECONDS = 90.0

# What counts as "gaming" for auto mode. Thresholds are asymmetric on purpose:
# turning ON is quick and eager, turning OFF is slow and reluctant, so a
# loading screen, menu, or cutscene never blanks your case mid-session.
ACTIVATE_GPU_UTIL = 40      # % - either of these turns the lighting on
ACTIVATE_CPU_UTIL = 50
DEACTIVATE_GPU_UTIL = 20    # % - BOTH must fall below these to turn it off
DEACTIVATE_CPU_UTIL = 25
ACTIVATE_DWELL = 8.0        # seconds of load before it lights up
DEACTIVATE_DWELL = 120.0    # seconds of calm before it goes dark again

WAVE_PALETTE = rgb_effects.SYNTHWAVE   # or rgb_effects.SUNSET
WAVE_FPS = 30.0             # animation rate on Direct-mode devices
WAVE_CYCLES = 1.5           # palette repeats across the strip
WAVE_SPEED = 0.12           # base travel, palette cycles per second
# The wave speeds up as the GPU heats, so the lighting still tells you
# something instead of being purely decorative.
WAVE_SPEED_BASE_TEMP = 60.0
WAVE_SPEED_MAX_MULT = 2.5

# Claude Code "thinking" glow. Claude Code hooks touch/remove CLAUDE_FLAG;
# while it exists, an orange bloom overrides whatever else is showing.
# Not __file__: the LED Studio exe imports this module for its Fans tab, and
# there __file__ is inside the bundle. The override flag in particular has to
# be the one file every process agrees on, or the editor takes the LEDs and
# the daemon never notices.
from app_paths import DATA as _DATA          # noqa: E402
_BASE = str(_DATA)
# One flag PER SESSION: with several Claudes open, a single shared flag meant
# whichever finished first switched the glow off while the others worked.
CLAUDE_FLAG_DIR = os.path.join(_BASE, "claude_flags")
# While this exists, the daemon leaves the hardware alone so the dashboard can
# drive it manually without the two fighting.
MANUAL_FLAG = os.path.join(_BASE, "manual_override.flag")
CLAUDE_GLOW_COLOUR = (255, 120, 0)
CLAUDE_GLOW_SPEED = 0.85
# If a Stop hook is ever missed (crash, kill -9) the flag would strand the
# lights on forever, so treat a stale flag as absent.
CLAUDE_FLAG_TIMEOUT = 600.0

# Which device TYPES take part in the effects.
#   KEYBOARD is INCLUDED on request. Caveat: Razer Synapse autostarts and will
#   fight OpenRGB for the keyboard - whichever wrote last wins. If the keyboard
#   flickers or reverts, that is Synapse, not this code.
#   DRAM was added after a rescan revealed two Corsair Vengeance DDR5 sticks
#   that had never been driven.
RGB_DEVICE_TYPES = {"GPU", "MOTHERBOARD", "LEDSTRIP", "DRAM", "KEYBOARD"}

# Devices whose zones control nothing physical. Verified one zone at a time:
# lighting either GPU zone produced no visible change, because this card's
# ARGB is cabled into motherboard header "Aura Addressable 2" instead. Driving
# it would be pure waste - and it is Static-only, so every write risks flash.
RGB_EXCLUDE_NAMES = ("ZOTAC GAMING GeForce RTX 5090",)

# The RTX 5090 exposes no Direct mode, only Static. Static can write to device
# flash on some hardware, so repainting on every 1 C wobble is a bad idea.
# Only repaint when the colour moves meaningfully, and never faster than this.
RGB_MIN_INTERVAL = 5.0      # seconds between LED writes
RGB_MIN_DELTA = 10          # per-channel change required to repaint
LHM_URL = "http://localhost:8085/data.json"   # LibreHardwareMonitor, if running
LHM_TIMEOUT = 1.5           # seconds
LHM_RETRY_SECONDS = 60.0    # back off this long after a failed CPU-temp read

def read_published_sensors():
    """Sensors from the elevated mobo_daemon (CPU Tctl, VRAM junction, ...).

    Returns {} when the daemon is not running or the file is stale, so the
    curves silently fall back to GPU core alone rather than freezing on old
    numbers.
    """
    try:
        path = os.path.join(_BASE, SENSOR_FILE)
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return {}
    ts = data.get("ts")
    if ts is None or (time.time() - ts) > SENSOR_MAX_AGE:
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, (int, float)) and k != "ts"}


def claude_thinking():
    """True while ANY Claude Code session is working.

    Each session owns a flag file, so concurrent sessions compose instead of
    cancelling each other. A flag older than CLAUDE_FLAG_TIMEOUT is ignored,
    so a crashed session cannot strand the lights on.
    """
    now = time.time()
    try:
        names = os.listdir(CLAUDE_FLAG_DIR)
    except OSError:
        return False
    for name in names:
        if not name.endswith(".flag"):
            continue
        try:
            if now - os.path.getmtime(
                    os.path.join(CLAUDE_FLAG_DIR, name)) < CLAUDE_FLAG_TIMEOUT:
                return True
        except OSError:
            continue
    return False


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True         # cannot tell - assume the owner is still there


def manual_override(scope="all"):
    """True while an editor has taken manual control of the given hardware.

    SCOPE MATTERS. This flag used to mean "pause everything", because the old
    dashboard drove fans as well as LEDs. LED Studio only ever touches LEDs -
    so once it holds the flag continuously (it now takes control on launch and
    is meant to stay open), an unscoped flag would stop the CASE FAN CURVES
    from running for as long as the editor is open. The fans would hold their
    last duty and never ramp under load.

    A flag with no `scope=` line is from one of the older tools and still
    pauses everything, which is what those tools expect.

    The one-hour mtime window is kept for the older tools, which write the
    flag once and never touch it again. It has two failure modes though: an
    editor open for longer than an hour silently loses control back to this
    daemon mid-session, and one that crashes keeps the daemon stood down for
    up to an hour afterwards.

    So newer writers stamp `pid=` into the flag and refresh it periodically.
    If that process is gone the flag is stale immediately, whatever its mtime
    says - which is the difference between trusting a claim and checking it.

    `hold=1` is the exception to both checks. It means "the user set this
    lighting deliberately and wants it kept" - so it survives the editor
    closing and does not expire. Without it, closing the editor handed the
    LEDs straight back here, and at idle this daemon blanks them: the user
    would set up their lighting, close the window, and watch it go dark.
    """
    try:
        age = time.time() - os.path.getmtime(MANUAL_FLAG)
    except OSError:
        return False
    try:
        with open(MANUAL_FLAG) as fh:
            body = fh.read()
    except OSError:
        return True
    alive, declared, hold = True, "all", False
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                alive = _pid_alive(int(line[4:]))
            except ValueError:
                alive = True
        elif line.startswith("scope="):
            declared = line[6:].strip() or "all"
        elif line.startswith("hold="):
            hold = line[5:].strip() not in ("", "0", "false", "False")
    if not hold:
        if age >= 3600:
            return False
        if not alive:
            return False
    if declared == "all" or scope == "all":
        return True
    return scope in [s.strip() for s in declared.split(",")]


# ------------------------------------------------------------- SENSORS -----


# Under pythonw there is no console, so every subprocess would flash up a new
# console window - once per poll, forever. CREATE_NO_WINDOW suppresses it.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_gpu_util = None


def read_gpu_temp():
    """GPU temp via nvidia-smi, caching utilisation from the same call.

    One subprocess per poll instead of two - each spawn costs ~30 ms and,
    under pythonw, would otherwise be a second console window to suppress.
    """
    global _gpu_util
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=_NO_WINDOW,
        )
        temp, util = out.stdout.strip().splitlines()[0].split(",")
        _gpu_util = float(util)
        return float(temp)
    except Exception:
        _gpu_util = None
        return None


def read_gpu_util():
    """GPU utilisation %, cached by the last read_gpu_temp() call."""
    return _gpu_util


def read_cpu_util():
    """CPU utilisation % since the previous call (non-blocking)."""
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:
        return None


def _walk_lhm(node, out):
    text = node.get("Text", "")
    value = str(node.get("Value", ""))
    if "C" in value and any(ch.isdigit() for ch in value):
        try:
            out[text] = float(value.split()[0].replace(",", "."))
        except (ValueError, IndexError):
            pass
    for child in node.get("Children", []):
        _walk_lhm(child, out)


_lhm_next_try = 0.0


def read_cpu_temp():
    """CPU temp via LibreHardwareMonitor's HTTP server. Returns float C or None.

    Requires LHM running with 'Remote Web Server' enabled on port 8085.
    Without it there is NO CPU temperature source on this machine.

    A dead TCP connect on Windows blocks ~4s, which would dominate the poll
    interval, so failures back off for LHM_RETRY_SECONDS.
    """
    global _lhm_next_try
    if time.monotonic() < _lhm_next_try:
        return None
    try:
        with urllib.request.urlopen(LHM_URL, timeout=LHM_TIMEOUT) as resp:
            data = json.load(resp)
    except Exception:
        _lhm_next_try = time.monotonic() + LHM_RETRY_SECONDS
        return None

    temps = {}
    _walk_lhm(data, temps)
    # 9800X3D reports Tctl/Tdie; fall back to any CPU-ish core sensor.
    for key in ("Core (Tctl/Tdie)", "CPU Package", "Core (Tdie)", "CPU Cores"):
        if key in temps:
            return temps[key]
    for name, val in temps.items():
        if "tctl" in name.lower() or "tdie" in name.lower():
            return val
    return None


SENSORS = {"gpu": read_gpu_temp, "cpu": read_cpu_temp}

# --------------------------------------------------------------- CURVE -----


def interpolate(curve, temp):
    """Piecewise-linear interpolation over (temp, duty) points."""
    if temp <= curve[0][0]:
        return float(curve[0][1])
    if temp >= curve[-1][0]:
        return float(curve[-1][1])
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            span = t1 - t0
            if span == 0:
                return float(d1)
            return d0 + (d1 - d0) * (temp - t0) / span
    return float(curve[-1][1])


def lerp_color(stops, temp):
    if temp <= stops[0][0]:
        return stops[0][1]
    if temp >= stops[-1][0]:
        return stops[-1][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t0 <= temp <= t1:
            f = (temp - t0) / (t1 - t0) if t1 != t0 else 0.0
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return stops[-1][1]


# ---------------------------------------------------------------- RGB ------


class RGBOutput:
    """Best-effort OpenRGB output. Never fatal if the server is down.

    Two modes:
      "synthwave"  a travelling pink/purple/blue wave, animated on a worker
                   thread at WAVE_FPS, independent of the slow fan poll
      "thermal"    one colour for everything, mapped from temperature

    Devices are split by capability, because they are not equivalent:
      strips       Direct mode, many LEDs -> per-LED animation
      points       Direct mode, few LEDs  -> sample one colour from the wave
      static_only  no Direct mode (the 5090) -> Static may write to flash, so
                   these are updated at most every STATIC_UPDATE_SECONDS
    """

    RECONNECT_COOLDOWN = 30.0
    STATIC_UPDATE_SECONDS = 10.0

    def __init__(self, enabled, mode=RGB_MODE):
        self.client = None
        self.enabled = enabled
        self.mode = mode
        self.last = None
        self.next_retry = 0.0
        self.last_write = 0.0
        self.strips = []
        self.points = []
        self.static_only = []
        self.spatial = []
        self.temp = None
        self.apply = False
        self._thread = None
        self._active = False
        self._glow = rgb_effects.OutwardGlow(colour=CLAUDE_GLOW_COLOUR,
                                             speed=CLAUDE_GLOW_SPEED)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        if self.enabled:
            self.connect()

    # ---- connection

    @staticmethod
    def _resolve(client, el):
        """Map a layout element to (device, absolute LED offset)."""
        matches = [d for d in client.devices
                   if d is not None and getattr(d, "type", None) is not None
                   and el["device"].lower() in d.name.lower()]
        if not matches:
            return None, None
        dev = matches[min(el.get("dev_index", 0), len(matches) - 1)]
        off = 0
        for z in dev.zones:
            if el["zone"].lower() in z.name.lower():
                return dev, off + el["start"]
            off += len(z.leds)
        return None, None

    def maybe_reconnect(self):
        if not self.enabled or self.client is not None:
            return
        if time.monotonic() < self.next_retry:
            return
        if self.connect():
            self.start_wave()   # always: the glow must work even when dark

    def connect(self):
        self.next_retry = time.monotonic() + self.RECONNECT_COOLDOWN
        # if OpenRGB was closed, bring it back rather than sitting dark
        if not openrgb_boot.sdk_up():
            openrgb_boot.ensure_running(wait=15.0, quiet=True)
        try:
            from openrgb import OpenRGBClient
            client = OpenRGBClient(OPENRGB_HOST, OPENRGB_PORT, "thermal-loop")
            strips, points, static_only = [], [], []
            for dev in client.devices:
                if dev is None or getattr(dev, "type", None) is None:
                    # OpenRGB returns a None entry for a device that failed to
                    # parse; skipping beats crashing the whole daemon.
                    continue
                if dev.type.name not in RGB_DEVICE_TYPES:
                    print(f"[rgb] skipping {dev.name} ({dev.type.name})")
                    continue
                if any(x in dev.name for x in RGB_EXCLUDE_NAMES):
                    print(f"[rgb] skipping {dev.name} (phantom zones)")
                    continue
                modes = [m.name.lower() for m in dev.modes]
                # Corsair DRAM has no Direct/Static; its per-LED mode is
                # "Custom". Treat that as equivalent to Direct.
                if "direct" not in modes and "custom" in modes:
                    try:
                        dev.set_mode("custom")
                        (strips if len(dev.leds) > 4 else points).append(dev)
                        print(f"[rgb] {dev.name}: custom (per-LED), "
                              f"{len(dev.leds)} LED(s)")
                        continue
                    except Exception:
                        pass
                if "direct" in modes:
                    dev.set_mode("direct")
                    (strips if len(dev.leds) > 4 else points).append(dev)
                    kind = "strip" if len(dev.leds) > 4 else "point"
                    print(f"[rgb] {dev.name}: direct, {len(dev.leds)} LED(s)"
                          f" -> {kind}")
                elif "static" in modes:
                    dev.set_mode("static")
                    static_only.append(dev)
                    print(f"[rgb] {dev.name}: STATIC only, {len(dev.leds)}"
                          f" LED(s) -> slow updates to spare its flash")
                else:
                    print(f"[rgb] {dev.name}: no usable mode, skipping")
            # spatial map: element -> (device, offset, [(led_index, nx, ny)])
            spatial = []
            byel = {}
            for el, i, nx, ny in case_layout.led_positions():
                byel.setdefault(el["id"], (el, []))[1].append((i, nx, ny))
            for el_id, (el, pts) in byel.items():
                dev, off = self._resolve(client, el)
                if dev is not None:
                    spatial.append((dev, off, pts))

            with self._lock:
                self.client = client
                self.strips, self.points = strips, points
                self.static_only = static_only
                self.spatial = spatial
            print(f"[rgb] spatial map: {sum(len(p) for _d,_o,p in spatial)} "
                  f"LEDs across {len(spatial)} runs")
            total = len(strips) + len(points) + len(static_only)
            print(f"[rgb] connected, driving {total} device(s) in {self.mode}")
            if self.mode == "synthwave":
                self._active = True
            elif not self._active:
                self.blackout()
            return True
        except Exception as exc:
            print(f"[rgb] unavailable ({exc}); continuing without lighting")
            self.client = None
            return False

    def set_active(self, on):
        """Auto mode: switch the wave on, or blank the LEDs. Idempotent."""
        if on == self._active:
            return
        self._active = on
        if self.client is None:
            return
        print("[rgb] load detected -> synthwave on" if on
              else "[rgb] idle -> lights off")

    @property
    def active(self):
        return self._active

    def blackout(self):
        """Actually turn the LEDs off, not merely stop updating them.

        Prefers each device's native Off mode; falls back to painting black,
        because leaving them on a stale colour is not "off".
        """
        if self.client is None or not self.apply:
            return
        try:
            from openrgb.utils import RGBColor
            black = RGBColor(0, 0, 0)
            with self._lock:
                devices = self.strips + self.points + self.static_only
            for dev in devices:
                modes = [m.name.lower() for m in dev.modes]
                if "off" in modes:
                    try:
                        dev.set_mode("off")
                        continue
                    except Exception:
                        pass
                dev.set_colors([black] * len(dev.leds), fast=True)
            print(f"[rgb] blanked {len(devices)} device(s)")
        except Exception as exc:
            print(f"[rgb] blackout failed ({exc})")

    # ---- shared state

    def set_temp(self, temp):
        self.temp = temp

    def set_apply(self, apply_changes):
        self.apply = apply_changes

    def _speed(self):
        """Hotter GPU -> faster wave, so the lighting still carries meaning."""
        base = WAVE_SPEED
        if self.temp is None:
            return base
        boost = 1.0 + max(0.0, self.temp - WAVE_SPEED_BASE_TEMP) / 30.0
        return base * min(boost, WAVE_SPEED_MAX_MULT)

    # ---- synthwave

    def start_wave(self):
        """Start the render thread. It runs for the whole session and decides
        each frame what to show, so the Claude glow can pre-empt anything."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()

    def _colours_for(self, dev, kind, phase, t):
        """Build the colour list for one device in the given state."""
        from openrgb.utils import RGBColor
        n = len(dev.leds)
        if kind == "glow":
            zones = [len(z.leds) for z in dev.zones if len(z.leds) > 0]
            if not zones or sum(zones) != n:
                zones = [n]        # fall back to one bloom across the device
            cols = self._glow.render_zones(zones, t)
            return [RGBColor(*c) for c in cols]
        # wave
        return [RGBColor(*rgb_effects.gamma(rgb_effects.cyclic_gradient(
            WAVE_PALETTE, (i / n) * WAVE_CYCLES + phase))) for i in range(n)]

    def _render_loop(self):
        """One thread, three states: glow (Claude working) > wave > dark.

        The thread always runs while connected. Blanking is done ONCE on the
        transition into "dark" rather than every frame - repainting black 30
        times a second is pointless traffic, and the GPU is static-only.
        """
        from openrgb.utils import RGBColor
        period = 1.0 / WAVE_FPS
        phase = 0.0
        last_frame = time.monotonic()
        last_static = 0.0
        shown = None

        while not self._stop.is_set():
            now = time.monotonic()
            phase += (now - last_frame) * self._speed()
            last_frame = now

            if not self.apply:
                time.sleep(period)
                continue

            if manual_override("leds"):
                if shown != "manual":
                    print("[rgb] manual override -> dashboard has control",
                          flush=True)
                    shown = "manual"
                time.sleep(0.5)
                continue

            if claude_thinking():
                want = "glow"
            elif self._active:
                want = "wave"
            else:
                want = "dark"

            if want != shown:
                if want == "dark":
                    self.blackout()
                if shown is not None or want != "dark":
                    print(f"[rgb] {shown or 'start'} -> {want}", flush=True)
                shown = want
                last_static = 0.0

            if want == "dark":
                time.sleep(0.25)     # nothing to draw; poll the flag gently
                continue

            try:
                with self._lock:
                    strips = list(self.strips)
                    points = list(self.points)
                    statics = list(self.static_only)

                if want == "wave" and self.spatial:
                    # SPATIAL rendering: every LED is coloured from its real
                    # position in the case, so an effect sweeps across the
                    # machine rather than restarting at each fan.
                    fn = rgb_effects.SPATIAL.get(self._effect_now(now),
                                                 rgb_effects.fx_wave)
                    bufs = {}
                    with self._lock:
                        runs = list(self.spatial)
                    for dev, off, pts in runs:
                        buf = bufs.get(dev.id)
                        if buf is None:
                            buf = [RGBColor(0, 0, 0)] * len(dev.leds)
                            bufs[dev.id] = (buf, dev)
                            buf = bufs[dev.id][0]
                        for i, nx, ny in pts:
                            k = off + i
                            if 0 <= k < len(buf):
                                buf[k] = RGBColor(*fn(nx, ny, now,
                                                      WAVE_PALETTE))
                    for buf, dev in bufs.values():
                        dev.set_colors(buf, fast=True)
                else:
                    for dev in strips:
                        dev.set_colors(self._colours_for(dev, want, phase, now),
                                       fast=True)

                if points or statics:
                    if want == "glow":
                        c = RGBColor(*self._glow.render_zone(1, now)[0])
                    else:
                        c = RGBColor(*rgb_effects.gamma(
                            rgb_effects.cyclic_gradient(WAVE_PALETTE, phase)))
                    for dev in points:
                        dev.set_colors([c] * len(dev.leds), fast=True)
                    if statics and now - last_static >= self.STATIC_UPDATE_SECONDS:
                        for dev in statics:
                            dev.set_colors([c] * len(dev.leds), fast=True)
                        last_static = now
            except Exception as exc:
                print(f"[rgb] render stopped ({exc}); will reconnect")
                with self._lock:
                    self.client = None
                    self.strips = self.points = self.static_only = []
                return

            time.sleep(period)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    # ---- thermal (single colour)

    def push(self, rgb, apply_changes):
        """Repaint only on a meaningful colour change, and never too often."""
        if self.mode != "thermal" or not self.enabled or self.client is None:
            return
        targets = self.strips + self.points + self.static_only
        if not targets:
            return
        if self.last is not None:
            if max(abs(a - b) for a, b in zip(rgb, self.last)) < RGB_MIN_DELTA:
                return
            if time.monotonic() - self.last_write < RGB_MIN_INTERVAL:
                return
        if not apply_changes:
            self.last = rgb
            return
        try:
            from openrgb.utils import RGBColor
            colour = RGBColor(*rgb)
            for dev in targets:
                dev.set_color(colour, fast=True)
            self.last = rgb
            self.last_write = time.monotonic()
        except Exception as exc:
            print(f"[rgb] push failed ({exc}); will retry connection")
            self.client = None


# --------------------------------------------------------------- MAIN ------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually drive hardware (default: dry run)")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--no-rgb", action="store_true", help="skip lighting")
    ap.add_argument("--no-fans", action="store_true", help="skip fan control")
    ap.add_argument("--preview", action="store_true",
                    help="print the resolved curve table and exit")
    ap.add_argument("--rgb-mode", choices=["auto", "off", "synthwave", "thermal"],
                    default=RGB_MODE, help=f"lighting mode (default {RGB_MODE})")
    ap.add_argument("--csv", action="store_true",
                    help="append telemetry to thermal_log.csv for curve fitting")
    ap.add_argument("--log", action="store_true",
                    help="also append output to thermal_loop.log (for pythonw,"
                         " which has no console)")
    args = ap.parse_args()

    # Only one process may drive these devices. Two mobo_daemons were
    # found running at once, fighting over the same headers - and
    # schtasks /End had reported success while leaving one alive.
    if args.apply and not single_instance.claim("ThermalRGBLoop"):
        print("another ThermalRGBLoop is already driving hardware - exiting")
        return 1

    if args.log:
        import pathlib
        logfile = pathlib.Path(_BASE) / "thermal_loop.log"

        class _Tee:
            """Mirror stdout to a file; pythonw has no console to print to."""

            def __init__(self, path):
                self.stream = open(path, "a", buffering=1, encoding="utf-8")

            def write(self, text):
                self.stream.write(text)
                return len(text)

            def flush(self):
                self.stream.flush()

        sys.stdout = sys.stderr = _Tee(logfile)
        print(f"\n===== started (pid {__import__('os').getpid()}) =====")

    if args.preview:
        temps = list(range(40, 96, 5))
        width = max(len(c["label"]) for c in FAN_CHANNELS.values()) + 12
        print("Each channel shows the duty EACH sensor would demand at that "
              "temperature.")
        print("The channel runs at whichever is highest.")
        print()
        print(f"{'temp C':<{width}}  " + "  ".join(f"{t:>4}" for t in temps))
        print("-" * (width + len(temps) * 6))
        for ch, cfg in FAN_CHANNELS.items():
            for src, curve in cfg["curves"].items():
                row = "  ".join(
                    f"{max(MIN_DUTY, min(MAX_DUTY, round(interpolate(curve, t)))):>4}"
                    for t in temps)
                print(f"{cfg['label'] + ' / ' + src:<{width}}  {row}")
            print()
        return 0

    csv_fh = None
    if args.csv:
        import csv as _csv, pathlib as _pl, time as _t
        _p = _pl.Path(_BASE) / "thermal_log.csv"
        new = not _p.exists()
        csv_fh = open(_p, "a", newline="", encoding="utf-8")
        csv_w = _csv.writer(csv_fh)
        if new:
            csv_w.writerow(["ts", "gpu_c", "gpu_util", "cpu_util",
                            "cpu_tctl", "cpu_ccd1", "gpu_vram",
                            "pump_rpm", "rad_rpm", "rad_duty",
                            "f1_duty", "f2_duty", "f3_duty",
                            "f1_rpm", "f2_rpm", "f3_rpm"])
        csv_fh.flush()

    mode = "APPLY" if args.apply else "DRY RUN (nothing will be changed)"
    print(f"=== thermal_rgb_loop - {mode} ===")

    rgb = RGBOutput(enabled=not args.no_rgb, mode=args.rgb_mode)
    rgb.set_apply(args.apply)
    if rgb.enabled and rgb.client is not None:
        if rgb.mode == "synthwave":
            rgb.set_active(True)
        elif rgb.mode in ("off", "auto"):
            rgb.blackout()
        # the render thread runs for the whole session regardless of mode, so
        # the Claude glow can pre-empt even a dark idle
        rgb.start_wave()

    # auto-mode activation state
    busy_since = None
    calm_since = None

    nzxt = None if args.no_fans else nzxt_util.find_nzxt()
    if nzxt is None and not args.no_fans:
        print("[fan] no NZXT controller found; running without fan control")

    smoothed = {}
    commanded = {}
    fall_pending = {}

    ctx = nzxt.connect() if nzxt is not None else None
    if ctx is not None:
        ctx.__enter__()
        nzxt.initialize()

    try:
        while True:
            now = time.monotonic()

            if manual_override("fans"):
                print("manual override active - dashboard has the fans",
                      flush=True)
                # forget commanded state so curves re-apply cleanly afterwards
                commanded.clear()
                fall_pending.clear()
                time.sleep(POLL_SECONDS)
                continue

            raw = {name: fn() for name, fn in SENSORS.items()}

            # merge in the elevated daemon's sensors; nvidia-smi remains the
            # fallback for gpu_core so this works with no daemon at all
            pub = read_published_sensors()
            sources = {}
            if raw.get("gpu") is not None:
                sources["gpu_core"] = raw["gpu"]
            for k in ("gpu_core", "gpu_vram", "cpu_tctl"):
                if pub.get(k) is not None:
                    sources[k] = pub[k]

            # exponential smoothing
            for name, val in raw.items():
                if val is None:
                    continue
                smoothed[name] = (val if name not in smoothed
                                  else EMA_ALPHA * val
                                  + (1 - EMA_ALPHA) * smoothed[name])

            parts = []
            for name in SENSORS:
                if name in smoothed and raw[name] is not None:
                    parts.append(f"{name}={raw[name]:.0f}C"
                                 f"(sm {smoothed[name]:.1f})")
                else:
                    parts.append(f"{name}=n/a")
            line = " | ".join(parts)

            # smooth every available source
            for k, v in sources.items():
                smoothed[k] = (v if k not in smoothed
                               else EMA_ALPHA * v + (1 - EMA_ALPHA) * smoothed[k])

            parts2 = [f"{k.replace('gpu_','').replace('cpu_','')}"
                      f"={smoothed[k]:.0f}" for k in sorted(smoothed)
                      if k in ("gpu_core", "gpu_vram", "cpu_tctl")]
            if parts2:
                line = " ".join(parts2)

            # fans: each sensor proposes a duty; the loudest demand wins
            trims = fan_tuning.load_trims()
            profile = fan_tuning.load_profile()
            for ch, cfg in FAN_CHANNELS.items():
                proposals = {}
                for src, curve in channel_curves(ch, profile).items():
                    t = smoothed.get(src)
                    if t is not None:
                        proposals[src] = interpolate(curve, t)
                if not proposals:
                    line += f" | {cfg['label']}: no sensors"
                    continue
                lead = max(proposals, key=proposals.get)
                # user trim, already clamped by fan_tuning, then the hard
                # MIN/MAX clamp - so a trim can never command a stall
                target = max(MIN_DUTY, min(MAX_DUTY,
                             round(proposals[lead] + trims.get(ch, 0.0))))
                current = commanded.get(ch)

                if current is None:
                    change = True
                elif abs(target - current) < DUTY_DEADBAND:
                    change = False
                elif target < current:
                    # ramping down: require the drop to persist
                    first_seen = fall_pending.get(ch)
                    if first_seen is None:
                        fall_pending[ch] = now
                        change = False
                    else:
                        change = (now - first_seen) >= FALL_DELAY_SECONDS
                else:
                    fall_pending.pop(ch, None)
                    change = True

                if change:
                    fall_pending.pop(ch, None)
                    commanded[ch] = target
                    line += f" | {cfg['label']}->{target}%({lead.split('_')[-1]})"
                    if args.apply and nzxt is not None:
                        try:
                            # verified write: the controller silently drops
                            # rapid consecutive writes (see nzxt_util)
                            if not nzxt_util.set_duty(nzxt, ch, target):
                                line += " (WRITE DROPPED)"
                                commanded[ch] = None
                        except Exception as exc:
                            line += f" (FAILED: {exc})"
                            commanded[ch] = None
                else:
                    line += f" | {cfg['label']}={current}%"

            # lighting
            temp_rgb = smoothed.get(RGB_SOURCE)
            rgb.maybe_reconnect()
            if temp_rgb is not None:
                rgb.set_temp(temp_rgb)

            if rgb.mode == "auto":
                gu, cu = read_gpu_util(), read_cpu_util()
                gu_s = f"{gu:3.0f}" if gu is not None else "  ?"
                cu_s = f"{cu:3.0f}" if cu is not None else "  ?"
                line += f" | gpu {gu_s}% cpu {cu_s}%"

                busy = ((gu is not None and gu >= ACTIVATE_GPU_UTIL)
                        or (cu is not None and cu >= ACTIVATE_CPU_UTIL))
                calm = ((gu is None or gu <= DEACTIVATE_GPU_UTIL)
                        and (cu is None or cu <= DEACTIVATE_CPU_UTIL))

                if not rgb.active:
                    calm_since = None
                    if busy:
                        busy_since = busy_since or now
                        if now - busy_since >= ACTIVATE_DWELL:
                            rgb.set_active(True)
                    else:
                        busy_since = None
                else:
                    busy_since = None
                    if calm:
                        calm_since = calm_since or now
                        if now - calm_since >= DEACTIVATE_DWELL:
                            rgb.set_active(False)
                    else:
                        calm_since = None

                if claude_thinking():
                    line += " | GLOW (claude)"
                elif rgb.active:
                    line += f" | wave x{rgb._speed() / WAVE_SPEED:.2f}"
                else:
                    waited = f" {now - busy_since:.0f}s" if busy_since else ""
                    line += f" | dark{waited}"

            elif rgb.mode == "synthwave":
                line += f" | wave x{rgb._speed() / WAVE_SPEED:.2f}"
            elif rgb.mode == "thermal" and temp_rgb is not None:
                colour = lerp_color(RGB_STOPS, temp_rgb)
                rgb.push(colour, args.apply)
                line += f" | rgb={colour}"

            # publish state for the layout viewer
            try:
                _st = {"ts": time.time(),
                       "temps": {k: round(v, 1) for k, v in smoothed.items()},
                       "fans": {}, "rgb_mode": rgb.mode,
                       "rgb_active": bool(getattr(rgb, "_active", False)),
                       "claude": claude_thinking()}
                for _ch, _cfg in FAN_CHANNELS.items():
                    _st["fans"][_ch] = {"label": _cfg["label"],
                                        "duty": commanded.get(_ch)}
                if nzxt is not None:
                    _r = nzxt_util.read_speeds(nzxt)
                    for _i, _ch in enumerate(FAN_CHANNELS, start=1):
                        _st["fans"][_ch]["rpm"] = _r.get(_i)
                (pathlib.Path(_BASE) / "fan_state.json").write_text(
                    json.dumps(_st, indent=2))
            except Exception:
                pass

            if csv_fh is not None and nzxt is not None:
                try:
                    import time as _t
                    d = nzxt_util.read_duties(nzxt)
                    r = nzxt_util.read_speeds(nzxt)
                    _pub = read_published_sensors()
                    csv_w.writerow([int(_t.time()),
                                    raw.get("gpu"), read_gpu_util(),
                                    read_cpu_util(),
                                    _pub.get("cpu_tctl"), _pub.get("cpu_ccd1"),
                                    _pub.get("gpu_vram"), _pub.get("pump_rpm"),
                                    _pub.get("rad_rpm"), _pub.get("rad_duty"),
                                    d.get(1), d.get(2), d.get(3),
                                    r.get(1), r.get(2), r.get(3)])
                    csv_fh.flush()
                except Exception:
                    pass

            print(line, flush=True)

            if args.once:
                break
            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        rgb.stop()
        if args.apply and rgb.mode in ("auto", "off"):
            rgb.blackout()
        if args.apply and nzxt is not None:
            for ch in FAN_CHANNELS:
                try:
                    nzxt_util.set_duty(nzxt, ch, SAFE_EXIT_DUTY)
                except Exception:
                    pass
            print(f"restored all channels to {SAFE_EXIT_DUTY}%")
        if ctx is not None:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
