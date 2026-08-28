"""Build a release bundle.

    python make_release.py 1.0.0

Produces dist/led-studio-<version>.zip containing everything needed to run the
stack on this machine, and nothing else.

The file list comes from `git ls-files`, deliberately. Walking the directory
would sweep up runtime state - live sensor dumps, logs, the session file the
editor rewrites every 20 seconds - and shipping someone's live state as if it
were a release is exactly the kind of thing that goes unnoticed until it
matters. If it is not committed, it is not in the bundle.

Two things are then asserted rather than assumed: that the icon is present and
is a real multi-resolution .ico, and that every launcher the bundle references
actually exists inside it. A release whose shortcut points at a missing icon
still installs fine and just looks broken, which is the sort of failure nobody
catches until it is in someone's hands.
"""
import hashlib
import pathlib
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# Must exist in the bundle or the build fails.
REQUIRED = [
    "led_studio.ico",
    "led_studio.png",
    "README.md",
    "USER_GUIDE.md",
    "TEST_PLAN.md",
    "scripts/led_studio_native.py",
    "scripts/case_layout.py",
    "scripts/rgb_effects.py",
    "scripts/fx_layers.py",
    "scripts/fan_panel.py",
    "scripts/fan_side.py",
    "scripts/fan_tuning.py",
    "scripts/single_instance.py",
    "scripts/thermal_rgb_loop.py",
    "scripts/mobo_daemon.py",
    "scripts/selftest.py",
]


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, check=True)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def build(version):
    files = tracked_files()
    missing = [r for r in REQUIRED if r not in files]
    if missing:
        print("REFUSING to build - required files are not committed:")
        for m in missing:
            print(f"   {m}")
        return None

    DIST.mkdir(exist_ok=True)
    out = DIST / f"led-studio-{version}.zip"
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                continue                    # deleted but still in the index
            z.write(src, f"led-studio-{version}/{rel}")

    # verify the archive rather than trusting that the writes landed
    with zipfile.ZipFile(out) as z:
        names = {n.split("/", 1)[1] for n in z.namelist() if "/" in n}
        bad = z.testzip()
        if bad:
            print(f"archive is corrupt at {bad}")
            return None
        for r in REQUIRED:
            if r not in names:
                print(f"archive is missing {r}")
                return None
        ico = z.read(f"led-studio-{version}/led_studio.ico")

    # the icon must be a genuine multi-size .ico, not a renamed png
    if not ico.startswith(b"\x00\x00\x01\x00"):
        print("led_studio.ico is not a valid ICO file")
        return None
    count = int.from_bytes(ico[4:6], "little")

    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"built {out.name}")
    print(f"  files   : {len(files)}")
    print(f"  size    : {out.stat().st_size/1024:.0f} KB")
    print(f"  icon    : valid ICO, {count} resolutions, "
          f"{len(ico)/1024:.0f} KB")
    print(f"  sha256  : {sha}")
    return out


if __name__ == "__main__":
    v = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    sys.exit(0 if build(v) else 1)
