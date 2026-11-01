"""Every question must have a test that runs its answers against a real cluster.

This test needs no cluster: it only checks that each question id is exercised by a test in this
folder. A contributor who cannot write the cluster test yet lists the id in iaac/PENDING.txt
("ID reason"); the maintainer then adds the test. A stale PENDING line (the question is covered
now) fails too, so the list only ever shrinks.

    python3 iaac/tests/test_coverage.py --table     # which tests exercise which question
"""
import re
import sys
import unittest
from pathlib import Path

from lib import QUESTIONS

HERE = Path(__file__).resolve().parent
PENDING_FILE = HERE.parent / "PENDING.txt"
ID = re.compile(r"\b((?:CLU|WRK|STO|NET|TRB)-\d{3})\b")
NAME = re.compile(r"def (test_((?:clu|wrk|sto|net|trb)\d{3})(?:_((?:clu|wrk|sto|net|trb)\d{3}))?\w*)")


def covering_tests():
    """{question id: [test names]}: ids found in a test's name or in the answer()/final_file() calls
    of its body."""
    found = {}
    for path in sorted(HERE.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        parts = re.split(r"(?m)^    def (?=test_)", text)
        for part in parts[1:]:
            name = part.split("(")[0]
            ids = set(ID.findall(part))
            ids |= {f"{m[:3].upper()}-{m[3:]}" for m in re.findall(r"^test_((?:clu|wrk|sto|net|trb)\d{3})", name)}
            ids |= {f"{m[:3].upper()}-{m[3:]}" for m in re.findall(r"_((?:clu|wrk|sto|net|trb)\d{3})", name)}
            for qid in ids:
                found.setdefault(qid, []).append(f"{path.stem}.{name}")
    return found


def pending():
    if not PENDING_FILE.exists():
        return {}
    rows = [ln.split(None, 1) for ln in PENDING_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    return {r[0]: (r[1] if len(r) > 1 else "") for r in rows}


class Coverage(unittest.TestCase):
    def test_every_question_is_tested_or_pending_with_a_reason(self):
        covered, wait = covering_tests(), pending()
        missing = [q["id"] for q in QUESTIONS if q["id"] not in covered and q["id"] not in wait]
        self.assertEqual(missing, [], "no cluster test for these questions: add one in iaac/tests (see "
                                       "iaac/README.md) or list the id in iaac/PENDING.txt with a reason")

    def test_pending_list_has_no_stale_or_unexplained_lines(self):
        covered, wait = covering_tests(), pending()
        known = {q["id"] for q in QUESTIONS}
        self.assertEqual([i for i in wait if i not in known], [], "PENDING.txt names unknown questions")
        self.assertEqual([i for i in wait if i in covered], [], "these are tested now: remove them from PENDING.txt")
        self.assertEqual([i for i, why in wait.items() if not why.strip()], [], "give a reason for each PENDING id")


def table():
    covered = covering_tests()
    for q in QUESTIONS:
        tests = covered.get(q["id"], [])
        print(f"{q['id']:8} {len(tests)} test(s)  {', '.join(t.split('.', 1)[1] for t in tests) or '-'}")
    print(f"\n{sum(1 for q in QUESTIONS if q['id'] in covered)}/{len(QUESTIONS)} questions covered")


if __name__ == "__main__":
    if "--table" in sys.argv:
        table()
    else:
        unittest.main()
