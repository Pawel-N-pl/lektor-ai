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


# -- the wide band: seams the tight threshold cannot reach --------------------

WIDE_CFG = {"clean": {"merge_gap_ms": 140, "merge_gap_wide_ms": 250, "merge_max_cues": 4,
                      "merge_max_chars": 160, "merge_max_span_ms": 9000,
                      "drop_repeated_cards": False}}


def _cue(idx, start, end, text):
    from lektorlib.srt import Cue
    return Cue(idx=idx, start_ms=start, end_ms=end, raw=text, text=text)


def test_a_lowercase_continuation_four_frames_away_is_joined():
    """Verbatim from NCIS S23E01, cards 2 and 3: one sentence, 166 ms apart, which is exactly four
    frames at 23.976 fps. The tight 140 ms threshold refused it and the condenser then shortened
    each half alone, leaving "rodziny przestępczej Środkowego" - a stranded fragment the lektor
    read and stopped."""
    from lektorlib.srt import merge_contiguous
    cues = [_cue(2, 7123, 9459, "Carla Marino, matriarcha największej"),
            _cue(3, 9625, 11836, "zorganizowanej rodziny przestępczej na Środkowym Zachodzie.")]
    out = merge_contiguous(cues, WIDE_CFG)
    assert len(out) == 1
    assert out[0].text.startswith("Carla Marino, matriarcha największej zorganizowanej")


def test_the_wide_band_refuses_a_capitalised_opener():
    """The regression that widening the threshold outright caused: the recap title card glued onto
    the sentence after it. Both are four frames apart and neither carries a full stop, so only the
    capital letter distinguishes a new sentence from a continuation."""
    from lektorlib.srt import merge_contiguous
    cues = [_cue(1, 4954, 6956, "Poprzednio w NCIS"),
            _cue(2, 7123, 9459, "Carla Marino, matriarcha największej")]
    out = merge_contiguous(cues, WIDE_CFG)
    assert len(out) == 2


def test_the_tight_band_is_untouched_by_the_wide_one():
    """A seam inside the original threshold joins on the old rules, capital letter or not - so no
    release that merged before merges differently now."""
    from lektorlib.srt import merge_contiguous
    cues = [_cue(1, 0, 2000, "Powiedział mi wtedy"),
            _cue(2, 2083, 4000, "Coś, czego nie zapomnę.")]
    assert len(merge_contiguous(cues, WIDE_CFG)) == 1


def test_nothing_joins_past_the_wide_band():
    from lektorlib.srt import merge_contiguous
    cues = [_cue(1, 0, 2000, "Powiedział mi wtedy"),
            _cue(2, 2400, 4000, "coś, czego nie zapomnę.")]
    assert len(merge_contiguous(cues, WIDE_CFG)) == 2


# -- captions by colour, and the limits of that signal ------------------------

COLOUR_CFG = {"clean": {"merge_gap_ms": 140, "merge_gap_wide_ms": 250, "merge_max_cues": 4,
                        "merge_max_chars": 160, "merge_max_span_ms": 9000,
                        "drop_repeated_cards": False, "caption_colour_max_share": 0.10}}


def _raw_cue(idx, start, end, raw, text):
    from lektorlib.srt import Cue
    return Cue(idx=idx, start_ms=start, end_ms=end, raw=raw, text=text)


def test_a_coloured_caption_is_not_glued_to_the_dialogue_after_it():
    """Verbatim from NCIS S23E01: a location caption, the dialogue under it, then the second half
    of the same caption. Merged, the lektor said "Filadelfia" twice inside one breath."""
    from lektorlib.srt import colour_marks_captions, caption_colour, merge_contiguous
    cues = [_raw_cue(26, 68142, 68935, "<font color=#FFc30f>Filadelfia</font>", "Filadelfia"),
            _raw_cue(27, 68935, 70561, "Prezydent Carter ogłosił...", "Prezydent Carter ogłosił..."),
            _raw_cue(28, 70561, 72355, "<font color=#FFc30f>Filadelfia</font>\n"
                                       "<font color=#FFc30f>1977</font>", "Filadelfia 1977")]
    cues += [_raw_cue(100 + i, 100000 + i * 2000, 101000 + i * 2000, "Zwykly dialog.",
                      "Zwykly dialog.") for i in range(40)]
    assert colour_marks_captions(cues, COLOUR_CFG)
    for c in cues:
        if caption_colour(c.raw):
            c.screen_text = True
    out = merge_contiguous(cues[:3], COLOUR_CFG)
    assert len(out) == 3


def test_white_is_not_a_caption_colour():
    """White is what a subtitle already is, so painting a line white states nothing."""
    from lektorlib.srt import caption_colour
    assert caption_colour('<font color=#FFFFFF>Zwykly dialog.</font>') == ""
    assert caption_colour('<font color="white">Zwykly dialog.</font>') == ""
    assert caption_colour("<font color=#FFc30f>Filadelfia</font>") == "#ffc30f"


def test_the_rule_disqualifies_itself_when_the_whole_file_is_painted():
    """A release that colours every line carries the markup with none of the meaning. Reading it
    as caption evidence there would refuse every merge in the file."""
    from lektorlib.srt import colour_marks_captions
    painted = [_raw_cue(i, i * 1000, i * 1000 + 900,
                        f"<font color=#00FF00>Linia {i}.</font>", f"Linia {i}.") for i in range(40)]
    assert not colour_marks_captions(painted, COLOUR_CFG)


def test_a_file_with_no_colour_at_all_is_untouched():
    from lektorlib.srt import colour_marks_captions
    plain = [_raw_cue(i, i * 1000, i * 1000 + 900, f"Linia {i}.", f"Linia {i}.") for i in range(40)]
    assert not colour_marks_captions(plain, COLOUR_CFG)
