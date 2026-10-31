#!/usr/bin/env python3
"""Validate a batch of NEW questions (a fragment file) before merging it into questions.json.

Stricter than --validate: id range, domain, 2-4 steps, 3-4 options, explain/tip/recap, command
length (<= 130), correct command accepted by the judge, share of questions with an edit step.

usage: python3 tools/validate_fragment.py FRAGMENT.json PREFIX FIRST LAST
  e.g. python3 tools/validate_fragment.py new-clu.json CLU 26 31   (ids CLU-026 .. CLU-031)
"""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cka_trainer as t

DOMAIN = {"CLU": "cluster", "WRK": "workloads", "STO": "storage", "NET": "networking", "TRB": "troubleshooting"}
path, prefix, first, last = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
domains, existing = t.load_db(t.DEFAULT_DB)
frag = json.load(open(path, encoding="utf-8"))
qs = frag["questions"]
errors, warnings = [], []
expected = {f"{prefix}-{i:03d}" for i in range(first, last + 1)}
got = [q.get("id") for q in qs]
if set(got) != expected or len(got) != len(expected):
    errors.append(f"ids must be exactly {prefix}-{first:03d}..{prefix}-{last:03d}; missing {sorted(expected - set(got))}, extra {sorted(set(got) - expected)}")
seen = {q["id"] for q in existing}
edit_qs = 0
for q in qs:
    qid = q.get("id", "?")
    try:
        t.validate_question(q, domains, seen)
    except (ValueError, KeyError, TypeError, IndexError) as e:
        errors.append(f"{qid}: {type(e).__name__}: {e}")
        continue
    if q["domain"] != DOMAIN[prefix]:
        errors.append(f"{qid}: domain must be '{DOMAIN[prefix]}'")
    for k in ("topic", "tip", "difficulty", "docs"):
        if k not in q:
            errors.append(f"{qid}: missing '{k}'")
    path_steps = t.correct_path(q)
    if not 2 <= len(path_steps) <= 4:
        errors.append(f"{qid}: the solution path must have 2-4 steps (has {len(path_steps)})")
    if any(s["type"] == "edit" for _, s in path_steps):
        edit_qs += 1
    for sid, s in q["steps"].items():
        if not s.get("explain"):
            errors.append(f"{qid}/{sid}: missing 'explain'")
        if not 3 <= len(s["options"]) <= 4:
            errors.append(f"{qid}/{sid}: needs 3-4 options")
        if not 30 <= s.get("time_limit", 0) <= 120:
            errors.append(f"{qid}/{sid}: time_limit must be 30-120")
        if s["type"] == "command":
            for o in s["options"]:
                if len(o["cmd"]) > 130 or "\n" in o["cmd"]:
                    errors.append(f"{qid}/{sid}: command too long / multi-line: {o['cmd'][:60]}...")
                # the judge's typo detection would mistake near-identical options
            good = t.correct_option(s)
            res = t.evaluate_command(s, good["cmd"])
            if not res["ok"]:
                errors.append(f"{qid}/{sid}: correct command not accepted")
        else:
            if len(t.lines_of(t.correct_option(s)["snippet"])) > 10:
                warnings.append(f"{qid}/{sid}: snippet longer than 10 lines")
            if "recap" not in s:
                errors.append(f"{qid}/{sid}: edit step without 'recap'")
    blob = json.dumps(q)
    if re.search(r"[à-ÿ]", blob):
        warnings.append(f"{qid}: non-ASCII accented text? (content must be English)")
print(f"{len(qs)} questions, {edit_qs} with an edit step ({100 * edit_qs // max(1, len(qs))}%; aim >= 30%)")
for w in warnings:
    print("WARNING:", w)
for e in errors:
    print("ERROR:", e)
print("RESULT:", "OK" if not errors else f"{len(errors)} error(s)")
sys.exit(1 if errors else 0)
