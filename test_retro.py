import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import retro


class PixelArt(unittest.TestCase):
    def test_font_glyphs_are_5x7(self):
        for ch, rows in retro.FONT.items():
            self.assertEqual(len(rows), 7, ch)
            self.assertTrue(all(len(r) == 5 and set(r) <= {"0", "1"} for r in rows), ch)

    def test_big_and_half_sizes(self):
        self.assertEqual(len(retro.big("CKA")), 7)
        self.assertEqual(retro.width(retro.big("CKA")), 3 * 10 + 2 * 2)
        self.assertEqual(len(retro.half("MEGATRAINER")), 4)
        self.assertEqual(retro.width(retro.half("MEGATRAINER")), 11 * 6 - 1)

    def test_helm_grid_and_rotation(self):
        a, b = retro.helm(0), retro.helm(math.pi / 14)
        self.assertEqual((len(a), len(a[0])), (retro.HELM_H, retro.HELM_W))
        self.assertNotEqual(a, b)
        self.assertEqual(a, retro.helm(2 * math.pi / 7))  # 7-fold symmetry

    def test_layout_degrades_with_terminal_size(self):
        self.assertEqual(retro._layout(40, 100, 9)[0], "full")
        self.assertEqual(retro._layout(28, 100, 9)[0], "medium")
        self.assertEqual(retro._layout(20, 80, 9)[0], "small")

    def test_menu_lines_have_constant_width(self):
        lines = [retro._menu_line("WORKLOADS & SCHEDULING", "2Q 15%", s) for s in (True, False)]
        lines.append(retro._menu_line("QUIT", "", False))
        self.assertEqual({len(x) for x in lines}, {retro.MENU_W})


class Chiptune(unittest.TestCase):
    def test_score_bars_are_complete(self):
        bars = [b for b in retro.LEAD.split("|")]
        self.assertEqual(len(bars), len(retro.BASS_ROOTS))
        for bar in bars:
            self.assertEqual(sum(n for _, n in retro.parse(bar)), 16, bar)

    def test_midi(self):
        self.assertEqual(retro.midi("A4"), 69)
        self.assertAlmostEqual(retro.freq(retro.midi("A5")), 880.0)

    def test_sounds_written_as_valid_wav(self):
        with tempfile.TemporaryDirectory() as d:
            files = retro.build_sounds(Path(d))
            self.assertEqual(set(files), {*retro.SONGS, *retro.SFX})
            with wave.open(str(files["theme"])) as wf:
                seconds = wf.getnframes() / wf.getframerate()
            self.assertAlmostEqual(seconds, 2 * 8 * 16 * retro.S16, delta=0.1)

    def test_game_sounds_exist_and_are_short(self):
        for name, max_s in [("level", 2.0), ("win", 0.5), ("lose", 1.0), ("clear", 1.5), ("fail", 1.5)]:
            seconds = len(retro.SFX[name]()) / retro.SR
            self.assertLess(seconds, max_s, name)

    def test_songs_bars_are_complete(self):
        for score, roots in [(retro.VICTORY, retro.VICTORY_ROOTS), (retro.GAMEOVER, retro.GAMEOVER_ROOTS)]:
            bars = score.split("|")
            self.assertEqual(len(bars), len(roots))
            for bar in bars:
                self.assertEqual(sum(n for _, n in retro.parse(bar)), 16, bar)

    def test_pod_name_is_stable_and_kubectl_like(self):
        self.assertEqual(retro.pod_name("storage"), retro.pod_name("storage"))
        self.assertRegex(retro.pod_name("storage"), r"^storage-[a-z0-9]{5}$")

    def test_silent_and_missing_player_degrade(self):
        self.assertEqual(retro.Sound(enabled=False).status, "SOUND OFF")
        with mock.patch.object(retro.shutil, "which", return_value=None):
            s = retro.Sound()
            self.assertEqual(s.status, "NO AUDIO")
            s.start_music()
            s.play("move")
            self.assertIsNone(s.music)


if __name__ == "__main__":
    unittest.main()
