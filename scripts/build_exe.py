"""Build LED Studio as a standalone Windows executable.

    python build_exe.py

Produces C:\\HardwareControl\\LEDStudio.exe with the app icon compiled in, so
Windows shows it in Explorer, the taskbar and Alt-Tab without a shortcut
having to supply one.

ONEDIR, not onefile. A onefile build unpacks itself into a fresh temporary
directory on every launch, which costs seconds of startup for an app that runs
at logon, and puts `sys._MEIPASS` somewhere different each time. This app
shares JSON state with two daemons, so a stable location matters more than a
single tidy file. The exe sits at the top of C:\\HardwareControl and its
support files live in `_internal` beside it.

The data directory is NOT inside the bundle - see app_paths. The exe finds
`scripts\\` next to itself and reads and writes the same sensors.json,
fan_tuning.json and led_studio_state.json the daemons use. Get that wrong and
the app writes settings into a folder that is deleted when it exits, with no
error and no effect.
"""
import shutil
import subprocess
import sys
import time
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ICON = ROOT / "led_studio.ico"
NAME = "LEDStudio"

# Imported dynamically or only inside functions, so PyInstaller's static
# analysis does not see them.
HIDDEN = [
    "openrgb", "openrgb.utils", "openrgb.orgb", "openrgb.network",
    "PIL.ImageTk", "PIL._tkinter_finder",
    "psutil", "numpy", "soundcard",
    "led_player", "usage_levels", "audio_levels", "fan_side", "fan_panel",
    "fx_layers", "ui_widgets", "led_render", "case_layout", "rgb_effects",
    "fan_tuning", "single_instance", "app_paths", "openrgb_boot",
    "thermal_rgb_loop", "mobo_daemon",
]

EXCLUDE = ["pytest", "setuptools", "pip", "matplotlib", "pandas"]


def stop_running():
    """Close any running copy so the folder can actually be replaced.

    Building over a running app is the normal case - you launch it to check
    something, then rebuild - so this is handled rather than treated as user
    error.
    """
    try:
        import psutil
    except ImportError:
        return
    killed = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == f"{NAME.lower()}.exe":
                p.terminate()
                killed.append(p.info["pid"])
        except Exception:
            pass
    if killed:
        print(f"closed running {NAME}: {killed}")
        time.sleep(2)


def main():
    if not ICON.exists():
        print(f"icon missing: {ICON}")
        return 1
    stop_running()
    work = ROOT / "_build"
    dist = ROOT / "_dist"
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--windowed",                 # no console window behind the GUI
           "--onedir",
           f"--name={NAME}",
           f"--icon={ICON}",
           f"--distpath={dist}",
           f"--workpath={work}",
           f"--specpath={work}",
           f"--paths={SCRIPTS}",
           # the icon is read at runtime for the window and Start menu too
           f"--add-data={ICON}{';'}.",
           ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]
    cmd.append(str(SCRIPTS / "led_studio_native.py"))

    print("building - this takes a minute\n")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("\nPyInstaller failed")
        return r.returncode

    built = dist / NAME
    target = ROOT / NAME
    if target.exists():
        # NOT ignore_errors. With the app running, Windows locks the exe and
        # the DLLs beside it, the delete quietly fails, and shutil.move then
        # moves the new build INSIDE the old folder - LEDStudio\LEDStudio.
        # The build still prints "built ...\LEDStudio\LEDStudio.exe" and that
        # path still exists, because it is the stale one. Everything after
        # that is testing the previous build.
        try:
            shutil.rmtree(target)
        except OSError as exc:
            print(f"\ncannot replace {target}: {exc}")
            print("  the app is probably still running - close LED Studio "
                  "(and any player) and build again")
            return 1
    shutil.move(str(built), str(target))
    exe = target / f"{NAME}.exe"

    # Assert what was actually produced rather than reporting the path that
    # was asked for.
    if not exe.is_file():
        print(f"\nexpected {exe} and it is not there")
        return 1
    stray = [p for p in target.iterdir() if p.is_dir() and p.name == NAME]
    if stray:
        print(f"\nnested build detected: {stray[0]} - the old folder was not "
              f"replaced")
        return 1
    ico = target / "_internal" / ICON.name
    if not ico.is_file():
        print(f"\nthe icon did not make it into the bundle: {ico}")
        return 1
    age = time.time() - exe.stat().st_mtime
    if age > 300:
        print(f"\n{exe.name} is {age/60:.0f} minutes old - this is not a "
              f"fresh build")
        return 1

    print(f"\nbuilt {exe}")
    print(f"  size {exe.stat().st_size/1024/1024:.1f} MB")
    print(f"  icon compiled in from {ICON.name}, and bundled at "
          f"_internal/{ICON.name}")
    print(f"  fresh: written {age:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
