#!/usr/bin/env python3
"""Merge validated fragments into questions.json, sorted by domain then id, in house style
(one-line option objects, file lines one per line). Also used to re-format the database.

usage: python3 tools/merge_questions.py questions.json questions.json [FRAGMENT.json ...]"""
import json, sys

ORDER = ["cluster", "workloads", "storage", "networking", "troubleshooting"]


def j(v):
    return json.dumps(v, ensure_ascii=False)


def fmt(v, ind=0):
    pad, pad2 = "  " * ind, "  " * (ind + 1)
    if isinstance(v, dict):
        scalar = all(not isinstance(x, (dict, list)) for x in v.values())
        if scalar and len(j(v)) <= 200:
            return j(v)
        if "snippet" in v:  # option with a snippet: header on one line, lines below
            head = ", ".join(f"{j(k)}: {j(x)}" for k, x in v.items() if k != "snippet")
            lines = (",\n" + pad2 + "  ").join(j(s) for s in v["snippet"])
            return "{" + head + (", " if head else "") + '"snippet": [\n' + pad2 + "  " + lines + "]}"
        items = [f"{pad2}{j(k)}: {fmt(x, ind + 1)}" for k, x in v.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(isinstance(x, str) for x in v):
            if len(j(v)) <= 90:
                return j(v)
            return "[\n" + ",\n".join(pad2 + j(x) for x in v) + "\n" + pad + "]"
        return "[\n" + ",\n".join(pad2 + fmt(x, ind + 1) for x in v) + "\n" + pad + "]"
    return j(v)


out, base, frags = sys.argv[1], sys.argv[2], sys.argv[3:]
db = json.load(open(base, encoding="utf-8"))
for f in frags:
    db["questions"] += json.load(open(f, encoding="utf-8"))["questions"]
ids = [q["id"] for q in db["questions"]]
assert len(ids) == len(set(ids)), "duplicate ids"
db["questions"].sort(key=lambda q: (ORDER.index(q["domain"]), q["id"]))
text = "{\n" + '  "domains": [\n' + ",\n".join("    " + j(d) for d in db["domains"]) + "\n  ],\n"
text += '  "questions": ' + fmt(db["questions"], 1) + "\n}\n"
assert json.loads(text) == db, "formatter changed the data"
open(out, "w", encoding="utf-8").write(text)
print(f"{out}: {len(db['questions'])} questions")
