"""User trim on the fan curves, shared by both daemons.

A trim shifts a tuned curve by a few duty points without editing it. The
curves were derived from measured thermal data, so this deliberately cannot
reshape them - only nudge, and only within TRIM_LIMIT.

The pump is NOT trimmable. It is a fixed duty chosen for durability and it
already has its own clamp; letting a UI slider move it toward cavitation
territory is exactly the failure this design is meant to prevent.

Read fresh on every poll, so a change takes effect within one poll interval
with no daemon restart.
"""
import json
import pathlib

from app_paths import DATA

TRIM_FILE = DATA / "fan_tuning.json"
TRIM_LIMIT = 15.0          # duty points, either direction
TRIM_KEYS = ("fan1", "fan2", "fan3", "rad")

# Cooling profile. AGGRESSIVE is the default and is what the measured tuning
# produced: cooling first, noise not a constraint. QUIET trades temperature
# for silence by moving every knee later and lower.
#
# The PUMP is not part of this. It is a fixed duty chosen for flow and
# longevity and it is inaudible on this machine, so there is nothing to gain
# by slowing it and something real to lose.
PROFILES = ("aggressive", "quiet")
DEFAULT_PROFILE = "aggressive"


def load_profile():
    """Current profile name. Anything unrecognised falls back to the default,
    so a hand-edited or truncated file cannot leave the fans in limbo."""
    try:
        name = json.loads(TRIM_FILE.read_text()).get("profile")
    except Exception:
        return DEFAULT_PROFILE
    return name if name in PROFILES else DEFAULT_PROFILE


def save_profile(name):
    if name not in PROFILES:
        name = DEFAULT_PROFILE
    try:
        cur = json.loads(TRIM_FILE.read_text())
    except Exception:
        cur = {}
    cur["profile"] = name
    TRIM_FILE.write_text(json.dumps(cur, indent=2))
    return name


def load_trims():
    """{key: offset} with every value clamped. Missing file -> all zero."""
    out = {k: 0.0 for k in TRIM_KEYS}
    try:
        raw = json.loads(TRIM_FILE.read_text()).get("trim") or {}
    except Exception:
        return out
    for k in TRIM_KEYS:
        try:
            v = float(raw.get(k, 0.0))
        except (TypeError, ValueError):
            continue
        out[k] = max(-TRIM_LIMIT, min(TRIM_LIMIT, v))
    return out


def save_trims(trims):
    """Clamp and persist. Returns what was actually written."""
    cur = {}
    try:
        cur = json.loads(TRIM_FILE.read_text())
    except Exception:
        cur = {}
    clean = {}
    for k in TRIM_KEYS:
        try:
            v = float(trims.get(k, 0.0))
        except (TypeError, ValueError):
            v = 0.0
        clean[k] = max(-TRIM_LIMIT, min(TRIM_LIMIT, v))
    cur["trim"] = clean
    TRIM_FILE.write_text(json.dumps(cur, indent=2))
    return clean
