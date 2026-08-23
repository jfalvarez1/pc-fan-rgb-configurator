"""Make sure OpenRGB is running with its SDK server.

OpenRGB must be alive for ANY lighting to work, and it must run ELEVATED or
it loses PawnIO/SMBus and the motherboard ARGB headers go dark. Rather than
require the user to remember that, everything that needs lighting calls
ensure_running() and this starts it.

It triggers the HardwareControl-OpenRGB scheduled task, which is registered to
run with highest privileges. Starting a task you own needs no elevation, so
this works from an unelevated process with NO UAC prompt.
"""
import socket
import subprocess
import time

HOST, PORT = "127.0.0.1", 6742
TASK = "HardwareControl-OpenRGB"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sdk_up(timeout=1.0):
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_running(wait=20.0, quiet=False):
    """Return True once the SDK server answers, starting OpenRGB if needed."""
    if sdk_up():
        return True
    if not quiet:
        print("[openrgb] not running - starting it via the scheduled task")
    try:
        subprocess.run(["schtasks", "/Run", "/TN", TASK],
                       capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except Exception as exc:
        if not quiet:
            print(f"[openrgb] could not start the task: {exc}")
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if sdk_up():
            if not quiet:
                print("[openrgb] up, SDK server listening")
            return True
        time.sleep(1.0)
    if not quiet:
        print(f"[openrgb] did not come up within {wait:.0f}s")
    return False


if __name__ == "__main__":
    print("SDK up" if ensure_running() else "FAILED to bring OpenRGB up")
