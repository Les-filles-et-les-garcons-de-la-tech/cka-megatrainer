"""80s arcade flavour for CKA Megatrainer.

Pixel font, rotating Kubernetes helm, starfield title screen and an original
NES-style chiptune synthesised here and played through the sound card
(pw-play / paplay / aplay / afplay). Python 3 standard library only.
"""
import array
import atexit
import curses
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
import zlib
from pathlib import Path

FRAME_MS = 66  # ~15 fps

# --------------------------------------------------------------------------
# Pixel font (5x7)
# --------------------------------------------------------------------------

FONT = {ch: rows.split(",") for ch, rows in {
    "A": "01110,10001,10001,11111,10001,10001,10001",
    "B": "11110,10001,10001,11110,10001,10001,11110",
    "C": "01111,10000,10000,10000,10000,10000,01111",
    "D": "11110,10001,10001,10001,10001,10001,11110",
    "E": "11111,10000,10000,11110,10000,10000,11111",
    "F": "11111,10000,10000,11110,10000,10000,10000",
    "G": "01111,10000,10000,10011,10001,10001,01111",
    "H": "10001,10001,10001,11111,10001,10001,10001",
    "I": "11111,00100,00100,00100,00100,00100,11111",
    "J": "00111,00010,00010,00010,00010,10010,01100",
    "K": "10001,10010,10100,11000,10100,10010,10001",
    "L": "10000,10000,10000,10000,10000,10000,11111",
    "M": "10001,11011,10101,10101,10001,10001,10001",
    "N": "10001,11001,10101,10011,10001,10001,10001",
    "O": "01110,10001,10001,10001,10001,10001,01110",
    "P": "11110,10001,10001,11110,10000,10000,10000",
    "Q": "01110,10001,10001,10001,10101,10010,01101",
    "R": "11110,10001,10001,11110,10100,10010,10001",
    "S": "01111,10000,10000,01110,00001,00001,11110",
    "T": "11111,00100,00100,00100,00100,00100,00100",
    "U": "10001,10001,10001,10001,10001,10001,01110",
    "V": "10001,10001,10001,10001,10001,01010,00100",
    "W": "10001,10001,10001,10101,10101,10101,01010",
    "X": "10001,10001,01010,00100,01010,10001,10001",
    "Y": "10001,10001,01010,00100,00100,00100,00100",
    "Z": "11111,00001,00010,00100,01000,10000,11111",
    "0": "01110,10001,10011,10101,11001,10001,01110",
    "1": "00100,01100,00100,00100,00100,00100,01110",
    "2": "01110,10001,00001,00010,00100,01000,11111",
    "3": "11110,00001,00001,01110,00001,00001,11110",
    "4": "00010,00110,01010,10010,11111,00010,00010",
    "5": "11111,10000,11110,00001,00001,10001,01110",
    "6": "01110,10000,10000,11110,10001,10001,01110",
    "7": "11111,00001,00010,00100,01000,01000,01000",
    "8": "01110,10001,10001,01110,10001,10001,01110",
    "9": "01110,10001,10001,01111,00001,00001,01110",
    " ": "00000,00000,00000,00000,00000,00000,00000",
    "!": "00100,00100,00100,00100,00100,00000,00100",
    ":": "00000,00100,00100,00000,00100,00100,00000",
    "/": "00001,00010,00010,00100,01000,01000,10000",
    "%": "11001,11010,00010,00100,01000,01011,10011",
    "-": "00000,00000,00000,11111,00000,00000,00000",
    ".": "00000,00000,00000,00000,00000,00000,00100",
}.items()}


def glyph(ch):
    return FONT.get(ch.upper(), FONT[" "])


def big(text):
    """7 rows, 2 columns per pixel, full blocks."""
    return ["  ".join("".join("██" if b == "1" else "  " for b in glyph(c)[r]) for c in text)
            for r in range(7)]


def half(text):
    """4 rows, 1 column per pixel, half blocks."""
    rows = []
    for r in range(0, 8, 2):
        cells = []
        for c in text:
            g = glyph(c) + ["00000"]
            cells.append("".join(" ▀▄█"[int(t) + 2 * int(b)] for t, b in zip(g[r], g[r + 1])))
        rows.append(" ".join(cells))
    return rows


# --------------------------------------------------------------------------
# Kubernetes helm: blue heptagon, white 7-spoke wheel (rotating)
# --------------------------------------------------------------------------

HELM_H, HELM_W = 13, 27  # cells; each cell = 2 stacked square pixels


def helm_pixels(angle, h=HELM_H * 2, w=HELM_W):
    """Pixel image: None (transparent), 'B' (blue heptagon) or 'W' (white helm)."""
    cy, cx = (h - 1) / 2, (w - 1) / 2
    r = min(cy, cx) + 0.5
    verts = [(cy - r * math.cos(2 * math.pi * k / 7), cx + r * math.sin(2 * math.pi * k / 7))
             for k in range(7)]
    spokes = [angle + 2 * math.pi * k / 7 for k in range(7)]

    def inside(y, x):
        return all((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1) >= 0
                   for (y1, x1), (y2, x2) in zip(verts, verts[1:] + verts[:1]))

    def on_helm(y, x):
        dy, dx = y - cy, x - cx
        d = math.hypot(dy, dx)
        if d < 2.0 or abs(d - 0.47 * r) < 0.8:  # hub and rim
            return True
        for a in spokes:
            sy, sx = -math.cos(a), math.sin(a)
            along = dy * sy + dx * sx
            if 0 < along < 0.7 * r and abs(dx * sy - dy * sx) < 0.65:  # spoke
                return True
            if math.hypot(dy - 0.7 * r * sy, dx - 0.7 * r * sx) < 1.4:  # handle knob
                return True
        return False

    return [[("W" if on_helm(y, x) else "B") if inside(y, x) else None for x in range(w)]
            for y in range(h)]


def helm(angle):
    """Rows of (char, kind): kind None (transparent), 'helm' (white on blue) or 'blue'."""
    px = helm_pixels(angle)
    rows = []
    for top, bot in zip(px[0::2], px[1::2]):
        row = []
        for t, b in zip(top, bot):
            if t is None and b is None:
                row.append((" ", None))
            elif t is None or b is None:  # heptagon edge: half pixel on the background
                row.append(("▄" if t is None else "▀", "blue" if (t or b) == "B" else "white"))
            elif t == b:
                row.append(("█" if t == "W" else " ", "helm"))
            else:
                row.append(("▀" if t == "W" else "▄", "helm"))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Colours and drawing
# --------------------------------------------------------------------------

C = {}
RAINBOW = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta")
NEON = ("red", "orange", "yellow", "lime", "cyan", "blue", "purple", "magenta", "pink", "white")
COLOR_NAMES = ("red", "orange", "yellow", "green", "lime", "cyan", "blue", "purple",
               "magenta", "pink", "white")

THEMES = ("retro", "soft")
THEME = {"name": "retro"}

# retro: exact RGB entries of the xterm-256 palette, like an arcade / EGA monitor,
# independent of the terminal's own (often muted) colour scheme.
RETRO_256 = {"red": 196, "orange": 208, "yellow": 226, "green": 46, "lime": 118, "cyan": 51,
             "blue": 21, "purple": 129, "magenta": 201, "pink": 213, "white": 231,
             "helm": 26,  # Kubernetes blue (#326CE5 ~ #005FD7)
             "bg": 16}    # pure black


def _basic_palette():
    """The terminal's 8 colours (its colour scheme decides the exact shades)."""
    c = curses
    return {"red": c.COLOR_RED, "orange": c.COLOR_YELLOW, "yellow": c.COLOR_YELLOW,
            "green": c.COLOR_GREEN, "lime": c.COLOR_GREEN, "cyan": c.COLOR_CYAN,
            "blue": c.COLOR_BLUE, "purple": c.COLOR_MAGENTA, "magenta": c.COLOR_MAGENTA,
            "pink": c.COLOR_MAGENTA, "white": c.COLOR_WHITE, "helm": c.COLOR_BLUE}


def set_theme(name):
    THEME["name"] = name


def init_colors(win=None):
    """Colour pairs for the current theme. retro also paints the window black."""
    if C:
        return
    curses.start_color()
    if THEME["name"] == "retro":
        pal = RETRO_256 if curses.COLORS >= 256 else {**_basic_palette(), "bg": curses.COLOR_BLACK}
        bg = pal["bg"]
    else:
        pal, bg = _basic_palette(), -1
        try:
            curses.use_default_colors()
        except curses.error:
            bg = curses.COLOR_BLACK
    for i, name in enumerate(COLOR_NAMES, 10):
        curses.init_pair(i, pal[name], bg)
        C[name] = curses.color_pair(i)
    curses.init_pair(30, pal["white"], pal["helm"])
    C["helm"] = curses.color_pair(30) | curses.A_BOLD
    curses.init_pair(31, pal["helm"], bg)
    C["helmedge"] = curses.color_pair(31)
    if win is not None and bg != -1:
        win.bkgd(" ", C["white"])  # plain text becomes white on black too


def put(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            win.addnstr(y, x, s, w - x, attr)
        except curses.error:
            pass


def sprite(win, y, x, rows, attr_at):
    """Draw only the non-space characters; attr_at(row, col) gives the colour."""
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch != " ":
                put(win, y + r, x + c, ch, attr_at(r, c))


def draw_helm(win, y, x, angle):
    for r, row in enumerate(helm(angle)):
        for c, (ch, kind) in enumerate(row):
            if kind:
                attr = {"helm": C["helm"], "blue": C["helmedge"]}.get(kind, C["white"] | curses.A_BOLD)
                put(win, y + r, x + c, ch, attr)


_STARS = [(random.Random(i).random(), random.Random(-i - 1).random(), 1 + i % 3) for i in range(70)]


def draw_stars(win, t):
    h, w = win.getmaxyx()
    for fy, fx, speed in _STARS:
        y = int(fy * (h - 1))
        x = int((fx * w - t * 4 * speed) % w)
        ch, attr = {1: (".", C["white"] | curses.A_DIM), 2: ("+", C["white"]),
                    3: ("*", C["cyan"] | curses.A_BOLD)}[speed]
        put(win, y, x, ch, attr)


def width(rows):
    return max(len(r) for r in rows)


# --------------------------------------------------------------------------
# Title screen
# --------------------------------------------------------------------------

MENU_W = 46


def _layout(h, w, n_items):
    """Pick the richest header that fits. Returns (name, header_height, header_width)."""
    fixed = n_items + 6
    cka, mega = width(big("CKA")), width(half("MEGATRAINER"))
    for name, hh, ww in (("full", HELM_H + 5, max(HELM_W + 3 + cka, mega)),
                         ("medium", 12, max(cka, mega)),
                         ("small", 5, mega),
                         ("tiny", 1, 30)):
        if hh + fixed <= h and ww <= w - 2:
            return name, hh, ww
    return "tiny", 1, 30


def _draw_header(win, layout, top, w, t):
    shine = (t * 45) % (width(half("MEGATRAINER")) + 60) - 30

    def cka_attr(r, c):
        return C[RAINBOW[(c // 4 + int(t * 6)) % len(RAINBOW)]] | curses.A_BOLD

    def mega_attr(r, c):
        if abs(c - shine) < 2.5:
            return C["white"] | curses.A_BOLD
        return (C["yellow"] if r < 2 else C["red"]) | curses.A_BOLD

    def shadow(r, c):
        return C["blue"] | curses.A_DIM

    mega = half("MEGATRAINER")
    if layout == "full":
        x0 = (w - (HELM_W + 3 + width(big("CKA")))) // 2
        draw_helm(win, top, x0, t * 0.9)
        sprite(win, top + 4, x0 + HELM_W + 4, big("CKA"), shadow)
        sprite(win, top + 3, x0 + HELM_W + 3, big("CKA"), cka_attr)
        sprite(win, top + HELM_H + 1, (w - width(mega)) // 2, mega, mega_attr)
    elif layout == "medium":
        x0 = (w - width(big("CKA"))) // 2
        sprite(win, top + 1, x0 + 1, big("CKA"), shadow)
        sprite(win, top, x0, big("CKA"), cka_attr)
        sprite(win, top + 8, (w - width(mega)) // 2, mega, mega_attr)
    elif layout == "small":
        sprite(win, top, (w - width(mega)) // 2, mega, mega_attr)
    else:
        text = "★ CKA MEGATRAINER ★"
        put(win, top, (w - len(text)) // 2, text, C["yellow"] | curses.A_BOLD)


def _menu_line(label, right, selected):
    inner = MENU_W - 4
    dots = "." * max(1, inner - len(label) - len(right) - 2)
    body = f"{label} {dots} {right}" if right else label.center(inner)
    return ("► " if selected else "  ") + body + (" ◄" if selected else "  ")


def draw_title(win, items, sel, t, sound_status, flash=None):
    h, w = win.getmaxyx()
    win.erase()
    if h < 20 or w < 60:
        put(win, 0, 0, "Enlarge the terminal (60x20 minimum).", C["red"])
        win.refresh()
        return
    draw_stars(win, t)
    layout, hh, hw = _layout(h, w, len(items))
    total = hh + len(items) + 6
    top = max(0, (h - total) // 2)
    for y in range(top, top + hh):  # no stars inside the logo's letters
        put(win, y, (w - hw) // 2 - 2, " " * (hw + 4))  # +2: drop shadow
    _draw_header(win, layout, top, w, t)

    y = top + hh
    sub = "KUBERNETES v1.37 · CERTIFIED KUBERNETES ADMINISTRATOR"
    if len(sub) > w - 2:
        sub = "KUBERNETES v1.37 · CKA"
    put(win, y, (w - len(sub)) // 2, sub, C["cyan"] | curses.A_BOLD)

    y += 2
    x = (w - MENU_W) // 2
    for i, (_, label, right) in enumerate(items):
        if i == sel and flash is not None:
            attr = (curses.A_REVERSE if flash % 2 == 0 else 0) | C["yellow"] | curses.A_BOLD
        elif i == sel:
            attr = (C["yellow"] if int(t * 4) % 2 else C["white"]) | curses.A_BOLD
        else:
            attr = C["white"]
        put(win, y + i, x, _menu_line(label, right, i == sel), attr)

    y += len(items) + 1
    if int(t * 2) % 2 == 0 or flash is not None:
        prompt = "PRESS ENTER TO START"
        put(win, y, (w - len(prompt)) // 2, prompt, C["magenta"] | curses.A_BOLD)
    footer = f"↑↓ SELECT   ENTER START   M {sound_status}   Q QUIT"
    put(win, h - 1, max(0, (w - len(footer)) // 2), footer, C["white"] | curses.A_DIM)
    put(win, h - 1, max(0, w - 10), "CREDIT 01", C["green"])
    win.refresh()


def title_screen(win, items, sel, sound, read_key):
    """Animated menu. items: [(value, label, right)]. Returns the chosen index or None."""
    init_colors()
    curses.curs_set(0)
    sound.start_music()
    t0 = time.monotonic()
    while True:
        win.timeout(FRAME_MS)
        sound.tick()
        t = time.monotonic() - t0
        draw_title(win, items, sel, t, sound.status)
        try:
            key = read_key(win)
        except curses.error:
            continue
        if key in (curses.KEY_UP, "k"):
            sel = (sel - 1) % len(items)
            sound.play("move")
        elif key in (curses.KEY_DOWN, "j"):
            sel = (sel + 1) % len(items)
            sound.play("move")
        elif key in ("\n", "\r", " ", curses.KEY_ENTER):
            sound.play("select")
            for i in range(10):  # the arcade "you picked it" flash
                draw_title(win, items, sel, time.monotonic() - t0, sound.status, flash=i)
                curses.napms(60)
            curses.flushinp()
            return sel
        elif key in ("m", "M"):
            sound.toggle_music()
        elif key in ("q", "Q", "\x1b"):
            return None


def get_ready(win, stage, sound):
    """'READY!' flash and a colour wipe before the first question."""
    init_colors()
    curses.curs_set(0)
    sound.stop_music()
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < 1.2:
        h, w = win.getmaxyx()
        win.erase()
        draw_stars(win, t + 100)
        word = big("READY!") if w >= width(big("READY!")) + 4 else half("READY!")
        y = max(0, (h - len(word) - 4) // 2)
        color = C[RAINBOW[int(t * 10) % len(RAINBOW)]] | curses.A_BOLD
        x = (w - width(word)) // 2
        for r in range(len(word)):  # no stars inside the letters
            put(win, y + r, x - 1, " " * (width(word) + 2))
        sprite(win, y, x, word, lambda r, c: color)
        line = f"STAGE: {stage}"
        put(win, y + len(word) + 2, (w - len(line)) // 2, line, C["cyan"] | curses.A_BOLD)
        win.refresh()
        curses.napms(FRAME_MS)
    h, w = win.getmaxyx()
    for y in range(h):
        put(win, y, 0, "█" * w, C[RAINBOW[y % len(RAINBOW)]])
        win.refresh()
        curses.napms(8)
    win.erase()
    win.refresh()
    curses.flushinp()


def level_screen(win, number, total, lines, sound, seconds=2.0):
    """'LEVEL n' interstitial with the level jingle. Enter / space skips it."""
    init_colors()
    curses.curs_set(0)
    sound.wait_ready(1.5)  # first launch after a sound change: they are still rendering
    sound.play("level")
    curses.flushinp()
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < seconds:
        h, w = win.getmaxyx()
        win.erase()
        draw_stars(win, t + 50)
        label = f"LEVEL {number}"
        word = big(label) if w >= width(big(label)) + 4 and h >= 7 + len(lines) + 8 else half(label)
        y = max(0, (h - len(word) - len(lines) - 5) // 2)
        x = (w - width(word)) // 2
        for r in range(len(word)):
            put(win, y + r, x - 1, " " * (width(word) + 2))
        sprite(win, y, x, word, lambda r, c: C[RAINBOW[(c // 4 + int(t * 6)) % len(RAINBOW)]] | curses.A_BOLD)
        y += len(word) + 1
        put(win, y, (w - 9) // 2, f"{number:>2} OF {total:<2}", C["white"] | curses.A_DIM)
        for i, (text, color) in enumerate(lines):
            text = text[:w - 2]
            put(win, y + 2 + i, (w - len(text)) // 2, text, C[color] | curses.A_BOLD)
        if int(t * 4) % 2 == 0:
            put(win, y + 3 + len(lines), (w - 13) // 2, "GET READY ...", C["magenta"] | curses.A_BOLD)
        win.refresh()
        win.timeout(FRAME_MS)
        try:
            key = win.get_wch()
        except curses.error:
            continue
        if key in ("\n", "\r", " ", curses.KEY_ENTER) and t > 0.3:
            break
    curses.flushinp()


# --------------------------------------------------------------------------
# Finale: YOU WIN / GAME OVER
# --------------------------------------------------------------------------

class Fireworks:
    """Volleys of shells with sparkling tails, bursting in five styles:
    helm (7 rays like the Kubernetes wheel), peony (multicolour sphere),
    double (two heptagons, two colours), crackle (sparks that burst into glitter)
    and willow (gold sparks drooping down with long trails).
    Burning sparks shed embers that fall, so every burst rains down."""

    KINDS = ("helm", "peony", "double", "crackle", "willow")
    MAX_SPARKS = 900  # embers stop spawning above this (keeps the frame rate)

    def __init__(self, seed=37):
        self.rnd = random.Random(seed)
        self.shells, self.sparks, self.rings = [], [], []
        self.next_launch, self.last_boom = 0.1, -1.0

    def _spark(self, x, y, a, speed, color, t, life, **extra):
        self.sparks.append({"x": x, "y": y, "vx": 2 * speed * math.cos(a), "vy": speed * math.sin(a),
                            "born": t, "life": life, "color": color, "trail": [], **extra})

    def explode(self, s, t, sound):
        rnd, x, y, col = self.rnd, s["x"], s["y"], s["color"]
        if t - self.last_boom > 0.15:  # several bursts at once: one boom is enough
            sound.play("boom")
            self.last_boom = t
        self.rings.append({"x": x, "y": y, "born": t})
        turn = rnd.uniform(0, 2 * math.pi)
        life = lambda: rnd.uniform(1.3, 2.0)
        if s["kind"] == "helm":
            for k in range(7):
                for speed in (3, 5, 7, 9, 11):
                    self._spark(x, y, turn + 2 * math.pi * k / 7, speed, col, t, life())
            for k in range(14):  # a white halo between the rays
                self._spark(x, y, turn + math.pi * k / 7 + math.pi / 14, 6, "white", t, life())
        elif s["kind"] == "peony":
            for k in range(48):
                self._spark(x, y, turn + 2 * math.pi * k / 48 + rnd.uniform(-0.06, 0.06),
                            rnd.uniform(8, 11), rnd.choice(NEON), t, life())
            for k in range(24):  # inner core
                self._spark(x, y, turn + 2 * math.pi * k / 24, rnd.uniform(3, 5), col, t, life())
        elif s["kind"] == "double":
            col2 = rnd.choice([c for c in NEON if c != col])
            for k in range(7):
                a = turn + 2 * math.pi * k / 7
                for speed in (8, 10, 12):
                    self._spark(x, y, a, speed, col, t, life())
                for speed in (3, 5, 7):
                    self._spark(x, y, a + math.pi / 7, speed, col2, t, life())
        elif s["kind"] == "crackle":
            for k in range(28):
                self._spark(x, y, turn + 2 * math.pi * k / 28, rnd.uniform(5, 10), col, t, life(),
                            crackle=rnd.uniform(0.45, 0.8))
        else:  # willow: slow gold sparks, long trails, they hang and droop
            for k in range(36):
                self._spark(x, y, turn + 2 * math.pi * k / 36, rnd.uniform(3, 7),
                            rnd.choice(("orange", "yellow")), t, rnd.uniform(2.4, 3.2),
                            willow=True, trail_len=7)

    def update(self, t, dt, h, w, sound):
        rnd = self.rnd
        if t >= self.next_launch:
            for _ in range(rnd.choice((1, 1, 2, 3))):  # sometimes a whole volley
                self.shells.append({"x": rnd.uniform(0.08, 0.92) * w, "y": h - 1.0,
                                    "vx": rnd.uniform(-3, 3), "vy": -rnd.uniform(24, 32),
                                    "burst": rnd.uniform(0.1, 0.7) * h,  # some go off low
                                    "color": rnd.choice(NEON[:-1]), "kind": rnd.choice(self.KINDS)})
            self.next_launch = t + rnd.uniform(0.25, 0.65)
        for s in self.shells[:]:
            s["x"] += s["vx"] * dt
            s["y"] += s["vy"] * dt
            if rnd.random() < 0.8:  # sparkling tail, falling behind the shell
                self._spark(s["x"], s["y"] + 1, rnd.uniform(0.3, math.pi - 0.3), rnd.uniform(1, 3),
                            rnd.choice(("orange", "yellow")), t, rnd.uniform(0.5, 0.9), tail=True)
            if s["y"] <= s["burst"]:
                self.shells.remove(s)
                self.explode(s, t, sound)
        for p in self.sparks[:]:
            p["trail"] = ([(p["x"], p["y"])] + p["trail"])[:p.get("trail_len", 4)]
            drag = 1 - (2.2 if p.get("willow") else 1.3) * dt
            p["vx"] *= drag
            p["vy"] = p["vy"] * drag + 8 * dt  # air drag + gravity
            p["x"] += p["vx"] * dt
            p["y"] += p["vy"] * dt
            age = t - p["born"]
            burning = not (p.get("tail") or p.get("glitter") or p.get("ember"))
            if burning and rnd.random() < 0.12 and len(self.sparks) < self.MAX_SPARKS:
                self._spark(p["x"], p["y"], math.pi / 2, rnd.uniform(0.5, 1.5),  # falls straight down
                            p["color"], t, rnd.uniform(0.6, 1.1), ember=True)
            if p.get("crackle") and age > p["crackle"]:
                p["crackle"] = None
                for _ in range(4):
                    self._spark(p["x"], p["y"], rnd.uniform(0, 2 * math.pi), rnd.uniform(2, 4),
                                rnd.choice(("white", "yellow")), t, 0.45, glitter=True)
            if age > p["life"] or not (0 <= p["y"] < h and 0 <= p["x"] < w):
                self.sparks.remove(p)
        self.rings = [r for r in self.rings if t - r["born"] < 0.3]

    def draw(self, win, t):
        rnd = self.rnd
        for r in self.rings:  # white shockwave, a heptagon of dots
            radius = 1 + (t - r["born"]) / 0.3 * 5
            for k in range(14):
                a = 2 * math.pi * k / 14
                put(win, int(r["y"] + radius * math.sin(a)), int(r["x"] + 2 * radius * math.cos(a)),
                    "∘", C["white"] | curses.A_BOLD)
            put(win, int(r["y"]), int(r["x"]), "◉", C["white"] | curses.A_BOLD)
        for s in self.shells:
            put(win, int(s["y"]), int(s["x"]), "●", C["white"] | curses.A_BOLD)
        for p in self.sparks:
            frac = (t - p["born"]) / p["life"]
            color = C[p["color"]]
            if p.get("tail") or p.get("glitter") or p.get("ember"):
                if rnd.random() < 0.75:
                    ch = "✧" if p.get("glitter") else "'" if p.get("ember") and frac < 0.5 else "·"
                    put(win, int(p["y"]), int(p["x"]), ch,
                        color | (curses.A_BOLD if frac < 0.6 else curses.A_DIM))
                continue
            for i, (tx, ty) in enumerate(p["trail"]):  # fresh part bright, the rest fading
                put(win, int(ty), int(tx), "·", color | (curses.A_BOLD if i < 2 else curses.A_DIM))
            if frac < 0.08:
                ch, attr = "✸", C["white"] | curses.A_BOLD  # white-hot at the burst
            elif frac < 0.4:
                ch, attr = "✦", color | curses.A_BOLD
            elif frac < 0.65:
                ch, attr = "*", color | curses.A_BOLD
            elif frac < 0.85:
                if rnd.random() < 0.25:  # twinkle
                    continue
                ch, attr = "+", color | curses.A_BOLD
            else:
                if rnd.random() < 0.5:
                    continue
                ch, attr = ".", color
            put(win, int(p["y"]), int(p["x"]), ch, attr)


def draw_rain(win, t):
    h, w = win.getmaxyx()
    for fy, fx, speed in _STARS:
        put(win, int((fy * h + t * 5 * speed) % h), int(fx * (w - 1)), "│", C["blue"] | curses.A_DIM)


def draw_wave(win, y, x, rows, t, attr_at, letter_w):
    """Rows of pixel text, each LETTER bobbing on a sine wave (columns would tear the glyphs)."""
    for c in range(width(rows)):
        dy = round(math.sin(t * 5 + (c // letter_w) * 0.9))
        for r, line in enumerate(rows):
            if c < len(line) and line[c] != " ":
                put(win, y + r + dy, x + c, line[c], attr_at(r, c))


def draw_scroller(win, y, text, t):
    """Demo-scene scroller: big half-block text crawling right to left on a wave."""
    h, w = win.getmaxyx()
    rows = half(text)
    total = width(rows)
    offset = int(t * 22) % (total + w)
    for c in range(w - 1):
        src = c + offset - w
        if 0 <= src < total:
            dy = round(math.sin(t * 4 + (src // 6) * 0.7))  # per letter: half() glyphs are 6 wide
            color = C[RAINBOW[(c // 6 + int(t * 3)) % len(RAINBOW)]] | curses.A_BOLD
            for r, line in enumerate(rows):
                if line[src] != " ":
                    put(win, y + r + dy, c, line[src], color)


def pod_name(domain):
    """kubectl-style name: the domain plus a stable random-looking suffix."""
    n, chars, suffix = zlib.crc32(domain.encode()), "bcdfghjklmnpqrstvwxz2456789", ""
    for _ in range(5):
        n, i = divmod(n, len(chars))
        suffix += chars[i]
    return f"{domain}-{suffix}"


STATUS_COLOR = {"Running": "green", "CrashLoopBackOff": "yellow", "Error": "red"}


def _pods_table(win, y, x, w, pods, t):
    """'kubectl get pods' typed out, then the pods come up one by one."""
    cmd = "$ kubectl get pods -n cka-exam"
    typed = cmd[:max(0, int((t - 0.4) * 28))]
    put(win, y, x, typed.ljust(w), C["white"] | curses.A_BOLD)
    if len(typed) < len(cmd):
        if int(t * 4) % 2:
            put(win, y, x + len(typed), "█", C["white"])
        return
    shown = int((t - 0.4 - len(cmd) / 28) / 0.25)
    head = f"{'NAME':<24}{'READY':<8}{'STATUS':<18}{'RESTARTS':<10}AGE"
    put(win, y + 1, x, head.ljust(w), C["white"] | curses.A_DIM)
    for i, (domain, ready, status, restarts, secs) in enumerate(pods[:max(0, shown)]):
        age = f"{int(secs // 60)}m{int(secs % 60):02d}s"
        put(win, y + 2 + i, x, " " * w)
        put(win, y + 2 + i, x, f"{pod_name(domain):<24}{ready:<8}", C["white"])
        put(win, y + 2 + i, x + 32, f"{status:<18}", C[STATUS_COLOR[status]] | curses.A_BOLD)
        put(win, y + 2 + i, x + 50, f"{restarts:<10}{age}", C["white"])


def finale(win, info, sound):
    """Last screen. info: won, score, percent, pass_percent, solved, total, pods, review.
    Returns when the player presses Enter / q / Esc."""
    init_colors()
    curses.curs_set(0)
    won = info["won"]
    sound.wait_ready(1.5)
    sound.start_music("victory" if won else "gameover")
    fireworks = Fireworks()
    word = "YOU WIN" if won else "GAME OVER"
    if won:
        crawl = (f"CONGRATULATIONS!  SCORE {info['score']}  -  EXAM {info['percent']}%  -  "
                 f"{info['solved']}/{info['total']} QUESTIONS  -  STATUS: RUNNING  -  "
                 f"YOU ARE READY FOR THE CKA  -  THANKS FOR PLAYING CKA MEGATRAINER  -  ")
    else:
        crawl = (f"GAME OVER  -  SCORE {info['score']}  -  EXAM {info['percent']}% / "
                 f"PASS {info['pass_percent']}%  -  KUBECTL ROLLOUT RESTART CANDIDATE  -  "
                 f"REVIEW THE RED PODS AND TRY AGAIN  -  INSERT COIN  -  ")
    curses.flushinp()
    t0 = last = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            t, dt, last = now - t0, now - last, now
            sound.tick()
            h, w = win.getmaxyx()
            win.erase()
            if won:
                draw_stars(win, t)
                fireworks.update(t, dt, h, w, sound)
                fireworks.draw(win, t)
            else:
                draw_rain(win, t)

            is_big = w >= width(big(word)) + 4 and h >= 30
            rows = big(word) if is_big else half(word)
            pal = RAINBOW if won else ("red", "magenta")
            wx = (w - width(rows)) // 2
            for r in range(1, len(rows) + 3):  # sparks pass BEHIND the title
                put(win, r, wx - 1, " " * (width(rows) + 2))
            draw_wave(win, 2, wx, rows, t,
                      lambda r, c: C[pal[(c // 4 + int(t * 6)) % len(pal)]] | curses.A_BOLD,
                      letter_w=12 if is_big else 6)
            y = 2 + len(rows) + 2

            line = (f"SCORE {info['score']}   ·   EXAM {info['percent']}%  (PASS ≥ {info['pass_percent']}%)"
                    f"   ·   {info['solved']}/{info['total']} QUESTIONS SOLVED")
            put(win, y, max(0, (w - len(line)) // 2), line, C["yellow"] | curses.A_BOLD)
            y += 2

            table_w = 66
            with_helm = w >= table_w + HELM_W + 4 and h >= y + HELM_H + 8
            block = table_w + (HELM_W + 3 if with_helm else 0)
            x = max(0, (w - block) // 2)
            if with_helm:
                draw_helm(win, y, x, t * 1.2)
                x += HELM_W + 3
            _pods_table(win, y + 1, x, table_w, info["pods"], t)
            yr = y + 3 + len(info["pods"]) + 1
            if t > 3.0:
                if info["review"]:
                    msg = "TO REVIEW: " + "  ".join(info["review"])
                    put(win, yr, x, msg[:w - x - 1], C["yellow"] | curses.A_BOLD)
                else:
                    put(win, yr, x, "ALL PODS RUNNING. NOTHING TO REVIEW.", C["green"] | curses.A_BOLD)

            if h >= 30:
                draw_scroller(win, h - 7, crawl, t)
            if int(t * 2) % 2 == 0 and t > 1.0:
                prompt = "PRESS ENTER TO EXIT"
                put(win, h - 2, (w - len(prompt)) // 2, prompt, C["magenta"] | curses.A_BOLD)
            win.refresh()

            win.timeout(FRAME_MS)
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r", " ", "q", "Q", "\x1b", curses.KEY_ENTER) and t > 1.0:
                return
    finally:
        sound.stop_music()


# --------------------------------------------------------------------------
# Chiptune synthesis (original composition, NES style)
# --------------------------------------------------------------------------

SR = 22050
BPM = 150
S16 = 60 / BPM / 4  # one sixteenth note, in seconds
SOUND_VERSION = 3

# 8 bars, "note:sixteenths", "." = rest
LEAD = """
A4:2 C5:2 E5:2 A5:4 G5:2 E5:2 C5:2 | F5:3 E5:1 F5:2 A5:2 C6:4 A5:4 |
G5:2 D5:2 G5:2 B5:2 D6:4 B5:2 G5:2 | E5:2 G#5:2 B5:2 E6:6 .:4 |
A5:2 E5:2 A5:2 C6:2 B5:2 A5:2 G5:2 E5:2 | F5:2 A5:2 C6:4 A5:2 F5:2 A5:4 |
G5:2 B5:2 D6:2 G6:4 F6:2 E6:2 D6:2 | E6:4 C6:2 A5:2 A5:6 .:2
"""
BASS_ROOTS = ["A2", "F2", "G2", "E2", "A2", "F2", "G2", "A2"]
SEMITONES = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
             "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def midi(note):
    return SEMITONES[note[:-1]] + 12 * (int(note[-1]) + 1)


def freq(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def parse(score):
    seq = []
    for tok in score.split():
        if tok == "|":
            continue
        note, n = tok.split(":")
        seq.append((None if note == "." else midi(note), int(n)))
    return seq


def square(duty):
    return lambda ph: 1.0 if ph < duty else -1.0


def triangle(ph):
    return 4 * abs(ph - 0.5) - 1


def _voice(buf, seq, wave_fn, vol, unit, decay=None):
    """seq: [(midi or None, units)]; unit in seconds."""
    pos = 0
    for m, units in seq:
        length = int(units * unit * SR)
        if m is not None:
            step = freq(m) / SR
            release = min(300, length // 4)
            for i in range(min(length, len(buf) - pos)):
                env = 1.0 if decay is None else 0.55 + 0.45 * math.exp(-i / (decay * SR))
                if i > length - release:
                    env *= (length - i) / release
                buf[pos + i] += vol * env * wave_fn((i * step) % 1.0)
        pos += length


def _drum(buf, pos, kind, rnd):
    n = int({"kick": 0.12, "snare": 0.12, "hat": 0.03}[kind] * SR)
    phase = 0.0
    for i in range(min(n, len(buf) - pos)):
        if kind == "kick":
            phase += (40 + 100 * math.exp(-i / (0.03 * SR))) / SR
            s = 0.45 * math.sin(2 * math.pi * phase) * (1 - i / n)
        elif kind == "snare":
            s = 0.22 * rnd.uniform(-1, 1) * math.exp(-i / (0.04 * SR))
        else:
            s = 0.08 * rnd.uniform(-1, 1) * math.exp(-i / (0.008 * SR))
        buf[pos + i] += s


# Victory theme (C major, bright) and game-over tune (A minor, slow). Original.
VICTORY = """
G4:2 C5:2 E5:2 G5:6 E5:2 G5:2 | A5:4 G5:2 F5:2 A5:8 |
G5:2 F5:2 E5:2 D5:2 B4:2 D5:2 G5:4 | E5:2 G5:2 C6:12 |
C6:2 B5:2 A5:2 E5:2 A5:4 C6:4 | A5:2 G5:2 F5:2 C5:2 F5:4 A5:4 |
B5:2 C6:2 D6:4 B5:2 G5:2 D6:4 | E6:4 D6:2 C6:2 C6:8
"""
VICTORY_ROOTS = ["C3", "F2", "G2", "C3", "A2", "F2", "G2", "C3"]
GAMEOVER = "E5:4 C5:4 A4:8 | F4:4 A4:4 C5:8 | D5:4 F5:2 E5:2 D5:4 C5:4 | B4:4 G#4:4 E4:8"
GAMEOVER_ROOTS = ["A2", "F2", "D2", "E2"]


def render_song(lead, roots, bpm, loops=2, drums=True):
    s16 = 60 / bpm / 4
    one = int(len(roots) * 16 * s16 * SR)
    buf = [0.0] * one
    _voice(buf, parse(lead), square(0.25), 0.20, s16, decay=0.08)
    bass = [(midi(r) + 12 * (i % 2), 2) for r in roots for i in range(8)]
    _voice(buf, bass, triangle, 0.38, s16)
    rnd = random.Random(1986)
    for bar in range(len(roots) if drums else 0):
        for s in range(16):
            pos = int((bar * 16 + s) * s16 * SR)
            if s in (0, 8, 10):
                _drum(buf, pos, "kick", rnd)
            if s in (4, 12):
                _drum(buf, pos, "snare", rnd)
            if s % 2 == 0:
                _drum(buf, pos, "hat", rnd)
    return buf * loops


def render_theme(loops=2):
    return render_song(LEAD, BASS_ROOTS, BPM, loops)


def render_boom():
    """Firework explosion: low-passed noise with a long decay."""
    rnd, lp, n = random.Random(7), 0.0, int(0.6 * SR)
    out = []
    for i in range(n):
        lp += 0.12 * (rnd.uniform(-1, 1) - lp)
        out.append(2.2 * lp * math.exp(-i / (0.15 * SR)))
    return out


SONGS = {
    "theme": render_theme,
    "victory": lambda: render_song(VICTORY, VICTORY_ROOTS, 132),
    "gameover": lambda: render_song(GAMEOVER, GAMEOVER_ROOTS, 96, drums=False),
}


def render_sfx(notes, duty=0.5, vol=0.3):
    """notes: [(note name, seconds)]."""
    buf = [0.0] * int(sum(d for _, d in notes) * SR + 1)
    _voice(buf, [(midi(n), d) for n, d in notes], square(duty), vol, 1.0)
    return buf


SFX = {
    "move": lambda: render_sfx([("E6", 0.035)]),
    "select": lambda: render_sfx([("C6", 0.05), ("E6", 0.05), ("G6", 0.05), ("C7", 0.2)], 0.25),
    # start of a question: triplet pickup, then up to the octave
    "level": lambda: render_sfx([("G4", 0.08), ("G4", 0.08), ("G4", 0.08), ("C5", 0.24),
                                 ("E5", 0.08), ("G5", 0.08), ("C6", 0.12), ("G5", 0.08),
                                 ("C6", 0.45)], 0.25),
    # step answered
    "win": lambda: render_sfx([("C6", 0.06), ("G6", 0.06), ("C7", 0.18)], 0.25),
    "lose": lambda: render_sfx([("E4", 0.12), ("D#4", 0.12), ("D4", 0.12), ("C#4", 0.4)], 0.5, 0.25),
    # question finished
    "clear": lambda: render_sfx([("C5", 0.09), ("E5", 0.09), ("G5", 0.09), ("C6", 0.09),
                                 ("E6", 0.09), ("G6", 0.27), ("E6", 0.09), ("G6", 0.6)], 0.25),
    "fail": lambda: render_sfx([("G4", 0.15), ("F4", 0.15), ("E4", 0.15), ("D4", 0.15),
                                ("C4", 0.7)], 0.5, 0.25),
    "boom": render_boom,
}


def write_wav(path, samples):
    data = array.array("h", (int(max(-1.0, min(1.0, s)) * 0.8 * 32767) for s in samples))
    if sys.byteorder == "big":
        data.byteswap()
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(data.tobytes())
    tmp.replace(path)


def sound_dir():
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "cka-megatrainer" / f"sound-v{SOUND_VERSION}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.mkdtemp(prefix="cka-sound-"))


def build_sounds(directory):
    """Render every sound once and cache it. Returns {name: path}."""
    files = {}
    for name, make in [*SONGS.items(), *SFX.items()]:
        path = directory / f"{name}.wav"
        if not path.exists():
            write_wav(path, make())
        files[name] = path
    return files


def find_player():
    for cmd in (["pw-play"], ["paplay"], ["aplay", "-q"], ["afplay"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


class Sound:
    """Background music + sound effects. Every failure degrades to silence."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.player = find_player() if enabled else None
        self.files = {}
        self.music = None
        self.music_started = 0.0
        self.quick_failures = 0
        self.want_music = False
        self.music_on = True
        self.track = "theme"
        self.ready = threading.Event()
        if self.player:
            threading.Thread(target=self._prepare, daemon=True).start()
        atexit.register(self._kill_music)

    def _prepare(self):
        try:
            self.files = build_sounds(sound_dir())
            self.ready.set()
        except OSError:
            self.player = None

    @property
    def status(self):
        if not self.enabled:
            return "SOUND OFF"
        if not self.player:
            return "NO AUDIO"
        return "MUSIC ON" if self.music_on else "MUSIC OFF"

    def _spawn(self, name):
        if not (self.player and self.ready.is_set() and name in self.files):
            return None
        try:
            return subprocess.Popen(self.player + [str(self.files[name])],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            self.player = None
            return None

    def play(self, name):
        self._spawn(name)

    def wait_ready(self, timeout):
        if self.player:
            self.ready.wait(timeout)

    def tick(self):
        """Call every frame: (re)starts the music loop when wanted."""
        if not (self.want_music and self.music_on):
            return
        if self.music and self.music.poll() is None:
            return
        if self.music:  # it ended: a loop, or the player failing right away
            ran = time.monotonic() - self.music_started
            self.quick_failures = self.quick_failures + 1 if ran < 2 else 0
            if self.quick_failures >= 3:
                self.music_on = False
                return
        self.music = self._spawn(self.track)
        self.music_started = time.monotonic()

    def start_music(self, track="theme"):
        if track != self.track:
            self._kill_music()
            self.track, self.quick_failures = track, 0
        self.want_music = True
        self.tick()

    def stop_music(self):
        self.want_music = False
        self._kill_music()

    def toggle_music(self):
        self.music_on = not self.music_on
        if self.music_on:
            self.quick_failures = 0
            self.tick()
        else:
            self._kill_music()

    def _kill_music(self):
        if self.music and self.music.poll() is None:
            self.music.terminate()
        self.music = None
