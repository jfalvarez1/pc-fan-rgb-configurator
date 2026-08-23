"""One-instance guard for the daemons.

    import single_instance
    if not single_instance.claim("MoboDaemon"):
        sys.exit("already running")

Why this exists: two `mobo_daemon` processes were found running at once - one
orphaned from an earlier session, one just started - both driving the same
SuperIO chip. They fought over the radiator header, which showed up in the log
as the commanded duty alternating between two values on consecutive polls,
each line written by a different process into the same file.

`schtasks /End` does not necessarily kill it: if the Task Scheduler has lost
track of the process it started, /End reports success and the orphan keeps
running. So the guard has to live in the daemon itself.

A named mutex rather than a PID file: the OS releases it when the process
dies, however it dies, so there is no stale-lock case to reason about and no
window where a crashed daemon blocks its own restart.
"""
import ctypes
import ctypes.wintypes

ERROR_ALREADY_EXISTS = 183

_held = []          # keep handles alive for the life of the process


def claim(name):
    """True if this process now owns `name`, False if something else does.

    Tries the Global namespace first so it covers every session, and falls
    back to Local, which needs no privilege - a non-elevated daemon can be
    denied Global.
    """
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.wintypes.HANDLE
    for scope in ("Global", "Local"):
        k32.SetLastError(0)
        handle = k32.CreateMutexW(None, False, f"{scope}\\HardwareControl.{name}")
        err = k32.GetLastError()
        if not handle:
            continue                    # denied this namespace; try the next
        if err == ERROR_ALREADY_EXISTS:
            k32.CloseHandle(handle)
            return False
        _held.append(handle)
        return True
    return True                         # cannot lock at all: do not block startup


if __name__ == "__main__":
    import sys
    import time
    tag = sys.argv[1] if len(sys.argv) > 1 else "SelfTest"
    print(f"claim({tag}) ->", claim(tag))
    print("second claim in the SAME process ->", claim(tag),
          "(same process already owns it, so this is the re-entrant case)")
    print("holding for 5s - run this again in another shell to see it refused")
    time.sleep(5)
