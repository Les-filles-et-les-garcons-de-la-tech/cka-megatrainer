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
import math
import os
import random
import re
import sys
import textwrap
import time
from pathlib import Path

import retro

os.environ.setdefault("ESCDELAY", "25")
if os.environ.get("TERM") == "xterm-kitty":
    # kitty's terminfo has `rep` (repeat last char): curses then draws the box borders and the
    # countdown bar (multi-byte ─ █) as one char + repeat, which kitty renders wrongly
    os.environ["TERM"] = "xterm-256color"

DEFAULT_DB = Path(__file__).with_name("questions.json")
MAX_TIME = 120
PASS_PERCENT = 66  # CKA passing score
EXAM_MIN, EXAM_MAX = 15, 20  # the CKA has 15-20 tasks
BASE_POINTS = 100
SPEED_BONUS = 50
LABELS = "ABCDEFGH"
MIN_W, MIN_H = 70, 20
MAX_STORY = 150  # stakes / analogy: one short line each

PASTE_BURST = 4        # keys arriving together = pasted, not typed
PASTE_QUARANTINE = 0.3  # seconds during which the tail of a paste is still dropped
PASTE_START, PASTE_END = -200, -201  # bracketed-paste markers (ints: the editor ignores them)

A = {}  # curses attributes, filled by init_colors()
SETTINGS = {"allow_paste": False, "silent": False, "learn": False, "recall": False, "hints": False,  # OPTIONS + flags
            "timer": "question", "level": "hard", "stereo": False, "autocomplete": False,
            "feedback": "step"}  # feedback "end": the answers only after the last question
MAX_HINT = 150  # characters of a step's 'hint' (HINTS option): a nudge, never the answer
RECALL_HINT = 0.5  # RECALL mode: the options appear once this share of the step's time has passed
LEVELS = {"easy": 1, "medium": 2, "hard": 3}  # a level plays the questions up to its difficulty
EXAM_SECONDS = 120 * 60  # the CKA: 2 hours for the whole exam
PRIME_OK = "Right. Before you move on: say why each of the others fails."
PRIME_MISS = "Good place to miss it: here a mistake is free; in production it costs sleep."
# REVIEW MISSED: the questions you got wrong, kept between runs until you solve them
STATE_DIR = Path(os.environ.get("CKA_MEGATRAINER_HOME") or Path.home() / ".cka_megatrainer")
REVIEW_FILE = "review.json"
SCORES_FILE = "highscores.json"  # HI-SCORES: the top TOP_SCORES of each board (round x LEVEL)
TOP_SCORES = 10
LIVES = 3  # SURVIVAL: a question not solved costs one
CONTINUE_SECONDS = 10
RUN = {"lives": None}  # SURVIVAL's lives while it runs (None otherwise): drawn as red hearts
TICK_GLOBAL = 60  # GLOBAL timer: the countdown ticks in its last minute (its red zone is 30 min long)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def as_text(value):
    return "\n".join(value) if isinstance(value, list) else (value or "")


def read_db_text(path):
    """The built-in database lives inside the archive when the game runs as a .pyz:
    zipimport's loader then serves it by its member name."""
    if Path(path) == DEFAULT_DB and hasattr(__loader__, "archive"):
        return __loader__.get_data(DEFAULT_DB.name).decode("utf-8")
    return Path(path).read_text(encoding="utf-8")


def load_db(path):
    data = json.loads(read_db_text(path))
    domains = {d["id"]: d for d in data["domains"]}
    seen = set()
    for q in data["questions"]:
        validate_question(q, domains, seen)
    return domains, data["questions"]


def short_name(domain):
    """Menu / header label of a domain: its optional 'short' field, else its name up to a comma."""
    return domain.get("short") or domain["name"].split(",")[0].upper()


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
    if q.get("difficulty") not in (1, 2, 3):  # LEVEL (OPTIONS) filters on it
        fail("'difficulty' must be 1 (easy), 2 (medium) or 3 (hard)")
    if not isinstance(q.get("docs"), list) or not q["docs"]:
        fail("'docs' must be a non-empty list of {title, url}")
    for d in q["docs"]:
        if not isinstance(d, dict) or not d.get("title") or not str(d.get("url", "")).startswith("https://"):
            fail(f"docs entries need a 'title' and an https 'url': {d}")
    for key in ("stakes", "analogy"):
        if not (isinstance(q.get(key), str) and 0 < len(q[key]) <= MAX_STORY):
            fail(f"'{key}' must be a short text (1-{MAX_STORY} characters)")
    steps = q["steps"]
    if q["start"] not in steps:
        fail(f"start step '{q['start']}' does not exist")
    for sid, s in steps.items():
        if not s.get("prompt"):
            fail(f"step {sid}: missing 'prompt'")
        if min(s.get("time_limit", MAX_TIME), MAX_TIME) <= 0:
            fail(f"step {sid}: time_limit must be positive")
        if "hint" in s:
            problem = hint_problem(s)
            if problem:
                fail(f"step {sid}: hint {problem}")
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


def hint_problem(s):
    """Why a step's 'hint' is not acceptable, or None: a short text that never contains the
    right answer (the correct command or one of its forms, or a line of the correct snippet)."""
    hint = s["hint"]
    if not (isinstance(hint, str) and 0 < len(hint) <= MAX_HINT):
        return f"must be a short text (1-{MAX_HINT} characters)"
    good = next((o for o in s.get("options", []) if o.get("correct")), {})
    flat = norm_cmd(hint)
    for form in [good.get("cmd"), *good.get("aliases", [])] if "cmd" in good else []:
        if norm_cmd(form) in flat:
            return "gives the answer away: " + form
    for ln in lines_of(good.get("snippet", [])):
        if len(ln.strip()) >= 12 and ln.strip() in hint:
            return "gives the answer away: " + ln.strip()
    return None


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


DIFF_GAP = "·"   # a character missing on one side of the typo diff
DIFF_CHUNK = 56  # columns per row of the typo diff: fits the narrowest answer page (70 columns)


def typo_target(typed, option):
    """The form of the option (command or alias) the typed line is closest to, written the way it
    was typed: with 'k ' if the player used the alias."""
    forms = [" ".join(c.split()) for c in [option["cmd"], *option.get("aliases", [])]]
    if typed.startswith("k "):
        forms = ["k " + f[len("kubectl "):] if f.startswith("kubectl ") else f for f in forms]
    return max(forms, key=lambda f: difflib.SequenceMatcher(None, typed, f, autojunk=False).ratio())


def typo_diff(typed, target):
    """Character-level alignment of a typo: (yours, right, marks), the same length. marks: '^' a wrong
    character, '+' one missing from yours, '-' one too many in yours; gaps are DIFF_GAP."""
    yours, right, marks = [], [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, typed, target, autojunk=False).get_opcodes():
        a, b = typed[i1:i2], target[j1:j2]
        n = max(len(a), len(b))
        yours.append(a.ljust(n, DIFF_GAP))
        right.append(b.ljust(n, DIFF_GAP))
        if op == "equal":
            marks.append(" " * n)
        else:
            both = min(len(a), len(b))
            marks.append("^" * both + ("-" if len(a) > len(b) else "+") * (n - both))
    return "".join(yours), "".join(right), "".join(marks)


def typo_lines(text, option):
    """The answer page's diff of a typo against the right option, in aligned chunks."""
    typed = " ".join(text.replace("\\\n", " ").split())
    yours, right, marks = typo_diff(typed, typo_target(typed, option))
    lines, i = [], 0
    while i < len(yours):
        end = i + DIFF_CHUNK
        if end < len(yours):  # break after a space both rows share, so no word is cut in two
            cut = max((k for k in range(i + 1, end) if yours[k] == right[k] == " "), default=end - 1)
            end = cut + 1
        lines += [("    yours  " + yours[i:end], A["yellow"], True),
                  ("    right  " + right[i:end], A["green"], True),
                  ("           " + marks[i:end].rstrip(), A["red"] | A["bold"], True)]
        i = end
    legend = [f"{m} {what}" for m, what in (("^", "wrong character"), ("+", "missing"), ("-", "one too many"))
              if m in marks]
    return lines + [("           " + "   ".join(legend), A["dim"], True)]


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


def correct_next(step):
    """The step after this one on the solution path (None at the end)."""
    return correct_option(step).get("next") if step["type"] == "command" else step.get("next")


def feedback_at_end():
    """FEEDBACK AT THE END, like the exam: nothing tells a step was right until the last screen.
    LEARN mode and SURVIVAL (you must know when a life goes) always explain each step."""
    return SETTINGS["feedback"] == "end" and not SETTINGS["learn"] and RUN["lives"] is None


def points_for(ok, elapsed, limit):
    if not ok:
        return 0
    return BASE_POINTS + int(SPEED_BONUS * max(0.0, 1 - elapsed / limit))


# --------------------------------------------------------------------------
# Multiline editor
# --------------------------------------------------------------------------

def completion_words(step):
    """AUTOCOMPLETE candidates: the words of the step's own variants (every option and alias;
    all are on screen, so nothing is revealed), plus each '--flag=' alone, completed first."""
    words = set()
    for o in step.get("options", []):
        for form in ([o["cmd"], *o.get("aliases", [])] if "cmd" in o else []):
            for w in form.split():
                words.add(w)
                if w.startswith("-") and "=" in w:
                    words.add(w.split("=", 1)[0] + "=")
    return words


def complete(line, x, words):
    """bash-like Tab on the word before the cursor: a unique match is completed (plus a space,
    except after '=' or '/'); several matches extend to their common prefix and are returned
    for display. Returns (line, x, matches)."""
    before = line[:x]
    word = "" if not before or before[-1].isspace() else before.split()[-1]
    matches = sorted(w for w in words if word and w.startswith(word) and w != word)
    if not matches:
        return line, x, []
    if len(matches) == 1:
        add = matches[0][len(word):]
        if not add.endswith(("=", "/")) and not line[x:].startswith(" "):
            add += " "
        return before + add + line[x:], x + len(add), []
    add = os.path.commonprefix(matches)[len(word):]
    return before + add + line[x:], x + len(add), matches


class Editor:
    def __init__(self, text=""):
        self.lines = text.split("\n") if text else [""]
        self.y = len(self.lines) - 1
        self.x = len(self.lines[-1])
        self.scroll = 0
        self.matches = []  # AUTOCOMPLETE: the choices of an ambiguous Tab, until the next key

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

    def handle(self, key, enter_submits, words=None):
        """Apply one key. Returns 'submit', 'skip' or None.
        words: AUTOCOMPLETE candidates; Tab then completes instead of inserting 2 spaces."""
        if key == curses.KEY_ENTER:
            key = "\n"
        self.matches = []
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
            elif key == "\t" and words is not None:
                self.lines[self.y], self.x, self.matches = complete(self.lines[self.y], self.x, words)
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
        green=C["green"], yellow=C["yellow"], red=C["red"], cyan=C["cyan"], magenta=C["magenta"],
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


def clock_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def countdown(stdscr, y, remaining, limit):
    w = stdscr.getmaxyx()[1]
    frac = max(0.0, remaining / limit)
    color = A["green"] if frac > 0.5 else A["yellow"] if frac > 0.25 else A["red"]
    label = f" {clock_text(remaining)} / {clock_text(limit)} " if limit >= 600 else f" {remaining:5.1f}s / {limit}s "
    width = max(10, w - len(label) - 1)
    filled = int(round(frac * width))
    put(stdscr, y, 0, "█" * filled, color | A["bold"])
    put(stdscr, y, filled, "░" * (width - filled), A["dim"])
    put(stdscr, y, width, label, color | A["bold"])


MISS_FLASHES, MISS_FLASH_MS = 3, 90  # a missed step: the countdown bar blinks red


def miss_flash(stdscr, how):
    """A wrong answer or a time-out: the countdown bar (row 1) blinks red with the verdict in it,
    right before the answer page. Not for a skip: that was a choice."""
    if how not in ("submit", "timeout"):
        return
    w = stdscr.getmaxyx()[1]
    label = " ⏱ TIME'S UP " if how == "timeout" else " ✘ WRONG "
    x = max(0, (w - len(label)) // 2)
    for i in range(MISS_FLASHES * 2):
        on = i % 2 == 0
        put(stdscr, 1, 0, ("█" if on else "░") * (w - 1), A["red"] | (A["bold"] if on else A["dim"]))
        put(stdscr, 1, x, label, A["red"] | A["bold"] | A["rev"])
        stdscr.refresh()
        curses.napms(MISS_FLASH_MS)
    curses.flushinp()  # keys hit during the flash do not page through the answer


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
        more = "  UP/DOWN scroll" if len(body) > view else ""  # VT323 has no arrow glyphs
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

def step_body(q, step, options, width, budget, hint_in=None):
    """The text above the input area, fitted into budget lines. When it does not fit,
    the scenario goes first, then the oldest context lines; prompt and options always stay.
    hint_in: RECALL mode, seconds before the options appear (None: they are shown)."""
    title = expand([(q["title"], A["bold"])], width)
    scenario = expand([(q["scenario"], A["normal"])], width)
    context = []
    if step.get("context"):
        context = expand([("│ " + ln, A["dim"], True) for ln in as_text(step["context"]).split("\n")], width)
    tail = [("", 0)] + expand([(step["prompt"], A["cyan"] | A["bold"])], width)
    hint = expand([("HINT: " + step["hint"], A["magenta"])], width) if SETTINGS["hints"] and step.get("hint") else []
    tail += hint
    if hint_in is not None:
        tail.append(("", 0))
        tail += expand([(f"RECALL: type it from memory. The options appear in {math.ceil(hint_in)}s.",
                         A["yellow"])], width)
    elif step["type"] == "command":
        tail.append(("", 0))  # a breath between the question and its options
        for label, o in zip(LABELS, options):
            if SETTINGS["learn"] and o.get("correct"):  # learn mode: the answer is shown
                tail += expand([(f"✔ {label}) {o['cmd']}", A["green"] | A["bold"], True)], width)
            else:
                tail += expand([(f"  {label}) {o['cmd']}", A["normal"], True)], width)
    gap = [("", 0)] if context else []
    for head in (title + [("", 0)] + scenario, title):
        body = head + gap + context + tail
        if len(body) <= budget:
            return body
    if hint and len(title) + len(tail) > budget:  # too small even without the scenario: drop the hint first
        tail = [x for x in tail if x not in hint]
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
    """[(label, indent, text, correct)] for the options panel; label is '' on continuation lines."""
    rows = []
    for label, o in zip(LABELS, options):
        for i, ln in enumerate(lines_of(o["snippet"])):
            rows.append((f"{label})" if i == 0 else "", len(ln) - len(ln.lstrip(" ")), ln.lstrip(" "),
                         bool(o.get("correct"))))
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
            label, indent, text, good = row
            show = SETTINGS["learn"] and good  # learn mode: the right snippet is marked
            put(stdscr, y + 1 + i, x + 2, f"{label:<3}", A["yellow"] | A["bold"])
            if show and label:
                put(stdscr, y + 1 + i, x + 1, "✔", A["green"] | A["bold"])
            put(stdscr, y + 1 + i, x + 5, ("·" * indent + text)[:w - 7],
                A["green"] | A["bold"] if show else A["normal"])
            put(stdscr, y + 1 + i, x + 5, "·" * min(indent, w - 7), A["dim"])
    if view["panel"] > 0:
        put(stdscr, y, x + w - 10, " ▲ PgUp ", A["yellow"] | A["bold"])
    if view["panel"] + inner < len(rows):
        put(stdscr, y + h - 1, x + w - 10, " ▼ PgDn ", A["yellow"] | A["bold"])


def draw_step(stdscr, title, q, step, options, ed, remaining, limit, warn=False, view=None, hint_in=None):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header(stdscr, title)
    if RUN["lives"] is not None:  # SURVIVAL: the lives, red hearts at the end of the title bar
        lives = " " + " ".join(retro.hearts(RUN["lives"], LIVES)) + " "  # spaced: ♥ ♥ ♡
        put(stdscr, 0, w - len(lives) - 1, lives, retro.C.get("heartbar", A["red"] | A["bold"]))
    countdown(stdscr, 1, remaining, limit)

    # rows: header, bar, blank, body, blank, input area (a command needs 3 rows, an edit ~8), footer
    body = step_body(q, step, options, w - 2, h - 5 - (3 if step["type"] == "command" else 8), hint_in)
    for i, (text, attr) in enumerate(body):
        put(stdscr, 3 + i, 1, text, attr)

    # input area: the editor box, plus the snippet options for an edit step
    top, ex, ew = 3 + len(body) + 1, 0, w
    eh = h - 1 - top
    if step["type"] == "edit" and hint_in is None:  # RECALL: no options panel before the hint
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
        tab = "Tab: complete   " if SETTINGS["autocomplete"] else ""
        hint = f"Enter: submit   {tab}\\+Enter: new line   Ctrl+X: skip   Ctrl+C: quit"
    else:
        pages = "" if hint_in is not None else "PgUp/PgDn: options   "
        hint = f"Ctrl+D: submit   Enter: new line   Tab: 2 spaces   {pages}Ctrl+X: skip"
    if warn:
        put(stdscr, h - 1, 0, "⛔ Paste blocked — type it yourself, that is the whole point!",
            A["red"] | A["bold"])
    elif ed.matches:  # an ambiguous Tab: the choices, like bash's double Tab
        listing = "  ".join(ed.matches)
        if len(listing) > w - 6:
            listing = listing[:w - 8] + " …"
        put(stdscr, h - 1, 0, "Tab: " + listing, A["cyan"] | A["bold"])
    else:
        put(stdscr, h - 1, 0, hint, A["dim"])
    try:
        stdscr.move(top + 1 + cy - ed.scroll, ex + 2 + cx)
    except curses.error:
        pass
    stdscr.refresh()


def tick_due(remaining, total, global_clock, last):
    """The countdown's red-zone tick: None, 'tick' or 'tick_hi' (the last 5 s), once per second.
    Returns (sound, the second it was for)."""
    zone = remaining <= TICK_GLOBAL if global_clock else remaining <= 0.25 * total
    sec = math.ceil(remaining)
    if not zone or remaining <= 0 or sec == last:
        return None, last
    return ("tick_hi" if remaining <= 5 else "tick"), sec


def run_step(stdscr, title, q, step, options, clock=None, sound=None):
    """Let the candidate answer one step. Returns (text, elapsed, how, pastes_blocked).
    clock: {"deadline", "total"} of a GLOBAL timer, which then replaces the step's own limit.
    sound: ticks in the red zone of the countdown."""
    limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
    ed = Editor(as_text(step.get("prefill")) if step["type"] == "edit" else "")
    curses.flushinp()  # drop keys typed on the previous screen
    stdscr.timeout(100)
    start = time.monotonic()
    pastes, in_paste, quarantine_until, warn_until = 0, False, 0.0, 0.0
    view = {"panel": 0}  # scroll position of the snippet options
    words = completion_words(step) if SETTINGS["autocomplete"] and step["type"] == "command" else None
    ticked = None
    while True:
        now = time.monotonic()
        elapsed = now - start
        hint_in = recall_hint_in(limit, elapsed)
        ed_words = None if hint_in is not None else words  # completion would reveal the options
        remaining, total = (clock["deadline"] - now, clock["total"]) if clock else (limit - elapsed, limit)
        if remaining <= 0:
            return ed.text(), (elapsed if clock else float(limit)), "timeout", pastes
        tick, ticked = tick_due(remaining, total, bool(clock), ticked)
        if tick and sound:
            sound.play(tick)
        if too_small(stdscr):
            curses.napms(200)
            continue
        curses.curs_set(1)
        draw_step(stdscr, title, q, step, options, ed, remaining, total, now < warn_until, view, hint_in)
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
            action = ed.handle(key, enter_submits=step["type"] == "command", words=ed_words)
            if action:
                return ed.text(), time.monotonic() - start, action, pastes


def variant_lines(label, option, why_attr, why=None, text_attr=None, mark=" ", also=(), yours=False):
    """One variant with its explanation: the text the player saw, then why. The explanation
    refers to a variant, so the variant has to be on the page too. mark: ✔ / ✘ on the
    'All the options' list; also: accepted equivalent forms; yours: the player typed this one."""
    if "cmd" in option:
        body = [option["cmd"]]
    else:  # a snippet: leading spaces as '·', like the options panel
        body = ["·" * (len(ln) - len(ln.lstrip(" "))) + ln.lstrip(" ") for ln in lines_of(option["snippet"])]
    attr = text_attr or A["normal"] | A["bold"]
    lines = [(f"{mark} {label}) {ln}" if i == 0 else f"     {ln}", attr, True) for i, ln in enumerate(body)]
    lines += [(f"     {form}   (also accepted)", attr, True) for form in also]
    if yours:
        lines.append(("     ^ your answer", A["red"] | A["bold"]))
    return lines + [("          → " + (why or option["why"]), why_attr), ("", 0)]


def docs_lines(docs):
    """Learn mode: the Kubernetes documentation of the question, one bare URL per line so the
    terminal can open it with a click (Cmd/Ctrl+click)."""
    if not (SETTINGS["learn"] and docs):
        return []
    lines = [("", 0), ("Read more in the documentation:", A["cyan"] | A["bold"])]
    for d in docs:
        lines += [("  " + d["title"], A["normal"]), ("  " + d["url"], A["cyan"], True)]
    return lines


def recall_hint_in(limit, elapsed):
    """RECALL mode: seconds left before a step's options appear (at RECALL_HINT of its own time
    limit), or None when they are shown."""
    if not SETTINGS["recall"] or elapsed >= limit * RECALL_HINT:
        return None
    return limit * RECALL_HINT - elapsed


def feedback_lines(step, options, text, res, elapsed, pts, pastes=0, docs=None, clock=None):
    limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
    if res["ok"]:
        verdict = ("✔ CORRECT", A["green"] | A["bold"])
    elif res["how"] == "timeout":
        verdict = ("⏱ TIME'S UP", A["red"] | A["bold"])
    elif res["how"] == "skip":
        verdict = ("» SKIPPED", A["yellow"] | A["bold"])
    else:
        verdict = ("✘ WRONG", A["red"] | A["bold"])
    if clock:
        left = clock_text(clock["deadline"] - time.monotonic())
        timing = f"Time {elapsed:.1f}s   +{pts} points   ·   global clock: {left} left"
    else:
        timing = f"Time {elapsed:.1f}s of {limit}s   +{pts} points"
    lines = [verdict, (timing, A["dim"])]
    if pastes:
        lines.append((f"⛔ {pastes} paste attempt(s) blocked", A["red"]))
    lines.append(("", 0))
    lines += [("Question:", A["cyan"] | A["bold"]), ("", 0), ("  " + step["prompt"], A["cyan"])]
    if step.get("context"):  # the output the options refer to
        lines += [("  │ " + ln, A["normal"], True) for ln in as_text(step["context"]).split("\n")]
    lines.append(("", 0))
    if SETTINGS["learn"]:  # affective priming + self-explanation, before the facts
        lines += [(PRIME_OK if res["ok"] else PRIME_MISS, A["yellow"]), ("", 0)]

    good = correct_option(step)
    if res["ok"]:  # the answer, then why it is right; learn mode also explains the traps avoided
        lines += [("Why this is right:", A["green"] | A["bold"]), ("", 0)]
        lines += variant_lines(LABELS[options.index(good)], good, A["green"], why=step["explain"],
                               text_attr=A["green"] | A["bold"])
        if SETTINGS["learn"]:
            others = [(LABELS[i], o) for i, o in enumerate(options) if not o.get("correct")]
            if others:
                lines += [("", 0), ("Why not the others:", A["yellow"] | A["bold"]), ("", 0)]
                for lab, o in others:
                    lines += variant_lines(lab, o, A["yellow"])
        return lines + docs_lines(docs)

    # not solved (wrong, time's up, skipped): what was typed, then EVERY option, the right one first
    chosen = res.get("option")
    if res["how"] == "submit":
        if step["type"] == "command":
            lines += [("You typed:", A["bold"]), ("  $ " + text.strip(), A["yellow"], True)]
            if chosen:
                lines.append((f"  That is option {LABELS[options.index(chosen)]}: see why below.", A["normal"]))
            elif res.get("close") is good:
                lines.append(("  Almost: you aimed at the right answer but made a typo. "
                              "In the exam a typo is a failed command.", A["normal"]))
                lines += [("", 0)] + typo_lines(text, good)
            elif res.get("close"):
                lines.append((f"  Closest to option {LABELS[options.index(res['close'])]} (with typos): "
                              "see why below.", A["normal"]))
            else:
                lines.append(("  This matches none of the options.", A["normal"]))
        elif chosen is good:
            lines.append(("You typed the right snippet, but the file is still wrong: check the "
                          "place, the indentation and the old lines you had to replace.", A["normal"]))
        elif chosen:
            lines.append((f"You typed option {LABELS[options.index(chosen)]}: see why below.", A["bold"]))
        lines.append(("", 0))
    if res.get("problems"):
        lines.append(("Problems found:", A["bold"]))
        lines += [("  • " + p, A["yellow"]) for p in res["problems"]]
        lines.append(("", 0))
    yours = chosen or (res.get("close") if res.get("close") is not good else None)
    lines += [("All the options:", A["cyan"] | A["bold"]), ("", 0)]
    lines += variant_lines(LABELS[options.index(good)], good, A["green"], why=step["explain"],
                           text_attr=A["green"] | A["bold"], mark="✔",
                           also=good.get("aliases", []), yours=good is yours)
    for i, o in enumerate(options):
        if not o.get("correct"):
            mine = o is yours
            lines += variant_lines(LABELS[i], o, A["yellow"], mark="✘", yours=mine,
                                   text_attr=(A["red"] if mine else A["normal"]) | A["bold"])
    return lines + docs_lines(docs)


def recap_lines(q, earned, solved):
    lines = [("✔ Question solved" if solved else "✘ Question not solved",
              (A["green"] if solved else A["red"]) | A["bold"]),
             (f"{earned} points", A["dim"]), ("", 0),
             ("The mission:", A["cyan"] | A["bold"]), ("  " + q["title"], A["bold"]),
             ("  " + q["scenario"], A["normal"]), ("", 0),
             ("The full solution path:", A["bold"])]
    for n, (_, step) in enumerate(correct_path(q), 1):
        if step["type"] == "command":
            lines.append((f"  {n}. $ {correct_option(step)['cmd']}", A["green"], True))
        else:
            lines.append((f"  {n}. [edit {step.get('filename', 'file')}] "
                          + step.get("recap", step["prompt"]), A["green"]))
    if q.get("tip"):
        lines += [("", 0), ("Exam tip:", A["cyan"] | A["bold"]), ("  " + q["tip"], A["normal"])]
    if q.get("stakes") and not solved:  # a wrong step ends the question: this is the moment it lands
        lines += [("", 0), ("Why it matters:", A["yellow"] | A["bold"]), ("  " + q["stakes"], A["normal"])]
    if q.get("analogy"):
        lines += [("", 0), ("Think of it like this:", A["cyan"] | A["bold"]), ("  " + q["analogy"], A["normal"])]
    return lines + docs_lines(q.get("docs"))


def ask_question(stdscr, q, n, total, score, domains, sound, clock=None):
    sid, stepno, earned, solved = q["start"], 0, 0, False
    path = correct_path(q)
    plen = len(path)
    log = []
    seconds = sum(min(s.get("time_limit", MAX_TIME), MAX_TIME) for _, s in path)
    retro.level_screen(stdscr, n, total, [
        (short_name(domains[q["domain"]]) + f"  ·  {q['id']}", "cyan"),
        (q["title"], "yellow"),
        (f"{plen} step{'s' * (plen > 1)}  ·  " + (f"{clock_text(clock['deadline'] - time.monotonic())} left "
                                                   f"on the global clock" if clock else f"{seconds}s on the clock"),
         "white"),
    ], sound, lives=None if RUN["lives"] is None else (RUN["lives"], LIVES))
    deferred = feedback_at_end()
    answers = []  # FEEDBACK AT THE END: each step's answer page, shown after the finale
    while sid:
        step = q["steps"][sid]
        stepno += 1
        options = list(step.get("options", []))
        random.shuffle(options)
        title = (f" {'CKA LEARN' if SETTINGS['learn'] else 'CKA MEGATRAINER'}  Q{n}{'/' + str(total) if total else ''}  {q['id']}  {domains[q['domain']]['name']}"
                 f"  ·  step {stepno}/{plen}" + ("" if deferred else f"  ·  score {score + earned}"))
        text, elapsed, how, pastes = run_step(stdscr, title, q, step, options, clock, sound)
        res = judge(step, text, how)
        limit = min(step.get("time_limit", MAX_TIME), MAX_TIME)
        pts = points_for(res["ok"], elapsed, limit)
        earned += pts
        log.append({"step": sid, "ok": res["ok"], "time": elapsed, "how": how, "pastes": pastes})
        page_title = f" {q['id']}  step {stepno}/{plen}  ·  {q['topic']}"
        page = feedback_lines(step, options, text, res, elapsed, pts, pastes, q.get("docs"), clock)
        if deferred:  # no verdict now; a wrong step goes on along the solution path, like the exam
            answers.append((page_title, page))
            quit_ = False
            solved = all(s["ok"] for s in log) and not correct_next(step)
            nxt = correct_next(step)
        else:
            sound.play("win" if res["ok"] else "lose")
            if not res["ok"]:
                miss_flash(stdscr, how)
            quit_ = pager(stdscr, page_title, page) == "quit"
            solved = res["ok"] and not res["next"]
            nxt = res["next"]
        timeup = bool(clock) and time.monotonic() >= clock["deadline"]
        if quit_ or timeup:  # the global clock ran out: the exam is over
            return {"id": q["id"], "domain": q["domain"], "points": earned, "solved": solved,
                    "steps": log, "quit": quit_, "timeup": timeup, "answers": answers}
        sid = nxt
    recap_title = f" {q['id']}  recap  ·  {q['title']}"
    if deferred:
        answers.append((recap_title, recap_lines(q, earned, solved)))
        quit_ = False
    else:
        sound.play("clear" if solved else "fail")
        quit_ = pager(stdscr, recap_title, recap_lines(q, earned, solved)) == "quit"
    return {"id": q["id"], "domain": q["domain"], "points": earned,
            "solved": solved, "steps": log, "quit": quit_, "answers": answers}


def answers_lines(results):
    """FEEDBACK AT THE END: every answer page and recap of the session, one after the other."""
    lines = [("Every step you played, in order: your answer, all the options and why.", A["dim"])]
    for n, r in enumerate(results, 1):
        if r.get("unreached"):
            continue
        lines += [("", 0), ("━" * 60, A["cyan"]),
                  (f"Q{n}  {r['id']}  ·  " + ("✔ solved" if r["solved"] else "✘ not solved")
                   + f"  ·  {r['points']} points", (A["green"] if r["solved"] else A["red"]) | A["bold"])]
        for title, page in r.get("answers", []):
            lines += [("", 0), (title.strip(), A["cyan"] | A["bold"]), ("", 0)] + page
    unreached = [r["id"] for r in results if r.get("unreached")]
    if unreached:
        lines += [("", 0), ("Not reached before the clock ran out: " + "  ".join(unreached), A["yellow"])]
    return lines


# --------------------------------------------------------------------------
# Review: the missed questions, kept between runs
# --------------------------------------------------------------------------

def review_path():
    return STATE_DIR / REVIEW_FILE


def load_review(path=None):
    """The ids to review, oldest miss first. A missing or damaged file is an empty list."""
    try:
        data = json.loads(Path(path or review_path()).read_text(encoding="utf-8"))
        ids = data.get("missed", []) if isinstance(data, dict) else []
        return [i for i in dict.fromkeys(ids) if isinstance(i, str)]
    except (OSError, ValueError):
        return []


def update_review(missed, results, learn=False):
    """The review list after a session: a question with a wrong (or skipped) step joins it; a solved
    one leaves it, except in LEARN mode, where the answer was shown. Questions quit before any wrong
    step or never reached (global clock) keep their place."""
    missed = list(missed)
    for r in results:
        if r["solved"] and not learn:
            missed = [i for i in missed if i != r["id"]]
        elif not r["solved"] and any(not s["ok"] for s in r["steps"]) and r["id"] not in missed:
            missed.append(r["id"])
    return missed


def save_review(missed, path=None):
    """Write the list (atomically); the game goes on if the disk says no."""
    path = Path(path or review_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"missed": missed}, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def review_questions(questions, missed=None):
    """The questions of the review list that still exist, in list order."""
    by_id = {q["id"]: q for q in questions}
    return [by_id[i] for i in (load_review() if missed is None else missed) if i in by_id]


# --------------------------------------------------------------------------
# HI-SCORES: one table per board (the kind of round x LEVEL)
# --------------------------------------------------------------------------

def scores_path():
    return STATE_DIR / SCORES_FILE


def load_scores(path=None):
    """{"boards": {board: [entry]}, "name": last initials}; a missing or damaged file is empty."""
    try:
        data = json.loads(Path(path or scores_path()).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("boards"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"boards": {}, "name": "AAA"}


def save_scores(data, path=None):
    path = Path(path or scores_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def score_board(choice, domains, args):
    """The table a round plays for, or None when its score compares with nothing: LEARN mode (the
    answers are shown), REVIEW MISSED, --id, --domain (all of it) and a shortened round."""
    if SETTINGS["learn"] or args.id or args.review:
        return None
    if choice == "exam":
        name = "CKA EXAM"
    elif choice == "survival":
        name = "SURVIVAL"
    elif choice in domains and not args.count:
        name = short_name(domains[choice])
    elif choice == "" and not args.count:
        name = "ALL QUESTIONS"
    else:
        return None
    return f"{name} · {SETTINGS['level'].upper()}"


def score_mode():
    """How the score was made, shown in its row: RECALL, HINTS, AUTO (autocomplete), END (feedback)."""
    tags = ["RECALL" if SETTINGS["recall"] else "EXAM"]
    tags += [t for t, on in (("HINTS", SETTINGS["hints"]), ("AUTO", SETTINGS["autocomplete"])) if on]
    return "+".join(tags)


def score_rank(table, score):
    """0-based place of a new score (after the equal ones: they were there first), None if it is off."""
    if score <= 0:
        return None
    rank = sum(e["score"] >= score for e in table)
    return rank if rank < TOP_SCORES else None


def add_score(data, board, entry):
    """Insert the entry in its board; returns its place (None if it did not make it)."""
    table = data["boards"].setdefault(board, [])
    rank = score_rank(table, entry["score"])
    if rank is not None:
        table.insert(rank, entry)
        del table[TOP_SCORES:]
        data["name"] = entry["name"]
    return rank


def survival(stdscr, domains, questions, args, sound):
    """SURVIVAL: endless random questions, LIVES lives; a question not solved costs one. With no life
    left, the credit's score goes to the HI-SCORES, then CONTINUE? 10..0: INSERT COIN (Enter) gives
    the lives back and the score starts again from 0. Returns every question's result."""
    pool = at_level(questions)
    board = score_board("survival", domains, args)
    results, credit, deck, credits = [], [], [], 1

    def end_credit():
        if board and credit:
            hi_scores(stdscr, board, finale_info(credit, pool, domains), sound)

    RUN["lives"] = LIVES
    try:
        n = 0
        while pool:
            if not deck:  # every question once, in a new random order each time round
                deck = random.sample(pool, len(pool))
            q = deck.pop()
            n += 1
            r = ask_question(stdscr, q, n, None, sum(x["points"] for x in credit), domains, sound)
            results.append(r)
            credit.append(r)
            if r["quit"]:
                end_credit()
                break
            if r["solved"]:
                continue
            RUN["lives"] -= 1
            retro.life_lost(stdscr, RUN["lives"], LIVES, sound)
            if RUN["lives"] == 0:
                end_credit()
                if not retro.continue_screen(stdscr, sound, read_key, CONTINUE_SECONDS):
                    break
                credits += 1
                credit = []
                RUN["lives"] = LIVES
        if results:
            save_review(update_review(load_review(), results, SETTINGS["learn"]))
            info = finale_info(results, pool, domains)
            info["won"] = False  # survival always ends the arcade way
            retro.finale(stdscr, info, sound)
    finally:
        RUN["lives"] = None
    return results


def hi_scores(stdscr, board, info, sound):
    """After the finale: the initials when the score makes the table, then the table."""
    data = load_scores()
    table = data["boards"].get(board, [])
    rank = score_rank(table, info["score"])
    if rank is not None:
        name = retro.enter_initials(stdscr, sound, read_key, data.get("name", "AAA"), [
            (f"YOU RANK {retro.ordinal(rank + 1)} ON", "cyan"), (board, "cyan"),
            (f"SCORE {info['score']}  ·  EXAM {info['percent']}%", "white"), ("ENTER YOUR INITIALS", "yellow")])
        add_score(data, board, {"name": name, "score": info["score"], "percent": info["percent"],
                                "mode": score_mode(), "date": time.strftime("%Y-%m-%d")})
        save_scores(data)
        table = data["boards"][board]
    if table:
        retro.high_scores(stdscr, sound, read_key, board, table, rank)


def scores_text(data):
    """--scores: every board, in the terminal."""
    if not data["boards"]:
        return f"No high score yet ({scores_path()}). Play a CKA EXAM or a domain round!"
    out = []
    for board, table in sorted(data["boards"].items()):
        out += [f"== {board} ==", *retro.score_rows(table), ""]
    return "\n".join(out).rstrip()


def rules_lines():
    b, n = A["bold"], A["normal"]
    return [
        ("Train like the exam: read the task, choose the right command and TYPE it in full.", n),
        ("CKA EXAM: 15-20 random questions in the exam's domain proportions. A domain line", n),
        ("plays a short round: its share of a 20-question exam (e.g. STORAGE 2/20 = 2).", n),
        ("SURVIVAL: endless random questions and 3 lives (red hearts); a question not solved", n),
        ("costs one. Out of lives: CONTINUE? 10..0, Enter = INSERT COIN (score from 0).", n),
        ("REVIEW MISSED (once you missed one): replays only the questions you got wrong, in", n),
        ("any run, at any LEVEL. Solve one and it leaves the list (also: --review).", n), ("", 0),
        ("LEVEL (title menu, LEFT/RIGHT): EASY everyday tasks, MEDIUM + medium, HARD all.", n),
        ("", 0),
        ("OPTIONS (title menu)", b),
        ("  MODE   EXAM, or LEARN: the right answer is marked, every answer links to the", n),
        ("         Kubernetes docs, and the results are tagged LEARN MODE (also: --learn).", n),
        ("         Or RECALL: the options are hidden, type the answer from memory; they", n),
        ("         appear as a hint at half of the step's time (also: --recall).", n),
        ("  HINTS  OFF, or ON: each step shows a short hint (where to look, what matters),", n),
        ("         never the answer itself (also: --hints).", n),
        ("  FEEDBACK  EACH STEP, or AT THE END like the exam: no verdict while you play, a", n),
        ("         wrong step goes on; all the answers come after the score (--feedback end).", n),
        ("  AUTOCOMPLETE  OFF, or ON: Tab completes commands, names and flags, like kubectl's", n),
        ("         completion in the exam (from the question's commands; not in YAML files).", n),
        ("  TIMER  BY QUESTION (a countdown per step) or GLOBAL: one clock for the session,", n),
        ("         120 min for the CKA EXAM. When it runs out, the rest counts as failed.", n),
        ("  MUSIC  ON / OFF.   SOUND  MONO, or STEREO with harmonies, echoes and wide fireworks.", n),
        ("", 0),
        ("Typing another option = choosing it. Typos count as wrong, like in a real shell.", n),
        ("A leading 'k ' is accepted as 'kubectl '. Copy/paste is blocked.", n), ("", 0),
        ("Countdown", b),
        ("  Every step has its own timer (max 2 min): green, yellow at 50 %, red at 25 %,", n),
        ("  where it ticks each second (GLOBAL timer: in its last minute).", n),
        ("  100 points per correct step + up to 50 speed bonus.", n), ("", 0),
        ("Steps", b),
        ("  Command step: Enter submits, '\\' + Enter continues on a new line.", n),
        ("  Edit step: a file opens in a multiline editor. Enter = new line, Ctrl+D submits.", n),
        ("  Ctrl+X skips a step, Ctrl+C quits.", n), ("", 0),
        ("A wrong step ends the question: read the explanation and the recap, then try again!", n),
        ("", 0),
        ("HI-SCORES", b),
        ("  The CKA EXAM, each domain round, ALL QUESTIONS and SURVIVAL (per credit) keep a", n),
        ("  top 10 per LEVEL: make it and you enter your initials. Not in LEARN mode", n),
        ("  (also: --scores lists them).", n),
        ("", 0),
        ("Credits", b),
        ("  RECALL, HINTS and the all-options answer page come from CKS Megatrainer,", n),
        ("  by @fedir, also free software under the GNU GPL v3.", n),
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


def pick_domain_round(questions, domains, domain, rnd=random, n=None):
    """A short round on one domain: its share of a 20-question exam (storage: 2), random."""
    pool = [q for q in questions if q["domain"] == domain]
    n = n or exam_plan(domains, EXAM_MAX)[domain]
    return rnd.sample(pool, min(n, len(pool)))


def at_level(questions, level=None):
    """The questions a LEVEL plays: EASY the easy ones, MEDIUM easy + medium, HARD all."""
    top = LEVELS[level or SETTINGS["level"]]
    return [q for q in questions if q["difficulty"] <= top]


def global_budget(questions, exam):
    """GLOBAL timer: the CKA's 120 minutes for a full exam, else the sum of the steps' own times."""
    if exam:
        return EXAM_SECONDS
    return sum(min(s.get("time_limit", MAX_TIME), MAX_TIME) for q in questions for _, s in correct_path(q))


def option_rows(questions, sound):
    """The OPTIONS screen: each row edits SETTINGS (or the sound) as soon as it changes."""
    def setter(key, mapping):
        return lambda value: SETTINGS.update({key: mapping[value]})

    music_ok = sound.enabled and sound.player is not None
    rows = [
        {"label": "MODE", "values": ["EXAM", "LEARN", "RECALL"],
         "index": 1 if SETTINGS["learn"] else 2 if SETTINGS["recall"] else 0,
         "on_change": lambda value: SETTINGS.update(learn=value == "LEARN", recall=value == "RECALL"),
         "hints": {"EXAM": "Like the real exam: the answers are not shown.",
                   "LEARN": "The right answer is marked and every answer links to the Kubernetes docs. "
                            "Results are tagged LEARN MODE.",
                   "RECALL": "The options are hidden: type the answer from memory. They appear as a hint "
                             "when half of the step's time is gone."}},
        {"label": "HINTS", "values": ["OFF", "ON"], "index": int(SETTINGS["hints"]),
         "on_change": setter("hints", {"OFF": False, "ON": True}),
         "hints": {"OFF": "No help on the question screen, like the exam.",
                   "ON": "Each step shows a short hint under its task: where to look, what matters. "
                         "Never the answer itself."}},
        {"label": "FEEDBACK", "values": ["EACH STEP", "AT THE END"],
         "index": int(SETTINGS["feedback"] == "end"),
         "on_change": setter("feedback", {"EACH STEP": "step", "AT THE END": "end"}),
         "hints": {"EACH STEP": "After each step: right or wrong, every option and why. A wrong step "
                                "ends the question.",
                   "AT THE END": "Like the exam: no verdict, no score while you play, a wrong step goes on. "
                                 "All the answers come after the final score. (LEARN mode: each step.)"}},
        {"label": "AUTOCOMPLETE", "values": ["OFF", "ON"], "index": int(SETTINGS["autocomplete"]),
         "on_change": setter("autocomplete", {"OFF": False, "ON": True}),
         "hints": {"OFF": "Type every character, Tab inserts 2 spaces.",
                   "ON": "Tab completes commands, names and flags like kubectl's completion in the "
                         "exam, from the words of the question's commands. Not in YAML files."}},
        {"label": "TIMER", "values": ["BY QUESTION", "GLOBAL"], "index": int(SETTINGS["timer"] == "global"),
         "on_change": setter("timer", {"BY QUESTION": "question", "GLOBAL": "global"}),
         "hints": {"BY QUESTION": "Each step has its own countdown.",
                   "GLOBAL": "One countdown for the whole session, like the CKA: 120 min for the CKA EXAM, "
                             "the sum of the steps' times for a shorter round. When it runs out, it is over."}},
        {"label": "MUSIC", "values": ["ON", "OFF"] if music_ok else ["OFF"],
         "index": 0 if not music_ok or sound.music_on else 1,
         "on_change": lambda value: sound.set_music(value == "ON") if music_ok else None,
         "hints": {"ON": "The title, level and finale themes play.", "OFF": "No music; sound effects stay."
                   if music_ok else ("Sound is off for this run (--silent)." if not sound.enabled
                                     else "No audio player was found (pw-play, paplay, aplay, afplay).")}},
        {"label": "SOUND", "values": ["MONO", "STEREO"], "index": int(SETTINGS["stereo"]),
         "on_change": lambda value: (SETTINGS.update(stereo=value == "STEREO"),
                                     sound.set_stereo(value == "STEREO")),
         "hints": {"MONO": "Classic 8-bit, one channel.",
                   "STEREO": "Wide 8-bit: harmonies, arpeggios bouncing between the speakers, echoes, "
                             "and fireworks heard where they burst."}},
    ]
    return rows


def options_status(sound):
    """One line under the title menu: the current OPTIONS."""
    return "  ·  ".join([
        "LEARN" if SETTINGS["learn"] else "RECALL" if SETTINGS["recall"] else "EXAM",
        *(["HINTS"] if SETTINGS["hints"] else []),
        *(["FEEDBACK AT END"] if feedback_at_end() else []),
        *(["AUTOCOMPLETE"] if SETTINGS["autocomplete"] else []),
        "GLOBAL TIMER" if SETTINGS["timer"] == "global" else "TIMER BY QUESTION",
        "STEREO" if SETTINGS["stereo"] else "MONO",
        sound.status])


def menu(stdscr, domains, questions, sound):
    """Arcade title screen. Returns 'exam', a domain id, '' for all, or None to quit."""
    share = exam_plan(domains, EXAM_MAX)  # e.g. storage: 2 of a 20-question exam

    def make_items():
        pool = at_level(questions)
        counts = {d: sum(q["domain"] == d for q in pool) for d in domains}
        items = [("exam", "CKA EXAM", f"{EXAM_MIN}-{EXAM_MAX}Q")]
        items += [(d, short_name(v), f"{share[d]}/{EXAM_MAX} {v['weight']:>2}%")
                  for d, v in domains.items() if counts[d]]
        missed = review_questions(questions)
        items.append(("", "ALL QUESTIONS", f"{len(pool)}Q"))
        items.append(("survival", "SURVIVAL", f"{LIVES} LIVES"))
        if missed:
            items.append(("review", "REVIEW MISSED", f"{len(missed)}Q"))
        return items + [
                        ("level", "LEVEL", SETTINGS["level"].upper()),  # changed in place
                        ("help", "HOW TO PLAY", ""), ("options", "OPTIONS", ""), (None, "QUIT", "")]

    def cycle_level(step):
        names = list(LEVELS)
        SETTINGS["level"] = names[(names.index(SETTINGS["level"]) + step) % len(names)]
        return make_items()

    sel = 0
    while True:
        items = make_items()
        sel = min(sel, len(items) - 1)
        sel = retro.title_screen(stdscr, items, sel, sound, read_key, status=options_status(sound),
                                 cycle={"level": cycle_level})
        if sel is None or items[sel][0] is None:
            return None
        value, label, _ = items[sel]
        if value == "help":
            pager(stdscr, " HOW TO PLAY", rules_lines(), footer="Enter: back")
            continue
        if value == "options":
            retro.options_screen(stdscr, option_rows(questions, sound), sound, read_key)
            continue
        retro.get_ready(stdscr, label, sound)
        return value


def summary_lines(results, domains):
    total_pts = sum(r["points"] for r in results)
    steps = [s for r in results for s in r["steps"]]
    good = [s for s in steps if s["ok"]]
    solved = sum(r["solved"] for r in results)
    lines = [("Learn mode: the answers were shown", A["green"])] if SETTINGS["learn"] else []
    lines += [(f"Score: {total_pts} points", A["bold"]),
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
                  (f"  Replay them with: python3 {sys.argv[0]} " + ("--learn " if SETTINGS["learn"] else "")
                   + " ".join(f"--id {m}" for m in missed), A["dim"]),
                  ("  or every question you missed so far: REVIEW MISSED in the menu, or --review", A["dim"])]
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
            "pods": pods, "review": [r["id"] for r in results if not r["solved"]],
            "learn": SETTINGS["learn"], "unreached": sum(bool(r.get("unreached")) for r in results)}


def play(stdscr, domains, questions, args):
    init_colors(stdscr)
    sys.stdout.write("\x1b[?2004h")  # bracketed paste: terminal marks pasted text
    sys.stdout.flush()
    sound = retro.Sound(enabled=not SETTINGS["silent"])
    sound.set_stereo(SETTINGS["stereo"])
    try:
        return session(stdscr, domains, questions, args, sound)
    finally:
        sound.stop_music()
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def session(stdscr, domains, questions, args, sound):
    choice = "exam" if args.exam else "review" if args.review else "survival" if args.survival else None
    if not args.id and args.domain is None and choice is None:
        choice = menu(stdscr, domains, questions, sound)
        if choice is None:
            return []
    if choice == "survival":
        return survival(stdscr, domains, questions, args, sound)
    if choice == "review":  # what you missed, whatever its level
        questions = review_questions(questions)
    elif not args.id:  # explicit --id plays exactly what was asked
        questions = at_level(questions)
    if choice == "exam":
        questions = pick_exam(questions, domains, n=args.count)
    elif choice and choice != "review":  # a domain line of the menu: its share of a 20-question exam
        questions = pick_domain_round(questions, domains, choice, n=args.count)
    else:  # ALL QUESTIONS, REVIEW MISSED, --domain (all its questions) or --id
        if args.shuffle:
            random.shuffle(questions)
        if args.count:
            questions = questions[:args.count]
    clock = None
    if SETTINGS["timer"] == "global":
        total = global_budget(questions, exam=choice == "exam")
        clock = {"total": total, "deadline": time.monotonic() + total}
    results, score = [], 0
    for n, q in enumerate(questions, 1):
        r = ask_question(stdscr, q, n, len(questions), score, domains, sound, clock)
        results.append(r)
        score += r["points"]
        if r["quit"] or r.get("timeup"):
            break
    if clock and results and results[-1].get("timeup"):  # what the clock left unreached counts as failed
        results += [{"id": q["id"], "domain": q["domain"], "points": 0, "solved": False, "steps": [],
                     "quit": False, "unreached": True} for q in questions[len(results):]]
    if results:
        save_review(update_review(load_review(), results, SETTINGS["learn"]))
        info = finale_info(results, questions, domains)
        retro.finale(stdscr, info, sound)
        board = score_board(choice, domains, args)
        if board:
            hi_scores(stdscr, board, info, sound)
        if any(r.get("answers") for r in results):  # FEEDBACK AT THE END: now the answers
            pager(stdscr, " THE ANSWERS", answers_lines(results), footer="Enter / q: done")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Timed, type-your-answer CKA trainer.")
    ap.add_argument("--db", default=DEFAULT_DB, help="question database (JSON)")
    ap.add_argument("--domain", help="all the questions of this domain id (skips the menu; "
                                     "a domain line of the menu plays a short round instead)")
    ap.add_argument("--id", action="append", help="only this question id (repeatable)")
    ap.add_argument("--exam", action="store_true",
                    help=f"CKA-like exam: {EXAM_MIN}-{EXAM_MAX} random questions in domain proportions (skips the menu)")
    ap.add_argument("--review", action="store_true",
                    help="replay only the questions you missed (kept in ~/.cka_megatrainer/review.json "
                         "until solved; CKA_MEGATRAINER_HOME moves it)")
    ap.add_argument("--survival", action="store_true",
                    help=f"SURVIVAL: endless random questions, {LIVES} lives, CONTINUE? when they are gone")
    ap.add_argument("--count", type=int, help="maximum number of questions (with --exam: the exam size)")
    ap.add_argument("--no-shuffle", dest="shuffle", action="store_false", help="keep database order")
    ap.add_argument("--allow-paste", action="store_true", help="do not block pasted input")
    ap.add_argument("--silent", action="store_true", help="no music or sound effects")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--learn", action="store_true",
                      help="learn mode: the right answer is marked with a green check, and each answer "
                           "page links to the Kubernetes documentation")
    mode.add_argument("--recall", action="store_true",
                      help="recall mode: the options are hidden, type the answer from memory; they appear "
                           "when half of the step's time is gone")
    ap.add_argument("--hints", action="store_true",
                    help="each step shows a short pedagogical hint under its task (never the answer)")
    ap.add_argument("--feedback", choices=["step", "end"], default="step",
                    help="step: the answer after each step (default); end: like the exam, no verdict "
                         "while you play, all the answers after the final score")
    ap.add_argument("--level", choices=list(LEVELS), default="hard",
                    help="easy: easy questions only; medium: easy + medium; hard: all (default)")
    ap.add_argument("--timer", choices=["question", "global"], default="question",
                    help="question: a countdown per step (default); global: one clock for the session "
                         "(120 min for a CKA exam)")
    ap.add_argument("--stereo", action="store_true", help="stereo sound with more voices")
    ap.add_argument("--autocomplete", action="store_true",
                    help="Tab completes commands, names and flags, like kubectl's completion in the exam")
    ap.add_argument("--theme", choices=retro.THEMES, default="retro",
                    help="retro: vivid RGB on black (default); soft: the terminal's own colours")
    ap.add_argument("--validate", action="store_true", help="check the database and exit")
    ap.add_argument("--list", action="store_true", help="list questions and exit")
    ap.add_argument("--scores", action="store_true", help="print the high-score tables and exit")
    args = ap.parse_args(argv)
    SETTINGS["allow_paste"] = args.allow_paste
    SETTINGS["silent"] = args.silent
    SETTINGS["learn"], SETTINGS["recall"], SETTINGS["hints"] = args.learn, args.recall, args.hints
    SETTINGS.update(level=args.level, timer=args.timer, feedback=args.feedback, stereo=args.stereo, autocomplete=args.autocomplete)
    retro.set_theme(args.theme)

    try:
        domains, questions = load_db(args.db)
    except (OSError, ValueError, KeyError) as e:
        sys.exit(f"Database error: {e}")

    if args.validate:
        nsteps = sum(len(q["steps"]) for q in questions)
        print(f"OK: {len(questions)} questions, {nsteps} steps, {len(domains)} domains")
        return
    if args.scores:
        print(scores_text(load_scores()))
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
    if args.review and not review_questions(questions):
        sys.exit(f"Nothing to review: no missed question in {review_path()} yet. Play a round first!")
    try:
        results = curses.wrapper(play, domains, questions, args)
    except KeyboardInterrupt:
        print("Bye - keep practising!")
        return
    if results:  # stays in the terminal after the game closes
        print("\n".join(line[0] for line in summary_lines(results, domains)))


if __name__ == "__main__":
    main()
