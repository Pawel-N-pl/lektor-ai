"""What happens to Whisper's output before it is a subtitle: the filters, and the cutting into cues.

All of it is plain Python over the JSON the transcription script returns, so every rule is tested
here on hand-built segments - no model, no GPU, no audio. The numbers in the settings are the
shipped defaults unless a test says otherwise; none of them has been measured on real
transcriptions of this project's material, which is why each rule is pinned by what it does to a
constructed case rather than by a score.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import transcribe as TR  # noqa: E402
from lektorlib.srt import parse_srt  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# The shipped [transcribe] table, so a default changed in the tuning file is a default changed here.
with (REPO / "lektor.tuning.toml").open("rb") as fh:
    T = tomllib.load(fh)["transcribe"]


def seg(text, start=0, end=2000, words=None, nsp=0.05, lp=-0.3, cr=1.4, i=0):
    return {"id": i, "start": start, "end": end, "text": text, "no_speech_prob": nsp,
            "avg_logprob": lp, "compression_ratio": cr, "words": words or []}


def words(spec, start=0, step=300, dur=250):
    """'Hello there. How' -> words at a steady pace, each with its leading space."""
    out = []
    at = start
    for tok in spec.split(" "):
        out.append([at, at + dur, " " + tok, 0.9])
        at += step
    return out


def W(text, start, end):
    return TR.Word(start, end, " " + text)


class TestNormalise(unittest.TestCase):
    def test_case_punctuation_and_spacing_fold_away(self):
        self.assertEqual(TR.normalise("  Thanks for watching!! "), "thanks for watching")
        self.assertEqual(TR.normalise("Ça va, l'ami?"), "ça va l ami")


class TestLoop(unittest.TestCase):
    def test_the_longest_back_to_back_repeat_is_found(self):
        toks = "i don t know i don t know i don t know".split()
        self.assertEqual(TR.longest_loop(toks, 6), (4, 3))

    def test_words_that_come_back_apart_are_not_a_loop(self):
        self.assertEqual(TR.longest_loop("no i said no".split(), 6), (0, 0))

    def test_n_is_capped(self):
        toks = "a b c d e f g a b c d e f g".split()
        self.assertEqual(TR.longest_loop(toks, 6), (0, 0))
        self.assertEqual(TR.longest_loop(toks, 7), (7, 2))


class TestDropReason(unittest.TestCase):
    def test_a_plain_line_is_kept(self):
        self.assertEqual(TR.drop_reason(seg("Where were you last night?"), T), "")

    def test_empty_and_music_go(self):
        self.assertEqual(TR.drop_reason(seg("   "), T), "empty")
        for text in ("♪", "♪ ♪", " [Music] ", "(music playing)", "*Musik*"):
            with self.subTest(text=text):
                self.assertEqual(TR.drop_reason(seg(text), T), "music")
        self.assertEqual(TR.drop_reason(seg("♪ I want to hold your hand ♪"), T), "")

    def test_a_blacklisted_phrase_goes_only_when_it_is_the_whole_segment(self):
        self.assertIn("blacklisted", TR.drop_reason(seg("Thanks for watching!"), T))
        self.assertIn("blacklisted",
                      TR.drop_reason(seg("Subtitles by the Amara.org community"), T))
        self.assertEqual(TR.drop_reason(seg("Thanks for watching my back out there, Sam."), T),
                         "")

    def test_the_blacklist_matches_whole_words_only(self):
        t = dict(T, blacklist=["the end"], blacklist_min_cover=0.1)
        self.assertEqual(TR.drop_reason(seg("They lend money."), t), "")
        self.assertIn("blacklisted", TR.drop_reason(seg("This is the end."), t))

    def test_no_speech_needs_both_halves_of_whisper_own_rule(self):
        self.assertIn("no speech", TR.drop_reason(seg("Hm.", nsp=0.9, lp=-1.4), T))
        # Confident decoding of a quiet line: only one half fires, the line stays.
        self.assertEqual(TR.drop_reason(seg("I know.", nsp=0.9, lp=-0.2), T), "")
        self.assertEqual(TR.drop_reason(seg("I know.", nsp=0.1, lp=-1.4), T), "")

    def test_a_high_compression_ratio_goes(self):
        self.assertIn("compression ratio", TR.drop_reason(seg("x", cr=3.1), T))

    def test_a_loop_inside_one_segment_goes_and_a_short_repeat_does_not(self):
        looped = "I'm sorry, " * 8
        self.assertIn("loop", TR.drop_reason(seg(looped, cr=1.5), T))
        self.assertEqual(TR.drop_reason(seg("No, no, no, no!"), T), "")

    def test_a_segment_outside_the_vads_speech_goes_and_no_evidence_drops_nothing(self):
        s = seg("Hello?", start=10_000, end=12_000)
        self.assertIn("outside speech", TR.drop_reason(s, T, [[0, 9_000], [11_800, 20_000]]))
        self.assertEqual(TR.drop_reason(s, T, [[9_000, 11_000]]), "")
        self.assertEqual(TR.drop_reason(s, T, None), "")


class TestFilterSegments(unittest.TestCase):
    def test_reasons_are_kept_with_the_dropped_segment(self):
        kept, dropped = TR.filter_segments([seg("Hi.", i=1), seg("♪", i=2)], T)
        self.assertEqual([s["id"] for s in kept], [1])
        self.assertEqual(dropped[0]["dropped"], "music")
        self.assertEqual(dropped[0]["id"], 2)

    def test_the_same_text_many_segments_in_a_row_keeps_the_first_copies(self):
        segs = [seg("Yeah.", i=i) for i in range(5)] + [seg("Okay.", i=9)]
        kept, dropped = TR.filter_segments(segs, T)
        self.assertEqual([s["id"] for s in kept], [0, 1, 9])
        self.assertEqual(len(dropped), 3)
        self.assertIn("3 segments in a row", dropped[0]["dropped"])

    def test_a_dropped_segment_does_not_break_a_run(self):
        segs = [seg("Yeah.", i=0), seg("♪", i=1), seg("Yeah.", i=2), seg("Yeah.", i=3)]
        kept, _ = TR.filter_segments(segs, T)
        self.assertEqual([s["id"] for s in kept], [0, 2])


class TestWordsOf(unittest.TestCase):
    def test_word_timestamps_are_taken_as_they_are(self):
        ws = TR.words_of([seg("Hi there.", words=[[100, 300, " Hi", 0.9],
                                                  [350, 600, " there.", 0.8]])])
        self.assertEqual([(w.start, w.end, w.text) for w in ws],
                         [(100, 300, " Hi"), (350, 600, " there.")])

    def test_a_segment_without_words_is_spread_over_its_span_not_lost(self):
        ws = TR.words_of([seg(" ab cd", start=1000, end=2000)])
        self.assertEqual([(w.start, w.end, w.text.strip()) for w in ws],
                         [(1000, 1500, "ab"), (1500, 2000, "cd")])


class TestLayout(unittest.TestCase):
    def test_a_short_line_stays_one_line(self):
        self.assertEqual(TR.layout("Where were you?"), "Where were you?")

    def test_a_long_one_breaks_into_two_balanced_lines_that_both_fit(self):
        text = "I told you a hundred times that we would never go back to that place again"
        out = TR.layout(text, 42, 2)
        a, b = out.split("\n")
        self.assertLessEqual(max(len(a), len(b)), 42)
        self.assertLessEqual(abs(len(a) - len(b)), 8)
        self.assertEqual(f"{a} {b}", text)

    def test_fits_lines_counts_greedily_and_refuses_an_oversized_word(self):
        self.assertTrue(TR.fits_lines("a " * 20, 42, 1))
        self.assertFalse(TR.fits_lines("word " * 20, 42, 2))
        self.assertFalse(TR.fits_lines("x" * 43, 42, 2))


class TestSegmentWords(unittest.TestCase):
    def cues(self, ws, **over):
        t = dict(T, **over)
        return [TR.cue_text(g) for g in TR.segment_words(ws, t)]

    def test_a_short_sentence_is_one_cue(self):
        ws = [W("Where", 0, 200), W("were", 250, 400), W("you?", 450, 700)]
        self.assertEqual(self.cues(ws), ["Where were you?"])

    def test_a_long_silence_always_ends_a_cue(self):
        ws = [W("Hello.", 0, 400), W("Anyone", 2000, 2300), W("there?", 2350, 2700)]
        self.assertEqual(self.cues(ws), ["Hello.", "Anyone there?"])

    def test_a_cue_too_long_to_read_splits_at_the_sentence_end_first(self):
        text = ("We should go now. The car is waiting outside, and the driver does not like to "
                "be kept waiting")
        ws = [TR.Word(w[0], w[1], w[2]) for w in words(text, step=150, dur=120)]
        got = self.cues(ws)
        self.assertEqual(got[0], "We should go now.")
        for c in got:
            self.assertTrue(TR.fits_lines(c, 42, 2), c)

    def test_without_a_sentence_end_a_pause_beats_a_comma(self):
        ws = [TR.Word(w[0], w[1], w[2]) for w in words(
            "one two three, four five six seven eight nine ten eleven twelve thirteen fourteen "
            "fifteen sixteen", step=150, dur=120)]
        # A 500 ms breath after 'seven' - more than split_pause_ms, less than a hard break.
        k = next(i for i, w in enumerate(ws) if w.text.strip() == "eight")
        for w in ws[k:]:
            w.start += 400
            w.end += 400
        got = self.cues(ws, max_line_chars=30)
        self.assertTrue(got[0].endswith("seven"), got)

    def test_with_neither_the_comma_is_taken(self):
        ws = [TR.Word(w[0], w[1], w[2]) for w in words(
            "one two three four five, six seven eight nine ten eleven twelve thirteen fourteen",
            step=150, dur=120)]
        got = self.cues(ws, max_line_chars=30)
        self.assertEqual(got[0], "one two three four five,")

    def test_a_cue_longer_than_max_cue_ms_is_split_even_when_the_text_fits(self):
        ws = [W("So", 0, 400), W("slow.", 900, 3000), W("Very", 3100, 3400),
              W("slow.", 3500, 7500)]
        got = self.cues(ws)
        self.assertEqual(got, ["So slow.", "Very slow."])

    def test_a_split_never_falls_inside_a_word(self):
        ws = [TR.Word(w[0], w[1], w[2]) for w in words("supercalifragilistic " * 6, step=200)]
        for c in self.cues(ws):
            self.assertTrue(all(tok == "supercalifragilistic" for tok in c.split()), c)


class TestTimeCues(unittest.TestCase):
    def test_a_cue_lingers_past_its_last_word_but_never_into_the_next(self):
        groups = [[W("Hi.", 1000, 1300)], [W("Bye.", 1700, 2900)]]
        a, b = TR.time_cues(groups, dict(T, linger_ms=400, min_cue_ms=0, min_gap_ms=80))
        self.assertEqual((a.start_ms, a.end_ms), (1000, 1620))
        self.assertEqual((b.start_ms, b.end_ms), (1700, 3300))

    def test_a_short_cue_is_held_for_the_minimum_where_there_is_room(self):
        (c,) = TR.time_cues([[W("Hi.", 1000, 1200)]], dict(T, linger_ms=0, min_cue_ms=1000))
        self.assertEqual((c.start_ms, c.end_ms), (1000, 2000))

    def test_the_onset_shift_moves_starts_later_and_ships_at_zero(self):
        self.assertEqual(T["onset_shift_ms"], 0)
        (c,) = TR.time_cues([[W("Hi", 1000, 1500), W("there.", 1600, 2000)]],
                            dict(T, onset_shift_ms=300))
        self.assertEqual(c.start_ms, 1300)

    def test_cues_never_overlap_even_when_words_do(self):
        groups = [[W("A.", 1000, 1500)], [W("B.", 1450, 1600)], [W("C.", 1550, 1700)]]
        cs = TR.time_cues(groups, T)
        for x, y in zip(cs, cs[1:]):
            self.assertLess(x.end_ms, y.start_ms)
            self.assertLess(x.start_ms, x.end_ms)


class TestBuildTranscript(unittest.TestCase):
    def test_end_to_end_on_a_canned_reply(self):
        raw = {
            "meta": {"language": "en"},
            "speech": [[0, 9000]],
            "segments": [
                seg(" Where were you?", 0, 1000, words("Where were you?", start=0)),
                seg(" Thanks for watching!", 2000, 3000, words("Thanks for watching!",
                                                               start=2000)),
                seg(" I was at home.", 4000, 5000, words("I was at home.", start=4000)),
            ],
        }
        tr = TR.build_transcript(raw, T)
        self.assertEqual([c.text for c in tr.cues], ["Where were you?", "I was at home."])
        self.assertEqual(len(tr.dropped), 1)

    def test_an_empty_vad_region_list_is_no_evidence(self):
        raw = {"speech": [], "segments": [seg(" Hi.", 0, 500, words("Hi.", start=0))]}
        self.assertEqual([c.text for c in TR.build_transcript(raw, T).cues], ["Hi."])

    def test_the_srt_parses_back_with_the_same_cues(self):
        raw = {"segments": [seg(" Hi.", 1000, 1500, [[1000, 1400, " Hi.", 0.9]]),
                            seg(" Bye.", 4000, 4500, [[4000, 4400, " Bye.", 0.9]])]}
        tr = TR.build_transcript(raw, T)
        with tempfile_srt(TR.format_srt(tr.cues, T)) as p:
            back = parse_srt(p)
        self.assertEqual([(c.start_ms, c.end_ms, c.raw) for c in back],
                         [(c.start_ms, c.end_ms, c.text) for c in tr.cues])


class tempfile_srt:
    def __init__(self, text):
        import tempfile
        self.td = tempfile.TemporaryDirectory()
        self.p = Path(self.td.name) / "x.srt"
        self.p.write_text(text, encoding="utf-8")

    def __enter__(self):
        return self.p

    def __exit__(self, *exc):
        self.td.cleanup()


class TestLanguage(unittest.TestCase):
    def test_tags_become_whisper_codes(self):
        self.assertEqual(TR.whisper_code("eng"), "en")
        self.assertEqual(TR.whisper_code("deu"), "de")
        self.assertEqual(TR.whisper_code("ger"), "de")
        self.assertEqual(TR.whisper_code("nor"), "no")
        self.assertEqual(TR.whisper_code("hr"), "hr")
        self.assertEqual(TR.whisper_code("und"), "")
        self.assertEqual(TR.whisper_code(""), "")

    def test_a_language_outside_the_subtitle_alias_table_is_still_told_to_whisper(self):
        """mkv tags are 639-2, and the alias table media folds knows thirty languages."""
        for tag, code in (("hrv", "hr"), ("ara", "ar"), ("heb", "he"), ("hin", "hi"),
                          ("rum", "ro"), ("ron", "ro"), ("slo", "sk"), ("slk", "sk"),
                          ("bul", "bg"), ("srp", "sr"), ("tha", "th"), ("vie", "vi"),
                          ("HRV", "hr"), ("haw", "haw"), ("yue", "yue"), ("jav", "jw")):
            with self.subTest(tag=tag):
                self.assertEqual(TR.whisper_code(tag), code)
        self.assertEqual(len(set(TR._WHISPER_CODE.values())), 100)

    def test_a_withdrawn_or_unknown_code_never_reaches_the_model(self):
        """faster-whisper raises ValueError on a code it does not know, after loading the model."""
        self.assertEqual([TR.whisper_code(t) for t in ("iw", "in", "ji", "jv", "nb")],
                         ["he", "id", "yi", "jw", "no"])
        for tag in ("xx", "zz", "qaa", "klingon"):
            with self.subTest(tag=tag):
                self.assertEqual(TR.whisper_code(tag), "")

    def test_whispers_answer_is_filed_under_the_tag_a_track_would_carry(self):
        self.assertEqual([TR.tag_for(c) for c in ("en", "de", "no", "hr", "zh", "haw")],
                         ["eng", "ger", "nor", "hrv", "chi", "haw"])

    def test_the_config_wins_then_the_audio_tag_then_detection(self):
        class Cfg:
            def __init__(self, wanted):
                self.raw = {"translate": {"source_language": wanted}}

        self.assertEqual(TR.requested_language(Cfg("ger"), {"audio_language": "eng"}),
                         ("de", "config"))
        self.assertEqual(TR.requested_language(Cfg("auto"), {"audio_language": "eng"}),
                         ("en", "tag"))
        self.assertEqual(TR.requested_language(Cfg("auto"), {"audio_language": ""}),
                         ("", "detect"))
        self.assertEqual(TR.requested_language(Cfg("hrv"), {"audio_language": "eng"}),
                         ("hr", "config"))
        self.assertEqual(TR.requested_language(Cfg("auto"), {"audio_language": "hrv"}),
                         ("hr", "tag"))
        self.assertEqual(TR.unknown_language(Cfg("hrv"), {}), "")

    def test_a_setting_whisper_cannot_use_is_passed_over_and_said(self):
        class Cfg:
            def __init__(self, wanted):
                self.raw = {"translate": {"source_language": wanted}}

        self.assertEqual(TR.requested_language(Cfg("xx"), {"audio_language": "eng"}),
                         ("en", "tag"))
        self.assertEqual(TR.unknown_language(Cfg("xx"), {"audio_language": "eng"}),
                         "translate.source_language 'xx' is no language Whisper knows – the "
                         "audio track's tag is used instead")
        self.assertEqual(TR.requested_language(Cfg("xx"), {}), ("", "detect"))
        self.assertIn("Whisper detects the language", TR.unknown_language(Cfg("xx"), {}))
        self.assertEqual(TR.requested_language(Cfg("auto"), {"audio_language": "qaa"}),
                         ("", "detect"))
        self.assertIn("the audio track's tag 'qaa' is no language Whisper knows",
                      TR.unknown_language(Cfg("auto"), {"audio_language": "qaa"}))
        self.assertEqual(TR.unknown_language(Cfg("auto"), {"audio_language": ""}), "")


if __name__ == "__main__":
    unittest.main()
