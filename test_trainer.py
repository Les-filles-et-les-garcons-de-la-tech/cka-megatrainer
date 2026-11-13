import copy
import unittest
from pathlib import Path

import cka_trainer as t

# The CKA library (content tests: pedagogy, screen fit, traps proven wrong...).
DOMAINS, QUESTIONS = t.load_db(t.DEFAULT_DB)
# Frozen questions for the ENGINE tests (judge, editor, validator, finale): they never change
# when the library grows: a frozen copy of 9 early CKA questions (shared with CKS Megatrainer).
FX_DOMAINS, FX_QUESTIONS = t.load_db(Path(__file__).resolve().parent / "fixtures" / "engine_questions.json")
BY_ID = {q["id"]: q for q in FX_QUESTIONS}
BATCH = {"cluster": 5, "workloads": 3, "storage": 2, "networking": 4, "troubleshooting": 6}


EDIT_STEPS = [(q["id"], s) for q in QUESTIONS for s in q["steps"].values() if s["type"] == "edit"]
FX_EDIT_STEPS = [(q["id"], s) for q in FX_QUESTIONS for s in q["steps"].values() if s["type"] == "edit"]


class Database(unittest.TestCase):
    def test_library_grows_in_batches_of_20_in_cka_proportions(self):
        # 20 questions = 5/3/2/4/6 (the CKA weights 25/15/10/20/30)
        self.assertEqual(set(BATCH), set(DOMAINS))
        self.assertEqual(sum(BATCH.values()), 20)
        for d, k in BATCH.items():
            self.assertEqual(k * 5, DOMAINS[d]["weight"], d)
        self.assertTrue(QUESTIONS and len(QUESTIONS) % 20 == 0, len(QUESTIONS))
        batches = len(QUESTIONS) // 20
        counts = {d: sum(q["domain"] == d for q in QUESTIONS) for d in DOMAINS}
        self.assertEqual(counts, {d: k * batches for d, k in BATCH.items()})

    def test_step_counts_follow_difficulty(self):
        for q in QUESTIONS:  # easy/medium: 2-4 steps; hard tasks have the exam's shape, up to 6
            n = len(t.correct_path(q))
            self.assertTrue(2 <= n <= (6 if q["difficulty"] == 3 else 4), (q["id"], n))
        hard = [q for q in QUESTIONS if q["difficulty"] == 3]
        self.assertTrue(all(len(t.correct_path(q)) >= 5 for q in hard), "hard tasks: prepare, change, wait, verify")

    def test_ids_carry_their_domain_prefix(self):
        for q in QUESTIONS:
            self.assertRegex(q["id"], rf"^{DOMAINS[q['domain']]['prefix']}-\d{{3}}$")

    def test_menu_labels_fit(self):
        for d in DOMAINS.values():  # the title menu line is 46 wide, with "4/20 20%" on the right
            self.assertLessEqual(len(t.short_name(d)), 30, d["id"])

    def test_every_question_has_a_tip_and_explained_steps(self):
        for q in QUESTIONS:
            self.assertTrue(q.get("tip"), q["id"])
            for s in q["steps"].values():
                self.assertTrue(s.get("explain"), q["id"])

    def test_every_step_has_a_hint_that_hides_the_answer(self):
        for q in QUESTIONS:
            for sid, s in q["steps"].items():
                self.assertTrue(s.get("hint"), (q["id"], sid))
                self.assertIsNone(t.hint_problem(s), (q["id"], sid))

    def test_every_question_links_the_documentation(self):
        for q in QUESTIONS:
            self.assertTrue(q.get("docs"), q["id"])
            for d in q["docs"]:
                # one URL per line in the 100-column game window, so the terminal can click it
                self.assertLessEqual(len(d["url"]) + 2, 97, d["url"])

    def test_every_question_teaches_with_stakes_and_an_analogy(self):
        for q in QUESTIONS:
            for key in ("stakes", "analogy"):
                self.assertTrue(q.get(key), (q["id"], key))
                self.assertLessEqual(len(q[key]), t.MAX_STORY, (q["id"], key))

    def test_explanations_stay_concise(self):
        for q in QUESTIONS:
            for sid, s in q["steps"].items():
                self.assertLessEqual(len(s["explain"]), 330, (q["id"], sid))

    def test_overlong_story_rejected(self):
        q = copy.deepcopy(BY_ID["CLU-001"])
        q["stakes"] = "x" * (t.MAX_STORY + 1)
        with self.assertRaises(ValueError):
            t.validate_question(q, FX_DOMAINS, set())

    def test_pedagogy_fields_required_and_typed(self):
        for key in ("docs", "stakes", "analogy"):  # --validate and the tests must agree
            q = copy.deepcopy(BY_ID["NET-001"])
            del q[key]
            with self.assertRaises(ValueError, msg=key):
                t.validate_question(q, FX_DOMAINS, set())
        for bad in ("https://kubernetes.io/docs/", ["https://kubernetes.io/docs/"], []):
            q = copy.deepcopy(BY_ID["NET-001"])
            q["docs"] = bad  # a malformed file must give a clean error, not an AttributeError
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.validate_question(q, FX_DOMAINS, set())

    def test_bad_docs_rejected(self):
        q = copy.deepcopy(BY_ID["NET-001"])
        q["docs"] = [{"title": "no url"}]
        with self.assertRaises(ValueError):
            t.validate_question(q, FX_DOMAINS, set())
        q["docs"] = [{"title": "plain http", "url": "http://kubernetes.io/docs/"}]
        with self.assertRaises(ValueError):
            t.validate_question(q, FX_DOMAINS, set())

    def test_duplicate_id_rejected(self):
        seen = {"CLU-001"}
        with self.assertRaises(ValueError):
            t.validate_question(copy.deepcopy(BY_ID["CLU-001"]), FX_DOMAINS, seen)

    def test_two_correct_options_rejected(self):
        q = copy.deepcopy(BY_ID["CLU-001"])
        q["steps"]["s1"]["options"][1]["correct"] = True
        with self.assertRaises(ValueError):
            t.validate_question(q, FX_DOMAINS, set())

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
        self.assertGreaterEqual(len(EDIT_STEPS), len(QUESTIONS) // 4)  # aim: an edit step in >= 30 %
        for qid, step in EDIT_STEPS:
            self.assertFalse(t.evaluate_edit(step, t.as_text(step["prefill"]))["ok"], qid)
            for o in step["options"]:
                res = t.evaluate_edit(step, t.apply_snippet(step, t.lines_of(o["snippet"])))
                self.assertEqual(res["ok"], bool(o.get("correct")), (qid, o["snippet"]))
                self.assertIs(res["option"], o, qid)  # the typed snippet is recognised

    def test_untouched_file_is_not_a_snippet(self):
        for qid, step in EDIT_STEPS + FX_EDIT_STEPS:  # TRB-001's "no limits" trap is a prefix of the prefill
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
            t.validate_question(q, FX_DOMAINS, set())

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
    # a pool shaped like the 100-question target: one question per percent of weight
    POOL = [{"id": f"{d}-{i}", "domain": d} for d, v in DOMAINS.items() for i in range(v["weight"])]

    def test_plan_sums_and_follows_weights(self):
        for n in range(t.EXAM_MIN, t.EXAM_MAX + 1):
            plan = t.exam_plan(DOMAINS, n)
            self.assertEqual(sum(plan.values()), n)
            for d, k in plan.items():  # never more than one question away from the exact share
                self.assertLess(abs(k - n * DOMAINS[d]["weight"] / 100), 1, (n, d))
            for a in DOMAINS:  # a heavier domain never gets fewer questions than a lighter one
                for b in DOMAINS:
                    if DOMAINS[a]["weight"] > DOMAINS[b]["weight"]:
                        self.assertGreaterEqual(plan[a], plan[b], (n, a, b))

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
        # CLU-001: 3/3, CLU-002: 1 of 3 steps -> 4/6 = 67 % >= 66 % (the CKA pass mark)
        info = t.finale_info([self.result("CLU-001", 3, True), self.result("CLU-002", 1, False)],
                             FX_QUESTIONS, FX_DOMAINS)
        self.assertEqual((info["percent"], info["won"]), (67, True))
        self.assertEqual(info["pods"], [("cluster", "1/2", "CrashLoopBackOff", 1, 45.0)])
        self.assertEqual(info["review"], ["CLU-002"])

    def test_game_over_below_pass_mark(self):
        info = t.finale_info([self.result("TRB-002", 1, False)], FX_QUESTIONS, FX_DOMAINS)
        self.assertEqual((info["percent"], info["won"]), (25, False))
        self.assertEqual(info["pods"][0][2], "Error")


class FakeSound:
    enabled, player, music_on = True, ["pw-play"], True
    status = "MUSIC ON"

    def __init__(self):
        self.calls = []

    def set_music(self, on):
        self.calls.append(("music", on))

    def set_stereo(self, on):
        self.calls.append(("stereo", on))


class Options(unittest.TestCase):
    def setUp(self):
        saved = dict(t.SETTINGS)
        self.addCleanup(lambda: (t.SETTINGS.clear(), t.SETTINGS.update(saved)))

    def test_levels_are_nested_and_hard_is_everything(self):
        easy, medium, hard = (t.at_level(QUESTIONS, lv) for lv in ("easy", "medium", "hard"))
        self.assertLess(len(easy), len(medium))
        self.assertLess(len(medium), len(hard))
        self.assertEqual(len(hard), len(QUESTIONS))
        self.assertTrue({q["id"] for q in easy} <= {q["id"] for q in medium})

    def test_every_level_fills_a_20_question_exam_with_variety(self):
        if len(QUESTIONS) < 100:
            self.skipTest("needs at least 100 questions")
        need = t.exam_plan(DOMAINS, t.EXAM_MAX)
        for lv in t.LEVELS:
            pool = t.at_level(QUESTIONS, lv)
            for d, k in need.items():  # 1.5x the share, so two EASY exams are not the same
                have = sum(q["domain"] == d for q in pool)
                self.assertGreaterEqual(have, -(-3 * k // 2), (lv, d, have, k))

    def test_global_budget(self):
        self.assertEqual(t.global_budget(QUESTIONS[:3], exam=True), 120 * 60)
        steps = sum(min(s.get("time_limit", t.MAX_TIME), t.MAX_TIME)
                    for q in QUESTIONS[:3] for _, s in t.correct_path(q))
        self.assertEqual(t.global_budget(QUESTIONS[:3], exam=False), steps)

    def test_option_rows_change_settings_and_sound(self):
        snd = FakeSound()
        rows = {r["label"]: r for r in t.option_rows(QUESTIONS, snd)}
        self.assertEqual(list(rows), ["MODE", "HINTS", "FEEDBACK", "AUTOCOMPLETE", "TIMER", "MUSIC", "SOUND"])  # LEVEL: title menu
        self.assertEqual(rows["AUTOCOMPLETE"]["values"][rows["AUTOCOMPLETE"]["index"]], "OFF")  # off by default
        for label, value, key, expected in (("MODE", "LEARN", "learn", True), ("TIMER", "GLOBAL", "timer", "global"),
                                            ("AUTOCOMPLETE", "ON", "autocomplete", True), ("HINTS", "ON", "hints", True),
                                            ("SOUND", "STEREO", "stereo", True),
                                            ("FEEDBACK", "AT THE END", "feedback", "end")):
            rows[label]["on_change"](value)
            self.assertEqual(t.SETTINGS[key], expected, label)
        rows["MUSIC"]["on_change"]("OFF")
        self.assertEqual(snd.calls, [("stereo", True), ("music", False)])
        for row in rows.values():  # every value explains itself on the screen
            self.assertEqual(set(row["hints"]), set(row["values"]), row["label"])

    def test_mode_recall_is_exclusive_with_learn(self):
        self.addCleanup(t.SETTINGS.update, learn=False, recall=False)
        mode = {r["label"]: r for r in t.option_rows(QUESTIONS, FakeSound())}["MODE"]
        self.assertEqual(mode["values"], ["EXAM", "LEARN", "RECALL"])
        mode["on_change"]("RECALL")
        self.assertEqual((t.SETTINGS["learn"], t.SETTINGS["recall"]), (False, True))
        self.assertEqual({r["label"]: r for r in t.option_rows(QUESTIONS, FakeSound())}["MODE"]["index"], 2)
        self.assertIn("RECALL", t.options_status(FakeSound()))
        mode["on_change"]("LEARN")
        self.assertEqual((t.SETTINGS["learn"], t.SETTINGS["recall"]), (True, False))

    def test_recall_hides_the_options_until_half_time(self):
        self.addCleanup(t.SETTINGS.update, recall=False)
        self.assertIsNone(t.recall_hint_in(60, 0))  # not in RECALL mode: always shown
        t.SETTINGS["recall"] = True
        self.assertEqual(t.recall_hint_in(60, 10), 20)
        self.assertIsNone(t.recall_hint_in(60, 30))  # half of the step's time: the hint
        q = BY_ID["CLU-001"]
        step = q["steps"][q["start"]]
        hidden = "\n".join(x[0] for x in t.step_body(q, step, step["options"], 98, 30, hint_in=20))
        shown = "\n".join(x[0] for x in t.step_body(q, step, step["options"], 98, 30))
        for o in step["options"]:
            self.assertNotIn(o["cmd"], hidden)
            self.assertIn(o["cmd"], shown)
        self.assertIn("appear in 20s", hidden)

    def test_hint_never_gives_the_answer(self):
        q = BY_ID["CLU-001"]
        step = dict(q["steps"][q["start"]])
        good = next(o for o in step["options"] if o.get("correct"))
        for bad in ("", "x" * (t.MAX_HINT + 1), "Run " + good["cmd"] + " and look."):
            self.assertTrue(t.hint_problem({**step, "hint": bad}), bad)
        self.assertIsNone(t.hint_problem({**step, "hint": "Think about which object holds that setting."}))

    def test_hint_shown_only_with_the_option(self):
        self.addCleanup(t.SETTINGS.update, hints=False)
        q = BY_ID["CLU-001"]
        step = {**q["steps"][q["start"]], "hint": "Look at the kind of object first."}
        text = lambda: "\n".join(x[0] for x in t.step_body(q, step, step["options"], 98, 30))  # noqa: E731
        self.assertNotIn("HINT:", text())
        t.SETTINGS["hints"] = True
        self.assertIn("HINT: Look at the kind of object first.", text())
        self.assertNotIn("HINT:", "\n".join(x[0] for x in t.step_body(q, step, step["options"], 98, 4)))  # tiny: dropped

    def test_status_line_shows_the_options(self):
        t.SETTINGS.update(learn=True, timer="global", level="easy", stereo=True)
        line = t.options_status(FakeSound())
        for part in ("LEARN", "GLOBAL TIMER", "STEREO", "MUSIC ON"):
            self.assertIn(part, line)

    def test_clock_text(self):
        self.assertEqual(t.clock_text(7200), "120:00")
        self.assertEqual(t.clock_text(61.9), "1:01")
        self.assertEqual(t.clock_text(-3), "0:00")

    def test_unreached_questions_count_as_failed(self):
        played = {"id": "CLU-001", "domain": "cluster", "points": 300, "solved": True,
                  "steps": [{"ok": True, "time": 5.0}] * 3}
        unreached = {"id": "CLU-002", "domain": "cluster", "points": 0, "solved": False, "steps": [],
                     "unreached": True}
        info = t.finale_info([played, unreached], FX_QUESTIONS, FX_DOMAINS)
        self.assertEqual((info["unreached"], info["percent"]), (1, 50))  # 3 of 6 steps


class Autocomplete(unittest.TestCase):
    WORDS = {"kubectl", "drain", "describe", "delete", "--ignore-daemonsets", "--image=", "--image=busybox:1.28",
             "--verb=", "--verb=get,list", "--verbs=", "--verbs=get,list", "nginx-7c5ddbdf54-x2k9p", "/opt/backup/"}

    def tab(self, line, x=None):
        return t.complete(line, len(line) if x is None else x, self.WORDS)

    def test_unique_match_completes_with_a_space(self):
        self.assertEqual(self.tab("kubectl dr")[:2], ("kubectl drain ", 14))
        self.assertEqual(self.tab("kubectl describe pod ng")[0], "kubectl describe pod nginx-7c5ddbdf54-x2k9p ")

    def test_flag_first_then_its_value(self):
        self.assertEqual(self.tab("kubectl run t --ima")[0], "kubectl run t --image=")  # no space after '='
        self.assertEqual(self.tab("kubectl run t --image=b")[0], "kubectl run t --image=busybox:1.28 ")

    def test_ambiguous_extends_the_common_prefix_and_lists(self):
        line, x, matches = self.tab("kubectl create role r --ve")
        self.assertEqual(line, "kubectl create role r --verb")
        self.assertEqual(matches, ["--verb=", "--verb=get,list", "--verbs=", "--verbs=get,list"])
        self.assertEqual(self.tab("kubectl de")[2], ["delete", "describe"])

    def test_nothing_to_complete(self):
        for line in ("kubectl zz", "kubectl ", ""):
            self.assertEqual(self.tab(line), (line, len(line), []))
        self.assertEqual(self.tab("cp x /opt/b")[0], "cp x /opt/backup/")  # no space after '/'

    def test_completion_in_the_middle_of_a_line(self):
        line, x, _ = self.tab("kubectl dr node01", x=10)
        self.assertEqual((line, x), ("kubectl drain node01", 13))  # no double space before node01

    def test_words_come_from_the_variants_and_tab_is_2_spaces_without_them(self):
        step = BY_ID["CLU-002"]["steps"]["s1"]
        words = t.completion_words(step)
        self.assertIn("--ignore-daemonsets", words)
        self.assertIn("cordon", words)  # traps too: all variants are on screen
        self.assertEqual(t.completion_words(BY_ID["WRK-001"]["steps"]["s2"]), set())  # edit step: no YAML
        ed = t.Editor("kubectl dr")
        ed.handle("\t", True)
        self.assertEqual(ed.text(), "kubectl dr  ")  # AUTOCOMPLETE OFF
        ed = t.Editor("kubectl dr")
        ed.handle("\t", True, words=words)
        self.assertEqual(ed.text(), "kubectl drain ")
        ed.handle("x", True, words=words)
        self.assertEqual(ed.matches, [])


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
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "magenta", "bold", "dim", "rev", "normal")})

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

    def test_hints_on_keep_every_step_playable_and_full_at_100x38(self):
        self.addCleanup(t.SETTINGS.update, hints=False)
        t.SETTINGS["hints"] = True
        self.test_every_step_playable_at_80x24()  # the hint is dropped first when space runs out
        for q in QUESTIONS:
            for s in q["steps"].values():
                lines = self.body(q, s, 38, 100)
                self.assertNotIn("│ …", lines, q["id"])
                self.assertTrue(any(x.startswith("HINT: ") for x in lines), q["id"])


class LearnTag(unittest.TestCase):
    """A learn-mode result (answers shown) must never pass for a real one."""

    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "magenta", "bold", "dim", "rev", "normal")})
        self.addCleanup(t.SETTINGS.update, learn=False)
        q = BY_ID["CLU-002"]
        self.results = [{"id": q["id"], "domain": q["domain"], "points": 0, "solved": False,
                         "steps": [{"ok": False, "time": 3.0}]}]

    def test_finale_and_summary_tagged_only_in_learn_mode(self):
        for learn in (False, True):
            t.SETTINGS["learn"] = learn
            self.assertEqual(t.finale_info(self.results, FX_QUESTIONS, FX_DOMAINS)["learn"], learn)
            text = [x[0] for x in t.summary_lines(self.results, FX_DOMAINS)]
            self.assertEqual(any("Learn mode" in x for x in text), learn)
            replay = next(x for x in text if "Replay them with" in x)
            self.assertEqual("--learn" in replay, learn)  # replaying keeps the mode


class LearnMode(unittest.TestCase):
    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "magenta", "bold", "dim", "rev", "normal")})
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


class Pedagogy(unittest.TestCase):
    """Answer pages: stakes on a miss, the traps explained, the analogy in the recap."""
    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "magenta", "bold", "dim", "rev", "normal")})
        self.addCleanup(t.SETTINGS.update, learn=False)
        self.q = BY_ID["CLU-001"]
        self.step = self.q["steps"][self.q["start"]]

    def page(self, typed, learn):
        t.SETTINGS["learn"] = learn
        res = t.judge(self.step, typed, "submit")
        lines = t.feedback_lines(self.step, self.step["options"], typed, res, 5.0, 100)
        return "\n".join(x[0] for x in lines)

    def test_stakes_in_the_recap_of_a_failed_question_only(self):
        for learn in (False, True):
            t.SETTINGS["learn"] = learn
            failed = "\n".join(x[0] for x in t.recap_lines(self.q, 0, False))
            solved = "\n".join(x[0] for x in t.recap_lines(self.q, 100, True))
            self.assertIn(self.q["stakes"], failed)
            self.assertNotIn(self.q["stakes"], solved)

    def test_traps_explained_after_a_correct_answer_only_in_learn_mode(self):
        good = t.correct_option(self.step)["cmd"]
        self.assertNotIn("Why not the others", self.page(good, learn=False))
        page = self.page(good, learn=True)
        self.assertIn("Why not the others", page)
        for o in self.step["options"]:
            if not o.get("correct"):
                self.assertIn(o["why"], page)
                self.assertIn(o["cmd"], page)  # the variant itself, not only its explanation

    def test_answer_page_repeats_the_question(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                res = t.judge(s, "", "submit")
                text = "\n".join(x[0] for x in t.feedback_lines(s, s["options"], "", res, 1.0, 0))
                self.assertIn(s["prompt"], text, q["id"])
                for ln in t.as_text(s.get("context", "")).split("\n"):
                    self.assertIn(ln, text, q["id"])

    def test_correct_page_shows_the_answer_with_its_explanation(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                good = t.correct_option(s)
                typed = good.get("cmd") or t.apply_snippet(s, t.lines_of(good["snippet"]))
                res = t.judge(s, typed, "submit")
                text = [x[0] for x in t.feedback_lines(s, s["options"], typed, res, 1.0, 0)]
                label = t.LABELS[s["options"].index(good)]
                at = next(n for n, ln in enumerate(text) if ln.startswith(f"  {label}) "))
                self.assertEqual(next(ln for ln in text[at:] if "→" in ln).strip(), "→ " + s["explain"], q["id"])

    def test_each_explanation_follows_its_own_variant(self):
        t.SETTINGS["learn"] = True
        for q in QUESTIONS:
            for s in q["steps"].values():
                res = t.judge(s, "", "submit")
                text = [x[0] for x in t.feedback_lines(s, s["options"], "", res, 1.0, 0)]
                for i, o in enumerate(s["options"]):
                    if o.get("correct"):
                        continue
                    at = next(n for n, ln in enumerate(text) if ln[2:].startswith(f"{t.LABELS[i]}) "))
                    nxt = text[at + 1:]
                    first = next(ln for ln in nxt if ln.strip().startswith("→"))
                    self.assertEqual(first.strip(), "→ " + o["why"], (q["id"], t.LABELS[i]))

    def test_unsolved_page_explains_every_option_right_one_first(self):
        for q in QUESTIONS:
            for s in q["steps"].values():
                good = t.correct_option(s)
                trap = next(o for o in s["options"] if not o.get("correct"))
                typed = trap.get("cmd") or t.apply_snippet(s, t.lines_of(trap["snippet"]))
                for how, text in (("submit", typed), ("timeout", ""), ("skip", "")):
                    res = t.judge(s, text, how)
                    page = [x[0] for x in t.feedback_lines(s, s["options"], text, res, 1.0, 0)]
                    rows = [n for n, ln in enumerate(page) if ln[:2] in ("✔ ", "✘ ") and ln[3:5] == ") "]
                    self.assertEqual(len(rows), len(s["options"]), (q["id"], how))
                    self.assertTrue(page[rows[0]].startswith("✔ "), (q["id"], how))  # the right one first
                    joined = "\n".join(page)
                    self.assertIn("→ " + s["explain"], joined, q["id"])
                    for o in s["options"]:
                        if not o.get("correct"):
                            self.assertIn("→ " + o["why"], joined, q["id"])
                    self.assertEqual(joined.count("^ your answer"), 1 if how == "submit" else 0, (q["id"], how))
                    if "cmd" in good:
                        for alias in good.get("aliases", []):
                            self.assertIn(alias + "   (also accepted)", joined, q["id"])

    def test_blank_line_under_each_section_heading(self):
        q = BY_ID["CLU-001"]
        step = q["steps"][q["start"]]
        good = t.correct_option(step)["cmd"]
        for learn, typed, heads in ((True, good, ("Question:", "Why this is right:", "Why not the others:")),
                                    (False, "kubectl get pods", ("Question:", "All the options:"))):
            t.SETTINGS["learn"] = learn
            res = t.judge(step, typed, "submit")
            text = [x[0] for x in t.feedback_lines(step, step["options"], typed, res, 3.0, 0)]
            for head in heads:
                i = text.index(head)
                self.assertEqual(text[i + 1], "", head)   # the blank line under the heading
                self.assertNotEqual(text[i + 2], "", head)  # then the content, not more space

    def test_priming_line_only_in_learn_mode(self):
        self.assertNotIn(t.PRIME_MISS, self.page("kubectl get pods", learn=False))
        self.assertIn(t.PRIME_MISS, self.page("kubectl get pods", learn=True))
        self.assertIn(t.PRIME_OK, self.page(t.correct_option(self.step)["cmd"], learn=True))

    def test_recap_reminds_the_mission(self):
        text = "\n".join(x[0] for x in t.recap_lines(self.q, 100, True))
        self.assertIn(self.q["title"], text)
        self.assertIn(self.q["scenario"], text)

    def test_recap_carries_the_analogy(self):
        t.SETTINGS["learn"] = False
        text = "\n".join(x[0] for x in t.recap_lines(self.q, 100, True))
        self.assertIn(self.q["analogy"], text)


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



class Review(unittest.TestCase):
    """REVIEW MISSED: the wrong questions are kept between runs until solved."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = t.STATE_DIR
        t.STATE_DIR = Path(self.tmp.name) / "state"  # never the player's own ~/.cka_megatrainer
        self.addCleanup(setattr, t, "STATE_DIR", self.old)

    @staticmethod
    def r(qid, steps, solved=False):
        return {"id": qid, "domain": "cluster", "points": 0, "solved": solved,
                "steps": [{"ok": ok, "time": 1.0} for ok in steps], "quit": False}

    def test_missing_or_damaged_file_is_an_empty_list(self):
        self.assertEqual(t.load_review(), [])
        t.review_path().parent.mkdir(parents=True)
        t.review_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(t.load_review(), [])
        t.review_path().write_text('["CLU-001"]', encoding="utf-8")
        self.assertEqual(t.load_review(), [])

    def test_save_then_load_round_trips(self):
        self.assertTrue(t.save_review(["CLU-002", "TRB-002"]))
        self.assertEqual(t.load_review(), ["CLU-002", "TRB-002"])
        self.assertFalse(list(t.review_path().parent.glob("*.tmp")))

    def test_wrong_step_joins_solved_leaves_others_stay(self):
        results = [self.r("CLU-001", [True, False]),           # missed: joins
                   self.r("CLU-002", [True, True, True], True),  # solved: leaves
                   self.r("TRB-002", [True]),                  # quit after a right step: unchanged
                   {**self.r("STO-001", []), "unreached": True}]  # global clock: unchanged
        self.assertEqual(t.update_review(["CLU-002", "TRB-002"], results), ["TRB-002", "CLU-001"])
        self.assertEqual(t.update_review(["CLU-001"], [self.r("CLU-001", [False])]), ["CLU-001"])  # no duplicate

    def test_learn_mode_solve_does_not_clear(self):
        solved = [self.r("CLU-002", [True, True, True], True)]
        self.assertEqual(t.update_review(["CLU-002"], solved, learn=True), ["CLU-002"])
        self.assertEqual(t.update_review(["CLU-002"], solved, learn=False), [])

    def test_review_questions_skip_unknown_ids_and_keep_order(self):
        got = t.review_questions(FX_QUESTIONS, ["TRB-002", "GONE-999", "CLU-001"])
        self.assertEqual([q["id"] for q in got], ["TRB-002", "CLU-001"])

    def test_review_flag_with_nothing_to_review_exits(self):
        with self.assertRaises(SystemExit) as cm:
            t.main(["--review", "--silent"])
        self.assertIn("Nothing to review", str(cm.exception.code))



class FeedbackAtEnd(unittest.TestCase):
    """FEEDBACK AT THE END: no verdict while playing, a wrong step goes on, the answers come last."""
    def setUp(self):
        saved = dict(t.SETTINGS)
        self.addCleanup(lambda: (t.SETTINGS.clear(), t.SETTINGS.update(saved)))
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "magenta", "bold", "dim", "rev", "normal")})
        self.pages, self.sounds = [], []
        for mod, name, fake in ((t, "pager", lambda scr, title, lines, **kw: self.pages.append(title) or "next"),
                                (t.retro, "level_screen", lambda *a, **kw: None)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, fake)
        self.sound = type("S", (), {"play": lambda _, name: self.sounds.append(name)})()

    def play(self, qid, typed):
        answers = iter(typed)
        self.addCleanup(setattr, t, "run_step", t.run_step)
        t.run_step = lambda *a: (next(answers), 3.0, "submit", 0)
        return t.ask_question(None, BY_ID[qid], 1, 1, 0, FX_DOMAINS, self.sound)

    def right(self, qid):
        return [t.correct_option(s)["cmd"] for _, s in t.correct_path(BY_ID[qid])]

    def test_learn_mode_keeps_feedback_after_each_step(self):
        t.SETTINGS.update(feedback="end", learn=True)
        self.assertFalse(t.feedback_at_end())
        t.SETTINGS["learn"] = False
        self.assertTrue(t.feedback_at_end())
        self.assertIn("FEEDBACK AT END", t.options_status(FakeSound()))

    def test_each_step_mode_ends_the_question_at_the_first_wrong_step(self):
        r = self.play("CLU-001", ["nope"])
        self.assertEqual([s["ok"] for s in r["steps"]], [False])
        self.assertEqual(len(self.pages), 2)  # the answer page, then the recap
        self.assertEqual(self.sounds, ["lose", "fail"])
        self.assertEqual(r["answers"], [])

    def test_wrong_step_goes_on_silently_and_answers_are_kept(self):
        t.SETTINGS["feedback"] = "end"
        right = self.right("CLU-001")
        r = self.play("CLU-001", ["nope"] + right[1:])
        self.assertEqual([s["ok"] for s in r["steps"]], [False, True, True])  # partial credit, like the exam
        self.assertFalse(r["solved"])
        self.assertEqual((self.pages, self.sounds), ([], []))  # no verdict while playing
        self.assertEqual(len(r["answers"]), 3 + 1)  # each step's page, then the recap
        text = [x[0] for x in t.answers_lines([r])]
        self.assertIn(f"Q1  CLU-001  ·  ✘ not solved  ·  {r['points']} points", text[:6])
        self.assertGreater(r["points"], 200)  # 2 right steps (+ speed bonus)
        self.assertIn("✘ WRONG", text)

    def test_all_right_is_solved(self):
        t.SETTINGS["feedback"] = "end"
        r = self.play("CLU-001", self.right("CLU-001"))
        self.assertTrue(r["solved"])
        self.assertEqual(self.sounds, [])

if __name__ == "__main__":
    unittest.main()
