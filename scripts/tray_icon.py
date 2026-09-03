"""System tray icon, so closing the window does not stop the lighting.

    tray = TrayIcon(root, on_open=..., on_quit=..., icon=Path(...))
    tray.start()

The window's X button hides to the notification area instead of exiting. The
process keeps running, keeps the override flag, and keeps writing frames to
the hardware - the case carries on animating with no window at all. Clicking
the tray icon brings the editor back.

This replaces the hand-off-to-a-player dance for the ordinary case. That
still exists for a real quit, but it was only ever a workaround for the app
being unable to stay alive without a window.

Two things make this fiddly, and both are handled here rather than at the
call site:

  * pystray runs its own Win32 message loop on a background thread. Tk is not
    thread-safe, so nothing from a menu callback may touch a widget directly;
    everything is marshalled back with `root.after(0, ...)`.
  * a withdrawn Tk window will not always come back with `deiconify()` alone,
    and when it does it can come back behind everything else. Restoring
    therefore lifts and focuses as well.
"""
import threading


class TrayIcon:
    def __init__(self, root, on_open, on_quit, icon=None, title="LED Studio"):
        self.root = root
        self.on_open = on_open
        self.on_quit = on_quit
        self.title = title
        self.icon_path = icon
        self._icon = None
        self._thread = None
        self.available = False
        try:
            import pystray                                     # noqa: F401
            from PIL import Image                              # noqa: F401
            self.available = True
        except Exception:
            # No tray library: the caller falls back to closing normally.
            # A missing icon must never mean an app you cannot exit.
            self.available = False

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self.available or self._icon is not None:
            return False
        try:
            import pystray
            from PIL import Image

            img = None
            if self.icon_path:
                try:
                    img = Image.open(self.icon_path)
                    img.load()
                    img = img.convert("RGBA")
                except Exception:
                    img = None
            if img is None:
                img = Image.new("RGBA", (32, 32), (255, 45, 149, 255))

            menu = pystray.Menu(
                pystray.MenuItem("Open LED Studio", self._open, default=True),
                pystray.MenuItem("Quit", self._quit),
            )
            self._icon = pystray.Icon("LEDStudio", img, self.title, menu)
            # daemon: a tray thread must never be the reason the process
            # refuses to exit.
            self._thread = threading.Thread(target=self._icon.run,
                                            daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._icon = None
            self.available = False
            return False

    def stop(self):
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    # -- callbacks (background thread - marshal everything) ----------------

    def _open(self, icon=None, item=None):
        self._call(self.on_open)

    def _quit(self, icon=None, item=None):
        self._call(self.on_quit)

    def _call(self, fn):
        try:
            self.root.after(0, fn)
        except Exception:
            pass        # interpreter already gone; nothing to do

    # -- window helpers ----------------------------------------------------

    def hide(self):
        """Send the window to the tray."""
        try:
            self.root.withdraw()
        except Exception:
            pass

    def show(self):
        """Bring it back, in front, focused."""
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
            # A brief topmost flick is the reliable way to jump the
            # foreground-lock rules from a background-thread request; leaving
            # it set would pin the window over everything, so it is undone
            # immediately.
            self.root.attributes("-topmost", True)
            self.root.after(150,
                            lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass


if __name__ == "__main__":
    import pathlib
    import tkinter as tk

    root = tk.Tk()
    root.title("LED Studio")
    tk.Label(root, text="close me - I should go to the tray",
             padx=40, pady=40).pack()
    ico = pathlib.Path(__file__).resolve().parent.parent / "led_studio.ico"
    tray = TrayIcon(root, on_open=lambda: tray.show(),
                    on_quit=lambda: (tray.stop(), root.destroy()),
                    icon=ico if ico.exists() else None)
    print("tray available:", tray.available, "started:", tray.start())
    root.protocol("WM_DELETE_WINDOW", tray.hide)
    root.mainloop()
