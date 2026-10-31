# Contributing to CKA Megatrainer

Thanks for helping! Issues and pull requests (merge requests) are welcome.

The project is licensed under the [GNU General Public License v3.0](LICENSE). By submitting a
pull request, you agree that your contribution (code, questions, images) is licensed under the
same terms.

## Reporting how it runs on your platform

The game has only been tested on Fedora Silverblue (see the README). Please open an issue with:

- OS / distribution and version, desktop session (Wayland or X11)
- terminal emulator, and whether you used `make start-exam` (game window) or `make start-exam-here`
- Python version (`python3 --version`)
- what worked, what did not (a screenshot helps), the output of `make test`

## Before opening a pull request

```bash
make test           # validates questions.json + runs the unit tests
make check-links    # only when you touched documentation links (needs network)
```

- **Python standard library only**: no pip dependencies.
- **English** for all UI text and question content.
- For UI changes, look at the result: `make screenshot` (needs tmux) and check 80x24 as well as
  100x38. If the screens shown in the README change, regenerate them with `make readme-media`.
- Sound must stay optional: no player, `--silent` or a failing player must never break the game.
  All music is original: please do not add melodies from existing games.

## Adding questions

Questions live in `questions.json`. Each one is a small tree of **steps**; every step is a
multiple choice where the player must **type** the chosen answer.

```jsonc
{
  "id": "TRB-004",              // unique; prefix = domain: CLU, WRK, STO, NET, TRB
  "domain": "troubleshooting",  // one of the ids in "domains"
  "topic": "…", "difficulty": 1,  // 1-3
  "title": "…", "scenario": "…",
  "tip": "an exam tip shown in the recap",
  "docs": [                      // learn mode links these on the answer pages (1-3 entries)
    {"title": "Using RBAC Authorization",
     "url": "https://kubernetes.io/docs/reference/access-authn-authz/rbac/"}
  ],
  "stakes": "what goes wrong in real life if you get this wrong (shown when the question fails)",
  "analogy": "a familiar comparison that makes the idea stick (shown in the recap)",
  "start": "s1",
  "steps": {
    "s1": {
      "type": "command",
      "time_limit": 60,          // seconds, max 120
      "context": ["optional terminal output shown to the player"],
      "prompt": "Step 1 - …",
      "options": [
        {"cmd": "kubectl …", "correct": true, "aliases": ["an equivalent form"], "next": "s2"},
        {"cmd": "kubectl …", "why": "why this plausible command is wrong"}
      ],
      "explain": "the lesson, shown after the answer"
    },
    "s2": {
      "type": "edit",
      "filename": "pod.yaml",
      "prefill": ["the file", "as the player first sees it"],
      "target": ["the prefill lines the right snippet replaces"],
      "options": [
        {"correct": true, "snippet": ["  lines with their real indentation"]},
        {"why": "why this trap is wrong", "snippet": ["…"]}
      ],
      "checks": [{"regex": "…", "msg": "shown when the regex does NOT match"}],
      "forbid": [{"regex": "…", "msg": "shown when the regex matches"}],
      "recap": "one line for the end-of-question recap",
      "next": null,
      "explain": "…"
    }
  }
}
```

Rules (`./cka_trainer.py --validate` enforces most of them):

- Every step has 2–8 options, exactly **one** `correct`, and every wrong option has a `why`.
- Wrong options must be **plausible mistakes** and **really wrong** for Kubernetes v1.37. When two
  forms are valid, put the second in `aliases`.
- Edit steps are judged by `checks` / `forbid` on the whole file (regexes, multiline mode).
  `--validate` replaces `target` in `prefill` with each snippet and requires the correct one to
  pass and **every trap to fail**. If a trap passes, tighten the checks; never weaken the trap.
- **Documentation links** (`docs`, 1–3 per question, required for new questions) are shown in
  learn mode (`make start-learn`). Link the official page that explains what the question
  teaches: prefer <https://kubernetes.io/docs/> (use <https://helm.sh/docs/> for Helm, which
  kubernetes.io does not cover). Open every link in a browser and make sure the page really
  covers the topic. Use the current address without a version or language prefix, with no
  `#anchor` (anchors break silently) and no redirect. Keep each URL at 95 characters or fewer, so
  it stays on one line in the game window and remains clickable: when the page you want has a long
  address, link its parent concept page instead. `make check-links` fetches every link and
  reports the ones that moved or broke: run it before submitting and, when it flags a link,
  replace it with the new address.
- **Teaching lines** (`stakes` and `analogy`, required for new questions, one line each, at most 150
  characters). `stakes` is a concrete, slightly uncomfortable consequence in the real world ("one
  typo and kubectl itself is dead"), not a generic warning. `analogy` explains the mechanism by a
  comparison from everyday life ("a Role is a key card for one floor"). Both must be true to the
  question: check them against the `tip`, and keep the analogy faithful to the mechanism, since a
  cute but wrong picture teaches the wrong thing. They appear in the recap, next to the `tip`.
- **`explain`** answers "why is this the right choice?", not "what is the next command". Give the
  reason or the mechanism that makes the answer correct, tied to the scenario, and name what would
  go wrong with the closest alternative; follow-up tips and verification hints come after the
  reason, not instead of it. Keep it to about 330 characters (a test enforces it) and check every
  technical claim against the documentation, including version-specific ones. Do not quote numbers
  that depend on a history the player has not seen (such as "it comes back as revision 5").
- **Wrong options carry the teaching**: each `why` should say what would actually happen with that
  command (the concrete symptom), not just that it is wrong. Learn mode shows the `why` of every
  wrong option on every answer page, even after a correct answer, so write them as small lessons.
- Keep snippets short (≤ 8 lines, 3–4 options): they share the screen with the editor.
- Keep the library in the CKA proportions: **one question per percent of weight** per 100 questions
  (Troubleshooting 30 %, Cluster Architecture 25 %, Services & Networking 20 %, Workloads &
  Scheduling 15 %, Storage 10 %). The tests check the exact counts: add questions in proportional
  batches (the smallest exact batch is 20: 6/5/4/3/2) and update `test_hundred_questions_in_cka_proportions`.
- **Step by step**: (1) pick the domain and the next free id, e.g. `STO-011`; (2) write the
  question in a fragment file `{"questions": [...]}`, copying the shape above (a first step that
  generates or inspects, then the fix, then the verification, 2–4 steps); (3) add its `docs`, `stakes` and `analogy`;
  (4) check it with `tools/validate_fragment.py`; (5) merge it with `tools/merge_questions.py`,
  which also puts `questions.json` in house style; (6) run `make test`, `make check-links`,
  then play it with `./cka_trainer.py --learn --id STO-011` to see that the green check marks
  the right option and the links open the right pages; (7) because the library keeps the exam
  proportions, send questions in proportional batches (see above).
- To add several questions, write them in a separate file `{"questions": [...]}` and check it
  with `python3 tools/validate_fragment.py FILE.json PREFIX FIRST LAST` (stricter than
  `--validate`), then merge it with `python3 tools/merge_questions.py questions.json questions.json FILE.json`.
- Read your question in the game at 100x38 and at 80x24 (`make screenshot`): at 80x24 long
  `context` output is trimmed from the top, so put the essential lines last.
