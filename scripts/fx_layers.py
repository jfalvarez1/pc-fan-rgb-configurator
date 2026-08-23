"""Effect layers - a movable, resizable, rotatable box that applies an effect
only to the LEDs it covers, the way SignalRGB's effect blocks work.

    from fx_layers import Layer
    lay = Layer("wave", 400, 400, 300, 200, angle=0.4)
    lay.reindex(leds)            # leds: [{"x": px, "y": px, ...}, ...]
    u, v = lay.local(x, y)       # 0..1 inside the box, outside that range if not

Geometry lives here, with no Tk in sight, so it can be tested directly - the
rotation and anchored-resize maths is the part that is easy to get subtly
wrong and hard to eyeball in a GUI.

Coordinates are CANVAS pixels, because that is what the user is dragging. The
effect itself is evaluated in the box's own local space: an LED at the box's
top-left reads (0, 0) and one at its bottom-right reads (1, 1), so a wave runs
across the box regardless of where the box sits or how it is turned. v
increases downward, matching both the canvas and the effect convention where
ny=0 is the top.
"""
import math

HANDLE_R = 7.0           # grab radius for the corner handles
ROT_ARM = 34.0           # how far the rotate handle sits above the top edge
MIN_SIZE = 26.0          # a box smaller than this cannot be grabbed reliably

BLENDS = ("normal", "add", "max")


class Layer:
    _seq = 0

    def __init__(self, effect, x, y, w, h, angle=0.0, palette=None,
                 opacity=1.0, blend="normal", name=None, speed=1.0):
        Layer._seq += 1
        self.id = Layer._seq
        self.name = name or f"Layer {Layer._seq}"
        self.effect = effect
        self.palette = palette          # None -> inherit the app's palette
        self.x, self.y = float(x), float(y)      # centre, canvas px
        self.w, self.h = float(w), float(h)
        self.angle = float(angle)                # radians, clockwise on screen
        self.opacity = float(opacity)
        self.blend = blend if blend in BLENDS else "normal"
        self.speed = float(speed)
        self.on = True
        self.t0 = 0.0
        self.members = []               # LED records covered, cached

    # ---- geometry

    def axes(self):
        """Unit vectors of the box's own x (u) and y (v) directions."""
        c, s = math.cos(self.angle), math.sin(self.angle)
        return (c, s), (-s, c)

    def corners(self):
        """TL, TR, BR, BL in canvas pixels."""
        (ux, uy), (vx, vy) = self.axes()
        hw, hh = self.w / 2.0, self.h / 2.0
        out = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            out.append((self.x + ux * hw * sx + vx * hh * sy,
                        self.y + uy * hw * sx + vy * hh * sy))
        return out

    def rot_handle(self):
        """Point above the top edge, along -v."""
        (_, _), (vx, vy) = self.axes()
        d = self.h / 2.0 + ROT_ARM
        return (self.x - vx * d, self.y - vy * d)

    def top_mid(self):
        (_, _), (vx, vy) = self.axes()
        d = self.h / 2.0
        return (self.x - vx * d, self.y - vy * d)

    def local(self, px, py):
        """Canvas point -> (u, v), 0..1 inside the box."""
        dx, dy = px - self.x, py - self.y
        c, s = math.cos(self.angle), math.sin(self.angle)
        lx = dx * c + dy * s            # rotate by -angle
        ly = -dx * s + dy * c
        return lx / self.w + 0.5, ly / self.h + 0.5

    def contains(self, px, py):
        u, v = self.local(px, py)
        return 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0

    # ---- hit testing, topmost caller decides priority

    def hit_handle(self, px, py, rad=HANDLE_R):
        """Index of the corner handle under the point, or None."""
        r2 = rad * rad
        for i, (cx, cy) in enumerate(self.corners()):
            if (cx - px) ** 2 + (cy - py) ** 2 <= r2:
                return i
        return None

    def hit_rot(self, px, py, rad=HANDLE_R + 2):
        hx, hy = self.rot_handle()
        return (hx - px) ** 2 + (hy - py) ** 2 <= rad * rad

    # ---- edits

    def move_to(self, px, py):
        self.x, self.y = float(px), float(py)

    def rotate_to(self, px, py):
        """Turn so the rotate handle points at the cursor.

        The handle sits along -v, which is (sin a, -cos a); solving for that
        pointing at the cursor gives a = atan2(dy, dx) + pi/2.
        """
        self.angle = math.atan2(py - self.y, px - self.x) + math.pi / 2.0

    def resize_from(self, handle, px, py):
        """Drag corner `handle`, keeping the OPPOSITE corner pinned.

        Done in the box's own frame so it behaves correctly at any rotation -
        resizing around the centre instead would make a rotated box appear to
        slide away from the corner being dragged.

        The box is defined by the two opposite points: the pinned corner and
        the cursor. Deriving the centre as their midpoint is what keeps the
        anchor exactly still. An earlier version took the size from the drag
        but rebuilt the centre using the DRAGGED corner's sign, which only
        agreed with the anchor while the drag stayed on one side of it - past
        that the box jumped. Signs come from the drag itself, so dragging
        through the anchor flips the box, as in any drawing tool.
        """
        fixed = self.corners()[(handle + 2) % 4]
        sx, sy = ((-1, -1), (1, -1), (1, 1), (-1, 1))[handle]
        (ux, uy), (vx, vy) = self.axes()
        dx, dy = px - fixed[0], py - fixed[1]
        du = dx * ux + dy * uy          # extent along the box's own axes
        dv = dx * vx + dy * vy
        # clamp magnitude but keep the sign, or the anchor cannot hold
        if abs(du) < MIN_SIZE:
            du = MIN_SIZE * (sx if du == 0 else (1.0 if du > 0 else -1.0))
        if abs(dv) < MIN_SIZE:
            dv = MIN_SIZE * (sy if dv == 0 else (1.0 if dv > 0 else -1.0))
        self.w, self.h = abs(du), abs(dv)
        self.x = fixed[0] + ux * du / 2.0 + vx * dv / 2.0
        self.y = fixed[1] + uy * du / 2.0 + vy * dv / 2.0

    def nudge(self, dx, dy):
        self.x += dx
        self.y += dy

    # ---- membership

    def reindex(self, leds):
        """Cache the LED records this box covers.

        Recomputed only when the box changes, not per frame: the render loop
        then touches just the covered LEDs instead of testing all of them
        against every layer.
        """
        self.members = [r for r in leds
                        if r.get("item") is not None
                        and self.contains(r["x"], r["y"])]
        return self.members

    # ---- colour

    def apply(self, base, col):
        """Blend this layer's colour over what is underneath."""
        a = max(0.0, min(1.0, self.opacity))
        if self.blend == "add":
            return tuple(min(255, int(base[i] + col[i] * a)) for i in range(3))
        if self.blend == "max":
            return tuple(max(base[i], int(col[i] * a)) for i in range(3))
        return tuple(int(base[i] * (1.0 - a) + col[i] * a) for i in range(3))

    def cycle_blend(self):
        self.blend = BLENDS[(BLENDS.index(self.blend) + 1) % len(BLENDS)]
        return self.blend


if __name__ == "__main__":
    # geometry self-test: the parts that are easy to get wrong
    ok = True

    def chk(name, cond):
        global ok
        ok = ok and cond
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")

    L = Layer("wave", 100, 100, 200, 100)
    chk("centre is (0.5, 0.5)", L.local(100, 100) == (0.5, 0.5))
    chk("top-left corner is (0,0)",
        all(abs(a - b) < 1e-9 for a, b in zip(L.local(0, 50), (0.0, 0.0))))
    chk("bottom-right is (1,1)",
        all(abs(a - b) < 1e-9 for a, b in zip(L.local(200, 150), (1.0, 1.0))))
    chk("contains centre", L.contains(100, 100))
    chk("excludes outside", not L.contains(400, 100))
    chk("v increases downward", L.local(100, 140)[1] > L.local(100, 60)[1])

    # rotation: handle points at the cursor
    for deg in (0, 30, 90, 200, 359):
        R = Layer("wave", 100, 100, 80, 60)
        tx = 100 + 50 * math.cos(math.radians(deg))
        ty = 100 + 50 * math.sin(math.radians(deg))
        R.rotate_to(tx, ty)
        hx, hy = R.rot_handle()
        got = math.degrees(math.atan2(hy - 100, hx - 100)) % 360
        chk(f"rotate handle tracks cursor at {deg} deg",
            abs((got - deg + 180) % 360 - 180) < 1e-6)

    # Anchored resize. The target must be chosen PER HANDLE, out along that
    # corner's own diagonal - a single fixed target flips the box for most
    # handles, and then expecting corner `hnd` to be the one under the cursor
    # is simply the wrong assertion.
    SIGNS = ((-1, -1), (1, -1), (1, 1), (-1, 1))
    for ang in (0.0, 0.5, 1.2, 3.0):
        for hnd in range(4):
            S = Layer("wave", 300, 300, 160, 120, angle=ang)
            fixed_before = S.corners()[(hnd + 2) % 4]
            (ux, uy), (vx, vy) = S.axes()
            sx, sy = SIGNS[hnd]
            tx = fixed_before[0] + ux * 200 * sx + vx * 150 * sy
            ty = fixed_before[1] + uy * 200 * sx + vy * 150 * sy
            S.resize_from(hnd, tx, ty)
            d = math.dist(fixed_before, S.corners()[(hnd + 2) % 4])
            chk(f"anchor holds (angle={ang:.1f}, handle={hnd}) drift={d:.1e}",
                d < 1e-9)
            chk(f"  dragged corner on cursor (angle={ang:.1f} h={hnd})",
                math.dist(S.corners()[hnd], (tx, ty)) < 1e-9)
            chk(f"  size follows the drag (angle={ang:.1f} h={hnd})",
                abs(S.w - 200) < 1e-9 and abs(S.h - 150) < 1e-9)

    # Dragging through the anchor flips the box. The anchor POINT stays put,
    # but it becomes a different corner INDEX - so the invariant to assert is
    # that it is still one of the corners, not that it is still corners()[2].
    for ang in (0.0, 0.9):
        F = Layer("wave", 300, 300, 160, 120, angle=ang)
        before = F.corners()[2]
        F.resize_from(0, before[0] + 120, before[1] + 90)
        near = min(math.dist(before, c) for c in F.corners())
        chk(f"anchor still a corner after a flip (angle={ang:.1f})",
            near < 1e-9)

    # minimum size is respected, and the anchor survives the clamp
    T = Layer("wave", 100, 100, 200, 200)
    anchor = T.corners()[2]
    T.resize_from(0, anchor[0], anchor[1])
    chk("min size enforced", T.w >= MIN_SIZE and T.h >= MIN_SIZE)
    chk("anchor holds when clamped",
        math.dist(anchor, T.corners()[2]) < 1e-9)

    # membership
    leds = [{"x": x, "y": 100, "item": 1} for x in range(0, 300, 25)]
    M = Layer("wave", 100, 100, 100, 50)
    M.reindex(leds)
    chk("membership matches contains",
        all(M.contains(r["x"], r["y"]) for r in M.members)
        and len(M.members) == sum(1 for r in leds
                                  if M.contains(r["x"], r["y"])))
    leds.append({"x": 100, "y": 100, "item": None})
    M.reindex(leds)
    chk("gap slots excluded", all(r["item"] is not None for r in M.members))

    # blending
    B = Layer("wave", 0, 0, 10, 10, opacity=0.5)
    chk("normal blend", B.apply((0, 0, 0), (200, 100, 50)) == (100, 50, 25))
    B.blend = "add"
    chk("add blend", B.apply((100, 0, 0), (200, 100, 50)) == (200, 50, 25))
    B.blend = "max"
    chk("max blend", B.apply((150, 0, 0), (200, 100, 50)) == (150, 50, 25))

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
