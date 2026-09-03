"""Where this stack keeps its shared state, whether frozen or not.

    from app_paths import DATA
    DATA / "sensors.json"

This exists because of one trap. Every module here found its files with
`pathlib.Path(__file__).parent`, which is correct for a script and wrong for a
PyInstaller build: in a frozen app `__file__` points inside the temporary
directory the bundle unpacks into, a fresh one per launch. The editor would
have written its state, its override flag and its pump config into a folder
that is deleted on exit - and the two daemons, still running as scripts, would
have gone on reading the real ones. Nothing would error. The settings would
simply stop taking effect, which is far worse than a crash.

So the data directory is decided once, here:

  * running as a script - the folder the scripts live in, as before
  * frozen - the folder holding the .exe, or its `scripts` subfolder when one
    exists, so an exe dropped beside the daemons agrees with them

LED_STUDIO_DATA overrides both, which is what the tests use to work in a
scratch directory instead of the live one.
"""
import os
import pathlib
import sys


def data_dir():
    override = os.environ.get("LED_STUDIO_DATA")
    if override:
        p = pathlib.Path(override).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    if getattr(sys, "frozen", False):
        exe = pathlib.Path(sys.executable).resolve().parent
        # SEARCH UPWARD. A PyInstaller onedir build puts the exe in its own
        # folder, so "scripts beside me" is one level too deep - the first
        # build looked in LEDStudio\scripts, did not find it, and would have
        # written every setting into the program folder while the daemons went
        # on reading the real ones. Anchor on a file that is definitely part
        # of the live data set rather than on the folder name alone.
        marks = ("pump_config.json", "fan_tuning.json", "case_layout.py")
        here = exe
        for _ in range(4):
            for cand in (here / "scripts", here):
                if cand.is_dir() and any((cand / m).exists() for m in marks):
                    return cand
            if here.parent == here:
                break
            here = here.parent
        return exe
    return pathlib.Path(__file__).resolve().parent


def bundle_dir():
    """Where read-only bundled files live - the icon, and anything else
    shipped inside the exe rather than written at runtime."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(getattr(sys, "_MEIPASS",
                                    pathlib.Path(sys.executable).parent))
    return pathlib.Path(__file__).resolve().parent.parent


DATA = data_dir()
FROZEN = bool(getattr(sys, "frozen", False))


if __name__ == "__main__":
    print(f"frozen     : {FROZEN}")
    print(f"data dir   : {DATA}")
    print(f"bundle dir : {bundle_dir()}")
    for name in ("sensors.json", "fan_state.json", "led_studio_state.json",
                 "fan_tuning.json", "pump_config.json"):
        print(f"  {name:24} {'found' if (DATA / name).exists() else 'absent'}")
