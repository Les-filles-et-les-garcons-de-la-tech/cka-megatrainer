#!/usr/bin/env python3
"""Turn tmux captures (`capture-pane -p -e`) into SVG screenshots for the README.

    ansi2svg.py OUT.svg FRAME.ans                 # static screenshot
    ansi2svg.py OUT.svg --fps 8 F1.ans F2.ans ... # animated SVG (CSS, loops forever)

Block characters are drawn as rectangles (crisp pixel art in any font); text runs are
stretched to their exact cell width so columns stay aligned. Python stdlib only.
"""
import argparse
import html
import re
from pathlib import Path

CW, CH, FONT = 9, 18, 15  # cell width / height, font size (px)
DEFAULT_FG, DEFAULT_BG = (204, 204, 204), (0, 0, 0)
BASIC = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200),
         (188, 63, 188), (17, 168, 205), (229, 229, 229),
         (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67), (59, 142, 234),
         (214, 112, 214), (41, 184, 219), (255, 255, 255)]
# block character -> (x, y, w, h) as fractions of the cell
BLOCKS = {"█": (0, 0, 1, 1), "▀": (0, 0, 1, .5), "▄": (0, .5, 1, .5),
          "▌": (0, 0, .5, 1), "▐": (.5, 0, .5, 1)}
SGR = re.compile(r"\x1b\[([0-9;:]*)m")


def xterm256(n):
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return steps[n // 36], steps[n // 6 % 6], steps[n % 6]
    v = 8 + 10 * (n - 232)
    return v, v, v


def parse(text, width=None):
    """-> rows of cells (char, fg, bg, bold, dim), clipped to width (the pane's columns:
    tmux records curses' TAB cursor jumps, which may point past the right edge)."""
    rows = []
    st = {"fg": None, "bg": None, "bold": False, "dim": False, "rev": False}
    for line in text.split("\n"):
        cells, pos = [], 0
        for m in list(SGR.finditer(line)) + [None]:
            chunk = line[pos:m.start() if m else len(line)]
            for ch in chunk:
                if ch == "\t":  # tmux keeps the cursor jumps ncurses made with TAB
                    cells += [(" ", None, st["bg"], False, False)] * (8 - len(cells) % 8)
                    continue
                fg, bg = st["fg"], st["bg"]
                if st["rev"]:
                    fg, bg = bg or DEFAULT_BG, fg or DEFAULT_FG
                cells.append((ch, fg, bg, st["bold"], st["dim"]))
            if not m:
                break
            pos = m.end()
            codes = [int(c) if c else 0 for c in re.split("[;:]", m.group(1))] or [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    st.update(fg=None, bg=None, bold=False, dim=False, rev=False)
                elif c == 1:
                    st["bold"] = True
                elif c == 2:
                    st["dim"] = True
                elif c == 22:
                    st["bold"] = st["dim"] = False
                elif c == 7:
                    st["rev"] = True
                elif c == 27:
                    st["rev"] = False
                elif 30 <= c <= 37 or 90 <= c <= 97:
                    st["fg"] = BASIC[c - 30 if c < 90 else c - 82]
                elif 40 <= c <= 47 or 100 <= c <= 107:
                    st["bg"] = BASIC[c - 40 if c < 100 else c - 92]
                elif c == 39:
                    st["fg"] = None
                elif c == 49:
                    st["bg"] = None
                elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                    st["fg" if c == 38 else "bg"] = xterm256(codes[i + 2])
                    i += 2
                elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                    st["fg" if c == 38 else "bg"] = tuple(codes[i + 2:i + 5])
                    i += 4
                i += 1
        rows.append(cells[:width] if width else cells)
    while rows and not any(ch.strip() or bg for ch, _, bg, _, _ in rows[-1]):
        rows.pop()
    return rows


def rgb(c):
    return "#%02x%02x%02x" % c


def color_of(fg, dim):
    fg = fg or DEFAULT_FG
    if dim:  # dim = halfway to the background
        fg = tuple((a + b) // 2 for a, b in zip(fg, DEFAULT_BG))
    return rgb(fg)


def frame_svg(rows, cols):
    """SVG elements for one screen."""
    out = []
    for y, cells in enumerate(rows):
        cells = cells[:cols]
        x = 0
        while x < len(cells):  # backgrounds, merged by run
            bg = cells[x][2]
            end = x
            while end < len(cells) and cells[end][2] == bg:
                end += 1
            if bg and bg != DEFAULT_BG:
                out.append(f'<rect x="{x * CW}" y="{y * CH}" width="{(end - x) * CW}" height="{CH}" fill="{rgb(bg)}"/>')
            x = end
        x = 0
        while x < len(cells):  # glyphs, merged by run of same style
            ch, fg, _, bold, dim = cells[x]
            if ch == " ":
                x += 1
                continue
            is_block = ch in BLOCKS
            end = x
            while (end < len(cells) and cells[end][1:2] + cells[end][3:] == (fg, bold, dim)
                   and (cells[end][0] == ch if is_block else cells[end][0] not in BLOCKS)):
                end += 1
            col = color_of(fg, dim)
            if is_block:
                bx, by, bw, bh = BLOCKS[ch]
                out.append(f'<rect x="{x * CW + bx * CW:g}" y="{y * CH + by * CH:g}" '
                           f'width="{(end - x - 1 + bw) * CW:g}" height="{bh * CH:g}" fill="{col}"/>')
            else:
                text = "".join(c[0] for c in cells[x:end]).rstrip()
                n = len(text)
                weight = ' font-weight="bold"' if bold else ""
                out.append(f'<text x="{x * CW}" y="{y * CH + CH * 0.78:g}" fill="{col}"{weight} '
                           f'textLength="{n * CW}" lengthAdjust="spacingAndGlyphs">{html.escape(text)}</text>')
            x = end
    return out


def build(frames, fps):
    cols = max(len(r) for f in frames for r in f)
    lines = max(len(f) for f in frames)
    w, h = cols * CW, lines * CH
    head = [f'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{w + 16}" height="{h + 16}" '
            f'viewBox="-8 -8 {w + 16} {h + 16}">',
            "<style>text{font-family:'DejaVu Sans Mono','Menlo','Consolas',monospace;"
            f"font-size:{FONT}px;white-space:pre}}</style>",
            f'<rect x="-8" y="-8" width="{w + 16}" height="{h + 16}" rx="8" fill="{rgb(DEFAULT_BG)}"/>']
    if len(frames) == 1:
        return "\n".join(head + frame_svg(frames[0], cols) + ["</svg>"])
    n, period = len(frames), len(frames) / fps
    head.append(f"<style>.f{{visibility:hidden;animation:show {period:g}s step-end infinite}}"
                f"@keyframes show{{0%{{visibility:visible}}{100 / n:.4f}%{{visibility:hidden}}}}</style>")
    body = []
    for i, f in enumerate(frames):
        body.append(f'<g class="f" style="animation-delay:{i / fps:.3f}s">')
        body += frame_svg(f, cols)
        body.append("</g>")
    return "\n".join(head + body + ["</svg>"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--fps", type=float, default=8)
    ap.add_argument("--cols", type=int, help="terminal width (clips stray TAB padding)")
    args = ap.parse_args()
    frames = [parse(Path(f).read_text(encoding="utf-8", errors="replace"), args.cols)
              for f in args.frames]
    Path(args.out).write_text(build(frames, args.fps), encoding="utf-8")


if __name__ == "__main__":
    main()
