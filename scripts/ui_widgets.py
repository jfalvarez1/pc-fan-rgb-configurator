"""Rounded, modern replacements for the two widgets that date a Tk app.

    from ui_widgets import RoundButton, Slider

Tk's own Button and Scale cannot be made to look current: Button has square
corners and a 3D relief, and Scale draws a rectangular trough with a
rectangular handle. Both are drawn here on a Canvas instead, which can do
rounded corners, a filled progress track and a circular knob.

Both keep the interface of the widgets they replace, so calling code and tests
do not change:

    RoundButton   config(text=/bg=/fg=), cget("text"), bind, pack, ._bg
    Slider        from_, to, resolution, variable, command, orient, get, set

`cget` and `config` are intercepted only for the keys these widgets own; the
rest fall through to Canvas, so anything else still behaves normally.
"""
import tkinter as tk


def rounded(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle. Tk has no primitive for one, so it is a polygon
    with smoothed corners - the standard trick, and it antialiases cleanly."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """Flat button with rounded corners and a hover state."""

    def __init__(self, parent, text="", command=None, bg="#1c2130",
                 fg="#eef1f7", hover=None, border=None, font=("Segoe UI", 11),
                 radius=9, padx=12, pady=9, dot=False, dot_on=False,
                 dot_colour="#ff2d95", dot_off="#3a4256", **kw):
        self._dot = dot
        self._dot_on = dot_on
        self._dot_colour = dot_colour
        self._dot_off = dot_off
        self._text = text
        self._bg = bg
        self._fg = fg
        self._hover = hover or bg
        self._border = border or bg
        self._radius = radius
        self._font = font
        self._command = command
        h = kw.pop("height", None)
        # Width is requested from the TEXT, not left at Canvas's 378px
        # default and not pinned tiny. pack() can expand a widget past its
        # request but never shrink it below one, so 378 pushed everything
        # after the first button off the panel - and a fixed 10 clipped any
        # button that does not expand, which is how the "Lighting" tab became
        # "ghtin".
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent.cget("bg"),
                         width=kw.pop("width", self._text_width(text, font,
                                                                padx)),
                         height=h or (self._line_height(font) + pady * 2), **kw)
        self._shape = None
        self._label = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._draw(True))
        self.bind("<Leave>", lambda e: self._draw(False))
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")
        self._hot = False

    @staticmethod
    def _text_width(text, font, padx):
        try:
            import tkinter.font as tkfont
            return tkfont.Font(font=font).measure(text or "") + padx * 2
        except Exception:
            return max(40, len(text or "") * 8 + padx * 2)

    @staticmethod
    def _line_height(font):
        try:
            import tkinter.font as tkfont
            return tkfont.Font(font=font).metrics("linespace")
        except Exception:
            return 18

    def _click(self, _e=None):
        if self._command:
            self._command()

    def _draw(self, hot=None):
        if hot is not None:
            self._hot = hot
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return
        fill = self._hover if self._hot else self._bg
        self._shape = rounded(self, 1, 1, w - 1, h - 1, self._radius,
                              fill=fill, outline=self._border)
        # A status dot makes a toggle readable as a toggle. Without it an
        # "on" button and a primary action button look identical, which is
        # what a panel of solid accent-filled toggles ends up as.
        if self._dot:
            cy = h / 2
            self.create_oval(15, cy - 4, 23, cy + 4,
                             fill=self._dot_colour if self._dot_on
                             else self._dot_off, outline="")
            self._label = self.create_text(33, cy, text=self._text,
                                           fill=self._fg, font=self._font,
                                           anchor="w")
        else:
            self._label = self.create_text(w / 2, h / 2, text=self._text,
                                           fill=self._fg, font=self._font,
                                           justify="center")

    # ---- Label-compatible surface

    def config(self, **kw):
        redraw = False
        for key, attr in (("text", "_text"), ("bg", "_bg"), ("fg", "_fg"),
                          ("hover", "_hover"), ("font", "_font"),
                          ("dot_on", "_dot_on"), ("dot", "_dot"),
                          ("highlightbackground", "_border")):
            if key in kw:
                setattr(self, attr, kw.pop(key))
                redraw = True
        # Label-only geometry options. A Canvas has no padx/pady, and its
        # `width` is in pixels rather than characters, so honouring the value
        # a caller meant for a Label would produce a 12-pixel button. They are
        # absorbed instead: this widget sizes itself from its font.
        for junk in ("padx", "pady", "width", "anchor", "justify", "relief",
                     "borderwidth", "highlightthickness"):
            kw.pop(junk, None)
        if kw:
            super().config(**kw)
        if redraw:
            self._resize_to_font()
            self._draw()
        return None

    def _resize_to_font(self):
        try:
            super().configure(height=self._line_height(self._font) + 18,
                              width=self._text_width(self._text, self._font,
                                                     12))
        except Exception:
            pass

    configure = config

    def cget(self, key):
        return {"text": self._text, "bg": self._bg, "fg": self._fg,
                "highlightbackground": self._border}.get(key) \
            if key in ("text", "bg", "fg", "highlightbackground") \
            else super().cget(key)

    def __setitem__(self, key, value):
        self.config(**{key: value})

    def __getitem__(self, key):
        return self.cget(key)


class Slider(tk.Canvas):
    """Horizontal slider: rounded track, filled portion, circular knob.

    Drop-in for the tk.Scale calls this app makes - same from_/to/resolution/
    variable/command arguments, and the same get()/set().
    """

    def __init__(self, parent, from_=0, to=100, resolution=1, variable=None,
                 command=None, orient="horizontal", track="#242a37",
                 fill="#ff2d95", knob="#eef1f7", height=26, **kw):
        for junk in ("bg", "fg", "troughcolor", "highlightthickness", "bd",
                     "sliderrelief", "activebackground", "font", "showvalue",
                     "label", "length", "width"):
            kw.pop(junk, None)          # accepted and ignored, as Scale kwargs
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=parent.cget("bg"), width=kw.pop("width", 10), **kw)
        self._min = float(from_)
        self._max = float(to)
        self._step = float(resolution) or 1.0
        self._var = variable if variable is not None else tk.DoubleVar()
        self._command = command
        self._track = track
        self._fill = fill
        self._knob = knob
        self._dragging = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.configure(cursor="hand2")
        self._syncing = False
        try:
            self._var.trace_add("write", self._var_written)
        except Exception:
            pass
        self._draw()

    def _var_written(self, *_):
        """Keep the bound variable inside from_..to.

        tk.Scale did this silently, and code around here relied on it: setting
        a variable to -50 left it at -50 here, which then multiplied through
        into negative RGB. A slider that displays 0 while its variable says
        -50 is lying about its own state.
        """
        if self._syncing:
            return
        try:
            v = float(self._var.get())
        except Exception:
            return
        c = self._clamp(v)
        if abs(c - v) > 1e-9:
            self._syncing = True
            try:
                self._var.set(int(c) if isinstance(self._var, tk.IntVar) else c)
            finally:
                self._syncing = False
        self._draw()

    # ---- value

    def _clamp(self, v):
        v = max(self._min, min(self._max, v))
        if self._step:
            v = round((v - self._min) / self._step) * self._step + self._min
        return max(self._min, min(self._max, v))

    def get(self):
        return self._var.get()

    def set(self, v):
        self._var.set(self._clamp(float(v)))
        self._draw()

    def _frac(self):
        span = self._max - self._min
        if span <= 0:
            return 0.0
        try:
            v = float(self._var.get())
        except Exception:
            v = self._min
        return max(0.0, min(1.0, (v - self._min) / span))

    # ---- interaction

    def _press(self, e):
        self._dragging = True
        w = max(1, self.winfo_width())
        pad = 11
        frac = (e.x - pad) / max(1, w - pad * 2)
        val = self._clamp(self._min + frac * (self._max - self._min))
        cur = None
        try:
            cur = float(self._var.get())
        except Exception:
            pass
        if cur is None or abs(cur - val) > 1e-9:
            self._var.set(int(val) if float(val).is_integer()
                          and isinstance(self._var, tk.IntVar) else val)
            if self._command:
                self._command(val)
        self._draw()

    def _release(self, _e):
        self._dragging = False
        self._draw()

    # ---- paint

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return
        pad = 11
        mid = h / 2
        rounded(self, pad, mid - 3, w - pad, mid + 3, 3, fill=self._track,
                outline=self._track)
        x = pad + self._frac() * (w - pad * 2)
        if x > pad + 1:
            rounded(self, pad, mid - 3, x, mid + 3, 3, fill=self._fill,
                    outline=self._fill)
        r = 9 if self._dragging else 8
        self.create_oval(x - r, mid - r, x + r, mid + r, fill=self._knob,
                         outline=self._fill, width=2)
