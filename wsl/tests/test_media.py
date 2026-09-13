"""The native extract/mux layer, pinned against the PowerShell it replaces.

Two rules carry the whole module and both fail silently rather than loudly, which is why the
fixtures below are ffprobe payloads and not real files:

  pick_subtitle   picks the Polish DIALOGUE track out of 43 subtitle streams. Ted Lasso S03E01 to
                  E05 have pol at index 30; S03E06 has it at 31, because a forced Polish track was
                  added ahead of it. A port that quietly reverts to "first pol stream" produces an
                  episode of signage, and nothing downstream can tell that from a short episode.
  mux_args        appends the lektor after every original audio stream and indexes its metadata
                  and dispositions off the probed count. Getting that number wrong relabels a
                  commentary track as the lektor and makes THAT the default.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import media as M  # noqa: E402


def sub(index, language=None, title=None, codec="subrip", forced=0, no_tags=False):
    """One ffprobe subtitle stream as ffprobe really prints it, tags nested and all."""
    raw = {"index": index, "codec_type": "subtitle", "codec_name": codec,
           "disposition": {"default": 0, "forced": forced}}
    if not no_tags:
        tags = {}
        if language is not None:
            tags["language"] = language
        if title is not None:
            tags["title"] = title
        raw["tags"] = tags
    return raw


def streams(*raw):
    return M.streams_from_ffprobe({"streams": list(raw)})


def pick(raw_streams, language="pol", rejects=("SDH", "forced", "Forced")):
    """`rejects` defaults to the shipped extract.reject_title_patterns."""
    return M.pick_subtitle(streams(*raw_streams), language,
                           None if rejects is None else list(rejects))


class TestFlattening(unittest.TestCase):
    def test_tags_are_flattened_and_the_absolute_index_survives(self):
        s = streams(sub(30, "pol", "Polski"))[0]
        self.assertEqual((s.index, s.language, s.title, s.codec_name), (30, "pol", "Polski", "subrip"))

    def test_a_stream_with_no_tags_object_at_all_is_readable(self):
        """Remuxes drop the tags object entirely; reading it must not raise."""
        s = streams(sub(4, no_tags=True))[0]
        self.assertEqual((s.language, s.title), ("", ""))

    def test_audio_channels_come_through(self):
        s = M.stream_from_ffprobe({"index": 1, "codec_type": "audio", "codec_name": "eac3",
                                   "channels": 6})
        self.assertEqual(s.channels, 6)


class TestPickSubtitleByLanguage(unittest.TestCase):
    """Rule one: the language tag decides, never the index."""

    def test_polish_at_index_30_among_43_streams(self):
        """S03E01-E05: eng at 0-2, a long tail of other languages, pol at 30, por at 31."""
        raw = [sub(0, "eng", "English"), sub(1, "eng", "English [SDH]"), sub(2, "eng", "Signs")]
        raw += [sub(i, "fre") for i in range(3, 30)]
        raw += [sub(30, "pol", "Polski"), sub(31, "por", "Portugues")]
        got = pick(raw)
        self.assertEqual(got.index, 30)
        self.assertFalse(got.rejected)

    def test_polish_at_index_31_when_a_forced_track_was_added_ahead_of_it(self):
        """S03E06 of the same season. The naive 'first pol stream' picks the signage track."""
        raw = [sub(0, "eng", "English"), sub(1, "eng", "English [SDH]")]
        raw += [sub(i, "spa") for i in range(2, 30)]
        raw += [sub(30, "pol", "Polski [Forced]", forced=1), sub(31, "pol", "Polski"),
                sub(32, "por", "Portugues")]
        got = pick(raw)
        self.assertEqual(got.index, 31)
        self.assertFalse(got.rejected)

    def test_neighbouring_portuguese_is_never_confused_for_polish(self):
        got = pick([sub(0, "por", "Portugues"), sub(1, "pol", "Polski")])
        self.assertEqual(got.index, 1)

    def test_many_english_tracks_yield_nothing_when_polish_is_absent(self):
        raw = [sub(0, "eng", "English"), sub(1, "eng", "English [SDH]"),
               sub(2, "eng", "English [Forced]", forced=1), sub(3, "eng", "Commentary")]
        self.assertIsNone(pick(raw))

    def test_language_matching_is_case_insensitive_both_ways(self):
        """PowerShell `-ne` on strings ignores case, so POL has always matched 'pol'."""
        self.assertEqual(pick([sub(7, "POL", "Polski")]).index, 7)
        self.assertEqual(pick([sub(7, "pol", "Polski")], language="POL").index, 7)

    def test_a_stream_with_no_language_tag_is_never_a_candidate(self):
        """Not even as a fallback: an untagged stream here is as likely Portuguese as Polish."""
        self.assertIsNone(pick([sub(0, None, "Polski"), sub(1, no_tags=True)]))

    def test_an_empty_language_tag_is_not_a_candidate_either(self):
        self.assertIsNone(pick([sub(0, "", "Polski")]))

    def test_no_subtitle_streams_at_all(self):
        self.assertIsNone(pick([]))


class TestPickSubtitleRejection(unittest.TestCase):
    """Rule two: a title pattern or a forced disposition demotes a candidate."""

    def test_sdh_is_rejected_in_favour_of_the_plain_track(self):
        got = pick([sub(10, "pol", "Polski SDH"), sub(11, "pol", "Polski")])
        self.assertEqual(got.index, 11)

    def test_patterns_are_case_insensitive_so_lowercase_sdh_is_rejected_too(self):
        """`-match` is a case-insensitive regex, which is why 'forced' and 'Forced' in the shipped
        config are the same rule written twice."""
        got = pick([sub(10, "pol", "polski sdh"), sub(11, "pol", "Polski")])
        self.assertEqual(got.index, 11)
        self.assertEqual(pick([sub(3, "pol", "Napisy FORCED"), sub(4, "pol", "Napisy")]).index, 4)

    def test_a_forced_disposition_is_rejected_even_with_an_innocent_title(self):
        """A forced track carries signage only, and it is often titled like a normal one."""
        got = pick([sub(30, "pol", "Polski", forced=1), sub(31, "pol", "Polski")])
        self.assertEqual(got.index, 31)
        self.assertIn("forced", pick([sub(30, "pol", "Polski", forced=1)]).reason)

    def test_the_only_candidate_is_returned_even_when_its_title_rejects_it(self):
        """A rejected fallback, never a skip: an SDH track still reads as dialogue."""
        got = pick([sub(0, "eng", "English"), sub(12, "pol", "Polski SDH")])
        self.assertEqual(got.index, 12)
        self.assertTrue(got.rejected)
        self.assertIn("SDH", got.reason)

    def test_the_only_candidate_is_returned_even_when_it_is_forced(self):
        got = pick([sub(12, "pol", "Polski", forced=1)])
        self.assertEqual(got.index, 12)
        self.assertTrue(got.rejected)

    def test_all_rejected_falls_back_to_the_first_candidate_in_ffprobe_order(self):
        got = pick([sub(12, "pol", "Polski SDH"), sub(13, "pol", "Polski Forced", forced=1)])
        self.assertEqual(got.index, 12)
        self.assertTrue(got.rejected)

    def test_first_non_rejected_wins_even_when_a_rejected_one_has_a_lower_index(self):
        got = pick([sub(5, "pol", "Polski SDH"), sub(20, "pol", "Polski"), sub(21, "pol", "Polski")])
        self.assertEqual(got.index, 20)

    def test_every_pattern_is_tried_because_the_loop_does_not_break(self):
        got = pick([sub(9, "pol", "Polski SDH")], rejects=("nomatch", "alsonomatch", "SDH"))
        self.assertTrue(got.rejected)

    def test_patterns_are_regexes_not_substrings(self):
        self.assertTrue(pick([sub(9, "pol", "Polski SDH")], rejects=(r"S.H",)).rejected)
        self.assertFalse(pick([sub(9, "pol", "Not Forced Really")], rejects=(r"^Forced",)).rejected)

    def test_patterns_are_matched_against_the_title_only(self):
        """A pattern that happens to match the language must not shoot down every Polish track."""
        got = pick([sub(9, "pol", "Dialogi")], rejects=("pol", "subrip"))
        self.assertFalse(got.rejected)

    def test_an_empty_title_matches_nothing(self):
        """PowerShell '' -match 'SDH' is False, and so is re.search on an empty string."""
        self.assertFalse(pick([sub(9, "pol", "")]).rejected)
        self.assertFalse(pick([sub(9, "pol", None)]).rejected)

    def test_a_pattern_that_matches_the_empty_string_rejects_an_untitled_track(self):
        """PowerShell runs `-match` on the empty title as well, and `'' -match ''` is True. A
        `if title:` guard in front of re.search reads the same and answers the opposite."""
        for pattern in ("", ".*", "^$"):
            self.assertTrue(pick([sub(9, "pol", "")], rejects=(pattern,)).rejected, pattern)
            self.assertTrue(pick([sub(9, "pol", None)], rejects=(pattern,)).rejected, pattern)

    def test_an_unusable_pattern_is_named_rather_than_silently_dropped(self):
        with self.assertRaises(ValueError) as caught:
            pick([sub(9, "pol", "Polski")], rejects=("[unclosed",))
        self.assertIn("[unclosed", str(caught.exception))

    def test_no_patterns_at_all_rejects_nothing_but_forced(self):
        """An empty or absent reject list is not an error; the forced rule stands on its own."""
        self.assertFalse(pick([sub(9, "pol", "Polski SDH")], rejects=()).rejected)
        self.assertFalse(pick([sub(9, "pol", "Polski SDH")], rejects=None).rejected)
        self.assertTrue(pick([sub(9, "pol", "Polski", forced=1)], rejects=None).rejected)

    def test_the_chosen_codec_and_title_come_back_with_the_index(self):
        got = pick([sub(30, "pol", "Polski", codec="ass")])
        self.assertEqual((got.index, got.title, got.codec), (30, "Polski", "ass"))


class TestBitmapSubtitles(unittest.TestCase):
    """`-c:s srt` can only transcode text. The PowerShell never checked and died inside ffmpeg."""

    def test_text_codecs_pass(self):
        for codec in ("subrip", "ass", "ssa", "mov_text", "webvtt"):
            self.assertTrue(M.is_text_subtitle(codec), codec)

    def test_bitmap_codecs_are_refused(self):
        for codec in ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "HDMV_PGS_SUBTITLE"):
            self.assertFalse(M.is_text_subtitle(codec), codec)


class TestMuxArgs(unittest.TestCase):
    """Character for character against the argv windows/lektor.ps1 builds."""

    def expected(self, n_audio):
        args = ["-y", "-i", "/src.mkv", "-i", "/t.eac3", "-map", "0", "-map", "1:a:0", "-c", "copy",
                f"-metadata:s:a:{n_audio}", "language=pol",
                f"-metadata:s:a:{n_audio}", "title=Lektor AI (PL)"]
        for i in range(n_audio):
            args += [f"-disposition:a:{i}", "0"]
        return args + [f"-disposition:a:{n_audio}", "default", "/out.mkv"]

    def test_one_two_and_five_original_audio_streams(self):
        for n in (1, 2, 5):
            got = M.mux_args(Path("/src.mkv"), Path("/t.eac3"), n, Path("/out.mkv"))
            self.assertEqual(got, self.expected(n), f"n_audio={n}")

    def test_the_lektor_lands_after_every_original_stream(self):
        """Hardcoding 1 on a release with a commentary track made the COMMENTARY the default."""
        got = M.mux_args(Path("/src.mkv"), Path("/t.eac3"), 2, Path("/out.mkv"))
        self.assertIn("-metadata:s:a:2", got)
        self.assertNotIn("-metadata:s:a:1", got)
        self.assertEqual(got[got.index("-disposition:a:2") + 1], "default")
        self.assertEqual(got[got.index("-disposition:a:0") + 1], "0")
        self.assertEqual(got[got.index("-disposition:a:1") + 1], "0")

    def test_a_source_with_no_audio_at_all_puts_the_lektor_at_zero(self):
        got = M.mux_args(Path("/src.mkv"), Path("/t.eac3"), 0, Path("/out.mkv"))
        self.assertEqual(got, self.expected(0))
        self.assertEqual(got[-3:], ["-disposition:a:0", "default", "/out.mkv"])

    def test_everything_original_is_copied_not_re_encoded(self):
        got = M.mux_args(Path("/src.mkv"), Path("/t.eac3"), 1, Path("/out.mkv"))
        self.assertEqual(got[got.index("-c") + 1], "copy")
        self.assertEqual(got[5:9], ["-map", "0", "-map", "1:a:0"])

    def test_count_audio_streams_counts_what_ffprobe_returned(self, ):
        probed = [{"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
                  {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 2}]
        original = M.probe_streams
        M.probe_streams = lambda ffprobe, path, select=None, strict=False: M.streams_from_ffprobe(
            {"streams": probed if select == "a" else []})
        try:
            self.assertEqual(M.count_audio_streams("ffprobe", "/src.mkv"), 2)
        finally:
            M.probe_streams = original


class TestProbeFailureIsNotZero(unittest.TestCase):
    """A swallowed ffprobe failure is not a smaller mux, it is a wrong one that looks right."""

    def failing_probe(self, returncode=1, stdout="", stderr="boom"):
        import subprocess as sp

        def fake_run(args, **kw):
            return sp.CompletedProcess(args, returncode, stdout, stderr)
        return fake_run

    def test_a_failed_probe_is_an_empty_list_when_it_is_only_a_stream_list(self):
        original = M.subprocess.run
        M.subprocess.run = self.failing_probe()
        try:
            self.assertEqual(M.probe_streams("ffprobe", "/a.mkv", "s"), [])
        finally:
            M.subprocess.run = original

    def test_a_failed_probe_raises_when_the_answer_is_used_as_a_count(self):
        """0 is a legal audio count, so counting a failure as 0 cannot be told from the truth."""
        original = M.subprocess.run
        M.subprocess.run = self.failing_probe()
        try:
            with self.assertRaises(M.ProbeError):
                M.count_audio_streams("ffprobe", "/src.mkv")
        finally:
            M.subprocess.run = original

    def test_garbled_json_raises_in_strict_mode_too(self):
        original = M.subprocess.run
        M.subprocess.run = self.failing_probe(returncode=0, stdout="{not json")
        try:
            self.assertEqual(M.probe_streams("ffprobe", "/a.mkv", "a"), [])
            with self.assertRaises(M.ProbeError):
                M.count_audio_streams("ffprobe", "/src.mkv")
        finally:
            M.subprocess.run = original

    def test_mux_refuses_to_guess_rather_than_relabelling_the_original_track(self):
        """With n_audio silently 0 the argv titles the FIRST ORIGINAL audio stream
        'Lektor AI (PL)', sets language=pol on it and flags it default, while the real lektor gets
        nothing. Verified against a two-audio mkv before this was fixed."""
        original = M.subprocess.run
        M.subprocess.run = self.failing_probe()
        try:
            with TemporaryDirectory() as td:
                with self.assertRaises(M.ProbeError):
                    M.mux("ffmpeg", "ffprobe", Path("/src.mkv"), Path("/t.ac3"),
                          Path(td) / "out.mkv")
        finally:
            M.subprocess.run = original


class TestExtractableSubtitle(unittest.TestCase):
    """A bitmap first candidate must not sink an episode that also carries a text one."""

    def pickx(self, raw, language="pol", rejects=("SDH", "forced", "Forced")):
        return M.pick_extractable_subtitle(streams(*raw), language, list(rejects))

    def test_a_pgs_pol_track_ahead_of_a_subrip_pol_track_does_not_skip_the_episode(self):
        raw = [sub(2, "eng", "English"),
               sub(3, "pol", "Polski", codec="hdmv_pgs_subtitle"),
               sub(5, "pol", "Polski")]
        self.assertEqual(M.pick_subtitle(streams(*raw), "pol", []).index, 3)  # the faithful port
        got = self.pickx(raw)
        self.assertEqual((got.index, got.codec), (5, "subrip"))
        self.assertFalse(got.rejected)

    def test_the_rejection_rules_still_apply_inside_the_text_only_second_pass(self):
        raw = [sub(3, "pol", "Polski", codec="dvd_subtitle"),
               sub(4, "pol", "Polski SDH"),
               sub(5, "pol", "Polski")]
        self.assertEqual(self.pickx(raw).index, 5)

    def test_a_text_only_rejected_fallback_still_beats_the_bitmap_pick(self):
        raw = [sub(3, "pol", "Polski", codec="hdmv_pgs_subtitle"), sub(4, "pol", "Polski SDH")]
        got = self.pickx(raw)
        self.assertEqual(got.index, 4)
        self.assertTrue(got.rejected)

    def test_a_bitmap_only_release_still_comes_back_so_the_log_can_name_the_codec(self):
        got = self.pickx([sub(3, "pol", "Polski", codec="hdmv_pgs_subtitle")])
        self.assertEqual((got.index, got.codec), (3, "hdmv_pgs_subtitle"))
        self.assertFalse(M.is_text_subtitle(got.codec))

    def test_no_polish_at_all_is_still_none(self):
        self.assertIsNone(self.pickx([sub(0, "eng", "English")]))

    def test_a_usable_pick_is_returned_untouched(self):
        raw = [sub(30, "pol", "Polski"), sub(31, "pol", "Inne")]
        self.assertEqual(self.pickx(raw).index, 30)


class TestExtractArgs(unittest.TestCase):
    def test_subtitle_argv_matches_the_powershell(self):
        self.assertEqual(M.subtitle_args(Path("/a.mkv"), 30, Path("/w/a.srt")),
                         ["-y", "-i", "/a.mkv", "-map", "0:30", "-c:s", "srt", "/w/a.srt"])

    def test_downmix_argv_matches_the_powershell(self):
        self.assertEqual(
            M.downmix_args(Path("/a.mkv"), Path("/w/a.orig.flac"), "a:0", 2, 48000),
            ["-y", "-i", "/a.mkv", "-map", "0:a:0", "-ac", "2", "-ar", "48000",
             "-c:a", "flac", "-compression_level", "5", "/w/a.orig.flac"])

    def test_an_existing_output_is_not_re_extracted_without_force(self):
        with TemporaryDirectory() as td:
            out = Path(td) / "a.srt"
            out.write_text("1\n", encoding="utf-8")
            # A truthy ffmpeg path that does not exist: if the skip fails, this raises.
            self.assertFalse(M.extract_subtitles("/nonexistent-ffmpeg", Path("/a.mkv"), 30, out))
            self.assertFalse(M.extract_downmix("/nonexistent-ffmpeg", Path("/a.mkv"), out))


class TestSourceSidecar(unittest.TestCase):
    def test_the_shape_is_the_powershells_plus_a_usable_path(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "ep.source.json"
            choice = M.SubtitleChoice(index=30, title="Polski", codec="subrip")
            written = M.write_source_json(p, "ep", Path("/mnt/e/Filmy/ep.mkv"), choice, "pol")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data, written)
            self.assertEqual(set(data), {"base", "source", "source_windows", "subtitle_stream",
                                         "subtitle_title", "language", "extracted_at"})
            self.assertEqual(data["subtitle_stream"], 30)
            self.assertEqual(data["subtitle_title"], "Polski")
            self.assertEqual(data["language"], "pol")
            self.assertEqual(data["source"], "/mnt/e/Filmy/ep.mkv")
            # Translated back, so `windows/lektor.ps1 -Stage mux` can still finish this work dir.
            self.assertEqual(data["source_windows"], "E:\\Filmy\\ep.mkv")

    def test_a_sidecar_written_by_windows_powershell_is_readable_despite_its_bom(self):
        """Set-Content -Encoding UTF8 on PowerShell 5.1 writes a BOM and plain utf-8 chokes on it."""
        with TemporaryDirectory() as td:
            p = Path(td) / "ep.source.json"
            p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"base": "ep"}).encode("utf-8"))
            self.assertEqual(M.read_source_json(p)["base"], "ep")

    def test_the_error_log_line_is_tab_separated_and_appended(self):
        with TemporaryDirectory() as td:
            work = Path(td)
            M.log_extract_error(work, "ep1", "no 'pol' subtitle stream")
            M.log_extract_error(work, "ep2", "bitmap subtitle stream (hdmv_pgs_subtitle)")
            lines = (work / "error_log.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            stamp, base, message = lines[0].split("\t")
            self.assertEqual((base, message), ("ep1", "no 'pol' subtitle stream"))
            self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


class TestFindSources(unittest.TestCase):
    def test_recursive_sorted_by_name_and_the_work_dir_excluded(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "_lektor").mkdir()
            (root / "S01E02").mkdir()
            for rel in ("show.S01E03.mkv", "show.S01E01.mkv", "S01E02/show.S01E02.mkv",
                        "_lektor/show.S01E01.orig.mkv", "show.S01E04.mp4", "notes.txt"):
                (root / rel).write_bytes(b"")
            got = M.find_sources(root, "_lektor")
            self.assertEqual([p.name for p in got],
                             ["show.S01E01.mkv", "show.S01E02.mkv", "show.S01E03.mkv",
                              "show.S01E04.mp4"])

    def test_only_filters_by_substring_of_the_file_name(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("show.S01E01.mkv", "show.S01E02.mkv"):
                (root / rel).write_bytes(b"")
            self.assertEqual([p.name for p in M.find_sources(root, "_lektor", "E02")],
                             ["show.S01E02.mkv"])


class TestTrackDiscovery(unittest.TestCase):
    def test_the_base_is_the_name_minus_the_lektor_suffix(self):
        with TemporaryDirectory() as td:
            work = Path(td)
            for rel in ("show.S01E01.lektor.eac3", "show.S01E02.lektor.eac3",
                        "show.S01E01.lektor.ac3", "show.S01E01.orig.flac"):
                (work / rel).write_bytes(b"")
            self.assertEqual([b for b, _ in M.find_tracks(work, "eac3")],
                             ["show.S01E01", "show.S01E02"])
            self.assertEqual([b for b, _ in M.find_tracks(work, "ac3")], ["show.S01E01"])

    def test_a_suffixed_render_is_found_only_when_asked_for(self):
        with TemporaryDirectory() as td:
            work = Path(td)
            (work / "ep.lektor.eac3").write_bytes(b"")
            (work / "ep.lektorB.eac3").write_bytes(b"")
            self.assertEqual([b for b, _ in M.find_tracks(work, "eac3")], ["ep"])
            self.assertEqual([b for b, _ in M.find_tracks(work, "eac3", suffix="B")], ["ep"])


class TestNativePath(unittest.TestCase):
    def test_a_posix_path_is_left_alone(self):
        self.assertEqual(M.native_path("/mnt/e/Filmy/ep.mkv"), Path("/mnt/e/Filmy/ep.mkv"))

    def test_a_windows_path_translates_only_where_the_mount_exists(self):
        got = M.native_path("E:\\Filmy\\NCIS.S23E01\\ep.mkv")
        if Path("/mnt/e").is_dir():
            self.assertEqual(got, Path("/mnt/e/Filmy/NCIS.S23E01/ep.mkv"))
        else:
            self.assertEqual(got, Path("E:\\Filmy\\NCIS.S23E01\\ep.mkv"))


class TestWindowsPath(unittest.TestCase):
    def test_a_wsl_mount_becomes_a_drive_letter(self):
        self.assertEqual(M.windows_path("/mnt/e/Filmy/NCIS.S23E01/ep.mkv"),
                         "E:\\Filmy\\NCIS.S23E01\\ep.mkv")

    def test_a_path_with_no_windows_meaning_is_left_alone(self):
        """On a Linux box the field is a note about where the file came from, not an address."""
        self.assertEqual(M.windows_path("/home/user/filmy/ep.mkv"), "/home/user/filmy/ep.mkv")
        self.assertEqual(M.windows_path("/mnt/storage/ep.mkv"), "/mnt/storage/ep.mkv")

    def test_it_round_trips_with_native_path_where_the_mount_exists(self):
        if not Path("/mnt/e").is_dir():
            self.skipTest("no /mnt/e on this box")
        self.assertEqual(M.native_path(M.windows_path("/mnt/e/Filmy/ep.mkv")),
                         Path("/mnt/e/Filmy/ep.mkv"))


class TestBinaryResolution(unittest.TestCase):
    class Cfg:
        """A stub that borrows the REAL resolver rather than restating it.

        The lookup lives on Config, because three modules had re-derived it and that is where a
        rename goes wrong. Binding the real method here keeps this a test of the shipped logic
        instead of a test of a copy that can drift away from it.
        """

        from lektorlib.config import Config as _C
        binary = _C.binary

        def __init__(self, paths):
            self.raw = {"paths": paths}

    def test_the_canonical_key_wins_then_the_wsl_one(self):
        self.assertEqual(M.ffmpeg_bin(self.Cfg({"ffmpeg": "/opt/ffmpeg", "ffmpeg_wsl": "ffmpeg"})),
                         "/opt/ffmpeg")
        self.assertEqual(M.ffprobe_bin(self.Cfg({"ffprobe_wsl": "/usr/bin/ffprobe"})),
                         "/usr/bin/ffprobe")

    def test_a_config_with_only_the_windows_keys_still_runs(self):
        """The Windows binaries are never reached for: even where /mnt/c/Programy/ffmpeg/bin
        exists, that .exe cannot open the POSIX paths every stage here hands it."""
        cfg = self.Cfg({"ffmpeg_windows": "C:\\Programy\\ffmpeg\\bin\\ffmpeg.exe",
                        "ffprobe_windows": "C:\\Programy\\ffmpeg\\bin\\ffprobe.exe"})
        self.assertEqual(M.ffmpeg_bin(cfg), "ffmpeg")
        self.assertEqual(M.ffprobe_bin(cfg), "ffprobe")
        self.assertEqual(M.ffprobe_bin(self.Cfg({})), "ffprobe")


if __name__ == "__main__":
    unittest.main()


class TestExtractSkipsTheDeadDownmix(unittest.TestCase):
    """A surround render never reads <base>.orig.flac, so extract must stop writing it.

    `pick_source` rejects any source under six channels, so in 5.1 the two-channel downmix loses
    to <base>.orig.mka every time - and it is the 300-400 MB, minutes-long half of the step, on a
    drive that sits at 100%. Stereo is the one layout that mixes over it, and there it still gets
    written.
    """

    class Cfg:
        def __init__(self, tmp, layout):
            self.raw = {
                "paths": {"work_subdir": "_lektor"},
                "extract": {"subtitle_language": "pol"},
                "mix": {"channel_layout": layout},
            }
            self.input_dir = Path(tmp)
            self.work_dir = Path(tmp) / "_lektor"
            self.work_dir.mkdir(parents=True, exist_ok=True)

        def __getitem__(self, key):
            return self.raw[key]

        @staticmethod
        def binary(name):
            return name

    def run_extract(self, layout):
        with TemporaryDirectory() as td:
            video = Path(td) / "ep.mkv"
            video.write_bytes(b"not really an mkv")
            cfg = self.Cfg(td, layout)
            calls = []
            choice = M.SubtitleChoice(index=30, title="Polski", codec="subrip")
            with mock.patch.object(M, "probe_streams", return_value=[]), \
                 mock.patch.object(M, "pick_subtitle", return_value=choice), \
                 mock.patch.object(M, "extract_subtitles",
                                   side_effect=lambda *a, **k: calls.append("srt") or True), \
                 mock.patch.object(M, "extract_downmix",
                                   side_effect=lambda *a, **k: calls.append("flac") or True), \
                 mock.patch.object(M, "write_source_json", return_value={}):
                M.stage_extract(cfg)
            return calls

    def test_surround_writes_the_subtitle_and_not_the_downmix(self):
        self.assertEqual(self.run_extract("5.1(side)"), ["srt"])

    def test_stereo_still_writes_both(self):
        self.assertEqual(self.run_extract("stereo"), ["srt", "flac"])


class TestPackArgv(unittest.TestCase):
    """The deliverable is a .mka beside the film, and the tags are the reason it is a container."""

    def test_pack_argv_is_a_stream_copy_with_language_and_default(self):
        self.assertEqual(
            M.pack_args(Path("/w/ep.lektor.eac3"), Path("/f/ep.mka"), "pol", "Lektor AI (PL)"),
            ["-y", "-i", "/w/ep.lektor.eac3", "-c", "copy",
             "-metadata:s:a:0", "language=pol",
             "-metadata:s:a:0", "title=Lektor AI (PL)",
             "-disposition:a:0", "default", "/f/ep.mka"])

    def test_it_lands_next_to_the_video_even_one_directory_down(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "S01E01").mkdir()
            video = root / "S01E01" / "ep.mkv"
            video.write_bytes(b"x")

            class Cfg:
                input_dir = root
                work_dir = root / "_lektor"

                @staticmethod
                def get(key, default=None):
                    return default

            with mock.patch.object(M, "resolve_source", return_value=video):
                self.assertEqual(M.pack_path(Cfg, "ep"), root / "S01E01" / "ep.mka")
