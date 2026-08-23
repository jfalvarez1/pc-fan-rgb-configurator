"""Signal the RGB daemon that a Claude Code session is working.

Called by Claude Code hooks:
    python claude_signal.py on     # UserPromptSubmit
    python claude_signal.py off    # Stop / SessionEnd

PER-SESSION FLAGS
-----------------
Each session gets its OWN flag file under claude_flags/, keyed by the
session_id Claude Code passes on stdin. A single shared flag was wrong: with
two sessions open, whichever finished FIRST deleted the flag while the other
was still working, so the glow died early (or never appeared).

The daemon glows while ANY session's flag is present, so N concurrent Claudes
compose correctly. Sessions without a usable id share the "default" slot,
which still behaves like the old single-flag scheme rather than failing.
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
FLAG_DIR = os.path.join(BASE, "claude_flags")
STALE_SECONDS = 900     # sweep flags abandoned by a crashed session


def session_id():
    """Read session_id from the hook's stdin JSON. Falls back to 'default'."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return "default"
        raw = sys.stdin.read()
        if not raw.strip():
            return "default"
        sid = json.loads(raw).get("session_id")
        if not sid:
            return "default"
        # keep it filesystem-safe
        return "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:64] \
            or "default"
    except Exception:
        return "default"


def sweep():
    """Remove flags from sessions that died without firing their Stop hook."""
    try:
        now = time.time()
        for name in os.listdir(FLAG_DIR):
            path = os.path.join(FLAG_DIR, name)
            try:
                if now - os.path.getmtime(path) > STALE_SECONDS:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "on").lower()
    try:
        os.makedirs(FLAG_DIR, exist_ok=True)

        # Only on/off consume stdin. "status" must NOT read it: run from a
        # terminal with no piped input, the read blocks forever.
        if action in ("on", "off"):
            path = os.path.join(FLAG_DIR, f"{session_id()}.flag")

        if action == "on":
            with open(path, "w") as fh:
                fh.write(str(os.getpid()))
            sweep()
        elif action == "off":
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        elif action == "status":
            try:
                active = [f for f in os.listdir(FLAG_DIR) if f.endswith(".flag")]
            except OSError:
                active = []
            print(f"{len(active)} session(s) working: {active}" if active
                  else "idle")
    except Exception:
        pass          # never break the hook, whatever happens
    return 0


if __name__ == "__main__":
    sys.exit(main())
