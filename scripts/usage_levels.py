"""Live resource usage, for the usage-gradient effect.

    from usage_levels import SHARED
    SHARED.start()
    SHARED.cpu, SHARED.ram, SHARED.gpu, SHARED.overall   # each 0..1

CPU and RAM come from psutil, which is instant. GPU comes from nvidia-smi,
which takes a couple of hundred milliseconds - fine on its own thread every
couple of seconds, unusable inside a 30 fps render loop.

Values are smoothed. Raw CPU percent swings between 3% and 60% between
consecutive samples on an idle machine, and a light that tracks it exactly
just strobes; the point is to read load at a glance, not to win an accuracy
contest.

OVERALL deliberately does not treat RAM like the others. This machine idles at
96% RAM - normal once the OS has filled it with cache - so an overall figure
that averaged RAM in equally would sit pinned at red forever and tell you
nothing. RAM gets a small weight here and its own dedicated display on the RAM
sticks, where a constant high reading is at least the truth about that one
number rather than something that swamps everything else.
"""
import collections
import ctypes
import subprocess
import threading
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CPU_RAM_PERIOD = 0.5        # psutil is cheap
GPU_PERIOD = 2.0            # nvidia-smi is not

SMOOTH = 0.25               # EMA factor per sample; lower is calmer

# See the module docstring: RAM is included because it is a real signal, but
# weighted low so a permanently-full cache cannot peg the whole case red.
W_CPU, W_GPU, W_RAM = 0.45, 0.45, 0.10

# --- typing speed -----------------------------------------------------------
#
# ONLY THE SPACEBAR IS EVER READ. GetAsyncKeyState is asked about exactly one
# virtual key code and returns a single bit: is that key down right now. It
# cannot report which other keys were pressed, so this cannot see what you
# type. That is deliberate - the obvious implementation is a global keyboard
# hook, which would sit in the input chain and receive every keystroke, and
# nothing here needs that. Nothing is recorded either: only the timestamps of
# recent presses exist, in memory, and they age out.
VK_SPACE = 0x20
WPM_CAP = 200.0          # DEFAULT cap; the UI can change it per user
WPM_CAP_MIN, WPM_CAP_MAX = 30.0, 400.0
WPM_WINDOW = 8.0         # seconds of presses used to work out the rate
KEY_POLL = 0.02          # 50 Hz - one API call, far cheaper than a hook


class UsageLevels:
    def __init__(self):
        self.cpu = 0.0
        self.ram = 0.0
        self.gpu = 0.0
        self.wpm = 0.0
        self.typing = 0.0        # wpm scaled against the cap, 0..1
        self.cap = WPM_CAP       # per-instance so the UI can set it live
        self._presses = collections.deque()
        self.available = False
        self.gpu_available = False
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def overall(self):
        return max(0.0, min(1.0, W_CPU * self.cpu + W_GPU * self.gpu
                            + W_RAM * self.ram))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._keys = threading.Thread(target=self._run_keys, daemon=True)
        self._keys.start()

    def _run_keys(self):
        """Count spacebar presses to estimate words per minute.

        Polls one key rather than hooking the keyboard. A press is counted on
        the DOWN edge, so holding the spacebar counts once rather than
        repeating, and auto-repeat cannot inflate the figure.
        """
        try:
            get_state = ctypes.windll.user32.GetAsyncKeyState
        except Exception:
            return
        was_down = False
        while not self._stop.is_set():
            try:
                down = bool(get_state(VK_SPACE) & 0x8000)
                now = time.monotonic()
                if down and not was_down:
                    self._presses.append(now)
                was_down = down
                cutoff = now - WPM_WINDOW
                while self._presses and self._presses[0] < cutoff:
                    self._presses.popleft()
                wpm = len(self._presses) * (60.0 / WPM_WINDOW)
                # rise immediately, fall gently: a burst should light up at
                # once, and a pause between sentences should not slam to zero
                self.wpm = wpm if wpm > self.wpm else self.wpm + (wpm - self.wpm) * 0.15
                cap = max(1.0, float(self.cap))
                self.typing = max(0.0, min(1.0, self.wpm / cap))
            except Exception:
                pass
            self._stop.wait(KEY_POLL)

    def stop(self):
        self._stop.set()

    def set_cap(self, wpm):
        """Typing speed that should read as fully red. Clamped, because a cap
        at or below zero would divide by nothing and a huge one would make the
        keyboard permanently green."""
        try:
            wpm = float(wpm)
        except (TypeError, ValueError):
            return self.cap
        self.cap = max(WPM_CAP_MIN, min(WPM_CAP_MAX, wpm))
        cap = max(1.0, self.cap)
        self.typing = max(0.0, min(1.0, self.wpm / cap))
        return self.cap

    def value(self, source):
        return {"cpu": self.cpu, "gpu": self.gpu, "ram": self.ram,
                "wpm": self.typing,
                "all": self.overall}.get(source, self.overall)

    def _smooth(self, old, new):
        return old + (new - old) * SMOOTH

    def _run(self):
        try:
            import psutil
        except Exception as exc:
            self.error = f"psutil unavailable: {exc}"
            return
        psutil.cpu_percent(interval=None)      # prime; the first call is junk
        self.available = True
        last_gpu = 0.0
        while not self._stop.is_set():
            try:
                self.cpu = self._smooth(
                    self.cpu, psutil.cpu_percent(interval=None) / 100.0)
                self.ram = self._smooth(
                    self.ram, psutil.virtual_memory().percent / 100.0)
            except Exception:
                pass
            now = time.monotonic()
            if now - last_gpu >= GPU_PERIOD:
                last_gpu = now
                try:
                    out = subprocess.run(
                        ["nvidia-smi", "--query-gpu=utilization.gpu",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=8,
                        creationflags=NO_WINDOW)
                    val = float(out.stdout.strip().splitlines()[0])
                    self.gpu = self._smooth(self.gpu, val / 100.0)
                    self.gpu_available = True
                except Exception:
                    self.gpu_available = False
            self._stop.wait(CPU_RAM_PERIOD)


SHARED = UsageLevels()


if __name__ == "__main__":
    SHARED.start()
    print("sampling for 8s")
    for _ in range(8):
        time.sleep(1.0)
        print(f"  cpu {SHARED.cpu*100:5.1f}%  gpu {SHARED.gpu*100:5.1f}%  "
              f"ram {SHARED.ram*100:5.1f}%  ->  overall {SHARED.overall*100:5.1f}%")
    SHARED.stop()
