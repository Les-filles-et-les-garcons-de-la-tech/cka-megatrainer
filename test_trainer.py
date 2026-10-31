import copy
import unittest

import cka_trainer as t

DOMAINS, QUESTIONS = t.load_db(t.DEFAULT_DB)
BY_ID = {q["id"]: q for q in QUESTIONS}


EDIT_STEPS = [(q["id"], s) for q in QUESTIONS for s in q["steps"].values() if s["type"] == "edit"]


class Database(unittest.TestCase):
    def test_hundred_questions_in_cka_proportions(self):
        self.assertEqual(len(QUESTIONS), 100)
        counts = {d: sum(q["domain"] == d for q in QUESTIONS) for d in DOMAINS}
        self.assertEqual(counts, {d: v["weight"] for d, v in DOMAINS.items()})  # 1 question per %

    def test_every_question_has_a_tip_and_explained_steps(self):
        for q in QUESTIONS:
            self.assertTrue(q.get("tip"), q["id"])
            for s in q["steps"].values():
                self.assertTrue(s.get("explain"), q["id"])

    def test_every_question_links_the_documentation(self):
        for q in QUESTIONS:
            self.assertTrue(q.get("docs"), q["id"])
            for d in q["docs"]:
                # one URL per line in the 100-column game window, so the terminal can click it
                self.assertLessEqual(len(d["url"]) + 2, 97, d["url"])

    def test_bad_docs_rejected(self):
        q = copy.deepcopy(BY_ID["NET-001"])
        q["docs"] = [{"title": "no url"}]
        with self.assertRaises(ValueError):
            t.validate_question(q, DOMAINS, set())
        q["docs"] = [{"title": "plain http", "url": "http://kubernetes.io/docs/"}]
        with self.assertRaises(ValueError):
            t.validate_question(q, DOMAINS, set())

    def test_duplicate_id_rejected(self):
        seen = {"CLU-001"}
        with self.assertRaises(ValueError):
            t.validate_question(copy.deepcopy(BY_ID["CLU-001"]), DOMAINS, seen)

    def test_two_correct_options_rejected(self):
        q = copy.deepcopy(BY_ID["CLU-001"])
        q["steps"]["s1"]["options"][1]["correct"] = True
        with self.assertRaises(ValueError):
            t.validate_question(q, DOMAINS, set())

    def test_time_limits_capped(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                self.assertLessEqual(s.get("time_limit", t.MAX_TIME), t.MAX_TIME)


class Judge(unittest.TestCase):
    def test_command_normalisation(self):
        step = BY_ID["CLU-001"]["steps"]["s1"]
        for typed in ["kubectl create role pod-reader --verb=get,list --resource=pods -n dev",
                      "  k create role pod-reader   --verb=get,list \\\n --resource=pods -n dev "]:
            self.assertTrue(t.evaluate_command(step, typed)["ok"], typed)

    def test_distractor_identified(self):
        step = BY_ID["CLU-001"]["steps"]["s1"]
        res = t.evaluate_command(step, "kubectl create role pod-reader --verb=get,list --resource=pods")
        self.assertFalse(res["ok"])
        self.assertIn("-n dev", res["option"]["why"])

    def test_typo_points_to_closest(self):
        step = BY_ID["CLU-002"]["steps"]["s3"]
        res = t.evaluate_command(step, "kubectl uncordn node01")
        self.assertFalse(res["ok"])
        self.assertIs(res["close"], t.correct_option(step))

    def test_alias_accepted(self):
        step = BY_ID["NET-002"]["steps"]["s2"]
        self.assertTrue(t.evaluate_command(step, "k get ep web-svc")["ok"])

    def test_every_correct_option_accepted(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                if s["type"] == "command":
                    self.assertTrue(t.evaluate_command(s, t.correct_option(s)["cmd"])["ok"], q["id"])

    def test_edit_snippets_correct_passes_traps_fail(self):
        self.assertGreaterEqual(len(EDIT_STEPS), 50)
        for qid, step in EDIT_STEPS:
            self.assertFalse(t.evaluate_edit(step, t.as_text(step["prefill"]))["ok"], qid)
            for o in step["options"]:
                res = t.evaluate_edit(step, t.apply_snippet(step, t.lines_of(o["snippet"])))
                self.assertEqual(res["ok"], bool(o.get("correct")), (qid, o["snippet"]))
                self.assertIs(res["option"], o, qid)  # the typed snippet is recognised

    def test_untouched_file_is_not_a_snippet(self):
        for qid, step in EDIT_STEPS:  # TRB-001's "no limits" trap is a prefix of the prefill
            self.assertIsNone(t.evaluate_edit(step, t.as_text(step["prefill"]))["option"], qid)

    def test_right_snippet_wrong_place(self):
        step = BY_ID["WRK-002"]["steps"]["s2"]
        good = t.lines_of(t.correct_option(step)["snippet"])
        text = "\n".join(good) + "\n" + t.as_text(step["prefill"])  # pasted above apiVersion, TODO kept
        res = t.evaluate_edit(step, text)
        self.assertFalse(res["ok"])
        self.assertIs(res["option"], t.correct_option(step))

    def test_validator_rejects_a_trap_that_passes(self):
        q = copy.deepcopy(BY_ID["TRB-003"])
        q["steps"]["s3"]["options"][1]["snippet"] = ["    forward . /etc/resolv.conf "]
        with self.assertRaisesRegex(ValueError, "wrong snippet passes"):
            t.validate_question(q, DOMAINS, set())

    def test_tabs_rejected(self):
        step = BY_ID["TRB-001"]["steps"]["s2"]
        text = t.as_text(step["prefill"]).replace("memory: 64Mi", "memory: 256Mi").replace("  name: cache", "\tname: cache")
        self.assertFalse(t.evaluate_edit(step, text)["ok"])

    def test_memory_threshold(self):
        step = BY_ID["TRB-001"]["steps"]["s2"]
        base = t.as_text(step["prefill"])
        for value, ok in [("128Mi", False), ("255Mi", False), ("256Mi", True), ("512Mi", True), ("1Gi", True)]:
            self.assertEqual(t.evaluate_edit(step, base.replace("64Mi", value))["ok"], ok, value)

    def test_points_and_branching(self):
        self.assertEqual(t.points_for(True, 0, 60), 150)
        self.assertEqual(t.points_for(True, 60, 60), 100)
        self.assertEqual(t.points_for(False, 1, 60), 0)
        step = BY_ID["CLU-001"]["steps"]["s1"]
        self.assertEqual(t.judge(step, t.correct_option(step)["cmd"], "submit")["next"], "s2")
        self.assertIsNone(t.judge(step, "", "timeout")["next"])


class Exam(unittest.TestCase):
    # a pool shaped like the 100-question target: 25/15/10/20/30
    POOL = [{"id": f"{d}-{i}", "domain": d} for d, n in
            [("cluster", 25), ("workloads", 15), ("storage", 10), ("networking", 20), ("troubleshooting", 30)]
            for i in range(n)]

    def test_plan_sums_and_follows_weights(self):
        for n in range(t.EXAM_MIN, t.EXAM_MAX + 1):
            plan = t.exam_plan(DOMAINS, n)
            self.assertEqual(sum(plan.values()), n)
            for d, k in plan.items():  # never more than one question away from the exact share
                self.assertLess(abs(k - n * DOMAINS[d]["weight"] / 100), 1, (n, d))
            self.assertEqual(plan["troubleshooting"], max(plan.values()))  # ties allowed (n=19: 5 and 5)

    def test_exam_size_is_random_between_15_and_20(self):
        import random
        sizes = {len(t.pick_exam(self.POOL, DOMAINS, random.Random(seed))) for seed in range(60)}
        self.assertEqual(sizes, set(range(t.EXAM_MIN, t.EXAM_MAX + 1)))

    def test_domain_round_is_its_share_of_20(self):
        import random
        share = t.exam_plan(DOMAINS, t.EXAM_MAX)
        for d in DOMAINS:  # what the menu line shows ("2/20") is what gets played
            rnd = t.pick_domain_round(self.POOL, DOMAINS, d, random.Random(3))
            self.assertEqual(len(rnd), share[d], d)
            self.assertEqual({q["domain"] for q in rnd}, {d})
            self.assertEqual(len({q["id"] for q in rnd}), len(rnd))
        self.assertEqual(len(t.pick_domain_round(self.POOL, DOMAINS, "storage", n=4)), 4)

    def test_exam_questions_are_distinct_and_in_proportion(self):
        import random
        exam = t.pick_exam(self.POOL, DOMAINS, random.Random(1), n=20)
        self.assertEqual(len({q["id"] for q in exam}), 20)
        counts = {d: sum(q["domain"] == d for q in exam) for d in DOMAINS}
        self.assertEqual(counts, t.exam_plan(DOMAINS, 20))


class Finale(unittest.TestCase):
    def result(self, qid, ok_steps, solved):
        q = BY_ID[qid]
        return {"id": qid, "domain": q["domain"], "points": 100 * ok_steps, "solved": solved,
                "steps": [{"ok": True, "time": 10.0}] * ok_steps + [{"ok": False, "time": 5.0}] * (not solved)}

    def test_win_at_pass_mark_with_partial_credit(self):
        # CLU-001: 3/3, CLU-002: 1 of 3 steps -> 4/6 = 67 % >= 66 %
        info = t.finale_info([self.result("CLU-001", 3, True), self.result("CLU-002", 1, False)],
                             QUESTIONS, DOMAINS)
        self.assertEqual((info["percent"], info["won"]), (67, True))
        self.assertEqual(info["pods"], [("cluster", "1/2", "CrashLoopBackOff", 1, 45.0)])
        self.assertEqual(info["review"], ["CLU-002"])

    def test_game_over_below_pass_mark(self):
        info = t.finale_info([self.result("TRB-002", 1, False)], QUESTIONS, DOMAINS)
        self.assertEqual((info["percent"], info["won"]), (25, False))
        self.assertEqual(info["pods"][0][2], "Error")


class PasteDetection(unittest.TestCase):
    def test_typing_is_not_paste(self):
        self.assertFalse(t.is_paste(["k"]))
        self.assertFalse(t.is_paste(["k", " ", "g"]))

    def test_burst_or_bracketed_is_paste(self):
        self.assertTrue(t.is_paste(list("kubectl get po")))
        self.assertTrue(t.is_paste([t.PASTE_START, "k"]))

    def test_paste_markers_ignored_by_editor(self):
        ed = t.Editor()
        ed.handle(t.PASTE_START, True)
        ed.handle(t.PASTE_END, True)
        self.assertEqual(ed.text(), "")


class ScreenFit(unittest.TestCase):
    """Every step must be playable at 80x24 (the prompt and ALL options visible) and shown
    in full in the game window (100x38). Mirrors the budget used by draw_step."""

    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "bold", "dim", "rev", "normal")})

    def body(self, q, s, h, w):
        budget = h - 5 - (3 if s["type"] == "command" else 8)
        lines = [x[0] for x in t.step_body(q, s, s["options"], w - 2, budget)]
        self.assertLessEqual(len(lines), budget, q["id"])
        return lines

    def test_every_step_playable_at_80x24(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                lines = self.body(q, s, 24, 80)
                self.assertFalse(any("enlarge the terminal" in x for x in lines), q["id"])
                if s["type"] == "command":
                    for label in t.LABELS[:len(s["options"])]:
                        self.assertTrue(any(f"{label}) " in x for x in lines), (q["id"], label))

    def test_every_step_shown_in_full_at_100x38(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                lines = self.body(q, s, 38, 100)
                self.assertNotIn("│ …", lines, q["id"])
                self.assertIn(t.expand([(q["scenario"], 0)], 98)[0][0], lines, q["id"])


class LearnMode(unittest.TestCase):
    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "bold", "dim", "rev", "normal")})
        t.A["green"] = 1
        self.addCleanup(t.SETTINGS.update, learn=False)

    def command_body(self, learn):
        t.SETTINGS["learn"] = learn
        q = BY_ID["CLU-001"]
        step = q["steps"][q["start"]]
        return step, t.step_body(q, step, step["options"], 98, 30)

    def test_command_answer_marked_only_in_learn_mode(self):
        step, body = self.command_body(learn=True)
        marked = [x[0] for x in body if x[0].startswith("✔")]
        good = t.correct_option(step)["cmd"]
        self.assertEqual(len(marked), 1)
        self.assertTrue(marked[0].endswith(good))
        self.assertTrue(all(x[1] for x in body if x[0].startswith("✔")))  # drawn in green
        _, body = self.command_body(learn=False)
        self.assertFalse(any("✔" in x[0] for x in body))

    def test_edit_snippet_rows_flag_the_answer(self):
        step = next(s for q in QUESTIONS for s in q["steps"].values() if s["type"] == "edit")
        rows = [r for r in t.snippet_rows(step["options"]) if r]
        good = {r[3] for r in rows if r[0]}  # a label starts each option
        self.assertEqual(good, {True, False})
        flagged = [r for r in rows if r[3]]
        self.assertEqual([r[2] for r in flagged],
                         [ln.lstrip(" ") for ln in t.lines_of(t.correct_option(step)["snippet"])])

    def test_docs_on_answer_page_only_in_learn_mode(self):
        q = BY_ID["CLU-001"]
        step = q["steps"][q["start"]]
        res = t.judge(step, t.correct_option(step)["cmd"], "submit")
        args = (step, step["options"], "", res, 5.0, 100)
        t.SETTINGS["learn"] = False
        self.assertFalse(any("https://" in x[0] for x in t.feedback_lines(*args, docs=q["docs"])))
        t.SETTINGS["learn"] = True
        urls = [x[0].strip() for x in t.feedback_lines(*args, docs=q["docs"]) if "https://" in x[0]]
        self.assertEqual(urls, [d["url"] for d in q["docs"]])
        recap = [x[0].strip() for x in t.recap_lines(q, 100, True) if "https://" in x[0]]
        self.assertEqual(recap, urls)


class EditorKeys(unittest.TestCase):
    def test_typing_backspace_newline_indent(self):
        ed = t.Editor("spec:\n  containers:")
        for k in "\n- x":
            ed.handle(k, enter_submits=False)
        ed.handle(t.curses.KEY_BACKSPACE, False)
        self.assertEqual(ed.text(), "spec:\n  containers:\n  - ")
        self.assertEqual(ed.handle("\x04", False), "submit")

    def test_enter_submits_unless_backslash(self):
        ed = t.Editor()
        for k in "k get po \\":
            ed.handle(k, True)
        self.assertIsNone(ed.handle("\n", True))
        self.assertEqual(ed.handle("\n", True), "submit")

    def test_soft_wrap_cursor(self):
        ed = t.Editor("x" * 25)
        rows, cursor = ed.rows(10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(cursor, (2, 5))


if __name__ == "__main__":
    unittest.main()
