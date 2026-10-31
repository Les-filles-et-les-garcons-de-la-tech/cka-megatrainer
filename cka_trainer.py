#!/usr/bin/env python3
"""CKA Megatrainer - a timed, type-your-answer quiz for the CKA exam.

Each question is a tree of steps. On a "command" step you pick an option by
typing it in full before the countdown ends; on an "edit" step you fix a file
in a multiline editor. Python 3 standard library only.
"""
import argparse
import curses
import difflib
import json
import os
import random
import re
import sys
import textwrap
import time
from pathlib import Path

import retro

os.environ.setdefault("ESCDELAY", "25")

DEFAULT_DB = Path(__file__).with_name("questions.json")
MAX_TIME = 120
PASS_PERCENT = 66  # CKA passing score
EXAM_MIN, EXAM_MAX = 15, 20  # the CKA has 15-20 tasks
BASE_POINTS = 100
SPEED_BONUS = 50
LABELS = "ABCDEFGH"
MIN_W, MIN_H = 70, 20

PASTE_BURST = 4        # keys arriving together = pasted, not typed
PASTE_QUARANTINE = 0.3  # seconds during which the tail of a paste is still dropped
PASTE_START, PASTE_END = -200, -201  # bracketed-paste markers (ints: the editor ignores them)

A = {}  # curses attributes, filled by init_colors()
SETTINGS = {"allow_paste": False, "silent": False}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def as_text(value):
    return "\n".join(value) if isinstance(value, list) else (value or "")


def load_db(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    domains = {d["id"]: d for d in data["domains"]}
    seen = set()
    for q in data["questions"]:
        validate_question(q, domains, seen)
    return domains, data["questions"]


def validate_question(q, domains, seen):
    qid = q.get("id")
    if not qid:
        raise ValueError("a question has no 'id'")

    def fail(msg):
        raise ValueError(f"{qid}: {msg}")

    if qid in seen:
        fail("duplicate id")
    seen.add(qid)
    for key in ("domain", "title", "scenario", "start", "steps"):
        if key not in q:
            fail(f"missing '{key}'")
    if q["domain"] not in domains:
        fail(f"unknown domain '{q['domain']}'")
    steps = q["steps"]
    if q["start"] not in steps:
        fail(f"start step '{q['start']}' does not exist")
    for sid, s in steps.items():
        if not s.get("prompt"):
            fail(f"step {sid}: missing 'prompt'")
        if min(s.get("time_limit", MAX_TIME), MAX_TIME) <= 0:
            fail(f"step {sid}: time_limit must be positive")
        if s.get("type") not in ("command", "edit"):
            fail(f"step {sid}: unknown type {s.get('type')!r}")
        opts = s.get("options", [])
        if len(opts) < 2 or len(opts) > len(LABELS):
            fail(f"step {sid}: needs 2..{len(LABELS)} options")
        correct = [o for o in opts if o.get("correct")]
        if len(correct) != 1:
            fail(f"step {sid}: needs exactly one correct option")
        for o in opts:
            if not o.get("correct") and not o.get("why"):
                fail(f"step {sid}: wrong option without 'why': {o.get('cmd', o.get('snippet'))}")
        if s["type"] == "command":
            forms = [norm_cmd(c) for o in opts for c in [o["cmd"], *o.get("aliases", [])]]
            if len(forms) != len(set(forms)):
                fail(f"step {sid}: two options/aliases are identical")
            nxt = correct[0].get("next")
        else:
            validate_edit_step(s, lambda msg: fail(f"step {sid}: {msg}"))
            nxt = s.get("next")
        if nxt and nxt not in steps:
            fail(f"step {sid}: next step '{nxt}' does not exist")
    path = correct_path(q)
    if len(path) != len(set(sid for sid, _ in path)):
        fail("the correct path loops")


def validate_edit_step(s, fail):
    """The correct snippet, put in place of 'target', must pass the checks; every wrong one must fail."""
    if not s.get("checks"):
        fail("edit step without 'checks'")
    for c in s["checks"] + s.get("forbid", []):
        try:
            re.compile(c["regex"])
        except re.error as e:
            fail(f"bad regex {c['regex']!r}: {e}")
    if find_block(as_text(s.get("prefill")).split("\n"), lines_of(s.get("target"))) < 0:
        fail("'target' lines not found in 'prefill'")
    if evaluate_edit(s, as_text(s["prefill"]))["ok"]:
        fail("the untouched prefill already passes the checks")
    for o in s["options"]:
        res = evaluate_edit(s, apply_snippet(s, lines_of(o["snippet"])))
        if o.get("correct") and not res["ok"]:
            fail(f"the correct snippet fails the checks: {res['problems']}")
        if not o.get("correct") and res["ok"]:
            fail(f"a wrong snippet passes the checks: {lines_of(o['snippet'])}")


def lines_of(value):
    return as_text(value).split("\n")


def find_block(lines, block):
    """Index of block (a list of lines) inside lines, ignoring trailing spaces; -1 if absent."""
    lines = [ln.rstrip() for ln in lines]
    block = [ln.rstrip() for ln in block]
    for i in range(len(lines) - len(block) + 1):
        if lines[i:i + len(block)] == block:
            return i
    return -1


def apply_snippet(step, snippet):
    """The prefill with the step's target lines replaced by snippet."""
    lines, target = lines_of(step["prefill"]), lines_of(step["target"])
    i = find_block(lines, target)
    return "\n".join(lines[:i] + snippet + lines[i + len(target):])


def correct_path(q):
    """[(step_id, step)] following the correct answers from the start."""
    path, sid = [], q["start"]
    while sid:
        step = q["steps"][sid]
        path.append((sid, step))
        if len(path) > len(q["steps"]):  # loop; reported by validate_question
            break
        sid = correct_option(step).get("next") if step["type"] == "command" else step.get("next")
    return path


def correct_option(step):
    return next(o for o in step["options"] if o.get("correct"))


# --------------------------------------------------------------------------
# Judge
# --------------------------------------------------------------------------

def norm_cmd(text):
    t = " ".join(text.replace("\\\n", " ").split())
    return "kubectl " + t[2:] if t.startswith("k ") else t


def evaluate_command(step, text):
    typed = norm_cmd(text)
    for o in step["options"]:
        if typed in {norm_cmd(c) for c in [o["cmd"], *o.get("aliases", [])]}:
            return {"ok": bool(o.get("correct")), "option": o}
    by_norm = {norm_cmd(o["cmd"]): o for o in step["options"]}
    close = difflib.get_close_matches(typed, list(by_norm), n=1, cutoff=0.6)
    return {"ok": False, "option": None, "close": by_norm[close[0]] if close else None}


def evaluate_edit(step, text):
    problems = []
    if "\t" in text:
        problems.append("Tabs are not allowed in YAML - use spaces.")
    for c in step["checks"]:
        if not re.search(c["regex"], text, re.M):
            problems.append(c["msg"])
    for c in step.get("forbid", []):
        if re.search(c["regex"], text, re.M):
            problems.append(c["msg"])
    return {"ok": not problems, "problems": problems, "option": snippet_used(step, text)}


def snippet_used(step, text):
    """Which option's snippet the candidate typed into the file (None if unrecognisable)."""
    if not step.get("options") or not step.get("target"):
        return None
    lines = text.split("\n")
    target_still_there = find_block(lines, lines_of(step["target"])) >= 0
    prefill = lines_of(step["prefill"])
    found = [o for o in step["options"]
             if find_block(lines, lines_of(o["snippet"])) >= 0
             # a snippet already in the untouched file is no evidence, unless the target was replaced
             and (find_block(prefill, lines_of(o["snippet"])) < 0 or not target_still_there)]
    # the longest match wins: a trap may extend the correct snippet (e.g. adds '- {}'); ties -> correct
    return max(found, key=lambda o: (len(lines_of(o["snippet"])), bool(o.get("correct"))), default=None)


def judge(step, text, how):
    if how in ("timeout", "skip"):
        res = {"ok": False, "option": None, "close": None, "problems": []}
    elif step["type"] == "command":
        res = evaluate_command(step, text)
    else:
        res = evaluate_edit(step, text)
    res["how"] = how
    if res["ok"]:
        res["next"] = correct_option(step).get("next") if step["type"] == "command" else step.get("next")
    else:
        res["next"] = None
    return res


def points_for(ok, elapsed, limit):
    if not ok:
        return 0
    return BASE_POINTS + int(SPEED_BONUS * max(0.0, 1 - elapsed / limit))


# --------------------------------------------------------------------------
# Multiline editor
# --------------------------------------------------------------------------

class Editor:
    def __init__(self, text=""):
        self.lines = text.split("\n") if text else [""]
        self.y = len(self.lines) - 1
        self.x = len(self.lines[-1])
        self.scroll = 0

    def text(self):
        return "\n".join(self.lines)

    def _insert(self, s):
        line = self.lines[self.y]
        self.lines[self.y] = line[:self.x] + s + line[self.x:]
        self.x += len(s)

    def _newline(self):
        line = self.lines[self.y]
        indent = re.match(r" *", line).group()  # keep YAML indentation
        self.lines[self.y] = line[:self.x]
        self.lines.insert(self.y + 1, indent + line[self.x:].lstrip(" "))
        self.y += 1
        self.x = len(indent)

    def _backspace(self):
        if self.x > 0:
            line = self.lines[self.y]
            self.lines[self.y] = line[:self.x - 1] + line[self.x:]
            self.x -= 1
        elif self.y > 0:
            prev = self.lines[self.y - 1]
            self.x = len(prev)
            self.lines[self.y - 1] = prev + self.lines.pop(self.y)
            self.y -= 1

    def _delete(self):
        line = self.lines[self.y]
        if self.x < len(line):
            self.lines[self.y] = line[:self.x] + line[self.x + 1:]
        elif self.y + 1 < len(self.lines):
            self.lines[self.y] = line + self.lines.pop(self.y + 1)

    def handle(self, key, enter_submits):
        """Apply one key. Returns 'submit', 'skip' or None."""
        if key == curses.KEY_ENTER:
            key = "\n"
        if isinstance(key, str):
            if key in ("\n", "\r"):
                if enter_submits and not self.lines[self.y][:self.x].endswith("\\"):
                    return "submit"
                self._newline()
            elif key == "\x04":
                return "submit"
            elif key == "\x18":  # Ctrl+X (not Esc: vim users press Esc by reflex)
                return "skip"
            elif key in ("\x7f", "\b"):
                self._backspace()
            elif key == "\t":
                self._insert("  ")
            elif key == "\x15":  # Ctrl+U: delete to start of line
                self.lines[self.y] = self.lines[self.y][self.x:]
                self.x = 0
            elif key.isprintable():
                self._insert(key)
            return None
        n = len(self.lines)
        if key == curses.KEY_BACKSPACE:
            self._backspace()
        elif key == curses.KEY_DC:
            self._delete()
        elif key == curses.KEY_LEFT:
            if self.x > 0:
                self.x -= 1
            elif self.y > 0:
                self.y -= 1
                self.x = len(self.lines[self.y])
        elif key == curses.KEY_RIGHT:
            if self.x < len(self.lines[self.y]):
                self.x += 1
            elif self.y + 1 < n:
                self.y, self.x = self.y + 1, 0
        elif key == curses.KEY_UP and self.y > 0:
            self.y -= 1
            self.x = min(self.x, len(self.lines[self.y]))
        elif key == curses.KEY_DOWN and self.y + 1 < n:
            self.y += 1
            self.x = min(self.x, len(self.lines[self.y]))
        elif key == curses.KEY_HOME:
            self.x = 0
        elif key == curses.KEY_END:
            self.x = len(self.lines[self.y])
        return None

    def rows(self, width):
        """Soft-wrapped display rows and the cursor (row, col)."""
        out, cursor = [], (0, 0)
        for i, line in enumerate(self.lines):
            if i == self.y:
                cursor = (len(out) + self.x // width, self.x % width)
            for k in range(len(line) // width + 1):
                out.append(line[k * width:(k + 1) * width])
        return out, cursor


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

ESC_SEQ = {
    "[A": curses.KEY_UP, "OA": curses.KEY_UP, "[B": curses.KEY_DOWN, "OB": curses.KEY_DOWN,
    "[C": curses.KEY_RIGHT, "OC": curses.KEY_RIGHT, "[D": curses.KEY_LEFT, "OD": curses.KEY_LEFT,
    "[H": curses.KEY_HOME, "OH": curses.KEY_HOME, "[1~": curses.KEY_HOME,
    "[F": curses.KEY_END, "OF": curses.KEY_END, "[4~": curses.KEY_END, "[3~": curses.KEY_DC,
    "[200~": PASTE_START, "[201~": PASTE_END,
}


def read_key(stdscr):
    """get_wch() that also decodes raw ANSI sequences terminfo did not map.
    Returns None for an unknown sequence; raises curses.error on timeout."""
    key = stdscr.get_wch()
    if key != "\x1b":
        return key
    seq = ""
    stdscr.timeout(0)
    try:
        while len(seq) < 5 and seq not in ESC_SEQ:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            if not isinstance(ch, str):
                break
            seq += ch
    finally:
        stdscr.timeout(100)
    return ESC_SEQ.get(seq, None if seq else "\x1b")


def read_burst(stdscr):
    """One key (waits up to the current timeout) plus every key already queued."""
    keys = [read_key(stdscr)]
    try:
        while True:
            stdscr.timeout(0)
            try:
                keys.append(read_key(stdscr))
            except curses.error:
                break
    finally:
        stdscr.timeout(100)
    return keys


def is_paste(keys):
    return PASTE_START in keys or len(keys) >= PASTE_BURST


def init_colors(stdscr):
    """One palette for the whole game: the theme lives in retro.py."""
    retro.init_colors(stdscr)
    C = retro.C
    A.update(
        green=C["green"], yellow=C["yellow"], red=C["red"], cyan=C["cyan"],
        bold=curses.A_BOLD, dim=curses.A_DIM, rev=curses.A_REVERSE, normal=0,
    )


def put(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            win.addnstr(y, x, s, w - x, attr)
        except curses.error:
            pass  # writing the bottom-right cell raises, harmlessly


def expand(lines, width):
    """[(text, attr, pre?)] -> [(text, attr)] fitted to width."""
    width = max(10, width)
    out = []
    for item in lines:
        text, attr = item[0], item[1]
        pre = len(item) > 2 and item[2]
        for raw in text.split("\n"):
            if pre:
                parts = [raw[i:i + width] for i in range(0, max(len(raw), 1), width)]
            else:
                indent = " " * (len(raw) - len(raw.lstrip(" ")))
                parts = textwrap.wrap(raw, width, subsequent_indent=indent + "  ",
                                      break_on_hyphens=False) or [""]
            out += [(p, attr) for p in parts]
    return out


def header(stdscr, text):
    w = stdscr.getmaxyx()[1]
    put(stdscr, 0, 0, text.ljust(w), A["rev"] | A["bold"])


def too_small(stdscr):
    h, w = stdscr.getmaxyx()
    if h >= MIN_H and w >= MIN_W:
        return False
    stdscr.erase()
    put(stdscr, 0, 0, f"Terminal too small ({w}x{h}). Need {MIN_W}x{MIN_H}.", A["red"])
    stdscr.refresh()
    return True


def countdown(stdscr, y, remaining, limit):
    w = stdscr.getmaxyx()[1]
    frac = max(0.0, remaining / limit)
    color = A["green"] if frac > 0.5 else A["yellow"] if frac > 0.25 else A["red"]
    label = f" {remaining:5.1f}s / {limit}s "
    width = max(10, w - len(label) - 1)
    filled = int(round(frac * width))
    put(stdscr, y, 0, "█" * filled, color | A["bold"])
    put(stdscr, y, filled, "░" * (width - filled), A["dim"])
    put(stdscr, y, width, label, color | A["bold"])


def pager(stdscr, title, lines, footer="Enter: continue   q: quit"):
    """Scrollable text screen. Returns 'next' or 'quit'."""
    stdscr.timeout(-1)
    curses.curs_set(0)
    off = 0
    while True:
        if too_small(stdscr):
            stdscr.get_wch()
            continue
        h, w = stdscr.getmaxyx()
        body = expand(lines, w - 3)
        view = h - 3
        off = max(0, min(off, len(body) - view))
        stdscr.erase()
        header(stdscr, title)
        for i, (text, attr) in enumerate(body[off:off + view]):
            put(stdscr, 2 + i, 2, text, attr)
        more = "  ↑/↓ scroll" if len(body) > view else ""
        put(stdscr, h - 1, 0, footer + more, A["dim"])
        stdscr.refresh()
        try:
            key = read_key(stdscr)
        except curses.error:
            continue
        finally:
            stdscr.timeout(-1)
        if key in ("\n", "\r", " ", curses.KEY_ENTER):
            return "next"
        if key in ("q", "Q"):
            return "quit"
        if key == curses.KEY_UP:
            off -= 1
        elif key == curses.KEY_DOWN:
            off += 1
        elif key == curses.KEY_PPAGE:
            off -= view
        elif key == curses.KEY_NPAGE:
            off += view


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

def step_body(q, step, options, width, budget):
    """The text above the input area, fitted into budget lines. When it does not fit,
    the scenario goes first, then the oldest context lines; prompt and options always stay."""
    title = expand([(q["title"], A["bold"])], width)
    scenario = expand([(q["scenario"], A["normal"])], width)
    context = []
    if step.get("context"):
        context = expand([("│ " + ln, A["dim"], True) for ln in as_text(step["context"]).split("\n")], width)
    tail = [("", 0)] + expand([(step["prompt"], A["cyan"] | A["bold"])], width)
    if step["type"] == "command":
        for label, o in zip(LABELS, options):
            tail += expand([(f"  {label}) {o['cmd']}", A["normal"], True)], width)
    gap = [("", 0)] if context else []
    for head in (title + scenario, title):
        body = head + gap + context + tail
        if len(body) <= budget:
            return body
    room = budget - len(title) - len(gap) - len(tail) - 1  # 1: the "…" marker
    if context and room >= 1:
        return title + gap + [("│ …", A["dim"])] + context[-room:] + tail
    body = title + tail
    if len(body) > budget:
        body = body[:budget - 1] + [("… (enlarge the terminal to see everything)", A["dim"])]
    return body


def box(stdscr, y, x, h, w, label, attr):
    put(stdscr, y, x, "┌" + label + "─" * (w - 2 - len(label)) + "┐", attr)
    for row in range(y + 1, y + h - 1):
        put(stdscr, row, x, "│", attr)
        put(stdscr, row, x + w - 1, "│", attr)
    put(stdscr, y + h - 1, x, "└" + "─" * (w - 2) + "┘", attr)


def snippet_rows(options):
    """[(label, indent, text)] for the options panel; label is '' on continuation lines."""
    rows = []
    for label, o in zip(LABELS, options):
        for i, ln in enumerate(lines_of(o["snippet"])):
            rows.append((f"{label})" if i == 0 else "", len(ln) - len(ln.lstrip(" ")), ln.lstrip(" ")))
        rows.append(None)  # gap between options
    return rows[:-1]


def draw_options(stdscr, y, x, h, w, options, view):
    """Snippet options; leading spaces shown as '·' so the indentation can be counted."""
    rows = snippet_rows(options)
    inner = h - 2
    view["panel"] = max(0, min(view["panel"], len(rows) - inner))
    box(stdscr, y, x, h, w, " Options: type one ", A["yellow"])
    for i, row in enumerate(rows[view["panel"]:view["panel"] + inner]):
        if row:
            label, indent, text = row
            put(stdscr, y + 1 + i, x + 2, f"{label:<3}", A["yellow"] | A["bold"])
            put(stdscr, y + 1 + i, x + 5, ("·" * indent + text)[:w - 7], A["normal"])
            put(stdscr, y + 1 + i, x + 5, "·" * min(indent, w - 7), A["dim"])
    if view["panel"] > 0:
        put(stdscr, y, x + w - 10, " ▲ PgUp ", A["yellow"] | A["bold"])
    if view["panel"] + inner < len(rows):
        put(stdscr, y + h - 1, x + w - 10, " ▼ PgDn ", A["yellow"] | A["bold"])


def draw_step(stdscr, title, q, step, options, ed, remaining, limit, warn=False, view=None):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header(stdscr, title)
    countdown(stdscr, 1, remaining, limit)

    # rows: header, bar, blank, body, blank, input area (a command needs 3 rows, an edit ~8), footer
    body = step_body(q, step, options, w - 2, h - 5 - (3 if step["type"] == "command" else 8))
    for i, (text, attr) in enumerate(body):
        put(stdscr, 3 + i, 1, text, attr)

    # input area: the editor box, plus the snippet options for an edit step
    top, ex, ew = 3 + len(body) + 1, 0, w
    eh = h - 1 - top
    if step["type"] == "edit":
        rows = snippet_rows(options)
        widest = max(len(r[2]) + r[1] for r in rows if r) + 8
        pw = min(max(widest, 34), w // 2)
        if w - pw >= 40:  # side by side: YAML files are narrow
            ew = w - pw
            draw_options(stdscr, top, ew, eh, pw, options, view)
        else:  # stacked: options above the editor
            ph = min(len(rows) + 2, eh // 2)
            draw_options(stdscr, top, 0, ph, w, options, view)
            top, eh = top + ph, eh - ph
    label = " Your answer " if step["type"] == "command" else f" {step.get('filename', 'editor')} "
    box(stdscr, top, ex, eh, ew, label, A["cyan"])

    inner_w, rows_h = ew - 4, eh - 2
    rows, (cy, cx) = ed.rows(inner_w)
    if cy < ed.scroll:
        ed.scroll = cy
    elif cy >= ed.scroll + rows_h:
        ed.scroll = cy - rows_h + 1
    for i, text in enumerate(rows[ed.scroll:ed.scroll + rows_h]):
        put(stdscr, top + 1 + i, ex + 2, text)

    if step["type"] == "command":
        hint = "Enter: submit   \\+Enter: new line   Ctrl+X: skip   Ctrl+C: quit"
    else:
        hint = "Ctrl+D: submit   Enter: new line   Tab: 2 spaces   PgUp/PgDn: options   Ctrl+X: skip"
    if warn:
        put(stdscr, h - 1, 0, "⛔ Paste blocked — type it yourself, that is the whole point!",
            A["red"] | A["bold"])
    else:
        put(stdscr, h - 1, 0, hint, A["dim"])
    try:
        stdscr.move(top + 1 + cy - ed.scroll, ex + 2 + cx)
    except curses.error:
        pass
    stdscr.refresh()


def run_step(stdscr, title, q, step, options):
    """Let the candidate answer one step. Returns (text, elapsed, how, pastes_blocked)."""
    limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
    ed = Editor(as_text(step.get("prefill")) if step["type"] == "edit" else "")
    curses.flushinp()  # drop keys typed on the previous screen
    stdscr.timeout(100)
    start = time.monotonic()
    pastes, in_paste, quarantine_until, warn_until = 0, False, 0.0, 0.0
    view = {"panel": 0}  # scroll position of the snippet options
    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed >= limit:
            return ed.text(), float(limit), "timeout", pastes
        if too_small(stdscr):
            curses.napms(200)
            continue
        curses.curs_set(1)
        draw_step(stdscr, title, q, step, options, ed, limit - elapsed, limit, now < warn_until, view)
        try:
            keys = read_burst(stdscr)
        except curses.error:
            continue
        now = time.monotonic()
        if not SETTINGS["allow_paste"]:
            blocked = in_paste or now < quarantine_until or is_paste(keys)
            if blocked and not in_paste and now >= quarantine_until:
                pastes += 1
            for k in keys:
                if k == PASTE_START:
                    in_paste = True
                elif k == PASTE_END:
                    in_paste = False
            if blocked:
                quarantine_until = now + PASTE_QUARANTINE
                warn_until = now + 2
                continue
        for key in keys:
            if key is None or key == curses.KEY_RESIZE:
                continue
            if key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                view["panel"] += 5 if key == curses.KEY_NPAGE else -5
                view["panel"] = max(0, view["panel"])
                continue
            action = ed.handle(key, enter_submits=step["type"] == "command")
            if action:
                return ed.text(), time.monotonic() - start, action, pastes


def feedback_lines(step, options, text, res, elapsed, pts, pastes=0):
    limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
    if res["ok"]:
        verdict = ("✔ CORRECT", A["green"] | A["bold"])
    elif res["how"] == "timeout":
        verdict = ("⏱ TIME'S UP", A["red"] | A["bold"])
    elif res["how"] == "skip":
        verdict = ("» SKIPPED", A["yellow"] | A["bold"])
    else:
        verdict = ("✘ WRONG", A["red"] | A["bold"])
    lines = [verdict, (f"Time {elapsed:.1f}s of {limit}s   +{pts} points", A["dim"])]
    if pastes:
        lines.append((f"⛔ {pastes} paste attempt(s) blocked", A["red"]))
    lines.append(("", 0))

    if step["type"] == "command":
        good = correct_option(step)
        if res["how"] == "submit" and not res["ok"]:
            lines += [("You typed:", A["bold"]), ("  $ " + text.strip(), A["yellow"], True)]
            chosen = res.get("option")
            if chosen:
                label = LABELS[options.index(chosen)]
                lines += [(f"That is option {label} - why it is wrong:", A["bold"]),
                          ("  " + chosen["why"], A["normal"])]
            elif res.get("close") is good:
                lines.append(("Almost: you aimed at the right answer but made a typo. "
                              "In the exam a typo is a failed command.", A["normal"]))
            elif res.get("close"):
                label = LABELS[options.index(res["close"])]
                lines += [(f"Closest to option {label} (with typos) - why it is wrong:", A["bold"]),
                          ("  " + res["close"]["why"], A["normal"])]
            else:
                lines.append(("This matches none of the options.", A["normal"]))
            lines.append(("", 0))
        if not res["ok"]:
            lines += [("Correct answer:", A["bold"]), ("  $ " + good["cmd"], A["green"], True)]
            for alias in good.get("aliases", []):
                lines.append(("  $ " + alias + "   (also accepted)", A["green"], True))
            lines.append(("", 0))
    else:
        good, chosen = correct_option(step), res.get("option")
        if res["how"] == "submit" and not res["ok"]:
            if chosen is good:
                lines.append(("You typed the right snippet, but the file is still wrong: check the "
                              "place, the indentation and the old lines you had to replace.", A["normal"]))
            elif chosen:
                label = LABELS[options.index(chosen)]
                lines += [(f"You typed option {label} - why it is wrong:", A["bold"]),
                          ("  " + chosen["why"], A["normal"])]
            lines.append(("", 0))
        if res.get("problems"):
            lines.append(("Problems found:", A["bold"]))
            lines += [("  • " + p, A["yellow"]) for p in res["problems"]]
            lines.append(("", 0))
        if not res["ok"]:
            lines.append((f"Correct snippet (option {LABELS[options.index(good)]}):", A["bold"]))
            lines += [("  " + ln, A["green"], True) for ln in lines_of(good["snippet"])]
            lines.append(("", 0))

    if step.get("explain"):
        lines += [("Why:", A["cyan"] | A["bold"]), ("  " + step["explain"], A["normal"])]
    if not res["ok"]:
        others = [(LABELS[i], o) for i, o in enumerate(options)
                  if not o.get("correct") and o is not res.get("option")]
        if others:
            lines += [("", 0), ("The other traps:", A["dim"] | A["bold"])]
            lines += [(f"  {lab}) {o['why']}", A["dim"]) for lab, o in others]
    return lines


def recap_lines(q, earned, solved):
    lines = [("✔ Question solved" if solved else "✘ Question not solved",
              (A["green"] if solved else A["red"]) | A["bold"]),
             (f"{earned} points", A["dim"]), ("", 0),
             ("The full solution path:", A["bold"])]
    for n, (_, step) in enumerate(correct_path(q), 1):
        if step["type"] == "command":
            lines.append((f"  {n}. $ {correct_option(step)['cmd']}", A["green"], True))
        else:
            lines.append((f"  {n}. [edit {step.get('filename', 'file')}] "
                          + step.get("recap", step["prompt"]), A["green"]))
    if q.get("tip"):
        lines += [("", 0), ("Exam tip:", A["cyan"] | A["bold"]), ("  " + q["tip"], A["normal"])]
    return lines


def ask_question(stdscr, q, n, total, score, domains, sound):
    sid, stepno, earned, solved = q["start"], 0, 0, False
    path = correct_path(q)
    plen = len(path)
    log = []
    seconds = sum(min(s.get("time_limit", MAX_TIME), MAX_TIME) for _, s in path)
    retro.level_screen(stdscr, n, total, [
        (domains[q["domain"]]["name"].split(",")[0].upper() + f"  ·  {q['id']}", "cyan"),
        (q["title"], "yellow"),
        (f"{plen} step{'s' * (plen > 1)}  ·  {seconds}s on the clock", "white"),
    ], sound)
    while sid:
        step = q["steps"][sid]
        stepno += 1
        options = list(step.get("options", []))
        random.shuffle(options)
        title = (f" CKA MEGATRAINER  Q{n}/{total}  {q['id']}  {domains[q['domain']]['name']}"
                 f"  ·  step {stepno}/{plen}  ·  score {score + earned}")
        text, elapsed, how, pastes = run_step(stdscr, title, q, step, options)
        res = judge(step, text, how)
        sound.play("win" if res["ok"] else "lose")
        limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
        pts = points_for(res["ok"], elapsed, limit)
        earned += pts
        log.append({"step": sid, "ok": res["ok"], "time": elapsed, "how": how, "pastes": pastes})
        quit_ = pager(stdscr, f" {q['id']}  step {stepno}/{plen}  ·  {q['topic']}",
                      feedback_lines(step, options, text, res, elapsed, pts, pastes)) == "quit"
        solved = res["ok"] and not res["next"]
        if quit_:
            return {"id": q["id"], "domain": q["domain"], "points": earned,
                    "solved": solved, "steps": log, "quit": True}
        sid = res["next"]
    sound.play("clear" if solved else "fail")
    quit_ = pager(stdscr, f" {q['id']}  recap  ·  {q['title']}",
                  recap_lines(q, earned, solved)) == "quit"
    return {"id": q["id"], "domain": q["domain"], "points": earned,
            "solved": solved, "steps": log, "quit": quit_}


def rules_lines():
    b, n = A["bold"], A["normal"]
    return [
        ("Train like the exam: read the task, choose the right command and TYPE it in full.", n),
        ("CKA EXAM: 15-20 random questions in the exam's domain proportions. The domain", n),
        ("entries let you practise all the questions of one domain.", n), ("", 0),
        ("Typing another option = choosing it. Typos count as wrong, like in a real shell.", n),
        ("A leading 'k ' is accepted as 'kubectl '. Copy/paste is blocked.", n), ("", 0),
        ("Countdown", b),
        ("  Every step has its own timer (max 2 min): green, yellow at 50 %, red at 25 %.", n),
        ("  100 points per correct step + up to 50 speed bonus.", n), ("", 0),
        ("Steps", b),
        ("  Command step: Enter submits, '\\' + Enter continues on a new line.", n),
        ("  Edit step: a file opens in a multiline editor. Enter = new line, Ctrl+D submits.", n),
        ("  Ctrl+X skips a step, Ctrl+C quits.", n), ("", 0),
        ("A wrong step ends the question: read the explanation and the recap, then try again!", n),
    ]


def exam_plan(domains, n):
    """Questions per domain for an n-question exam, proportional to the CKA weights
    (largest remainder; ties go to the heavier domain)."""
    total = sum(v["weight"] for v in domains.values())
    raw = {d: n * v["weight"] / total for d, v in domains.items()}
    plan = {d: int(x) for d, x in raw.items()}
    extra = n - sum(plan.values())
    for d in sorted(raw, key=lambda d: (raw[d] - plan[d], domains[d]["weight"]), reverse=True)[:extra]:
        plan[d] += 1
    return plan


def pick_exam(questions, domains, rnd=random, n=None):
    """A CKA-like exam: 15-20 questions (random unless n is given), domains in proportion."""
    n = n or rnd.randint(EXAM_MIN, EXAM_MAX)
    picked = []
    for d, k in exam_plan(domains, n).items():
        pool = [q for q in questions if q["domain"] == d]
        picked += rnd.sample(pool, min(k, len(pool)))
    rnd.shuffle(picked)
    return picked


def menu(stdscr, domains, questions, sound):
    """Arcade title screen. Returns 'exam', a domain id, '' for all, or None to quit."""
    counts = {d: sum(q["domain"] == d for q in questions) for d in domains}
    share = exam_plan(domains, EXAM_MAX)  # e.g. storage: 2 of a 20-question exam
    items = [("exam", "CKA EXAM", f"{EXAM_MIN}-{EXAM_MAX}Q")]
    items += [(d, v["name"].split(",")[0].upper(), f"{share[d]}/{EXAM_MAX} {v['weight']:>2}%")
              for d, v in domains.items() if counts[d]]
    items += [("", "ALL QUESTIONS", f"{len(questions)}Q"),
              ("help", "HOW TO PLAY", ""), (None, "QUIT", "")]
    sel = 0
    while True:
        sel = retro.title_screen(stdscr, items, sel, sound, read_key)
        if sel is None or items[sel][0] is None:
            return None
        value, label, _ = items[sel]
        if value == "help":
            pager(stdscr, " HOW TO PLAY", rules_lines(), footer="Enter: back")
            continue
        retro.get_ready(stdscr, label, sound)
        return value


def summary_lines(results, domains):
    total_pts = sum(r["points"] for r in results)
    steps = [s for r in results for s in r["steps"]]
    good = [s for s in steps if s["ok"]]
    solved = sum(r["solved"] for r in results)
    lines = [(f"Score: {total_pts} points", A["bold"]),
             (f"Questions solved: {solved}/{len(results)}", 0),
             (f"Steps correct: {len(good)}/{len(steps)}", 0)]
    if good:
        lines.append((f"Average time on correct steps: {sum(s['time'] for s in good) / len(good):.1f}s", 0))
    lines += [("", 0), ("By domain:", A["bold"])]
    for d, v in domains.items():
        rs = [r for r in results if r["domain"] == d]
        if rs:
            ok = sum(r["solved"] for r in rs)
            color = A["green"] if ok == len(rs) else A["yellow"] if ok else A["red"]
            lines.append((f"  {v['name']:<52} {ok}/{len(rs)}", color, True))
    missed = [r["id"] for r in results if not r["solved"]]
    if missed:
        lines += [("", 0), ("Review these questions:", A["bold"]), ("  " + "  ".join(missed), A["yellow"]),
                  ("  Replay them with: ./cka_trainer.py " + " ".join(f"--id {m}" for m in missed), A["dim"])]
    return lines


def finale_info(results, questions, domains):
    """Numbers for the last screen. Exam % = correct steps / steps on the solution paths,
    like the CKA's partial credit; the CKA pass mark is 66 %."""
    by_id = {q["id"]: q for q in questions}
    steps_total = sum(len(correct_path(by_id[r["id"]])) for r in results)
    steps_ok = sum(s["ok"] for r in results for s in r["steps"])
    percent = round(100 * steps_ok / steps_total) if steps_total else 0
    pods = []
    for d in domains:
        rs = [r for r in results if r["domain"] == d]
        if rs:
            ok = sum(r["solved"] for r in rs)
            status = "Running" if ok == len(rs) else "Error" if ok == 0 else "CrashLoopBackOff"
            secs = sum(s["time"] for r in rs for s in r["steps"])
            pods.append((d, f"{ok}/{len(rs)}", status, len(rs) - ok, secs))
    return {"won": percent >= PASS_PERCENT, "score": sum(r["points"] for r in results),
            "percent": percent, "pass_percent": PASS_PERCENT,
            "solved": sum(r["solved"] for r in results), "total": len(results),
            "pods": pods, "review": [r["id"] for r in results if not r["solved"]]}


def play(stdscr, domains, questions, args):
    init_colors(stdscr)
    sys.stdout.write("\x1b[?2004h")  # bracketed paste: terminal marks pasted text
    sys.stdout.flush()
    sound = retro.Sound(enabled=not SETTINGS["silent"])
    try:
        return session(stdscr, domains, questions, args, sound)
    finally:
        sound.stop_music()
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def session(stdscr, domains, questions, args, sound):
    choice = "exam" if args.exam else None
    if not args.id and args.domain is None and not args.exam:
        choice = menu(stdscr, domains, questions, sound)
        if choice is None:
            return []
    if choice == "exam":
        questions = pick_exam(questions, domains, n=args.count)
    else:
        if choice:
            questions = [q for q in questions if q["domain"] == choice]
        if args.shuffle:
            random.shuffle(questions)
        if args.count:
            questions = questions[:args.count]
    results, score = [], 0
    for n, q in enumerate(questions, 1):
        r = ask_question(stdscr, q, n, len(questions), score, domains, sound)
        results.append(r)
        score += r["points"]
        if r["quit"]:
            break
    if results:
        retro.finale(stdscr, finale_info(results, questions, domains), sound)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Timed, type-your-answer CKA trainer.")
    ap.add_argument("--db", default=DEFAULT_DB, help="question database (JSON)")
    ap.add_argument("--domain", help="only this domain id (skips the menu)")
    ap.add_argument("--id", action="append", help="only this question id (repeatable)")
    ap.add_argument("--exam", action="store_true",
                    help=f"CKA-like exam: {EXAM_MIN}-{EXAM_MAX} random questions in domain proportions (skips the menu)")
    ap.add_argument("--count", type=int, help="maximum number of questions (with --exam: the exam size)")
    ap.add_argument("--no-shuffle", dest="shuffle", action="store_false", help="keep database order")
    ap.add_argument("--allow-paste", action="store_true", help="do not block pasted input")
    ap.add_argument("--silent", action="store_true", help="no music or sound effects")
    ap.add_argument("--theme", choices=retro.THEMES, default="retro",
                    help="retro: vivid RGB on black (default); soft: the terminal's own colours")
    ap.add_argument("--validate", action="store_true", help="check the database and exit")
    ap.add_argument("--list", action="store_true", help="list questions and exit")
    args = ap.parse_args(argv)
    SETTINGS["allow_paste"] = args.allow_paste
    SETTINGS["silent"] = args.silent
    retro.set_theme(args.theme)

    try:
        domains, questions = load_db(args.db)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Database error: {e}")

    if args.validate:
        nsteps = sum(len(q["steps"]) for q in questions)
        print(f"OK: {len(questions)} questions, {nsteps} steps, {len(domains)} domains")
        return
    if args.list:
        for q in questions:
            print(f"{q['id']:<9} {q['domain']:<16} {q['topic']:<38} {q['title']}")
        return
    if args.domain is not None:
        if args.domain not in domains:
            sys.exit(f"Unknown domain '{args.domain}'. Known: {', '.join(domains)}")
        questions = [q for q in questions if q["domain"] == args.domain]
    if args.id:
        wanted = set(args.id)
        unknown = wanted - {q["id"] for q in questions}
        if unknown:
            sys.exit(f"Unknown question id(s): {', '.join(sorted(unknown))}")
        questions = [q for q in questions if q["id"] in wanted]
    try:
        results = curses.wrapper(play, domains, questions, args)
    except KeyboardInterrupt:
        print("Bye - keep practising!")
        return
    if results:  # stays in the terminal after the game closes
        print("\n".join(line[0] for line in summary_lines(results, domains)))


if __name__ == "__main__":
    main()
