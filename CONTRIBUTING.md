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
make test        # validates questions.json + runs the unit tests
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
- Keep snippets short (≤ 8 lines, 3–4 options): they share the screen with the editor.
- Keep the library in the CKA proportions: **one question per percent of weight** per 100 questions
  (Troubleshooting 30 %, Cluster Architecture 25 %, Services & Networking 20 %, Workloads &
  Scheduling 15 %, Storage 10 %). The tests check the exact counts: add questions in proportional
  batches (the smallest exact batch is 20: 6/5/4/3/2) and update `test_hundred_questions_in_cka_proportions`.
- To add several questions, write them in a separate file `{"questions": [...]}` and check it
  with `python3 tools/validate_fragment.py FILE.json PREFIX FIRST LAST` (stricter than
  `--validate`), then merge it with `python3 tools/merge_questions.py questions.json questions.json FILE.json`.
- Read your question in the game at 100x38 and at 80x24 (`make screenshot`): at 80x24 long
  `context` output is trimmed from the top, so put the essential lines last.
