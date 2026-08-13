"""The per-line delay: a lektor who starts before the actor is moved onto him, and no further.

Three properties are load-bearing and each has its own test below:

  1. Nothing moves EARLIER, ever. Subtitles lead speech on purpose, and the card's own lead is the
     working room the whole timing model is built on.
  2. Nothing moves out of its own window. A delay that created an overrun would hand the bill to
     the speed planner, and speeding a line up to pay for having moved it is a net loss.
  3. With no evidence -- no speech.json, a low-confidence alignment, or timing.max_delay_ms at 0 --
     every line stays exactly where it has always been.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib.mix import Clip, assemble_voice  # noqa: E402
from lektorlib.speech import (Onsets, lead_counts, onsets_from, plan_delays)  # noqa: E402
from lektorlib.srt import Cue, predicted_ms  # noqa: E402

# 80 ms of fixed cost plus 73.5 ms a character, the model fitted over 598 real lines.
TIMING = {"overhead_ms": 80.0, "ms_per_char": 73.5, "safety": 0.96, "chars_per_second": 14.5,
          "max_delay_ms": 600, "delay_min_lead_ms": 300, "delay_keep_lead_ms": 100,
          "delay_min_score": 0.8, "delay_match_tol_ms": 120}


def cue(idx=1, start=10000, text="Kibice", window=6000, end=None):
    c = Cue(idx=idx, start_ms=start, end_ms=end if end is not None else start + window, raw=text)
    c.text = text
    c.window_ms = window
    return c


class TestOnsets(unittest.TestCase):
    def ons(self, rows=((10000, 10500, 0.9),), **over):
        return Onsets(rows=list(rows), **{**{"min_score": 0.8, "match_tol_ms": 120}, **over})

    def test_exact_card_match(self):
        self.assertEqual(self.ons().first_word_at(10000), 10500)

    def test_inside_the_tolerance(self):
        # The Polish card was authored 40 ms off its English counterpart; same card.
        self.assertEqual(self.ons().first_word_at(10040), 10500)

    def test_outside_the_tolerance_is_a_different_card(self):
        self.assertIsNone(self.ons().first_word_at(10500))

    def test_a_low_confidence_alignment_is_not_evidence(self):
        """This threshold is the correction to the ASR brief, not decoration.

        At score >= 0.5 the same episode reported leads of +2003 ms and +1866 ms; at 0.8 the worst
        lead in the whole episode is +634 ms. The multi-second ones are the aligner guessing on a
        shout over crowd noise, and acting on one would move a line that was never early.
        """
        self.assertIsNone(self.ons([(10000, 12003, 0.42)]).first_word_at(10000))

    def test_the_nearest_card_wins(self):
        ons = self.ons([(10000, 10400, 0.9), (10080, 10900, 0.9)])
        self.assertEqual(ons.first_word_at(10070), 10900)

    def test_no_rows_at_all(self):
        self.assertIsNone(Onsets().first_word_at(10000))


class TestOnsetsFrom(unittest.TestCase):
    def cfg(self, **t):
        return {"timing": dict(TIMING, **t)}

    def data(self):
        return {"cues": [{"cue_start_ms": 10000, "fa_first_ms": 10500, "score": 0.9},
                         {"cue_start_ms": 20000, "score": 0.9}]}

    def test_rows_without_an_onset_are_dropped(self):
        ons = onsets_from(self.data(), self.cfg())
        self.assertEqual(len(ons), 1)

    def test_max_delay_zero_switches_the_whole_feature_off(self):
        self.assertIsNone(onsets_from(self.data(), self.cfg(max_delay_ms=0)))

    def test_no_artefact_is_not_an_error(self):
        self.assertIsNone(onsets_from(None, self.cfg()))
        self.assertIsNone(onsets_from({"cues": []}, self.cfg()))


class TestPlanDelays(unittest.TestCase):
    def plan(self, cues, rows, **t):
        return plan_delays(cues, Onsets(rows=list(rows), min_score=0.8, match_tol_ms=120),
                           dict(TIMING, **t))

    def test_a_late_actor_moves_the_line(self):
        c = cue(text="Krótka")           # ~521 ms predicted, 6 s window: room to spare
        self.plan([c], [(10000, 10500, 0.9)])
        self.assertEqual(c.delay_ms, 400)   # 500 ms of lead, 100 kept

    def test_a_small_lead_is_left_alone(self):
        c = cue()
        self.plan([c], [(10000, 10250, 0.9)])
        self.assertEqual(c.delay_ms, 0)

    def test_the_cap_holds(self):
        c = cue()
        self.plan([c], [(10000, 11500, 0.9)])
        self.assertEqual(c.delay_ms, 600)

    def test_a_line_is_never_pushed_out_of_its_own_window(self):
        """The third limit, and the one that keeps the speed planner out of it."""
        text = "Kibice Richmond wciąż wspierają swój klub mimo wszystko"
        c = cue(text=text, window=int(predicted_ms(text, TIMING) / 0.96) + 200)
        self.plan([c], [(10000, 11500, 0.9)])
        self.assertLessEqual(c.delay_ms, 200)
        self.assertGreaterEqual(c.delay_ms, 0)
        # And it really does still fit after moving.
        self.assertLessEqual(predicted_ms(text, TIMING) + c.delay_ms, c.window_ms * 0.96 + 1)

    def test_a_line_with_no_room_at_all_does_not_move(self):
        text = "Kibice Richmond wciąż wspierają swój klub mimo wszystko"
        c = cue(text=text, window=int(predicted_ms(text, TIMING) / 0.96))
        self.plan([c], [(10000, 11500, 0.9)])
        self.assertEqual(c.delay_ms, 0)

    def test_an_unmatched_cue_keeps_its_card(self):
        c = cue(start=55000)
        self.plan([c], [(10000, 10500, 0.9)])
        self.assertEqual(c.delay_ms, 0)

    def test_no_onsets_means_no_change(self):
        c = cue()
        c.delay_ms = 123
        report = plan_delays([c], None, TIMING)
        self.assertEqual(report["delayed"], 0)
        self.assertEqual(c.delay_ms, 123)   # untouched, not reset

    def test_replanning_clears_a_stale_delay(self):
        """`retime` runs over a script that may already carry delays from an earlier evidence set."""
        c = cue()
        c.delay_ms = 500
        self.plan([c], [(10000, 10100, 0.9)])
        self.assertEqual(c.delay_ms, 0)

    def test_the_report_counts_what_happened(self):
        cues = [cue(idx=1, start=10000, text="Krótka"),
                cue(idx=2, start=20000, text="Krótka"),
                cue(idx=3, start=30000, text="Krótka")]
        r = self.plan(cues, [(10000, 10500, 0.9), (20000, 20100, 0.9)])
        self.assertEqual((r["considered"], r["matched"], r["early"], r["delayed"]), (3, 2, 1, 1))
        self.assertEqual(r["max_ms"], 400)


class TestLeadCounts(unittest.TestCase):
    def test_before_and_after(self):
        cues = [cue(idx=1, start=10000, text="Krótka"), cue(idx=2, start=20000, text="Krótka")]
        ons = Onsets(rows=[(10000, 10500, 0.9), (20000, 20900, 0.9)], min_score=0.8)
        plan_delays(cues, ons, TIMING)
        counts = lead_counts(cues, ons)
        self.assertEqual(counts["matched"], 2)
        self.assertEqual(counts["before"][300], 2)
        # One closed to 100 ms of lead, the other capped at 600 and still 300 ms early.
        self.assertEqual(counts["after"][300], 0)
        self.assertEqual(counts["after"][500], 0)


class TestAssembleHonoursDelay(unittest.TestCase):
    CFG = {"mix": {"sample_rate": 1000}}

    def clip(self, start, ms, delay=0):
        return Clip(cue={"start_ms": start, "delay_ms": delay, "id": 1},
                    audio=np.ones(ms, dtype=np.float32))

    def test_the_delay_moves_the_audio(self):
        _, spans = assemble_voice(self.CFG, [self.clip(1000, 100, delay=250)], 5000)
        self.assertEqual(spans, [(1250, 1350)])

    def test_a_cue_without_the_key_is_placed_exactly_as_before(self):
        clip = Clip(cue={"start_ms": 1000, "id": 1}, audio=np.ones(100, dtype=np.float32))
        _, spans = assemble_voice(self.CFG, [clip], 5000)
        self.assertEqual(spans, [(1000, 1100)])

    def test_a_negative_delay_can_never_pull_a_line_earlier(self):
        _, spans = assemble_voice(self.CFG, [self.clip(1000, 100, delay=-400)], 5000)
        self.assertEqual(spans, [(1000, 1100)])

    def test_a_delayed_line_still_cannot_overlap_the_previous_one(self):
        clips = [self.clip(1000, 500), self.clip(1400, 100, delay=100)]
        _, spans = assemble_voice(self.CFG, clips, 5000)
        self.assertEqual(spans[0], (1000, 1500))
        self.assertEqual(spans[1][0], 1560)      # pushed to the cursor, 60 ms of air


if __name__ == "__main__":
    unittest.main()
