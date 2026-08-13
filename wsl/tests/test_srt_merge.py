"""Merging is the one thing in the pipeline with no test coverage, and it shipped wrong once.

The first version merged on the timestamp gap alone. Measured over six episodes that authorised
1016 groups of which only ~11% were the mid-sentence continuation it existed for; the rest glued
finished sentences, speaker turns and on-screen title cards together. These tests pin the predicate
that replaced it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib.srt import Cue, continues_sentence, is_screen_text, merge_contiguous  # noqa: E402

CFG = {"clean": {"merge_gap_ms": 140, "merge_max_cues": 4, "merge_max_chars": 160,
                 "merge_max_span_ms": 9000}}


class Cfg(dict):
    def __getitem__(self, k):
        return CFG[k]


def cue(idx, start, end, text, raw=None):
    c = Cue(idx=idx, start_ms=start, end_ms=end, raw=raw if raw is not None else text)
    c.text = text
    return c


class TestPredicate(unittest.TestCase):
    def test_finished_sentence_is_not_continued(self):
        a = cue(1, 0, 1000, "Richmond wygrał.")
        b = cue(2, 1084, 2000, "Wszyscy się cieszą.")
        self.assertFalse(continues_sentence(a, b))

    def test_comma_is_a_continuation(self):
        a = cue(1, 0, 1000, "Remisując, wyrównają rekord remisów,")
        b = cue(2, 1084, 2000, "który należy do Southampton.")
        self.assertTrue(continues_sentence(a, b))

    def test_ellipsis_is_a_continuation(self):
        a = cue(1, 0, 1000, "Wtedy zmienili kształt...")
        b = cue(2, 1084, 2000, "...i nikt tego nie zauważył.")
        self.assertTrue(continues_sentence(a, b))

    def test_speaker_change_is_never_joined(self):
        a = cue(1, 0, 1000, "Ale co ty na to,")
        b = cue(2, 1084, 2000, "Nie wiem.", raw="- Nie wiem.")
        self.assertFalse(continues_sentence(a, b))

    def test_screen_text_is_never_joined(self):
        a = cue(1, 0, 1000, "Zaliczają czwartą wygraną,")
        card = cue(2, 1084, 2000, "Z MIŁOŚCI DO PIŁKI")
        self.assertTrue(is_screen_text(card))
        self.assertFalse(continues_sentence(a, card))
        self.assertFalse(continues_sentence(card, cue(3, 2084, 3000, "wszystko się zmieniło,")))

    def test_short_capitalised_word_is_not_screen_text(self):
        self.assertFalse(is_screen_text(cue(1, 0, 1, "Tak.")))


class TestMerge(unittest.TestCase):
    def merged(self, cues):
        return merge_contiguous(cues, Cfg())

    def test_joins_a_split_sentence(self):
        out = self.merged([cue(1, 0, 1000, "Kibice Richmond, którzy wiele przeszli,"),
                           cue(2, 1084, 2500, "wciąż wspierają swój klub.")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "Kibice Richmond, którzy wiele przeszli, wciąż wspierają swój klub.")
        self.assertEqual(out[0].merged_from, [1, 2])
        self.assertEqual(out[0].start_ms, 0)
        self.assertEqual(out[0].end_ms, 2500)
        # text_original comes from the cleaned parts, not from re-cleaning the joined raw
        self.assertEqual(out[0].text_original, out[0].text)

    def test_leaves_a_real_pause_alone(self):
        out = self.merged([cue(1, 0, 1000, "Zdanie urwane,"),
                           cue(2, 1600, 2500, "które wraca po pauzie.")])
        self.assertEqual([c.idx for c in out], [1, 2])

    def test_every_cue_survives_exactly_once(self):
        cues = [cue(i, i * 1000, i * 1000 + 916, f"część {i},") for i in range(1, 10)]
        out = self.merged(cues)
        folded = [i for c in out for i in (c.merged_from or [c.idx])]
        self.assertEqual(folded, [c.idx for c in cues])

    def test_limits_are_respected(self):
        cues = [cue(i, i * 1000, i * 1000 + 916, f"kawałek numer {i},") for i in range(1, 12)]
        for c in self.merged(cues):
            n = len(c.merged_from) or 1
            self.assertLessEqual(n, CFG["clean"]["merge_max_cues"])
            self.assertLessEqual(len(c.text), CFG["clean"]["merge_max_chars"])
            self.assertLessEqual(c.end_ms - c.start_ms, CFG["clean"]["merge_max_span_ms"])

    def test_empty_and_single(self):
        self.assertEqual(self.merged([]), [])
        one = [cue(1, 0, 1000, "Sam.")]
        self.assertEqual(self.merged(one), one)

    def test_disabled_is_a_passthrough(self):
        cues = [cue(1, 0, 1000, "Urwane,"), cue(2, 1084, 2000, "dokończenie.")]
        off = dict(CFG["clean"], merge_gap_ms=0)
        self.assertEqual(len(merge_contiguous(cues, {"clean": off})), 2)


if __name__ == "__main__":
    unittest.main()
