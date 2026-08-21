"""The merge gate: the delivery is allowed to veto a merge the text rules already allowed.

Two properties matter more than any single case here and both are asserted below:

  1. With no evidence (`ev=None`, no speech.json, gate off in config) every function behaves
     EXACTLY as it did before the gate existed. That is what keeps six episodes of already-paid
     condensation and a work dir full of cached ElevenLabs lines valid.
  2. The gate only ever REFUSES. It can never authorise a merge the text rules refused, so no
     amount of bad alignment can glue two finished sentences together.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib.speech import (SpeechEvents, cues_for_alignment, episode_tag,  # noqa: E402
                              events_from, load_events, words_and_gaps)
from lektorlib.srt import Cue, continues_sentence, merge_contiguous  # noqa: E402

CLEAN = {
    "merge_gap_ms": 140, "merge_max_cues": 4, "merge_max_chars": 160, "merge_max_span_ms": 9000,
    "merge_gate": "both", "merge_min_pause_ms": 300, "merge_max_pause_ms": 5000,
    "merge_pause_tol_ms": 250, "merge_cut_pad_ms": 0,
}


def cfg(**over):
    return {"clean": dict(CLEAN, **over)}


def cue(idx, start, end, text, raw=None):
    c = Cue(idx=idx, start_ms=start, end_ms=end, raw=raw if raw is not None else text)
    c.text = text
    return c


# The seam under test everywhere below: card one comes down at 1000, card two goes up at 1083,
# which is this release's authoring floor and says nothing on its own.
A = cue(1, 0, 1000, "Kibice Richmond, którzy wiele przeszli,")
B = cue(2, 1083, 2500, "wciąż wspierają swój klub.")


class TestPause(unittest.TestCase):
    def ev(self, gaps, **over):
        return SpeechEvents(gaps=gaps, cuts=[], **over)

    def test_silence_across_the_seam_refuses(self):
        # The actors stopped for 480 ms where the cards are 83 ms apart.
        self.assertFalse(continues_sentence(A, B, self.ev([(900, 1380)])))

    def test_short_silence_is_not_a_pause(self):
        self.assertTrue(continues_sentence(A, B, self.ev([(1000, 1180)])))

    def test_silence_far_from_the_seam_is_ignored(self):
        # 2 s later is somebody else's pause; tolerance is 250 ms either side.
        self.assertTrue(continues_sentence(A, B, self.ev([(3000, 3600)])))

    def test_tolerance_edges(self):
        ev = self.ev([(1400, 1800)])          # starts 317 ms after the card goes up
        self.assertTrue(continues_sentence(A, B, ev))
        self.assertFalse(continues_sentence(A, B, self.ev([(1300, 1700)])))

    def test_a_hole_in_the_alignment_is_not_a_pause(self):
        # 30 s of nothing is a stretch the aligner never covered, not a breath inside a sentence.
        self.assertTrue(continues_sentence(A, B, self.ev([(500, 30500)])))

    def test_threshold_is_the_configured_one(self):
        gaps = [(1000, 1200)]                  # 200 ms
        self.assertTrue(continues_sentence(A, B, self.ev(gaps)))
        self.assertFalse(continues_sentence(A, B, self.ev(gaps, min_pause_ms=150)))

    def test_pause_is_found_among_many(self):
        gaps = [(t, t + 400) for t in range(0, 60000, 5000)] + [(950, 1400)]
        self.assertFalse(continues_sentence(A, B, self.ev(gaps)))


class TestCut(unittest.TestCase):
    def ev(self, cuts, **over):
        return SpeechEvents(gaps=[], cuts=cuts, **over)

    def test_cut_inside_the_seam_refuses(self):
        self.assertFalse(continues_sentence(A, B, self.ev([1040])))

    def test_cut_outside_the_seam_is_ignored(self):
        self.assertTrue(continues_sentence(A, B, self.ev([500, 4000])))

    def test_cut_pad_widens_the_seam(self):
        self.assertTrue(continues_sentence(A, B, self.ev([1200])))
        self.assertFalse(continues_sentence(A, B, self.ev([1200], cut_pad_ms=200)))

    def test_mode_selects_which_signal_speaks(self):
        both = SpeechEvents(gaps=[(900, 1380)], cuts=[1040])
        self.assertFalse(continues_sentence(A, B, both))
        self.assertFalse(continues_sentence(A, B, SpeechEvents(gaps=[(900, 1380)], cuts=[],
                                                               mode="pause")))
        # mode 'cut' must not consult the pauses, and vice versa
        self.assertTrue(continues_sentence(A, B, SpeechEvents(gaps=[(900, 1380)], cuts=[],
                                                              mode="cut")))
        self.assertTrue(continues_sentence(A, B, SpeechEvents(gaps=[], cuts=[1040],
                                                              mode="pause")))


class TestGateOnlyRefuses(unittest.TestCase):
    """No evidence may ever CREATE a merge. The text rules stay sovereign."""

    def test_finished_sentence_stays_unmerged_whatever_the_audio_says(self):
        a = cue(1, 0, 1000, "Richmond wygrał.")
        b = cue(2, 1083, 2000, "Wszyscy się cieszą.")
        for ev in (None, SpeechEvents(gaps=[], cuts=[]), SpeechEvents(gaps=[(0, 60000)],
                                                                     cuts=list(range(0, 5000, 7)))):
            self.assertFalse(continues_sentence(a, b, ev))

    def test_speaker_dash_stays_unmerged(self):
        b = cue(2, 1083, 2000, "Nie wiem.", raw="- Nie wiem.")
        self.assertFalse(continues_sentence(A, b, SpeechEvents(gaps=[], cuts=[])))


class TestNoEvidenceIsYesterday(unittest.TestCase):
    def test_none_is_a_passthrough(self):
        self.assertTrue(continues_sentence(A, B, None))
        self.assertEqual(len(merge_contiguous([A, B], cfg(), None)), 1)

    def test_empty_evidence_changes_nothing(self):
        self.assertEqual(len(merge_contiguous([A, B], cfg(), SpeechEvents(gaps=[], cuts=[]))), 1)

    def test_events_from_returns_none_when_it_would_be_a_no_op(self):
        data = {"gaps": [(900, 1380)], "cuts": [1040]}
        self.assertIsNone(events_from(None, cfg()))
        self.assertIsNone(events_from({}, cfg()))
        self.assertIsNone(events_from(data, cfg(merge_gate="off")))
        self.assertIsNone(events_from(data, cfg(merge_gate="nonsense")))
        # An artefact with no signal in it must not look like an enabled gate either.
        self.assertIsNone(events_from({"gaps": [], "cuts": []}, cfg()))
        self.assertIsNotNone(events_from(data, cfg()))

    def test_load_events_tolerates_a_missing_or_broken_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            self.assertIsNone(load_events(work, "nothing-here", cfg()))
            (work / "ep.speech.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_events(work, "ep", cfg()))
            (work / "ep.speech.json").write_text(
                json.dumps({"gaps": [[900, 1380]], "cuts": [1040]}), encoding="utf-8")
            ev = load_events(work, "ep", cfg())
            self.assertIsNotNone(ev)
            self.assertFalse(continues_sentence(A, B, ev))


class TestMergeWithGate(unittest.TestCase):
    def test_gate_splits_a_group_at_the_pause(self):
        cues = [cue(1, 0, 1000, "Zaczynam zdanie,"),
                cue(2, 1083, 2000, "ciągnę je dalej,"),
                cue(3, 2083, 3000, "i kończę je tutaj.")]
        ungated = merge_contiguous(cues, cfg(), None)
        self.assertEqual([c.merged_from for c in ungated], [[1, 2, 3]])
        # A pause across the SECOND seam only.
        gated = merge_contiguous(cues, cfg(), SpeechEvents(gaps=[(1900, 2400)], cuts=[]))
        self.assertEqual([c.merged_from or [c.idx] for c in gated], [[1, 2], [3]])

    def test_every_cue_still_survives_exactly_once(self):
        cues = [cue(i, i * 1000, i * 1000 + 917, f"część {i},") for i in range(1, 12)]
        ev = SpeechEvents(gaps=[(3900, 4400), (7900, 8400)], cuts=[5950])
        out = merge_contiguous(cues, cfg(), ev)
        folded = [i for c in out for i in (c.merged_from or [c.idx])]
        self.assertEqual(folded, [c.idx for c in cues])
        # Refusing seams makes MORE groups, not fewer, so the invariant is on the joins: every
        # seam the gate vetoes is one join less, and none is ever added.
        # (The count is not simply "three joins fewer": merge_max_cues chops the ungated run
        # every four cards, so refusing a seam also moves where that cap next bites.)
        ungated = merge_contiguous(cues, cfg(), None)
        self.assertEqual([c.merged_from for c in ungated], [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11]])
        self.assertEqual([c.merged_from for c in out],
                         [[1, 2, 3], [4, 5], [6, 7], [8, 9, 10, 11]])
        joins = lambda o: len(cues) - len(o)  # noqa: E731
        self.assertLess(joins(out), joins(ungated))


class TestArtefactPlumbing(unittest.TestCase):
    def test_episode_tag(self):
        self.assertEqual(episode_tag("Ted.Lasso.S02E06.The.Signal.1080p.WEB-DL"), "S02E06")
        self.assertEqual(episode_tag("no tag here"), "")

    def test_words_and_gaps_drops_low_confidence_cues(self):
        fa = {"rows": [
            {"i": 1, "cue_start_ms": 0, "cue_end_ms": 900, "score": 0.9, "n_words": 2,
             "word_times": [[100, 300], [320, 800]]},
            {"i": 2, "cue_start_ms": 1000, "cue_end_ms": 1900, "score": 0.2, "n_words": 1,
             "word_times": [[1100, 1800]]},
            {"i": 3, "cue_start_ms": 2000, "cue_end_ms": 2900, "score": 0.8, "n_words": 1,
             "word_times": [[2100, 2800]]},
        ]}
        words, gaps, rows = words_and_gaps(fa, min_score=0.5, store_min_gap_ms=150)
        self.assertEqual(words, [[100, 300], [320, 800], [2100, 2800]])
        # The 20 ms gap inside the confident cue is below the store floor; the silence spanning
        # the discarded cue is kept, and it is the max_pause_ms rule that later refuses to read
        # it as a pause in the delivery.
        self.assertEqual(gaps, [[800, 2100]])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["fa_first_ms"], 100)

    def test_stage_degrades_instead_of_failing(self):
        """No video, no container, no reference text - the stage must survive all three.

        The pipeline has to keep working when the borrowed `asr` container is down or the media
        is not mounted, because the gate is an improvement on a working pipeline, not a
        prerequisite for one.
        """
        import tempfile

        from lektorlib.config import Config
        from lektorlib.speech import speech_path, stage_speech

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw = Config(Path("/home/pawel/projects/lektor-ai/lektor.toml")).raw
            raw["paths"]["input_dir_wsl"] = str(root / "media")
            raw["speech"]["tmp_dir"] = str(root / "tmp")
            raw["speech"]["asr_container"] = "no-such-container-here"
            cfg_path = root / "cfg.json"
            cfg_path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = Config(cfg_path)
            (root / "media").mkdir(exist_ok=True)
            with self.assertRaises(SystemExit):
                stage_speech(cfg, None, force=True)     # no subtitles at all
            (cfg.work_dir / "ep.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nCześć.\n", encoding="utf-8")
            stage_speech(cfg, None, force=True)         # subtitles, but no video
            self.assertFalse(speech_path(cfg.work_dir, "ep").exists())

    def test_cues_for_alignment_strips_markup_and_keeps_words(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.srt"
            p.write_text("1\n00:00:01,000 --> 00:00:02,000\n[ted] Hey, <i>Roy</i>!\n\n"
                         "2\n00:00:03,000 --> 00:00:04,000\n[door closes]\n\n"
                         "3\n00:00:05,000 --> 00:00:06,000\nI-I don't remember.\n",
                         encoding="utf-8")
            cues = cues_for_alignment(p)
        self.assertEqual([c["words"] for c in cues], [["Hey", "Roy"], ["I", "I", "don't", "remember"]])
        self.assertEqual(cues[0]["start"], 1000)


if __name__ == "__main__":
    unittest.main()
