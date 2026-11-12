#!/usr/bin/env python3
"""Redraw ACNH's button icons for other controllers (Xbox, PlayStation, keyboard), from the player's own RomFS.

    make_button_glyphs.py [--romfs private/acnh/extract/romfs] [--out private/acnh/fonts] [--preview PNG]
    make_button_glyphs.py --selftest

Where the icons come from: every button prompt in ACNH (dialogs, menus, the key guide at the bottom right) is a
private-use character drawn with ParkExt.bfttf, the game's own extension font in Font/ScalableFont.sarc.zs. All 21
combined fonts in Font/Fcpx.sarc.zs list ParkExt first, for E0A0-E153 and E200-E27F; the console's shared
extension font (pl type 5) is not in any of them. Messages carry the characters themselves (Dialog_*: E0A0
"A" etc.), and the key guide's labels get theirs from String_*/STR_KeyIcon.msbt (001 = E0E0 A ... 029 = E236).

So each style is a copy of ScalableFont.sarc.zs whose ParkExt has those glyphs replaced, drawn here as plain vector
shapes in the box of the glyph they replace (same advance, baseline and size). The letters inside the icons come
from the game's own label font (*P_Seurat-B). Output, one archive per style and face-button layout (input.layout):

    <out>/glyphs_<style>/<layout>/romfs/Font/ScalableFont.sarc.zs   (+ ParkExt.ttf next to romfs/, for inspection)

with style xbox or playstation, and switch for the xbox layout only (the original glyphs, A and B swapped);
switch with the console layout is the RomFS itself. The runtime serves the chosen archive in place of the RomFS
one (runtime/src/glyphs.cpp). Everything written embeds game data: it stays in private/ and is never committed.

The keyboard style (keycaps with the bound key's label) is the one exception to "one archive per layout": the
face-button layout only moves pad buttons, and the keys are the same either way, so it has no <layout> directory.
Because the keys can be remapped in the settings screen, its archive carries a keycap glyph for every label of the
catalog (one set shared by every prompt of the same shape) plus a small manifest (<out>/glyphs_keyboard/patch.txt),
and the runtime rewrites the font's cmap at boot to the labels of the keys actually bound
(runtime/src/glyphs_keys.cpp). The archive at <out>/glyphs_keyboard/romfs/ is the fallback (fresh bindings), kept so
the game always has valid icons.

The fonts are single-colour: the game tints glyphs with the text colour, so an Xbox icon is told apart by its
letter, not its colour. Requires fontTools and zstandard (Pillow for --preview). --selftest builds a synthetic
font in memory and checks the cmap patching the runtime does, so it needs no game data (CI runs it).

The Linux package carries this script frozen as glyphs-gen (tools/package_glyphs_gen.sh): a game folder prepared
by ACNHRecomp-Importer has no fonts/, so on the first start the player runs glyphs-gen --styles keyboard on that
folder's own RomFS and writes fonts/ into it (runtime/src/glyphs_keys.cpp). Run it the same way by hand:
glyphs-gen --romfs <game folder>/romfs --out <game folder>/fonts --styles keyboard.
"""
import argparse
import copy
import io
import math
import os
import struct
import sys

import zstandard
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.reverseContourPen import ReverseContourPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.DefaultTable import DefaultTable

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ARCHIVE = "Font/ScalableFont.sarc.zs"
EXT_FONT = "ParkExt.bfttf"
LABEL_FONT_SUFFIX = "P_Seurat-B.bfttf"  # the game's label font, by the end of its name
BFTTF_MAGIC = 0x18029A7F  # the decoded first word; the stored one is this XOR the key
K = 0.5522847498  # cubic Bezier circle constant

# Which physical button presses each of the game's face buttons, per input.layout. This follows sample_input in
# runtime/src/platform_sdl.cpp: the xbox layout swaps A and B only (confirm on the bottom), X and Y keep their
# Switch positions. Change it here if the runtime's mapping changes.
LAYOUTS = {
    "console": {"A": "east", "B": "south", "X": "north", "Y": "west"},
    "xbox": {"A": "south", "B": "east", "X": "north", "Y": "west"},
}
# What each controller prints on its face buttons, by position.
FACE_LABELS = {
    "switch": {"east": "A", "south": "B", "north": "X", "west": "Y"},
    "xbox": {"south": "A", "east": "B", "west": "X", "north": "Y"},
    "playstation": {"south": "cross", "east": "circle", "west": "square", "north": "triangle"},
}
# The other buttons do not move with the layout: SDL's shoulders, triggers, Start and Back (platform_sdl.cpp).
OTHER_LABELS = {
    "xbox": {"L": "LB", "R": "RB", "ZL": "LT", "ZR": "RT", "plus": "menu", "minus": "view"},
    "playstation": {"L": "L1", "R": "R1", "ZL": "L2", "ZR": "R2", "plus": "options", "minus": "create"},
}

# The keyboard style: the label each game action shows with a fresh binding. A remap only changes which key runs
# the action, and the runtime patches the font's cmap to that key's label at boot (runtime/src/glyphs_keys.cpp),
# so every label below is drawn for every prompt. These defaults mirror kKeyNames in runtime/src/platform_sdl.cpp,
# and the names in the catalog mirror key_label() in runtime/src/glyphs_keys.cpp: keep them in step.
KEYBOARD_DEFAULTS = {"a": "SPC", "b": "SHF", "x": "X", "y": "F", "l": "Q", "r": "E", "zl": "Z", "zr": "C",
                     "plus": "ENT", "minus": "ESC"}
# Every keycap label the prompts are drawn for; "KEY" is the runtime's fallback for a key it has no name for.
LABELS = (list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + list("0123456789") +
          ["SPC", "SHF", "ENT", "ESC", "TAB", "BSP", "CTL", "ALT", "UP", "DN", "LT", "RT",
           "PGU", "PGD", "HOM", "END", "INS", "DEL"] +
          ["F%d" % n for n in range(1, 13)] + ["K%d" % n for n in range(10)] + ["KEY"])

# ParkExt code points by the game button they show; "fill" is the solid set (E0Ex), "line" the outlined one (E0Ax).
FACE = {"A": (0xE0E0, 0xE0A0), "B": (0xE0E1, 0xE0A1), "X": (0xE0E2, 0xE0A2), "Y": (0xE0E3, 0xE0A3)}
SHOULDER = {"L": (0xE0E4, 0xE0A4), "R": (0xE0E5, 0xE0A5), "ZL": (0xE0E6, 0xE0A6), "ZR": (0xE0E7, 0xE0A7)}
# + and -: in a disc (filled, outlined) and bare (filled, outlined).
SYSTEM = {"plus": [(0xE0EF, "disc"), (0xE0B3, "ring"), (0xE0F1, "bare"), (0xE0B5, "bare")],
          "minus": [(0xE0F0, "disc"), (0xE0B4, "ring"), (0xE0F2, "bare"), (0xE0B6, "bare")]}
# The D-pad. ACNH draws it as the left Joy-Con's four round direction buttons: STR_KeyIcon 013-016 are E0AB-E0AE
# (right, down, up, left lit) and 021/026 E0EA (all of them); Videogame.msbt pairs E0CB (left/right) with the
# stick's left/right. A pad has a cross instead, so these become crosses: (style, lit arms, arrows).
#   style "line": outlined cross, "fill": solid cross; lit arms are filled in a line cross; arrows are drawn on
#   (line) or cut out of (fill) the arms they name.
DPAD = {
    0xE0AA: ("line", "", ""), 0xE0AB: ("line", "r", ""), 0xE0AC: ("line", "d", ""), 0xE0AD: ("line", "u", ""),
    0xE0AE: ("line", "l", ""), 0xE0EA: ("fill", "", ""),
    0xE0CA: ("line", "", "rd"), 0xE0CB: ("line", "", "lr"), 0xE0CC: ("line", "", "ur"),
    0xE0CD: ("line", "", "ld"), 0xE0CE: ("line", "", "ud"), 0xE0CF: ("line", "", "ul"),
    0xE23F: ("fill", "", "udlr"), 0xE240: ("line", "r", "r"), 0xE241: ("line", "d", "d"),
    0xE242: ("line", "u", "u"), 0xE243: ("line", "l", "l"),
    0xE0EB: ("fill", "", "u"), 0xE0EC: ("fill", "", "d"), 0xE0ED: ("fill", "", "l"), 0xE0EE: ("fill", "", "r"),
    0xE0AF: ("line", "", "u"), 0xE0B0: ("line", "", "d"), 0xE0B1: ("line", "", "l"), 0xE0B2: ("line", "", "r"),
}
DIR_ANGLE = {"r": 0, "u": 90, "l": 180, "d": 270}


# ---- archives ---------------------------------------------------------------------------------------------------

def read_sarc(path):
    """(header bytes up to the data, [(node offset, name, data)], alignment). ACNH's SARCs are little-endian."""
    raw = zstandard.ZstdDecompressor().decompress(open(path, "rb").read(), max_output_size=1 << 31)
    if raw[:4] != b"SARC" or raw[6:8] != b"\xff\xfe":
        sys.exit(f"{path}: not a little-endian SARC")
    data_off = struct.unpack_from("<I", raw, 0xC)[0]
    n = struct.unpack_from("<H", raw, 0x1A)[0]
    names = 0x20 + n * 0x10 + 8
    members, align = [], data_off
    for k in range(n):
        node = 0x20 + k * 0x10
        _, attr, start, end = struct.unpack_from("<IIII", raw, node)
        name = raw[names + (attr & 0xFFFF) * 4:].split(b"\0", 1)[0].decode()
        members.append((node, name, raw[data_off + start:data_off + end]))
        if start:
            align = math.gcd(align, start)
    return raw[:data_off], members, align


def sarc_parts(head, members, align):
    """(header, body, {name: body offset}) of the archive: nodes and their order kept, data re-laid out at the
    same alignment. The member data of name starts at len(header) + offset, so the absolute file offset is known."""
    head = bytearray(head)
    body, pos, starts = bytearray(), 0, {}
    for node, name, data in members:
        pos = (pos + align - 1) // align * align
        body += b"\0" * (pos - len(body))
        struct.pack_into("<II", head, node + 8, pos, pos + len(data))
        starts[name] = pos
        body += data
        pos += len(data)
    struct.pack_into("<I", head, 0x8, len(head) + len(body))
    return bytes(head), bytes(body), starts


def write_sarc(head, members, align, path):
    """The same archive with new member data: nodes and their order kept, data re-laid out at the same alignment."""
    head, body, _ = sarc_parts(head, members, align)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(zstandard.ZstdCompressor(level=9, threads=-1).compress(head + body))
    os.replace(tmp, path)


def bfttf_decode(b):
    """BFTTF: {magic, size} then the font, all as little-endian words XORed with a key (magic ^ 0x18029A7F); the
    size word holds the big-endian size."""
    key = struct.unpack_from("<I", b, 0)[0] ^ BFTTF_MAGIC
    size = struct.unpack(">I", struct.pack("<I", struct.unpack_from("<I", b, 4)[0] ^ key))[0]
    words = struct.unpack_from("<%dI" % ((len(b) - 8) // 4), b, 8)
    return struct.pack("<%dI" % len(words), *(w ^ key for w in words))[:size], key


def bfttf_encode(ttf, key):
    padded = ttf + b"\0" * (-len(ttf) % 4)
    words = struct.unpack("<%dI" % (len(padded) // 4), padded)
    size_word = struct.unpack("<I", struct.pack(">I", len(ttf)))[0]
    return struct.pack("<II", BFTTF_MAGIC ^ key, size_word ^ key) + struct.pack(
        "<%dI" % len(words), *(w ^ key for w in words))


# ---- drawing ----------------------------------------------------------------------------------------------------
# TrueType fills by non-zero winding: clockwise contours fill, counter-clockwise ones cut. Cut-outs never overlap
# each other (two overlapping holes would wind to -1 and fill again), so every symbol is drawn as disjoint pieces.

class Pen:
    def __init__(self):
        self.tt = TTGlyphPen(None)
        self.pen = Cu2QuPen(self.tt, max_err=1.0, reverse_direction=False)

    def _p(self, hole):
        return ReverseContourPen(self.pen) if hole else self.pen

    def circle(self, cx, cy, r, hole=False):
        p, k = self._p(hole), K * r
        p.moveTo((cx + r, cy))
        p.curveTo((cx + r, cy - k), (cx + k, cy - r), (cx, cy - r))
        p.curveTo((cx - k, cy - r), (cx - r, cy - k), (cx - r, cy))
        p.curveTo((cx - r, cy + k), (cx - k, cy + r), (cx, cy + r))
        p.curveTo((cx + k, cy + r), (cx + r, cy + k), (cx + r, cy))
        p.closePath()

    def rrect(self, x0, y0, x1, y1, radii, hole=False):
        """radii: one value, or (top left, top right, bottom right, bottom left)."""
        if not isinstance(radii, (tuple, list)):
            radii = (radii,) * 4
        lim = min(x1 - x0, y1 - y0) / 2
        tl, tr, br, bl = (max(0.0, min(r, lim)) for r in radii)
        p = self._p(hole)
        p.moveTo((x0, y0 + bl))
        p.lineTo((x0, y1 - tl))
        if tl:
            p.curveTo((x0, y1 - tl + K * tl), (x0 + tl - K * tl, y1), (x0 + tl, y1))
        p.lineTo((x1 - tr, y1))
        if tr:
            p.curveTo((x1 - tr + K * tr, y1), (x1, y1 - tr + K * tr), (x1, y1 - tr))
        p.lineTo((x1, y0 + br))
        if br:
            p.curveTo((x1, y0 + br - K * br), (x1 - br + K * br, y0), (x1 - br, y0))
        p.lineTo((x0 + bl, y0))
        if bl:
            p.curveTo((x0 + bl - K * bl, y0), (x0, y0 + bl - K * bl), (x0, y0 + bl))
        p.closePath()

    def poly(self, pts, hole=False):
        """pts clockwise (fills); hole=True cuts the same shape out."""
        p = self._p(hole)
        p.moveTo(pts[0])
        for q in pts[1:]:
            p.lineTo(q)
        p.closePath()

    def glyph(self):
        return self.tt.glyph()


def rot(pts, deg, cx, cy):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]


def plus_pts(a, t):
    """A plus sign around (0, 0): arm reach a, half thickness t; clockwise."""
    return [(-t, a), (t, a), (t, t), (a, t), (a, -t), (t, -t), (t, -a), (-t, -a), (-t, -t), (-a, -t), (-a, t),
            (-t, t)]


class Labels:
    """Letters for the icons, from the game's own font (scaled to a cap height, centred on a point)."""

    def __init__(self, font):
        self.font, self.cmap, self.gs = font, font.getBestCmap(), font.getGlyphSet()
        bp = BoundsPen(self.gs)
        self.gs[self.cmap[ord("H")]].draw(bp)
        self.cap = bp.bounds[3]

    def width(self, s, cap_h):
        k = cap_h / self.cap
        return k * (sum(self.font["hmtx"][self.cmap[ord(c)]][0] for c in s) - 0.04 * self.cap * (len(s) - 1))

    def has(self, s):
        """Whether the label font can draw every character of s (a glyph missing from it is not drawn)."""
        return all(ord(c) in self.cmap for c in s)

    def draw(self, pen, s, cx, cy, cap_h, hole):
        k = cap_h / self.cap
        x = -self.width(s, cap_h) / (2 * k)
        for c in s:
            name = self.cmap[ord(c)]
            target = pen._p(hole)
            self.gs[name].draw(TransformPen(target, (k, 0, 0, k, cx + x * k, cy - cap_h / 2)))
            x += self.font["hmtx"][name][0] - 0.04 * self.cap


class Painter:
    def __init__(self, labels):
        self.labels = labels

    # A box is (x0, y0, x1, y1): the replaced glyph's own bounds, so the new icon sits where the old one did.

    def face(self, box, label, solid):
        """A round face button: a letter (Xbox) or a PlayStation symbol, cut out of a disc or inside a ring."""
        x0, y0, x1, y1 = box
        cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, min(x1 - x0, y1 - y0) / 2
        pen, t = Pen(), 0.17 * r
        pen.circle(cx, cy, r)
        if not solid:
            pen.circle(cx, cy, r - t, hole=True)
        cut = solid  # the symbol is a hole in a disc, ink in a ring
        if label == "cross":
            pen.poly(rot(plus_pts(0.56 * r, 0.11 * r), 45, cx, cy), hole=cut)
        elif label == "circle":
            pen.circle(cx, cy, 0.48 * r, hole=cut)
            pen.circle(cx, cy, 0.48 * r - 0.2 * r, hole=not cut)
        elif label == "square":
            a, w = 0.44 * r, 0.19 * r
            pen.rrect(cx - a, cy - a, cx + a, cy + a, 0.06 * r, hole=cut)
            pen.rrect(cx - a + w, cy - a + w, cx + a - w, cy + a - w, 0, hole=not cut)
        elif label == "triangle":
            def tri(rr):  # clockwise, pointing up, centred on its centroid (a touch below cy so it looks centred)
                return [(cx + rr * math.cos(math.radians(a)), cy - 0.04 * r + rr * math.sin(math.radians(a)))
                        for a in (90, -30, 210)]
            pen.poly(tri(0.62 * r), hole=cut)
            pen.poly(tri(0.62 * r - 0.19 * r * 2), hole=not cut)  # inset by the stroke (inradius = R/2)
        else:
            self.labels.draw(pen, label, cx, cy, 1.05 * r, hole=cut)
        return pen.glyph()

    def key(self, box, label, solid):
        """A keycap: the key's label cut out of a filled cap, or drawn inside an outlined one."""
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r, t = 0.20 * min(w, h), 0.075 * min(w, h)
        pen = Pen()
        pen.rrect(x0, y0, x1, y1, r)
        if not solid:
            pen.rrect(x0 + t, y0 + t, x1 - t, y1 - t, max(0.0, r - t), hole=True)
        if label:
            pad = t + 0.06 * min(w, h)
            cap = min(0.58 * (h - 2 * t), 0.86 * (w - 2 * pad) / max(1e-6, self.labels.width(label, 1.0)))
            if cap > 0:
                self.labels.draw(pen, label, cx, cy, cap, hole=solid)
        return pen.glyph()

    def shoulder(self, box, label, trigger, solid):
        """LB/RB/L1/R1 as a rounded bar; LT/RT/L2/R2 as a trigger (round on top, square below), label inside."""
        x0, y0, x1, y1 = box
        h = y1 - y0
        radii = (0.42 * h, 0.42 * h, 0.08 * h, 0.08 * h) if trigger else 0.22 * h
        pen, t = Pen(), 0.085 * h
        pen.rrect(x0, y0, x1, y1, radii)
        if not solid:
            inner = tuple(max(0.0, r - t) for r in radii) if trigger else max(0.0, radii - t)
            pen.rrect(x0 + t, y0 + t, x1 - t, y1 - t, inner, hole=True)
        cap = min(0.52 * h, 0.72 * (x1 - x0 - 2 * t) / max(1e-6, self.labels.width(label, 1.0)))
        self.labels.draw(pen, label, (x0 + x1) / 2, (y0 + y1) / 2 - (0.03 * h if trigger else 0), cap, hole=solid)
        return pen.glyph()

    def system(self, box, kind, frame):
        """Menu/View (Xbox), Options/Create (PlayStation): in a disc ("disc"), a ring ("ring") or alone ("bare")."""
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r = min(x1 - x0, y1 - y0) / 2
        pen = Pen()
        if frame == "disc":
            pen.circle(cx, cy, r)
        elif frame == "ring":
            pen.circle(cx, cy, r)
            pen.circle(cx, cy, 0.83 * r, hole=True)
        else:
            r *= 1.25  # alone, the symbol takes the whole box
        cut = frame == "disc"
        s = 0.5 * r  # the symbol's half size
        if kind in ("menu", "options"):
            # three bars; Options' are shorter and sit in a narrow upright pill, as on the pad
            half = s if kind == "menu" else 0.62 * s
            th = 0.13 * r
            if kind == "options":
                pill = (cx - 0.82 * s, cy - 1.25 * s, cx + 0.82 * s, cy + 1.25 * s)
                if frame == "bare":
                    pen.rrect(*pill, 0.82 * s)
                    cut = True
                else:
                    pen.rrect(*pill, 0.82 * s, hole=cut)
                    cut = not cut
            for dy in (-0.62 * s, 0, 0.62 * s):
                pen.rrect(cx - half, cy + dy - th / 2, cx + half, cy + dy + th / 2, th / 2, hole=cut)
        elif kind == "view":
            # two overlapping windows: the front one solid, the back one only where it shows
            q, d, t, g = 0.68 * s, 0.5 * s, 0.13 * r, 0.1 * r
            bx0, by0 = cx - q - d / 2, cy - q + d / 2  # back square, up and to the left
            fx0, fy0 = bx0 + d, by0 - d
            side = 2 * q
            pen.rrect(fx0, fy0, fx0 + side, fy0 + side, 0.05 * r, hole=cut)
            for rect in ((bx0, by0 + side - t, bx0 + side, by0 + side),          # top edge
                         (bx0, by0, bx0 + t, by0 + side - t),                     # left edge
                         (bx0 + t, by0, fx0 - g, by0 + t),                        # bottom edge, up to the front
                         (bx0 + side - t, fy0 + side + g, bx0 + side, by0 + side - t)):  # right edge, above it
                if rect[2] > rect[0] and rect[3] > rect[1]:
                    pen.rrect(*rect, 0, hole=cut)
        elif kind == "create":
            # an upright pill with an arrow pointing up out of a tray (share)
            pill = (cx - 0.82 * s, cy - 1.25 * s, cx + 0.82 * s, cy + 1.25 * s)
            if frame == "bare":
                pen.rrect(*pill, 0.82 * s)
                cut = True
            else:
                pen.rrect(*pill, 0.82 * s, hole=cut)
                cut = not cut
            t = 0.12 * r
            arrow = [(cx, cy + 0.85 * s), (cx + 0.5 * s, cy + 0.3 * s), (cx + t / 2, cy + 0.3 * s),
                     (cx + t / 2, cy - 0.45 * s), (cx - t / 2, cy - 0.45 * s), (cx - t / 2, cy + 0.3 * s),
                     (cx - 0.5 * s, cy + 0.3 * s)]
            pen.poly(arrow, hole=cut)
            pen.rrect(cx - 0.5 * s, cy - 0.85 * s, cx + 0.5 * s, cy - 0.85 * s + t, t / 2, hole=cut)
        return pen.glyph()

    def dpad(self, box, style, lit, arrows):
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        a = min(x1 - x0, y1 - y0) / 2  # arm reach
        w = 0.40 * a                   # half arm width
        t = 0.13 * a                   # outline thickness
        g = 0.06 * a                   # gap between the outline and a lit arm
        pen = Pen()
        pen.poly(rot(plus_pts(a, w), 0, cx, cy))
        if style == "line":
            pen.poly(rot(plus_pts(a - t, w - t), 0, cx, cy), hole=True)
        lw = w - t - g  # a lit arm's half width
        for d in lit:  # a filled arm inside the outline, clear of it
            arm = [(lw, lw), (a - t - g, lw), (a - t - g, -lw), (lw, -lw)]
            pen.poly(rot(arm, DIR_ANGLE[d], cx, cy))
        for d in arrows:
            # a triangle on the arm: cut out of a solid or lit arm, drawn in an empty one (so smaller in a lit one)
            hw = 0.62 * lw if d in lit else 0.75 * (w - t)
            m = 0.66 * a
            tri = [(m + 0.55 * hw, 0), (m - 0.55 * hw, -hw), (m - 0.55 * hw, hw)]  # clockwise, pointing outwards
            pen.poly(rot(tri, DIR_ANGLE[d], cx, cy), hole=(style == "fill" or d in lit))
        return pen.glyph()


# ---- styles -----------------------------------------------------------------------------------------------------

def plan(style, layout):
    """{code point: ("draw", kind, args, code point whose box it fills) | ("copy", source code point) |
    ("keycaps", action, box code point, painter, solid)} for one style and layout."""
    pos = LAYOUTS[layout]
    out = {}
    if style == "switch":
        # The original icons, moved to the button that now does the job: with the xbox layout the game's A is the
        # pad's bottom button, which a Switch pad labels B.
        for game, (fill, line) in FACE.items():
            src = next(g for g, p in LAYOUTS["console"].items() if p == pos[game])
            if src != game:
                out[fill], out[line] = ("copy", FACE[src][0]), ("copy", FACE[src][1])
        return out
    if style == "keyboard":
        # One keycap per catalog label at every prompt's own box: the default label is what a fresh binding shows,
        # and the runtime points the cmap at the label of the key actually bound. The D-pad keeps its drawn cross
        # (it reads as the arrow keys), and so do the stick prompts.
        for game, (fill, line) in FACE.items():
            out[fill] = ("keycaps", game.lower(), fill, "key", True)
            out[line] = ("keycaps", game.lower(), line, "key", False)
        for game, (fill, line) in SHOULDER.items():
            out[fill] = ("keycaps", game.lower(), fill, "shoulder", True)
            out[line] = ("keycaps", game.lower(), line, "shoulder", False)
        for game, cps in SYSTEM.items():
            for i, (cp, frame) in enumerate(cps):
                # a bare minus is a flat bar: its replacement gets the bare plus's square box instead
                box_cp = 0xE0F1 if frame == "bare" else cp
                out[cp] = ("keycaps", game.lower(), box_cp, "key", i % 2 == 0)
        for cp, spec in DPAD.items():
            out[cp] = ("draw", "dpad", spec, cp)
        return out
    for game, (fill, line) in FACE.items():
        label = FACE_LABELS[style][pos[game]]
        out[fill] = ("draw", "face", (label, True), fill)
        out[line] = ("draw", "face", (label, False), line)
    for game, (fill, line) in SHOULDER.items():
        label = OTHER_LABELS[style][game]
        out[fill] = ("draw", "shoulder", (label, game.startswith("Z"), True), fill)
        out[line] = ("draw", "shoulder", (label, game.startswith("Z"), False), line)
    for game, cps in SYSTEM.items():
        for cp, frame in cps:
            # a bare minus is a flat bar: its replacement gets the bare plus's square box instead
            box_cp = 0xE0F1 if frame == "bare" else cp
            out[cp] = ("draw", "system", (OTHER_LABELS[style][game], frame), box_cp)
    for cp, spec in DPAD.items():
        out[cp] = ("draw", "dpad", spec, cp)
    return out


def strip_hinting(font):
    """Drop every glyph program. They are over 40% of ParkExt's glyf table, and the game's font loader indexes loca
    with 16-bit halves: a glyf table over 131070 bytes makes it read garbage offsets and crash at boot. The keyboard
    icons need the room for their keycaps; without hinting the outlines are drawn exactly the same, only unhinted."""
    for name in font.getGlyphOrder():
        glyph = font["glyf"][name]
        program = getattr(glyph, "program", None)
        if program is not None and program.getBytecode():
            program.fromBytecode(b"")


def build(ext_ttf, labels, style, layout):
    """ParkExt (TTF bytes) with the plan's glyphs replaced; returns (TTFont, {code point: kind}, keyboard) where
    keyboard is {code point: (action, {label: glyph name})} for the keycap prompts (empty for other styles)."""
    font = TTFont(io.BytesIO(ext_ttf))
    orig = TTFont(io.BytesIO(ext_ttf))
    sub = format4_subtable(font)
    cmap = sub.cmap if sub is not None else font.getBestCmap()
    ocmap = orig.getBestCmap()  # the original outlines: cmap[cp] may point at a keycap added for an earlier prompt
    glyf, hmtx = font["glyf"], font["hmtx"]
    painter = Painter(labels)
    order, done, keyboard = list(font.getGlyphOrder()), {}, {}  # a copy: glyf appends to its own order as glyphs go in
    keycaps = {}  # (kind, solid) -> {label: glyph name}: one set shared by every prompt of that shape
    for cp, spec in sorted(plan(style, layout).items()):
        name = cmap.get(cp)
        if not name:
            continue  # not in this font version: nothing to replace
        op = spec[0]
        if op == "copy":
            src = ocmap[spec[1]]
            glyf[name] = copy.deepcopy(orig["glyf"][src])
            hmtx[name] = orig["hmtx"][src]
            done[cp] = f"copy U+{spec[1]:04X}"
            continue
        if op == "keycaps":
            action, box_cp, kind, solid = spec[1:]
            if style == "keyboard":
                solid = True  # one filled keycap per shape for every prompt: four sets would not fit the glyf table
            add = keycaps.get((kind, solid))
            if add is None:
                # One keycap per catalog label, reused by every prompt of this shape. The runtime points the prompt's
                # cmap at the label of the key bound to it, so the label block only has to stay in LABELS order; a
                # font per prompt would blow up ParkExt's glyf table (see strip_hinting).
                g0 = orig["glyf"][ocmap[box_cp]]
                box = (g0.xMin, g0.yMin, g0.xMax, g0.yMax)
                add = {}
                for label in LABELS:
                    gname = "kbd%s%s_%s" % (kind, "" if solid else "line", label)
                    glyph = (painter.key(box, label, solid) if kind == "key"
                             else painter.shoulder(box, label, action.startswith("z"), solid))
                    glyf[gname] = glyph
                    glyph.recalcBounds(glyf)
                    hmtx[gname] = (orig["hmtx"][name][0], glyph.xMin)  # the box glyph's advance
                    order.append(gname)
                    add[label] = gname
                keycaps[(kind, solid)] = add
            keyboard[cp] = (action, add)
            cmap[cp] = add[KEYBOARD_DEFAULTS[action]]  # what a fresh binding shows (the fallback archive)
            done[cp] = "keycaps (%d labels)" % len(add)
            continue
        kind, params, box_cp = spec[1:]
        g0 = orig["glyf"][cmap[box_cp]]
        box = (g0.xMin, g0.yMin, g0.xMax, g0.yMax)
        glyph = getattr(painter, kind)(box, *params)
        glyf[name] = glyph
        glyph.recalcBounds(glyf)
        hmtx[name] = (orig["hmtx"][name][0], glyph.xMin)  # same advance as before; lsb from the new outline
        done[cp] = kind
    font.setGlyphOrder(order)
    if style == "keyboard":
        strip_hinting(font)
    if "DSIG" in font:  # a signature over the old tables
        del font["DSIG"]
    return font, done, keyboard


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--romfs", default=os.path.join(REPO, "private", "acnh", "extract", "romfs"))
    ap.add_argument("--out", default=os.path.join(REPO, "private", "acnh", "fonts"))
    ap.add_argument("--styles", default="xbox,playstation,switch,keyboard", help="comma separated")
    ap.add_argument("--preview", help="also render every style's icons side by side to this PNG (needs Pillow)")
    ap.add_argument("--selftest", action="store_true", help="check the cmap patching on a synthetic font (no game data)")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    head, members, align = read_sarc(os.path.join(args.romfs, ARCHIVE))
    by_name = {name: k for k, (_, name, _) in enumerate(members)}
    LABEL_FONT = next((n for n in by_name if n.endswith(LABEL_FONT_SUFFIX)), LABEL_FONT_SUFFIX)
    for need in (EXT_FONT, LABEL_FONT):
        if need not in by_name:
            sys.exit(f"{ARCHIVE} has no {need}: not ACNH 2.0+ data?")
    ext_ttf, key = bfttf_decode(members[by_name[EXT_FONT]][2])
    labels = Labels(TTFont(io.BytesIO(bfttf_decode(members[by_name[LABEL_FONT]][2])[0])))

    variants = []
    for style in [s.strip() for s in args.styles.split(",") if s.strip()]:
        for layout in ([None] if style == "keyboard" else LAYOUTS):
            if style == "switch" and layout == "console":
                continue  # the RomFS as it is
            font, done, keyboard = build(ext_ttf, labels, style, layout or "console")
            buf = io.BytesIO()
            font.save(buf)
            ttf = buf.getvalue()
            offsets = {}
            if keyboard:  # the runtime patches glyph ids, so the cmap needs a word per prompt (canonical_cmap)
                canon, offsets = canonical_cmap(ttf)
                if canon is not None:
                    ttf = canon
            base = os.path.join(args.out, f"glyphs_{style}", layout or "")
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "ParkExt.ttf"), "wb") as f:
                f.write(ttf)
            new = list(members)
            node, name, _ = new[by_name[EXT_FONT]]
            new[by_name[EXT_FONT]] = (node, name, bfttf_encode(ttf, key))
            write_sarc(head, new, align, os.path.join(base, "romfs", ARCHIVE))
            if keyboard and offsets:
                # The uncompressed archive the runtime patches (the same bytes as romfs/, uncompressed), where the
                # font starts in it, and the idDelta word of each prompt relative to the font. The runtime copies
                # the bytes and only XORs the BFTTF key back in at each absolute position.
                shead, sbody, starts = sarc_parts(head, new, align)
                font_at = len(shead) + starts[name] + 8  # past the BFTTF header (magic and size)
                with open(os.path.join(base, "base.sarc"), "wb") as f:
                    f.write(shead + sbody)
                with open(os.path.join(base, "patch.txt"), "w", encoding="utf-8", newline="\n") as f:
                    f.write("# keyboard icons: the cmap idDelta word of each prompt, patched by runtime/src/glyphs_keys.cpp\n")
                    f.write("key %08x\n" % key)
                    f.write("base %d\n" % font_at)
                    f.write("labels %s\n" % ",".join(LABELS))
                    for cp in sorted(keyboard):
                        action, add = keyboard[cp]
                        gid = font.getGlyphID(add[LABELS[0]])
                        f.write("cp %04x %s %d %d\n" % (cp, action, offsets[cp], gid))
                print(f"glyphs_{style}: {len(done)} glyphs redrawn, {len(keyboard)} prompts follow the bound keys")
            else:
                print(f"glyphs_{style}/{layout}: {len(done)} glyphs redrawn")
            variants.append((f"{style} / {layout or 'keys'} layout", os.path.join(base, "ParkExt.ttf")))

    if args.preview:
        orig_path = os.path.join(args.out, "ParkExt.original.ttf")
        with open(orig_path, "wb") as f:
            f.write(ext_ttf)
        label_ttf = bfttf_decode(members[by_name[LABEL_FONT]][2])[0]
        render_preview([("switch (original)", orig_path)] + variants, label_ttf, args.preview)



# ---- cmap patching (the keyboard icons follow the player's keys) ---------------------------------------------------
# The runtime rewrites 16-bit glyph ids only, so a keyboard archive's cmap subtable is rebuilt with one segment per
# code point (idRangeOffset 0): every prompt's mapping is then a single idDelta word that can be pointed at. The
# table is put back as a raw table object, so fontTools still lays out and checksums the file.


def sfnt_tables(ttf):
    """{tag: (offset, length)} of a TrueType font's tables."""
    if len(ttf) < 12 or ttf[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        return {}
    out = {}
    for i in range(struct.unpack_from(">H", ttf, 4)[0]):
        tag, _, off, length = struct.unpack_from(">4sIII", ttf, 12 + i * 16)
        out[tag.decode("latin-1")] = (off, length)
    return out


def subtable_len(ttf, at):
    """A cmap subtable's length: u32 for formats 8/10/12/13, u16 for the rest."""
    if struct.unpack_from(">H", ttf, at)[0] in (8, 10, 12, 13):
        return struct.unpack_from(">I", ttf, at + 4)[0]
    return struct.unpack_from(">H", ttf, at + 2)[0]


def format4_subtable(font):
    """The format 4 subtable the game reads (Windows Unicode BMP if there is one)."""
    best = None
    for sub in font["cmap"].tables:
        if sub.format != 4:
            continue
        if (sub.platformID, sub.platEncID) == (3, 1):
            return sub
        if best is None:
            best = sub
    return best


def decode_format4(ttf, at):
    """{code point: glyph id} of a format 4 cmap subtable."""
    seg = struct.unpack_from(">H", ttf, at + 6)[0] // 2
    ends = struct.unpack_from(">%dH" % seg, ttf, at + 14)
    starts = struct.unpack_from(">%dH" % seg, ttf, at + 16 + 2 * seg)
    deltas = struct.unpack_from(">%dH" % seg, ttf, at + 16 + 4 * seg)
    ros = struct.unpack_from(">%dH" % seg, ttf, at + 16 + 6 * seg)
    out = {}
    for i in range(seg):
        base = at + 16 + 6 * seg + 2 * i  # idRangeOffset[i], which its offsets are relative to
        for cp in range(starts[i], ends[i] + 1):
            if ros[i]:
                gid = struct.unpack_from(">H", ttf, base + ros[i] + 2 * (cp - starts[i]))[0]
                gid = (gid + deltas[i]) & 0xFFFF if gid else 0
            else:
                gid = (cp + deltas[i]) & 0xFFFF
            if gid:
                out[cp] = gid
    return out


def build_format4(mapping):
    """(bytes, {code point: idDelta byte offset}) of a format 4 subtable with one segment per code point."""
    cps = [cp for cp in sorted(mapping) if cp < 0xFFFF] + [0xFFFF]
    seg = len(cps)
    length = 16 + 8 * seg
    if length > 0xFFFF:
        return None, {}  # too many code points for one segment each: no live patching
    search = 2 * (1 << int(math.log2(seg)))
    table = bytearray(length)
    struct.pack_into(">7H", table, 0, 4, length, 0, 2 * seg, search, int(math.log2(seg)), 2 * seg - search)
    struct.pack_into(">%dH" % seg, table, 14, *cps)  # endCode
    struct.pack_into(">%dH" % seg, table, 16 + 2 * seg, *cps)  # startCode
    struct.pack_into(">%dH" % seg, table, 16 + 4 * seg, *((mapping.get(cp, 0) - cp) & 0xFFFF for cp in cps))
    return bytes(table), {cp: 16 + 4 * seg + 2 * i for i, cp in enumerate(cps) if cp != 0xFFFF}


def canonical_cmap(ttf):
    """(ttf bytes, {code point: absolute idDelta offset in them}) with the format 4 subtable rebuilt as one segment
    per code point; (None, {}) when the font has no such subtable to patch."""
    tabs = sfnt_tables(ttf)
    if "cmap" not in tabs:
        return None, {}
    coff = tabs["cmap"][0]
    n = struct.unpack_from(">H", ttf, coff + 2)[0]
    recs = [struct.unpack_from(">HHI", ttf, coff + 4 + i * 8) for i in range(n)]
    target = None
    for i, (plat, enc, off) in enumerate(recs):
        if struct.unpack_from(">H", ttf, coff + off)[0] == 4 and (target is None or (plat, enc) == (3, 1)):
            target = i
            if (plat, enc) == (3, 1):
                break
    if target is None:
        return None, {}
    sub, inside = build_format4(decode_format4(ttf, coff + recs[target][2]))
    if sub is None:
        return None, {}
    subs, offsets, pos = [], [], 4 + 8 * n
    for i, (_, _, off) in enumerate(recs):
        data = sub if i == target else ttf[coff + off:coff + off + subtable_len(ttf, coff + off)]
        subs.append(data)
        offsets.append(pos)
        pos += len(data)
        pos = (pos + 3) // 4 * 4
    table = bytearray(pos)
    struct.pack_into(">HH", table, 0, 0, n)
    for i, (plat, enc, _) in enumerate(recs):
        struct.pack_into(">HHI", table, 4 + 8 * i, plat, enc, offsets[i])
    for data, off in zip(subs, offsets):
        table[off:off + len(data)] = data
    font = TTFont(io.BytesIO(ttf))
    raw = DefaultTable("cmap")  # the rebuilt table, written as it is
    raw.data = bytes(table)
    font["cmap"] = raw
    buf = io.BytesIO()
    font.save(buf)
    out = buf.getvalue()
    cmap_off = sfnt_tables(out)["cmap"][0]
    return out, {cp: cmap_off + offsets[target] + at for cp, at in inside.items()}


def selftest():
    """The cmap patching, on a font built here: no game data (the CI's verify job runs this)."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    # The catalog and the plan agree with the runtime's action names and defaults.
    for action, label in KEYBOARD_DEFAULTS.items():
        if label not in LABELS:
            sys.exit("selftest: default %s for %s has no keycap" % (label, action))
    for cp, spec in plan("keyboard", "console").items():
        if spec[0] == "keycaps" and spec[1] not in KEYBOARD_DEFAULTS:
            sys.exit("selftest: U+%04X follows unknown action %s" % (cp, spec[1]))

    def box():
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((600, 0))
        pen.lineTo((600, 600))
        pen.lineTo((0, 600))
        pen.closePath()
        return pen.glyph()

    cps = sorted({cp for pair in FACE.values() for cp in pair} |
                 {cp for pair in SHOULDER.values() for cp in pair} |
                 {cp for specs in SYSTEM.values() for cp, _ in specs})
    names = [".notdef"] + ["g%04X" % cp for cp in cps]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupGlyf({name: box() for name in names})
    fb.setupHorizontalMetrics({name: (600, 0) for name in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupCharacterMap({cp: "g%04X" % cp for cp in cps})
    fb.setupNameTable({"familyName": "Selftest", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.setupMaxp()
    buf = io.BytesIO()
    fb.font.save(buf)
    ttf = buf.getvalue()

    canon, offsets = canonical_cmap(ttf)
    if canon is None:
        sys.exit("selftest: no format 4 cmap subtable")
    if set(offsets) < set(cps):
        sys.exit("selftest: %d prompts are not patchable" % len(set(cps) - set(offsets)))
    before = TTFont(io.BytesIO(canon))
    mapping = before.getBestCmap()
    for cp in cps:
        if mapping.get(cp) != "g%04X" % cp:
            sys.exit("selftest: U+%04X moved during the cmap rewrite" % cp)

    # What the runtime does: write (gid - cp) into the idDelta word of the prompt's segment.
    cp, other = cps[0], cps[-1]
    gid = len(names) - 1
    patched = canon[:offsets[cp]] + struct.pack(">H", (gid - cp) & 0xFFFF) + canon[offsets[cp] + 2:]
    after = TTFont(io.BytesIO(patched)).getBestCmap()
    if after.get(cp) != names[gid]:
        sys.exit("selftest: patching U+%04X did not take" % cp)
    if after.get(other) != "g%04X" % other:
        sys.exit("selftest: patching U+%04X moved U+%04X too" % (cp, other))

    key = 0x1A2B3C4D
    if bfttf_decode(bfttf_encode(ttf, key))[0] != ttf:
        sys.exit("selftest: BFTTF round trip")

    # The keyboard build: a keycap for every catalog label at every prompt, then the cmap the runtime patches.
    # The label font stands in for the game's *P_Seurat-B: every printable ASCII character as the same box.
    lnames = [".notdef"] + ["c%02X" % c for c in range(32, 127)]
    lfb = FontBuilder(1000, isTTF=True)
    lfb.setupGlyphOrder(lnames)
    lfb.setupGlyf({name: box() for name in lnames})
    lfb.setupHorizontalMetrics({name: (600, 0) for name in lnames})
    lfb.setupHorizontalHeader(ascent=800, descent=-200)
    lfb.setupCharacterMap({c: "c%02X" % c for c in range(32, 127)})
    lfb.setupNameTable({"familyName": "Selftest Labels", "styleName": "Regular"})
    lfb.setupOS2()
    lfb.setupPost()
    lfb.setupMaxp()
    lbuf = io.BytesIO()
    lfb.font.save(lbuf)

    want = {cp for pair in FACE.values() for cp in pair} | \
           {cp for pair in SHOULDER.values() for cp in pair} | \
           {cp for specs in SYSTEM.values() for cp, _ in specs}
    kfont, _, kbd = build(ttf, Labels(TTFont(io.BytesIO(lbuf.getvalue()))), "keyboard", "console")
    if set(kbd) != want:
        sys.exit("selftest: keyboard draws %d of %d prompts" % (len(kbd), len(want)))
    kbuf = io.BytesIO()
    kfont.save(kbuf)
    kcanon, koffsets = canonical_cmap(kbuf.getvalue())
    if kcanon is None or set(kbd) - set(koffsets):
        sys.exit("selftest: %d keycap prompts are not patchable" % len(set(kbd) - set(koffsets)))
    kmap = TTFont(io.BytesIO(kcanon)).getBestCmap()
    for cp, (action, add) in kbd.items():
        first = kfont.getGlyphID(add[LABELS[0]])
        for i, label in enumerate(LABELS):  # the manifest's first_gid + index(label)
            if kfont.getGlyphID(add[label]) != first + i:
                sys.exit("selftest: U+%04X: the %s glyph is not label %d" % (cp, label, i))
        if kmap.get(cp) != add[KEYBOARD_DEFAULTS[action]]:
            sys.exit("selftest: U+%04X does not start on the default %s keycap" % (cp, action))
    cp = min(kbd)
    action, add = kbd[cp]
    kgid = kfont.getGlyphID(add["SPC"])
    kpatched = kcanon[:koffsets[cp]] + struct.pack(">H", (kgid - cp) & 0xFFFF) + kcanon[koffsets[cp] + 2:]
    if TTFont(io.BytesIO(kpatched)).getBestCmap().get(cp) != add["SPC"]:
        sys.exit("selftest: patching U+%04X to its bound key did not take" % cp)

    print("selftest: %d keycap prompts, %d patchable, cmap rewrite and BFTTF round trip ok" % (len(kbd), len(offsets)))


PREVIEW_ROWS = [
    ("A", [0xE0E0, 0xE0A0]), ("B", [0xE0E1, 0xE0A1]), ("X", [0xE0E2, 0xE0A2]), ("Y", [0xE0E3, 0xE0A3]),
    ("L", [0xE0E4, 0xE0A4]), ("R", [0xE0E5, 0xE0A5]), ("ZL", [0xE0E6, 0xE0A6]), ("ZR", [0xE0E7, 0xE0A7]),
    ("+", [0xE0EF, 0xE0B3, 0xE0F1]), ("-", [0xE0F0, 0xE0B4, 0xE0F2]),
    ("D-pad", [0xE0EA, 0xE0AA, 0xE0AB, 0xE0AC, 0xE0AD, 0xE0AE]),
    ("D-pad arrows", [0xE23F, 0xE240, 0xE0CB, 0xE0CE, 0xE0EB, 0xE0B2]),
]


def render_preview(columns, text_ttf, png):
    from PIL import Image, ImageDraw, ImageFont

    size, cell, label_w = 46, 54, 150
    widths = [max(len(cps) for _, cps in PREVIEW_ROWS) * cell + 20] * len(columns)
    W = label_w + sum(widths)
    H = 70 + len(PREVIEW_ROWS) * 64 + 90
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    small = ImageFont.load_default(size=15)
    fonts = [ImageFont.truetype(p, size) for _, p in columns]
    x = label_w
    for (title, _), w in zip(columns, widths):
        d.text((x + 6, 20), title, font=small, fill=(40, 40, 40))
        d.line([(x, 10), (x, H - 100)], fill=(220, 220, 220))
        x += w
    for i, (name, cps) in enumerate(PREVIEW_ROWS):
        y = 70 + i * 64
        d.text((10, y + 18), f"game {name}", font=small, fill=(90, 90, 90))
        x = label_w
        for f, w in zip(fonts, widths):
            d.line([(x + 4, y + 48), (x + w - 8, y + 48)], fill=(200, 200, 255))  # baseline
            for k, cp in enumerate(cps):
                d.text((x + 8 + k * cell, y + 48), chr(cp), font=f, fill="black", anchor="ls")
            x += w
    # in a sentence, at text size (words in the game's font): the icons keep the line's metrics
    import io
    words = ImageFont.truetype(io.BytesIO(text_ttf), 24)
    sentence = [("\uE0E0", 1), (" Pick up   ", 0), ("\uE0E1", 1), (" Run   ", 0), ("\uE0E4", 1), ("/", 0),
                ("\uE0E5", 1), (" Tools   ", 0), ("\uE0F0", 1), (" Map", 0)]
    for k, (_, path) in enumerate(columns):
        icons = ImageFont.truetype(path, 24)
        x, y = 10 + (k % 3) * (W // 3), H - 66 + (k // 3) * 34
        for text, is_icon in sentence:
            f = icons if is_icon else words
            d.text((x, y), text, font=f, fill="black", anchor="ls")
            x += d.textlength(text, font=f)
    img.save(png)
    print(f"preview {png}")


if __name__ == "__main__":
    main()
