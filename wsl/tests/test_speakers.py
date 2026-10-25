"""Speaker hints from the source subtitle, offline: what counts as a label, what the model is shown.

The rule worth a test is the narrow one: a bracket at the start of a line is a sound far more
often than a name, and a hint naming a character called "Laughs" is worse than no hint. The other
is the null case - a file with no labels comes out of harvest exactly as it went in.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import glossary as G  # noqa: E402
from lektorlib import speakers as SP  # noqa: E402
from lektorlib.srt import Cue  # noqa: E402

GLOSSARY = G.parse_glossary(tomllib.loads("""
[characters.Ted]
gender = 'm'
aliases = ['Coach']
[characters.Mary]
gender = 'f'
"""))


def cues(*raws):
    """Cues as translate.source_cues leaves them: text joined, raw kept."""
    out = []
    for n, raw in enumerate(raws, 1):
        text = " ".join(ln.strip() for ln in raw.split("\n") if ln.strip())
        out.append(Cue(idx=n, start_ms=n * 1000, end_ms=n * 1000 + 900, raw=raw, text=text))
    return out


class TestLabels(unittest.TestCase):
    def test_a_capitals_label_comes_out_of_the_text_and_into_a_hint(self):
        cs = cues("JOHN: Where were you?")
        h = SP.harvest(cs)
        self.assertEqual(cs[0].text, "Where were you?")
        self.assertEqual(h.text, {1: "{mówi: JOHN}"})
        self.assertEqual(h.names, {"JOHN"})

    def test_the_glossary_gives_the_label_its_name_and_gender(self):
        cs = cues("TED: I'm ready.", "COACH (V.O.): Me too.")
        h = SP.harvest(cs, GLOSSARY)
        self.assertEqual(h.text, {1: "{mówi: Ted, mężczyzna}", 2: "{mówi: Ted, mężczyzna}"})
        self.assertEqual(cs[1].text, "Me too.")

    def test_a_mixed_case_label_needs_the_glossary(self):
        cs = cues("Mary: Hi.", "Note: this is a sentence.")
        h = SP.harvest(cs, GLOSSARY)
        self.assertEqual(h.text, {1: "{mówi: Mary, kobieta}"})
        self.assertEqual(cs[1].text, "Note: this is a sentence.")
        self.assertEqual(SP.harvest(cues("Mary: Hi.")).text, {})

    def test_a_bracket_is_a_name_only_when_something_says_it_is_one(self):
        cs = cues("[Mary] Hello.", "[Laughs] Very funny.", "(JOHN) Hi.", "JOHN: Bye.")
        h = SP.harvest(cs, GLOSSARY)
        # Mary by the glossary, JOHN because he is labelled the SDH way elsewhere; Laughs is a sound.
        self.assertEqual(h.text, {1: "{mówi: Mary, kobieta}", 3: "{mówi: JOHN}", 4: "{mówi: JOHN}"})
        self.assertEqual([c.text for c in cs], ["Hello.", "[Laughs] Very funny.", "Hi.", "Bye."])

    def test_a_dialogue_card_with_two_labels_names_both_in_order(self):
        cs = cues("- TED: Ready?\n- MARY: Always.")
        h = SP.harvest(cs, GLOSSARY)
        self.assertEqual(cs[0].text, "- Ready? - Always.")
        self.assertEqual(h.text, {1: "{mówią po kolei: Ted (mężczyzna), Mary (kobieta)}"})
        # The card's own dashes already show the two turns.
        self.assertEqual(h.turns, {})

    def test_labels_on_lines_without_dashes_get_the_dashes_that_show_the_turns(self):
        cs = cues("JOHN: Ready?\nMARY: Always.")
        h = SP.harvest(cs)
        self.assertEqual(cs[0].text, "- Ready? - Always.")
        self.assertEqual(h.turns, {1: 2})

    def test_a_label_alone_on_its_line_opens_the_next_one(self):
        cs = cues("JOHN:\nReady?\nMARY:\nAlways.")
        h = SP.harvest(cs)
        self.assertEqual(cs[0].text, "- Ready? - Always.")
        self.assertEqual(h.turns, {1: 2})

    def test_one_speaker_over_two_lines_is_one_speaker(self):
        cs = cues("JOHN: I was at the pub\nall night.")
        h = SP.harvest(cs)
        self.assertEqual(cs[0].text, "I was at the pub all night.")
        self.assertEqual(h.text, {1: "{mówi: JOHN}"})
        self.assertEqual(h.turns, {})

    def test_a_card_that_was_only_a_label_is_left_empty_for_the_caller_to_drop(self):
        cs = cues("JOHN:")
        SP.harvest(cs)
        self.assertEqual(cs[0].text, "")

    def test_a_clock_and_a_single_letter_are_not_labels(self):
        cs = cues("10:30 tomorrow.", "A: the first option.")
        self.assertFalse(SP.harvest(cs))
        self.assertEqual(cs[0].text, "10:30 tomorrow.")


class TestDashes(unittest.TestCase):
    def test_a_dropped_first_dash_is_said_as_a_count(self):
        cs = cues("Who?\n- Me.")
        h = SP.harvest(cs)
        self.assertEqual(h.text, {1: "{mówiących: 2}"})
        self.assertEqual(cs[0].text, "Who? - Me.")

    def test_a_card_that_shows_its_dashes_needs_no_hint(self):
        self.assertFalse(SP.harvest(cues("- Who?\n- Me.")))


class TestNothingToHarvest(unittest.TestCase):
    def test_a_file_with_no_labels_comes_out_as_it_went_in(self):
        raws = ("<i>Where have you been?</i>", "- I was at the pub.\n- Again?",
                "[door creaks] Hello?", "I'm ready.")
        cs = cues(*raws)
        before = [(c.idx, c.text, c.raw) for c in cs]
        h = SP.harvest(cs, GLOSSARY)
        self.assertFalse(h)
        self.assertEqual((h.turns, h.names), ({}, set()))
        self.assertEqual([(c.idx, c.text, c.raw) for c in cs], before)


if __name__ == "__main__":
    unittest.main()
