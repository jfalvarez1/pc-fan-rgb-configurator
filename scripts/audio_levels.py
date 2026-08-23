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

Three things here were got wrong first time round and are worth keeping
straight, because each produced a meter that looked plausible but lied:

1. `np.fft.rfft` is UNNORMALISED - bin magnitude scales with block size, so a
   quiet signal measured ~1000x too loud and every band sat far above the
   floor. Dividing by `window.sum()/2` makes a bin read as the amplitude of
   that component, so the dB values below are real dBFS.
2. Log-spaced bands are narrower than the FFT bin spacing at the bottom end.
   At 2048 samples the spacing is 23.4 Hz and the 46-53 Hz band contained no
   bins at all, so it read a permanent 0.00. Hence the larger block and the
   nearest-bin fallback.
3. A fixed dB floor cannot work. Steady loud content (a game, a mix) parks
   every band near the top and the lower rows never go out - which is exactly
   the "bottom is always filled" complaint. Each band therefore tracks its own
   rolling floor and peak and is normalised into THAT, so what you see is how
   loud a band is relative to how loud it has recently been.
"""
import math
import threading
import time
import warnings

warnings.filterwarnings("ignore")

RATE = 48000
BLOCK = 4096            # 11.7 Hz bins; 2048 left the lowest bands empty
BANDS_MAX = 24

# Absolute scale, in real dBFS now that the FFT is normalised.
DB_MIN = -75.0          # below this is nothing
DB_MAX = -10.0          # a loud band
ABS_GATE_DB = -62.0     # genuinely silent; forced to zero regardless of range

# Rolling per-band range. This is what lets the bottom rows move.
#
# A rolling MIN/MAX over a short window, not an exponential tracker. The first
# attempt used a slowly-rising baseline, and measurement killed it: at 0.0008
# per block the time constant was about 100 seconds, so across a 15s capture
# the baseline never left zero and every band stayed pinned near its peak -
# the bottom row was dark 0% of the time. A window is also easier to reason
# about: a band reads 1.0 when it is at its loudest in the last few seconds
# and 0.0 when at its quietest.
WINDOW_SEC = 4.0
MIN_RANGE = 0.10        # never stretch a tiny variation to full scale

GATE = 0.25             # fraction of a band's own range that counts as off
EXPO = 1.6              # >1 pushes quiet content down, opening up the bottom
GAIN = 1.0              # user sensitivity multiplier

SILENCE_PEAK = 3e-4     # waveform peak below this = nothing is playing

SMOOTH_UP = 0.55        # fast attack
SMOOTH_DN = 0.40        # release. Measured, not guessed: at 0.14 the fall
                        # took ~1.3s, so the brief dips that let the lowest row
                        # go dark were smoothed away entirely.


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
        self._ring = None       # (WINDOW, BANDS_MAX) rolling history
        self._ri = 0
        self._filled = 0
        self.gain = GAIN

    # ---- lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_gain(self, g):
        """Sensitivity multiplier, roughly 0.3 (calm) .. 3.0 (hot)."""
        self.gain = max(0.1, min(4.0, float(g)))

    # ---- capture

    def _run(self):
        try:
            import numpy as np
            import soundcard as sc
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return

        win = np.hanning(BLOCK)
        fnorm = win.sum() / 2.0
        freqs = np.fft.rfftfreq(BLOCK, 1.0 / RATE)
        sel = self._band_bins(np, freqs)
        span = DB_MAX - DB_MIN
        nwin = max(4, int(round(WINDOW_SEC * RATE / BLOCK)))
        self._ring = np.zeros((nwin, BANDS_MAX))
        self._ri = 0
        self._filled = 0

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
                        if len(mono) < BLOCK:
                            continue
                        mono = mono[:BLOCK]

                        wave_peak = float(np.abs(mono).max())
                        self.active = wave_peak > SILENCE_PEAK
                        if not self.active:
                            with self._lock:
                                for i in range(BANDS_MAX):
                                    self._bands[i] *= 0.5
                            continue

                        spec = np.abs(np.fft.rfft(mono * win)) / fnorm
                        mag = np.array([spec[b].max() for b in sel])
                        db = 20.0 * np.log10(mag + 1e-12)
                        raw = np.clip((db - DB_MIN) / span, 0.0, 1.0)

                        # rolling window: how loud is this band compared with
                        # how loud it has been over the last few seconds?
                        self._ring[self._ri] = raw
                        self._ri = (self._ri + 1) % nwin
                        self._filled = min(self._filled + 1, nwin)
                        hist = self._ring[:self._filled]
                        mn = hist.min(axis=0)
                        mx = hist.max(axis=0)

                        v = (raw - mn) / np.maximum(MIN_RANGE, mx - mn)
                        v = np.where(v < GATE, 0.0, (v - GATE) / (1.0 - GATE))
                        v = np.clip(v, 0.0, 1.0) ** EXPO * self.gain
                        v = np.where(db <= ABS_GATE_DB, 0.0, v)
                        new = np.clip(v, 0.0, 1.0)

                        with self._lock:
                            for i in range(BANDS_MAX):
                                cur = self._bands[i]
                                tgt = float(new[i])
                                s = SMOOTH_UP if tgt > cur else SMOOTH_DN
                                self._bands[i] = cur + (tgt - cur) * s
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

    @classmethod
    def _band_bins(cls, np, freqs):
        """Bin indices per band, with a nearest-bin fallback. The lowest
        log-bands are narrower than the bin spacing, and an empty selection
        reads as a permanent zero rather than as a quiet band."""
        out = []
        for lo, hi in cls._edges(BANDS_MAX):
            idx = np.where((freqs >= lo) & (freqs < hi))[0]
            if len(idx) == 0:
                idx = np.array([int(np.argmin(np.abs(freqs - (lo + hi) / 2)))])
            out.append(idx)
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
