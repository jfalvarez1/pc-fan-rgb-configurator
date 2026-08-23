"""Shared helpers for the NZXT RGB & Fan Controller.

WHY THIS EXISTS
---------------
The NZXT SmartDevice2 silently DROPS rapid consecutive HID writes. Measured
on this machine:

    three set_fixed_speed() calls back-to-back  -> only the first applied
    the same three calls with 0.4s gaps         -> all applied

No exception is raised; liquidctl reports success and the duty simply never
changes. Every write in this project must therefore go through set_duty(),
which spaces writes apart and verifies the result by reading it back.
"""
import time

WRITE_GAP = 0.4        # minimum seconds between HID writes
VERIFY_DELAY = 0.5     # wait before reading the duty back
MAX_RETRIES = 3

_last_write = 0.0


def find_nzxt():
    """Return the NZXT controller, or None."""
    from liquidctl import find_liquidctl_devices
    for dev in find_liquidctl_devices():
        if "NZXT" in dev.description:
            return dev
    return None


def read_status(dev):
    return {k: v for k, v, _ in dev.get_status()}


def read_duties(dev):
    s = read_status(dev)
    return {i: s[f"Fan {i} duty"] for i in (1, 2, 3) if f"Fan {i} duty" in s}


def read_speeds(dev):
    s = read_status(dev)
    return {i: s[f"Fan {i} speed"] for i in (1, 2, 3) if f"Fan {i} speed" in s}


def _channel_index(channel):
    """'fan2' -> 2"""
    return int("".join(c for c in channel if c.isdigit()))


def set_duty(dev, channel, duty, verify=True, tolerance=2):
    """Set a fan channel duty, spacing and verifying the write.

    Returns True if the duty was confirmed applied (or verify=False).
    Raises nothing on a dropped write - check the return value.
    """
    global _last_write
    idx = _channel_index(channel)
    duty = int(round(duty))

    for attempt in range(1, MAX_RETRIES + 1):
        gap = WRITE_GAP - (time.monotonic() - _last_write)
        if gap > 0:
            time.sleep(gap)

        dev.set_fixed_speed(channel, duty)
        _last_write = time.monotonic()

        if not verify:
            return True

        time.sleep(VERIFY_DELAY)
        actual = read_duties(dev).get(idx)
        if actual is not None and abs(actual - duty) <= tolerance:
            return True

    return False


def set_many(dev, targets, verify=True):
    """Apply {channel: duty} safely. Returns {channel: bool applied}."""
    return {ch: set_duty(dev, ch, duty, verify=verify)
            for ch, duty in targets.items()}
