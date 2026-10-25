"""Extract, when the film has no Polish subtitle track: which foreign track, and what is recorded.

The rule that matters most here is the one that is NOT new: a film that does carry a Polish text
track has to come out of extract exactly as it always did - same file, same sidecar keys, no
translation branch touched. Everything else is about choosing the track a translation is made from
and saying, in `<base>.source.json`, that the episode's Polish text is going to be a translation.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import media as M  # noqa: E402


def sub(index, language=None, title=None, codec="subrip", forced=0, hi=0):
    raw = {"index": index, "codec_type": "subtitle", "codec_name": codec,
           "disposition": {"default": 0, "forced": forced, "hearing_impaired": hi}, "tags": {}}
    if language is not None:
        raw["tags"]["language"] = language
    if title is not None:
        raw["tags"]["title"] = title
    return raw


def aud(index, language=None):
    raw = {"index": index, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {}}
    if language is not None:
        raw["tags"]["language"] = language
    return raw


def streams(*raw):
    return M.streams_from_ffprobe({"streams": list(raw)})


REJECTS = ["SDH", "forced", "Forced"]


def pick(raw, audio="", wanted="auto", prefer_plain=True):
    return M.pick_translation_source(streams(*raw), "pol", audio, wanted, REJECTS, prefer_plain)


class TestLanguageKey(unittest.TestCase):
    def test_two_letter_and_bibliographic_terminology_pairs_fold_together(self):
        self.assertEqual(M.language_key("en"), "eng")
        self.assertEqual(M.language_key("ENG"), "eng")
        self.assertEqual(M.language_key("deu"), M.language_key("ger"))
        self.assertEqual(M.language_key("fra"), M.language_key("fre"))
        self.assertEqual(M.language_key("zho"), "chi")

    def test_an_unknown_code_passes_through_lowercased(self):
        self.assertEqual(M.language_key("Hrv"), "hrv")

    def test_the_audio_language_is_read_off_the_stream_the_downmix_uses(self):
        a = streams(aud(1, "eng"), aud(2, "ger"))
        self.assertEqual(M.audio_language(a, "a:0"), "eng")
        self.assertEqual(M.audio_language(a, "a:1"), "ger")
        # An index past the end, or a spec this cannot parse, falls back to the first track.
        self.assertEqual(M.audio_language(a, "a:7"), "eng")
        self.assertEqual(M.audio_language(a, "0:1"), "eng")

    def test_untagged_or_undetermined_audio_has_no_language(self):
        self.assertEqual(M.audio_language(streams(aud(1)), "a:0"), "")
        self.assertEqual(M.audio_language(streams(aud(1, "und")), "a:0"), "")
        self.assertEqual(M.audio_language([], "a:0"), "")


class TestPickTranslationSource(unittest.TestCase):
    def test_the_audios_own_language_beats_english(self):
        """A translation of what the actors say beats a translation of a translation."""
        got = pick([sub(2, "eng", "English"), sub(3, "ger", "Deutsch")], audio="deu")
        self.assertEqual((got.index, got.language), (3, "ger"))

    def test_english_is_second_when_the_audio_language_has_no_subtitle(self):
        got = pick([sub(2, "fre"), sub(3, "eng"), sub(4, "spa")], audio="jpn")
        self.assertEqual((got.index, got.language), (3, "eng"))

    def test_anything_else_comes_last_in_stream_order(self):
        got = pick([sub(5, "spa"), sub(4, "fre")])
        self.assertEqual(got.index, 4)

    def test_plain_dialogue_beats_sdh_by_title_and_by_disposition(self):
        got = pick([sub(2, "eng", "English [SDH]"), sub(3, "eng", "English")], audio="eng")
        self.assertEqual(got.index, 3)
        self.assertFalse(got.sdh)
        got = pick([sub(2, "eng", "English", hi=1), sub(3, "eng", "English")], audio="eng")
        self.assertEqual(got.index, 3)

    def test_sdh_is_used_when_it_is_all_there_is_and_says_so(self):
        got = pick([sub(2, "eng", "English (Hearing Impaired)")], audio="eng")
        self.assertEqual(got.index, 2)
        self.assertTrue(got.sdh)
        self.assertIn("SDH", got.reason)

    def test_prefer_plain_off_ranks_sdh_like_any_other_track(self):
        got = pick([sub(2, "eng", "English [SDH]"), sub(3, "eng", "English")], prefer_plain=False)
        self.assertEqual(got.index, 2)

    def test_the_language_rank_outweighs_sdh(self):
        """SDH in the audio's language still beats plain English: the source language decides."""
        got = pick([sub(2, "eng", "English"), sub(3, "ger", "Deutsch SDH")], audio="ger")
        self.assertEqual(got.index, 3)

    def test_forced_tracks_are_never_candidates(self):
        """Signage, not dialogue: translating it gives a lektor who reads out street names."""
        self.assertIsNone(pick([sub(2, "eng", "English", forced=1)]))
        self.assertIsNone(pick([sub(2, "eng", "English (Forced)")]))
        got = pick([sub(2, "eng", "Forced"), sub(3, "eng", "English")])
        self.assertEqual(got.index, 3)

    def test_bitmap_polish_untagged_and_undetermined_are_not_candidates(self):
        self.assertIsNone(pick([sub(2, "eng", codec="hdmv_pgs_subtitle"),
                                sub(3, "pol"), sub(4, "PL"), sub(5), sub(6, "und")]))

    def test_another_reject_pattern_ranks_below_sdh(self):
        got = M.pick_translation_source(streams(sub(2, "eng", "Commentary"),
                                                sub(3, "eng", "English SDH")),
                                        "pol", "", "auto", REJECTS + ["Commentary"])
        self.assertEqual(got.index, 3)
        got = M.pick_translation_source(streams(sub(2, "eng", "Commentary")),
                                        "pol", "", "auto", REJECTS + ["Commentary"])
        self.assertEqual(got.index, 2)
        self.assertTrue(got.rejected)

    def test_an_explicit_source_language_takes_only_that_one(self):
        raw = [sub(2, "eng"), sub(3, "deu")]
        self.assertEqual(pick(raw, wanted="ger").index, 3)
        self.assertEqual(pick(raw, wanted="en").index, 2)
        self.assertIsNone(pick(raw, wanted="fre"))

    def test_the_source_path_is_out_of_reach_of_every_srt_glob(self):
        """Every stage finds episodes with work_dir.glob('*.srt'); `subs/` keeps this out of it."""
        p = M.translation_source_path(Path("/w"), "ep", "eng")
        self.assertEqual(p, Path("/w/subs/ep.src.eng.srt"))


class Cfg:
    def __init__(self, tmp, translate=None, layout="stereo"):
        self.raw = {
            "paths": {"work_subdir": "_lektor"},
            "extract": {"subtitle_language": "pol", "reject_title_patterns": REJECTS},
            "mix": {"channel_layout": layout},
        }
        if translate is not None:
            self.raw["translate"] = translate
        self.input_dir = Path(tmp)
        self.work_dir = Path(tmp) / "_lektor"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def __getitem__(self, key):
        return self.raw[key]

    @staticmethod
    def binary(name):
        return name


class TestExtractBranches(unittest.TestCase):
    """stage_extract over canned ffprobe output, with ffmpeg replaced by a file writer."""

    def run_extract(self, subs, audio=(), translate=None, force=False):
        td = TemporaryDirectory()
        self.addCleanup(td.cleanup)
        video = Path(td.name) / "ep.mkv"
        video.write_bytes(b"not really an mkv")
        cfg = Cfg(td.name, translate)
        calls = []

        def probe(ffprobe, path, select=None, strict=False):
            return streams(*(subs if select == "s" else audio))

        def extract(ffmpeg, video, index, out, force=False):
            calls.append((index, out))
            out.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
            return True

        with mock.patch.object(M, "probe_streams", side_effect=probe), \
             mock.patch.object(M, "extract_subtitles", side_effect=extract), \
             mock.patch.object(M, "extract_downmix", return_value=True), \
             mock.patch.object(M, "say"):
            M.stage_extract(cfg, None, force)
        return cfg, calls

    def sidecar(self, cfg):
        return json.loads((cfg.work_dir / "ep.source.json").read_text(encoding="utf-8"))

    def test_a_polish_track_takes_the_old_path_and_writes_the_old_sidecar(self):
        cfg, calls = self.run_extract([sub(2, "eng"), sub(30, "pol", "Polski")],
                                      translate={"mode": "auto"})
        self.assertEqual(calls, [(30, cfg.work_dir / "ep.srt")])
        self.assertEqual(set(self.sidecar(cfg)),
                         {"base", "source", "source_windows", "subtitle_stream", "subtitle_title",
                          "language", "extracted_at"})
        self.assertFalse((cfg.work_dir / "subs").exists())

    def test_a_config_with_no_translate_table_behaves_as_auto(self):
        cfg, calls = self.run_extract([sub(30, "pol")], translate=None)
        self.assertEqual(calls, [(30, cfg.work_dir / "ep.srt")])

    def test_no_polish_track_extracts_the_foreign_source_and_records_where_it_came_from(self):
        cfg, calls = self.run_extract([sub(2, "eng", "English [SDH]"), sub(3, "eng", "English")],
                                      audio=[aud(1, "eng")], translate={"mode": "auto"})
        src = cfg.work_dir / "subs" / "ep.src.eng.srt"
        self.assertEqual(calls, [(3, src)])
        self.assertFalse((cfg.work_dir / "ep.srt").exists())
        d = self.sidecar(cfg)
        self.assertEqual(d["subtitle_origin"], "subs")
        self.assertEqual(d["source_language"], "eng")
        self.assertEqual(d["source_srt"], "subs/ep.src.eng.srt")
        self.assertEqual(d["subtitle_stream"], 3)
        self.assertFalse(d["source_sdh"])
        self.assertEqual(d["audio_language"], "eng")
        # The target language stays the one later stages are told the text is in.
        self.assertEqual(d["language"], "pol")

    def test_mode_never_is_the_old_skip(self):
        cfg, calls = self.run_extract([sub(2, "eng")], translate={"mode": "never"})
        self.assertEqual(calls, [])
        self.assertFalse((cfg.work_dir / "ep.source.json").exists())
        self.assertIn("no 'pol' subtitle stream",
                      (cfg.work_dir / "error_log.txt").read_text(encoding="utf-8"))

    def test_mode_always_translates_even_with_a_polish_track(self):
        cfg, calls = self.run_extract([sub(2, "eng"), sub(30, "pol")], translate={"mode": "always"})
        self.assertEqual(calls, [(2, cfg.work_dir / "subs" / "ep.src.eng.srt")])

    def test_a_bitmap_only_polish_track_is_translated_around_under_auto(self):
        cfg, calls = self.run_extract([sub(2, "eng"), sub(30, "pol", codec="hdmv_pgs_subtitle")],
                                      translate={"mode": "auto"})
        self.assertEqual(calls, [(2, cfg.work_dir / "subs" / "ep.src.eng.srt")])

    def test_asr_is_reported_as_not_implemented_and_skipped(self):
        cfg, calls = self.run_extract([sub(2, "eng")], translate={"mode": "auto", "source": "asr"})
        self.assertEqual(calls, [])
        log = (cfg.work_dir / "error_log.txt").read_text(encoding="utf-8")
        self.assertIn("not implemented", log)
        self.assertFalse((cfg.work_dir / "ep.source.json").exists())

    def test_nothing_to_translate_from_is_logged_like_a_missing_track(self):
        cfg, calls = self.run_extract([sub(2, "eng", forced=1)], translate={"mode": "auto"})
        self.assertEqual(calls, [])
        self.assertIn("to translate from",
                      (cfg.work_dir / "error_log.txt").read_text(encoding="utf-8"))

    def test_a_misspelled_mode_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            self.run_extract([sub(30, "pol")], translate={"mode": "sometimes"})


if __name__ == "__main__":
    unittest.main()
