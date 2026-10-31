#!/usr/bin/env python3
"""Check the documentation links of questions.json (the "docs" lists shown in learn mode).

A link is fine when it answers 200 at the very address written in the database: a redirect means
the page moved and the link should be updated. Needs network access. Exit status 1 on any problem.

usage: python3 tools/check_links.py [questions.json]"""
import concurrent.futures
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(url):
    req = urllib.request.Request(url, headers={"User-Agent": "cka-megatrainer-link-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            if r.geturl().rstrip("/") != url.rstrip("/"):
                return f"moved to {r.geturl()}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"unreachable ({e})"
    return None


def main():
    db = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "questions.json")
                    .read_text(encoding="utf-8"))
    users = {}
    for q in db["questions"]:
        for d in q.get("docs", []):
            users.setdefault(d["url"], []).append(q["id"])
    missing = [q["id"] for q in db["questions"] if not q.get("docs")]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = dict(zip(users, pool.map(check, users)))
    problems = {u: r for u, r in results.items() if r}
    for url, why in problems.items():
        print(f"{why}: {url}  (used by {', '.join(users[url])})")
    for qid in missing:
        print(f"no docs: {qid}")
    print(f"{len(users)} links checked, {len(problems)} problem(s), {len(missing)} question(s) without docs")
    sys.exit(1 if problems or missing else 0)


if __name__ == "__main__":
    main()
