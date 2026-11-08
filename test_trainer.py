import copy
import unittest

import cka_trainer as t

DOMAINS, QUESTIONS = t.load_db(t.DEFAULT_DB)
BY_ID = {q["id"]: q for q in QUESTIONS}


EDIT_STEPS = [(q["id"], s) for q in QUESTIONS for s in q["steps"].values() if s["type"] == "edit"]


class Database(unittest.TestCase):
    def test_library_in_cka_proportions(self):
        self.assertEqual(len(QUESTIONS), 120)
        counts = {d: sum(q["domain"] == d for q in QUESTIONS) for d in DOMAINS}
        self.assertEqual(counts, {d: v["weight"] * 6 // 5 for d, v in DOMAINS.items()})  # 1.2 questions per %

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
            t.validate_question(q, DOMAINS, set())

    def test_pedagogy_fields_required_and_typed(self):
        for key in ("docs", "stakes", "analogy"):  # --validate and the tests must agree
            q = copy.deepcopy(BY_ID["NET-001"])
            del q[key]
            with self.assertRaises(ValueError, msg=key):
                t.validate_question(q, DOMAINS, set())
        for bad in ("https://kubernetes.io/docs/", ["https://kubernetes.io/docs/"], []):
            q = copy.deepcopy(BY_ID["NET-001"])
            q["docs"] = bad  # a malformed file must give a clean error, not an AttributeError
            with self.assertRaises(ValueError, msg=repr(bad)):
                t.validate_question(q, DOMAINS, set())

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
        self.assertEqual(list(rows), ["MODE", "AUTOCOMPLETE", "TIMER", "MUSIC", "SOUND"])  # LEVEL: title menu
        self.assertEqual(rows["AUTOCOMPLETE"]["values"][rows["AUTOCOMPLETE"]["index"]], "OFF")  # off by default
        for label, value, key, expected in (("MODE", "LEARN", "learn", True), ("TIMER", "GLOBAL", "timer", "global"),
                                            ("AUTOCOMPLETE", "ON", "autocomplete", True),
                                            ("SOUND", "STEREO", "stereo", True)):
            rows[label]["on_change"](value)
            self.assertEqual(t.SETTINGS[key], expected, label)
        rows["MUSIC"]["on_change"]("OFF")
        self.assertEqual(snd.calls, [("stereo", True), ("music", False)])
        for row in rows.values():  # every value explains itself on the screen
            self.assertEqual(set(row["hints"]), set(row["values"]), row["label"])

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
        info = t.finale_info([played, unreached], QUESTIONS, DOMAINS)
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


class LearnTag(unittest.TestCase):
    """A learn-mode result (answers shown) must never pass for a real one."""

    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "bold", "dim", "rev", "normal")})
        self.addCleanup(t.SETTINGS.update, learn=False)
        q = BY_ID["CLU-002"]
        self.results = [{"id": q["id"], "domain": q["domain"], "points": 0, "solved": False,
                         "steps": [{"ok": False, "time": 3.0}]}]

    def test_finale_and_summary_tagged_only_in_learn_mode(self):
        for learn in (False, True):
            t.SETTINGS["learn"] = learn
            self.assertEqual(t.finale_info(self.results, QUESTIONS, DOMAINS)["learn"], learn)
            text = [x[0] for x in t.summary_lines(self.results, DOMAINS)]
            self.assertEqual(any("Learn mode" in x for x in text), learn)
            replay = next(x for x in text if "Replay them with" in x)
            self.assertEqual("--learn" in replay, learn)  # replaying keeps the mode


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


class Pedagogy(unittest.TestCase):
    """Answer pages: stakes on a miss, the traps explained, the analogy in the recap."""
    def setUp(self):
        t.A.update({k: 0 for k in ("green", "yellow", "red", "cyan", "bold", "dim", "rev", "normal")})
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
                    at = next(n for n, ln in enumerate(text) if ln.startswith(f"  {t.LABELS[i]}) "))
                    nxt = text[at + 1:]
                    first = next(ln for ln in nxt if ln.strip().startswith("→"))
                    self.assertEqual(first.strip(), "→ " + o["why"], (q["id"], t.LABELS[i]))

    def test_blank_line_under_each_section_heading(self):
        q = BY_ID["CLU-001"]
        step = q["steps"][q["start"]]
        good = t.correct_option(step)["cmd"]
        for learn, typed, heads in ((True, good, ("Question:", "Why this is right:", "Why not the others:")),
                                    (False, "kubectl get pods", ("Question:", "Why:", "The other traps:"))):
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


if __name__ == "__main__":
    unittest.main()
