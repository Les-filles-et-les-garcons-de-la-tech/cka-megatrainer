#!/usr/bin/env python3
"""Render the promo video (docs/media/promo.mp4): about 35 s, 1080x1080, with the game's own music.

    python3 tools/promo_video.py [--short] [--out FILE] [--cache FILE] [--stills DIR]

The real game runs in tmux and is driven by keystrokes (like tools/readme_media.py). Its screen is
captured about 25 times a second, drawn with the VT323 font (box and block characters as
rectangles, as a terminal does) and encoded by ffmpeg, which also mixes in the sounds the game
synthesises itself (~/.cache/cka-megatrainer, filled the first time the game plays).

--cache FILE   keep the raw captures: re-run with the same file to change captions, speeds or the
               mix without playing the game again
--stills DIR   also save a PNG of every scene, to check the layout quickly

Needs tmux, ffmpeg, Pillow (pip install pillow) and the VT323 font (make start-exam installs it).
"""
import argparse
import bisect
import pickle
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is needed: pip install pillow")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import ansi2svg  # noqa: E402

COLS, ROWS = 100, 38                      # the game's intended size
VW, VH, FPS = 1080, 1080, 30
TOP, BOTTOM = 48, 120                     # title band and caption band
CW, CH = VW / COLS, (VH - TOP - BOTTOM) / ROWS
CAPTURE_FPS = 25
SOUNDS = Path.home() / ".cache" / "cka-megatrainer" / "sound-v3"
FONT_DIRS = [Path.home() / "Library/Fonts", Path.home() / ".local/share/fonts/cka-megatrainer",
             Path("/Library/Fonts"), Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]
SYMBOL_FONTS = ["/System/Library/Fonts/Apple Symbols.ttf", "/System/Library/Fonts/Menlo.ttc",
                "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
ANSI = re.compile(r"\x1b\[[0-9;:]*m")
WHITE, CYAN, YELLOW, GREEN, MAGENTA, RED = ((255, 255, 255), (95, 255, 255), (255, 255, 95),
                                            (95, 255, 95), (255, 95, 255), (255, 80, 80))


def find_font(name):
    for d in FONT_DIRS:
        if d.is_dir():
            for p in d.rglob(name):
                return str(p)
    sys.exit(f"font {name} not found: run `make start-exam` once (it installs the retro fonts)")


# --------------------------------------------------------------------------
# Recording: play the game in tmux and capture its screen
# --------------------------------------------------------------------------

class Rec:
    def __init__(self, *args):
        self.sock = f"cka-promo-{time.monotonic_ns()}"
        cmd = f"LANG=C.UTF-8 python3 cka_trainer.py --silent --allow-paste {' '.join(args)}"
        self.tmux("new-session", "-d", "-x", str(COLS), "-y", str(ROWS), "-c", str(ROOT), cmd)
        self.frames, self.t0, self.running = [], time.monotonic(), True
        self.thread = threading.Thread(target=self._capture, daemon=True)
        self.thread.start()

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], check=True, capture_output=True,
                              text=True).stdout

    def _capture(self):
        n = 0
        while self.running:
            t = time.monotonic() - self.t0
            try:
                text = self.tmux("capture-pane", "-p", "-e", "-N")
                cur = self.tmux("display-message", "-p", "#{cursor_x} #{cursor_y} #{cursor_flag}").split()
            except subprocess.CalledProcessError:
                return
            self.frames.append((t, text, int(cur[0]), int(cur[1]), cur[2] == "1"))
            n += 1
            time.sleep(max(0.0, self.t0 + n / CAPTURE_FPS - time.monotonic()))

    def now(self):
        return time.monotonic() - self.t0

    def sleep(self, seconds):
        time.sleep(seconds)

    def keys(self, *keys, wait=0.15):
        self.tmux("send-keys", *keys)
        time.sleep(wait)

    def type(self, text, cps=22):
        for ch in text:
            self.tmux("send-keys", "-l", "--", ch)
            time.sleep(1 / cps)

    def screen(self):
        return ANSI.sub("", self.frames[-1][1]) if self.frames else ""

    def wait_for(self, text, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if text in self.screen():
                return self.now()
            time.sleep(0.05)
        raise SystemExit(f"timed out waiting for {text!r}; the screen shows:\n{self.screen()}")

    def stop(self):
        self.running = False
        self.thread.join(5)
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], capture_output=True)
        return self.frames


class Clip:
    """The captured frames of one game session (picklable: no tmux handle)."""

    def __init__(self, frames):
        self.frames = frames
        self.times = [f[0] for f in frames]

    def at(self, t):
        return self.frames[max(0, bisect.bisect_right(self.times, t) - 1)]


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

BOX = {"─": "lr", "│": "ud", "┌": "rd", "┐": "ld", "└": "ru", "┘": "lu", "├": "urd", "┤": "uld",
       "┬": "lrd", "┴": "lru", "┼": "lrud"}
SHADE = {"░": .28, "▒": .5, "▓": .75}


class Painter:
    def __init__(self):
        probe = ImageFont.truetype(find_font("VT323-Regular.ttf"), 100)
        size = CW / (probe.getlength("M") / 100)  # the size whose advance is exactly one cell
        self.vt = ImageFont.truetype(find_font("VT323-Regular.ttf"), size)
        self.fallbacks = [ImageFont.truetype(p, int(CH * .8)) for p in SYMBOL_FONTS if Path(p).exists()]
        self.notdef = bytes(self.vt.getmask("￿"))
        self.tw, self.th, self.base = int(CW) + 4, int(CH) + 4, CH * .78 + 2
        self.masks, self.big = {}, {}
        self.last_key, self.last_img = None, None

    def font_for(self, ch):
        if bytes(self.vt.getmask(ch)) != self.notdef or not self.fallbacks:
            return self.vt
        for f in self.fallbacks:
            if bytes(f.getmask(ch)) != bytes(f.getmask("￿")):
                return f
        return self.vt

    def mask(self, ch, bold):
        key = (ch, bold)
        if key not in self.masks:
            m = Image.new("L", (self.tw, self.th), 0)
            d = ImageDraw.Draw(m)
            f = self.font_for(ch)
            d.text((self.tw / 2, self.base), ch, font=f, fill=255, anchor="ms")
            if bold:
                d.text((self.tw / 2 + .7, self.base), ch, font=f, fill=255, anchor="ms")
            self.masks[key] = m
        return self.masks[key]

    def terminal(self, frame, blink):
        """The game's screen as an image of VW x (ROWS * CH)."""
        _, text, cx, cy, cursor = frame
        key = (text, cx, cy, cursor and blink)
        if key == self.last_key:
            return self.last_img
        rows = ansi2svg.parse(text, COLS)
        img = Image.new("RGB", (VW, round(ROWS * CH)), (0, 0, 0))
        d = ImageDraw.Draw(img)
        for y, cells in enumerate(rows[:ROWS]):
            y0 = round(y * CH)
            y1 = round((y + 1) * CH)
            for x, (ch, fg, bg, bold, dim) in enumerate(cells):
                x0, x1 = round(x * CW), round((x + 1) * CW)
                color = tuple(fg or ansi2svg.DEFAULT_FG)
                if dim:
                    color = tuple(c // 2 for c in color)
                if bg and tuple(bg) != (0, 0, 0):
                    d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=tuple(bg))
                self.cell(img, d, ch, color, bold, x, y, x0, y0, x1, y1)
        if cursor and blink and cy < ROWS and cx < COLS:
            x0, x1, y0, y1 = round(cx * CW), round((cx + 1) * CW), round(cy * CH), round((cy + 1) * CH)
            d.rectangle([x0, y0 + 2, x1 - 1, y1 - 2], fill=(240, 240, 240))
        self.last_key, self.last_img = key, img
        return img

    def cell(self, img, d, ch, color, bold, x, y, x0, y0, x1, y1):
        if ch == " ":
            return
        if ch in ansi2svg.BLOCKS:
            bx, by, bw, bh = ansi2svg.BLOCKS[ch]
            d.rectangle([x0 + bx * (x1 - x0), y0 + by * (y1 - y0), x0 + (bx + bw) * (x1 - x0) - 1,
                         y0 + (by + bh) * (y1 - y0) - 1], fill=color)
        elif ch in SHADE:
            d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=tuple(int(c * SHADE[ch]) for c in color))
        elif ch in BOX:
            cx, cy, w = (x0 + x1) // 2, (y0 + y1) // 2, BOX[ch]
            for side, rect in (("l", [x0, cy - 1, cx + 1, cy]), ("r", [cx - 1, cy - 1, x1 - 1, cy]),
                               ("u", [cx - 1, y0, cx, cy + 1]), ("d", [cx - 1, cy - 1, cx, y1 - 1])):
                if side in w:
                    d.rectangle(rect, fill=color)
        else:
            tile = self.mask(ch, bold)
            img.paste(color, (round((x0 + x1) / 2 - self.tw / 2), y0 - 2), tile)


def text_width(font, s):
    return font.getlength(s)


class Caption:
    def __init__(self, main, sub="", color=YELLOW, sub_color=WHITE):
        self.main, self.sub, self.color, self.sub_color, self.start = main, sub, color, sub_color, None


class Overlay:
    """Title band on top, typed captions below."""

    def __init__(self):
        path = find_font("VT323-Regular.ttf")
        self.f_main, self.f_sub, self.f_brand = (ImageFont.truetype(path, s) for s in (62, 34, 30))

    def draw(self, img, caption, t):
        d = ImageDraw.Draw(img)
        d.text((16, TOP / 2), "CKA MEGATRAINER", font=self.f_brand, fill=YELLOW, anchor="lm")
        d.text((VW - 16, TOP / 2), "KUBERNETES v1.37", font=self.f_brand, fill=CYAN, anchor="rm")
        d.rectangle([0, TOP - 2, VW, TOP - 1], fill=MAGENTA)
        y0 = VH - BOTTOM
        d.rectangle([0, y0, VW, y0 + 1], fill=CYAN)
        if not caption or caption.start is None:
            return
        shown = int(max(0, t - caption.start) * 45)
        main, sub = caption.main[:shown], caption.sub[:max(0, shown - len(caption.main))]
        d.text((VW / 2, y0 + 46), main, font=self.f_main, fill=caption.color, anchor="mm")
        d.text((VW / 2, y0 + 96), sub, font=self.f_sub, fill=caption.sub_color, anchor="mm")


# --------------------------------------------------------------------------
# The film: pieces of recorded sessions, played at their own speed
# --------------------------------------------------------------------------

class Piece:
    def __init__(self, clip, t0, t1, speed=1.0, caption=None, sfx=()):
        self.clip, self.t0, self.t1, self.speed, self.caption = clip, t0, t1, speed, caption
        self.sfx = list(sfx)  # (time in the session, sound name, volume)
        self.duration = (t1 - t0) / speed
        self.start = 0.0
        self.fade_in = self.fade_out = 0.0


def lay_out(pieces):
    t, prev_caption = 0.0, None
    for i, p in enumerate(pieces):
        p.start = t
        t += p.duration
        p.fade_in = 0.25 if i == 0 else (0.12 if pieces[i - 1].clip is not p.clip else 0.0)
        p.fade_out = 0.7 if i == len(pieces) - 1 else (0.12 if pieces[i + 1].clip is not p.clip else 0.0)
        if p.caption is not prev_caption and p.caption is not None:
            p.caption.start = p.start + 0.15
        prev_caption = p.caption
    return t


def audio_plan(pieces, total, win_start):
    """ffmpeg input files and filter graph: theme, victory fanfare, and the game's sound effects."""
    names = [("theme.wav", None)]
    cmd_inputs = ["-stream_loop", "-1", "-i", str(SOUNDS / "theme.wav"), "-i", str(SOUNDS / "victory.wav")]
    filt = [f"[1:a]atrim=0:{win_start + 0.5:.2f},afade=t=out:st={win_start - 0.9:.2f}:d=1.4,volume=0.5[th]",
            f"[2:a]atrim=0:{total - win_start:.2f},afade=t=out:st={total - win_start - 1.4:.2f}:d=1.4,"
            f"adelay={int(win_start * 1000)}|{int(win_start * 1000)},volume=0.85[vi]"]
    labels, n = ["[th]", "[vi]"], 3
    for p in pieces:
        for when, name, vol in p.sfx:
            out_t = p.start + (when - p.t0) / p.speed
            cmd_inputs += ["-i", str(SOUNDS / f"{name}.wav")]
            filt.append(f"[{n}:a]adelay={int(out_t * 1000)}|{int(out_t * 1000)},volume={vol}[s{n}]")
            labels.append(f"[s{n}]")
            n += 1
    filt.append("".join(labels) + f"amix=inputs={n - 1}:normalize=0:duration=longest,"
                f"afade=t=out:st={total - 0.8:.2f}:d=0.8,alimiter=limit=0.95[a]")
    return cmd_inputs, ";".join(filt), names


def render(pieces, out, stills=None):
    total = lay_out(pieces)
    win_start = pieces[-1].start
    painter, overlay = Painter(), Overlay()
    audio_inputs, graph, _ = audio_plan(pieces, total, win_start)
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{VW}x{VH}",
         "-r", str(FPS), "-i", "-", *audio_inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k", str(out)], stdin=subprocess.PIPE)
    n, black = round(total * FPS), Image.new("RGB", (VW, VH), (0, 0, 0))
    still_at = {round((p.start + p.duration * .6) * FPS): i for i, p in enumerate(pieces)} if stills else {}
    pi = 0
    for i in range(n):
        t = i / FPS
        while pi + 1 < len(pieces) and t >= pieces[pi + 1].start:
            pi += 1
        p = pieces[pi]
        local = t - p.start
        frame = p.clip.at(p.t0 + local * p.speed)
        canvas = Image.new("RGB", (VW, VH), (0, 0, 0))
        canvas.paste(painter.terminal(frame, int(t * 2.5) % 2 == 0), (0, TOP))
        overlay.draw(canvas, p.caption, t)
        fade = 1.0
        if p.fade_in and local < p.fade_in:
            fade = local / p.fade_in
        if p.fade_out and p.duration - local < p.fade_out:
            fade = min(fade, (p.duration - local) / p.fade_out)
        if fade < 1:
            canvas = Image.blend(black, canvas, max(0.0, fade))
        if i in still_at:
            Path(stills).mkdir(parents=True, exist_ok=True)
            canvas.save(Path(stills) / f"{still_at[i]:02d}.png")
        ff.stdin.write(canvas.tobytes())
    ff.stdin.close()
    if ff.wait():
        sys.exit("ffmpeg failed")
    print(f"{out}  {total:.1f} s  {out.stat().st_size // 1024} KB")


# --------------------------------------------------------------------------
# The script of the video
# --------------------------------------------------------------------------

def play():
    """Play the scenes for real; returns {name: (Clip, marks)}."""
    scenes = {}

    r = Rec()  # 1. title screen
    r.sleep(1.0)
    r.sleep(2.2)
    r.keys("Down")
    r.sleep(1.3)
    r.keys("Down")
    r.sleep(1.0)
    scenes["title"] = (Clip(r.stop()), {})

    r = Rec("--id", "TRB-001")  # 2. the countdown, then a fast typed answer
    marks = {"level": 0.0, "question": r.wait_for("Step 1", 20)}
    r.sleep(33)  # the bar drains from green to red: shown in time-lapse
    marks["type"] = r.now()
    r.type("kubectl describe pod cache", cps=20)
    r.sleep(0.25)
    marks["submit"] = r.now()
    r.keys("Enter", wait=0.2)
    r.wait_for("continue")
    r.sleep(1.7)
    marks["end"] = r.now()
    scenes["countdown"] = (Clip(r.stop()), marks)

    r = Rec("--id", "STO-001")  # 3. an edit step
    r.wait_for("Step 1", 20)
    r.sleep(0.4)
    r.type("kubectl describe pvc data-pvc", cps=30)
    r.keys("Enter", wait=0.3)
    r.wait_for("continue")
    r.keys("Enter", wait=0.3)
    marks = {"edit": r.wait_for("Options: type one", 20)}
    r.sleep(1.4)
    marks["edits"] = r.now()
    r.keys("Up", "Up", "Up", "End", wait=0.35)
    r.keys("BSpace", "BSpace", "BSpace", "BSpace", wait=0.1)
    r.type("Once", cps=9)
    r.sleep(0.5)
    r.keys("Down", "Down", "Down", "End", wait=0.35)
    r.keys("BSpace", "BSpace", "BSpace", "BSpace", wait=0.1)
    r.type("10Gi", cps=9)
    r.sleep(0.8)
    marks["submit"] = r.now()
    r.keys("C-d", wait=0.3)
    r.wait_for("continue")
    r.sleep(1.5)
    marks["end"] = r.now()
    scenes["edit"] = (Clip(r.stop()), marks)

    r = Rec("--learn", "--id", "CLU-002")  # 4. learn mode: the answer is marked, a trap is explained
    marks = {"question": r.wait_for("Step 1", 20)}
    r.sleep(2.6)
    marks["type"] = r.now()
    r.type("kubectl cordon node01", cps=20)
    r.sleep(0.3)
    marks["submit"] = r.now()
    r.keys("Enter", wait=0.2)
    r.wait_for("continue")
    marks["page"] = r.now()
    r.sleep(2.0)
    r.keys("PageDown", wait=2.0)
    r.keys("PageDown", wait=1.6)
    marks["end"] = r.now()
    scenes["learn"] = (Clip(r.stop()), marks)

    r = Rec("--id", "CLU-002")  # 5. a perfect run: speed-run, then YOU WIN
    marks = {"start": r.wait_for("Step 1", 20)}
    r.sleep(0.5)
    marks["answers"] = []
    for answer in ("kubectl drain node01 --ignore-daemonsets --delete-emptydir-data",
                   "sudo kubeadm upgrade node", "kubectl uncordon node01"):
        r.type(answer, cps=28)
        marks["answers"].append(r.now())
        r.keys("Enter", wait=0.3)
        r.wait_for("continue")
        r.sleep(0.5)
        marks.setdefault("pages", []).append(r.now())
        r.keys("Enter", wait=0.3)
    r.wait_for("full solution", 20)
    r.sleep(0.8)
    marks["recap"] = r.now()
    r.keys("Enter", wait=0.4)
    marks["win"] = r.now()
    r.sleep(12.5)
    marks["end"] = r.now()
    scenes["win"] = (Clip(r.stop()), marks)
    return scenes


def film(scenes, short=False):
    cap = {
        "title": Caption("Kubernetes. Arcade style.", "Train for the CKA exam"),
        "level": Caption("100 real exam tasks", "weighted like the real CKA", CYAN),
        "clock": Caption("Beat the clock.", "green, then yellow, then red", RED),
        "type": Caption("You type. You don't click.", "no copy/paste, no multiple choice", GREEN),
        "edit": Caption("Real YAML. Real indentation.", "edit the file: all of it is checked", CYAN),
        "learn": Caption("LEARN mode", "the right answer is marked", GREEN),
        "trap": Caption("Wrong? Every trap is explained.", "plus links to the Kubernetes docs", YELLOW),
        "score": Caption("Pass mark: 66%", "scored like the real exam", MAGENTA),
        "win": Caption("YOU WIN!", "github.com/Les-filles-et-les-garcons-de-la-tech/cka-megatrainer",
                       GREEN, CYAN),
    }
    c, m = scenes["title"][0], {}
    pieces = [Piece(c, 0.3, 3.8 if short else c.frames[-1][0], 1.0, cap["title"])]

    c, m = scenes["countdown"]
    pieces += [Piece(c, 0.0, m["question"], 1.0, cap["level"], [(0.2, "level", 0.9)]),
               Piece(c, m["question"], m["type"], 11.0, cap["clock"]),
               Piece(c, m["type"], m["submit"] + 0.3, 1.0, cap["type"]),
               Piece(c, m["submit"] + 0.3, m["end"], 1.0, cap["type"], [(m["submit"], "win", 0.9)])]

    c, m = scenes["edit"]
    pieces += [Piece(c, m["edit"] - 0.2, m["edits"], 1.0, cap["edit"]),
               Piece(c, m["edits"], m["submit"], 1.3, cap["edit"]),
               Piece(c, m["submit"], m["end"], 1.0, cap["edit"], [(m["submit"], "win", 0.9)])]

    c, m = scenes["learn"]
    pieces += [Piece(c, m["question"], m["type"], 1.0, cap["learn"]),
               Piece(c, m["type"], m["submit"] + 0.3, 1.0, cap["trap"]),
               Piece(c, m["submit"] + 0.3, m["page"] + 3.2 if short else m["end"], 1.0, cap["trap"],
                     [(m["submit"], "lose", 0.9)])]

    c, m = scenes["win"]
    sfx = [(t, "win", 0.7) for t in m["answers"]]
    pieces += [Piece(c, m["start"], m["recap"], 2.6, cap["score"], sfx + [(m["recap"] - 0.4, "clear", 0.8)]),
               Piece(c, m["win"], m["win"] + 9.0 if short else m["end"] - 1.5, 1.0, cap["win"])]
    return pieces


def load_cache(path):
    """Captures pickled by a run of this script (as __main__) load from an import too."""
    class Loader(pickle.Unpickler):
        def find_class(self, module, name):
            return getattr(sys.modules[__name__], name) if module == "__main__" else super().find_class(module, name)
    with open(path, "rb") as f:
        return Loader(f).load()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "media" / "promo.mp4")
    ap.add_argument("--short", action="store_true", help="about 38 s instead of 45 s")
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--stills", type=Path)
    args = ap.parse_args()
    for tool in ("tmux", "ffmpeg"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found")
    if not (SOUNDS / "theme.wav").exists():
        sys.exit("play the game once first (python3 cka_trainer.py): it synthesises the sounds")
    if args.cache and args.cache.exists():
        scenes = load_cache(args.cache)
    else:
        scenes = play()
        if args.cache:
            args.cache.write_bytes(pickle.dumps(scenes))
    render(film(scenes, args.short), args.out, args.stills)


if __name__ == "__main__":
    main()
