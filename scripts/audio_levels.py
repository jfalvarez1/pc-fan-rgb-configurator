"""Real audio levels from the speaker's loopback, for the VU effects.

Captures whatever Windows is playing (WASAPI loopback via `soundcard`), runs
an FFT, and buckets it into logarithmically-spaced bands - because pitch is
logarithmic, and linear bins would put almost every musical note in the first
couple of bars.

    from audio_levels import AudioLevels
    a = AudioLevels(); a.start()
    a.levels(8)      # -> [0..1] per band, or None if capture is unavailable
    a.active         # True when audio is actually playing

Runs on a daemon thread and never raises into the caller: if the device
disappears or the library is missing, levels() returns None and the effect
falls back to its synthesised motion, which the UI states plainly.
"""
import math
import threading
import time
import warnings

warnings.filterwarnings("ignore")

RATE = 48000
BLOCK = 2048
BANDS_MAX = 24
FLOOR_DB = -62.0        # quieter than this counts as silence
SMOOTH_UP = 0.55        # fast attack
SMOOTH_DN = 0.14        # slow release, so bars fall like a real meter


class AudioLevels:
    def __init__(self):
        self._bands = [0.0] * BANDS_MAX
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.available = False
        self.active = False
        self.device = None
        self.error = None

    # ---- lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- capture

    def _run(self):
        try:
            import numpy as np
            import soundcard as sc
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return

        edges = self._edges(BANDS_MAX)
        window = None

        while not self._stop.is_set():
            try:
                spk = sc.default_speaker()
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                self.device = spk.name
                self.available = True
                self.error = None
                with mic.recorder(samplerate=RATE, channels=2,
                                  blocksize=BLOCK) as rec:
                    while not self._stop.is_set():
                        data = rec.record(numframes=BLOCK)
                        mono = data.mean(axis=1)
                        if window is None or len(window) != len(mono):
                            window = np.hanning(len(mono))
                        spec = np.abs(np.fft.rfft(mono * window))
                        freqs = np.fft.rfftfreq(len(mono), 1.0 / RATE)

                        peak = float(np.abs(mono).max())
                        self.active = peak > 3e-4

                        new = []
                        for lo, hi in edges:
                            sel = (freqs >= lo) & (freqs < hi)
                            mag = float(spec[sel].mean()) if sel.any() else 0.0
                            db = 20.0 * math.log10(mag + 1e-9)
                            v = (db - FLOOR_DB) / (-FLOOR_DB + 20.0)
                            new.append(max(0.0, min(1.0, v)))

                        with self._lock:
                            for i, v in enumerate(new):
                                cur = self._bands[i]
                                a = SMOOTH_UP if v > cur else SMOOTH_DN
                                self._bands[i] = cur + (v - cur) * a
            except Exception as exc:
                self.available = False
                self.active = False
                self.error = f"{type(exc).__name__}: {exc}"
                time.sleep(2.0)      # device changed or busy; retry

    @staticmethod
    def _edges(n, lo=40.0, hi=16000.0):
        """Logarithmic band edges - pitch is logarithmic, so linear bins would
        cram nearly every musical note into the first two bars."""
        out = []
        for i in range(n):
            a = lo * (hi / lo) ** (i / n)
            b = lo * (hi / lo) ** ((i + 1) / n)
            out.append((a, b))
        return out

    # ---- read

    def levels(self, bars):
        """`bars` values in 0..1, or None when capture is unavailable."""
        if not self.available:
            return None
        bars = max(2, min(BANDS_MAX, int(bars)))
        with self._lock:
            src = list(self._bands)
        if bars == BANDS_MAX:
            return src
        # fold the fixed analysis bands down to however many bars are wanted
        out = []
        step = BANDS_MAX / bars
        for i in range(bars):
            a, b = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
            out.append(max(src[a:b]))
        return out


SHARED = AudioLevels()


if __name__ == "__main__":
    SHARED.start()
    print("capturing for 6s - play something to see it move")
    for _ in range(12):
        time.sleep(0.5)
        lv = SHARED.levels(8)
        if lv is None:
            print(f"  unavailable: {SHARED.error}")
        else:
            bar = "".join("#" if v > 0.55 else ("+" if v > 0.25 else ".")
                          for v in lv)
            print(f"  [{bar}]  active={SHARED.active}  {SHARED.device}")
    SHARED.stop()
