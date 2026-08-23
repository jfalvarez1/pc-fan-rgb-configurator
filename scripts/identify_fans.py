"""Identify which physical fan is on which NZXT channel.

Isolates ONE channel at a time: the others are held at their original quiet
duty while the channel under test is driven to PROBE_DUTY.

All writes go through nzxt_util.set_duty(), which verifies them - the
controller silently drops rapid consecutive writes (see nzxt_util docstring).

Run:  python identify_fans.py
Ramping UP is always thermally safe. Duties are restored on exit, including
on Ctrl+C.
"""
import sys
import time

import nzxt_util as nz

PROBE_DUTY = 100
HOLD_SECONDS = 10
SETTLE_SECONDS = 4
CHANNELS = ["fan1", "fan2", "fan3"]


def main():
    dev = nz.find_nzxt()
    if dev is None:
        sys.exit("No NZXT controller found.")

    print(f"Device: {dev.description}\n")
    with dev.connect():
        dev.initialize()
        original = nz.read_duties(dev)
        baseline = nz.read_speeds(dev)
        print(f"Original duties: {original}")
        print(f"Baseline speeds: {baseline}")
        print(f"\nEach channel goes to {PROBE_DUTY}% ALONE for {HOLD_SECONDS}s.")
        print("Watch the blades / feel the airflow.\n")

        results = {}
        try:
            for i, ch in enumerate(CHANNELS, start=1):
                base = original.get(i)
                if base is None:
                    print(f"{ch}: nothing connected, skipping")
                    continue

                print(f"===== {ch} =====")
                ok = nz.set_duty(dev, ch, PROBE_DUTY)
                if not ok:
                    print(f"  !! write to {ch} was dropped after retries")
                    continue
                print(f"  {ch}: {base}% -> {PROBE_DUTY}%  (write confirmed)")

                peak = 0
                for remaining in range(HOLD_SECONDS, 0, -1):
                    time.sleep(1)
                    s = nz.read_speeds(dev)
                    peak = max(peak, s.get(i, 0))
                    if remaining % 3 == 0 or remaining == 1:
                        marks = "  ".join(
                            f"{'>>' if j == i else '  '}fan{j}={s.get(j, '?')}"
                            for j in sorted(s)
                        )
                        print(f"  [{remaining:2d}s] {marks}")

                delta = peak - baseline.get(i, 0)
                results[ch] = (peak, delta)
                print(f"  peak {peak} rpm  (+{delta} over idle)")

                nz.set_duty(dev, ch, base)
                print(f"  restored {ch} to {base}%\n")
                time.sleep(SETTLE_SECONDS)

        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            for i, ch in enumerate(CHANNELS, start=1):
                if i in original:
                    nz.set_duty(dev, ch, original[i], verify=False)
            time.sleep(1)
            print(f"Restored: {nz.read_duties(dev)}")

        if results:
            print("\n=== SUMMARY ===")
            for ch, (peak, delta) in results.items():
                print(f"  {ch}: {peak} rpm at {PROBE_DUTY}%  (+{delta} rpm)")


if __name__ == "__main__":
    main()
