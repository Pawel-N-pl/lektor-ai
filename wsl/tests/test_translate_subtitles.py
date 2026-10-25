"""Polish subtitles as a by-product of a translation: beside the film, or in the remux, or not at all.

The rule that must hold: they are added only where this pipeline did the translating. A film whose
Polish text came from its own track already has that track, and a second "AI" copy of it would be
noise. And when subtitles are off, the mux argv is exactly what it always was.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lektor  # noqa: E402
from lektorlib import media as M  # noqa: E402
from lektorlib import pipeline as P  # noqa: E402
from lektorlib import translate as T  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:02,000\nCześć.\n"


class Cfg:
    """A work dir holding one episode. `ours` stamps `<base>.translate.json` the way the translate
    stage does, so the `<base>.srt` counts as its own; without it the file is somebody else's."""

    def __init__(self, root, subtitles="off", origin="subs", ours=True):
        self.input_dir = Path(root)
        self.work_dir = Path(root) / "_lektor"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.raw = {"translate": {"subtitles": subtitles, "subtitle_title": "Polski (AI)"},
                    "paths": {"work_subdir": "_lektor", "output_subdir": "_lektor/output"}}
        (self.input_dir / "ep.mkv").write_bytes(b"x")
        side = {"base": "ep", "source": str(self.input_dir / "ep.mkv")}
        if origin:
            side["subtitle_origin"] = origin
        (self.work_dir / "ep.source.json").write_text(json.dumps(side), encoding="utf-8")
        (self.work_dir / "ep.srt").write_text(SRT, encoding="utf-8")
        if origin in T.TRANSLATED and ours:
            (self.work_dir / "ep.translate.json").write_text(json.dumps(
                {"srt_sha256": T.file_sha256(self.work_dir / "ep.srt")}), encoding="utf-8")

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    @staticmethod
    def binary(name):
        return name


class TestMuxArgs(unittest.TestCase):
    def test_without_subtitles_the_argv_is_the_old_one(self):
        self.assertEqual(M.mux_args(Path("/s.mkv"), Path("/t.eac3"), 2, Path("/o.mkv")),
                         M.mux_args(Path("/s.mkv"), Path("/t.eac3"), 2, Path("/o.mkv"), None, 7))

    def test_subtitles_go_in_after_every_original_tagged_polish_and_not_default(self):
        got = M.mux_args(Path("/s.mkv"), Path("/t.eac3"), 2, Path("/o.mkv"),
                         Path("/w/ep.srt"), 43, "Polski (AI)")
        self.assertEqual(got[:7], ["-y", "-i", "/s.mkv", "-i", "/t.eac3", "-i", "/w/ep.srt"])
        self.assertEqual(got[7:13], ["-map", "0", "-map", "1:a:0", "-map", "2:0"])
        tail = got[got.index("-metadata:s:s:43"):]
        self.assertEqual(tail, ["-metadata:s:s:43", "language=pol",
                                "-metadata:s:s:43", "title=Polski (AI)",
                                "-disposition:s:43", "0", "/o.mkv"])
        # The lektor stays the default audio, exactly as without subtitles.
        self.assertIn("-disposition:a:2", got)
        self.assertEqual(got[got.index("-disposition:a:2") + 1], "default")

    def test_mux_indexes_the_new_subtitle_off_the_probed_count(self):
        # mux_args is told the index; this is the line that counts it. Got wrong, the film's own
        # first subtitle would be relabelled 'Polski (AI)' and every argv test above would pass.
        def probe(ffprobe, path, select=None, strict=False):
            return [object()] * (2 if select == "a" else 5)

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "out" / "ep.mkv"
            for subs in (Path("/w/ep.srt"), None):
                with self.subTest(subs=subs), \
                        mock.patch.object(M, "probe_streams", side_effect=probe) as ps, \
                        mock.patch.object(M, "run_ffmpeg") as ff:
                    M.mux("ffmpeg", "ffprobe", Path("/s.mkv"), Path("/t.eac3"), dest, subs=subs)
                argv = ff.call_args.args[1]
                asked = [c.args[2] if len(c.args) > 2 else c.kwargs.get("select")
                         for c in ps.call_args_list]
                self.assertIn("-disposition:a:2", argv)
                if subs is not None:
                    self.assertIn("-metadata:s:s:5", argv)
                    self.assertIn("-disposition:s:5", argv)
                else:
                    self.assertNotIn("s", asked)
                    self.assertFalse(any(a.startswith("-metadata:s:s") for a in argv))

    def test_the_subtitle_index_is_counted_strictly(self):
        with mock.patch.object(M, "probe_streams", return_value=[1, 2, 3]) as ps:
            self.assertEqual(M.count_subtitle_streams("ffprobe", "/s.mkv"), 3)
        self.assertEqual(ps.call_args.kwargs.get("strict"), True)


class TestWhenSubtitlesExist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(M, "say").start()
        self.addCleanup(mock.patch.stopall)

    def test_only_a_translated_episode_has_polish_subtitles_to_offer(self):
        cfg = Cfg(self.tmp.name, origin="subs")
        self.assertEqual(M.polish_subtitle_for(cfg, "ep"), cfg.work_dir / "ep.srt")

    def test_a_transcribed_and_translated_episode_offers_its_subtitles_too(self):
        cfg = Cfg(self.tmp.name, "sidecar", origin="asr")
        self.assertEqual(M.polish_subtitle_for(cfg, "ep"), cfg.work_dir / "ep.srt")
        side = M.sync_sidecar(cfg, "ep")
        self.assertEqual(side, cfg.input_dir / "ep.pl.srt")
        self.assertEqual(side.read_text(encoding="utf-8"), SRT)

    def test_a_polish_track_is_never_duplicated(self):
        cfg = Cfg(self.tmp.name, "sidecar", origin=None)
        self.assertIsNone(M.polish_subtitle_for(cfg, "ep"))
        self.assertIsNone(M.sync_sidecar(cfg, "ep"))
        self.assertFalse((cfg.input_dir / "ep.pl.srt").exists())

    def test_an_srt_the_translate_stage_did_not_write_is_not_offered_as_the_translation(self):
        # A Polish track left over from before translate.mode changed, or a file put there by
        # hand: the sidecar says "translated", but this file is not the translation.
        cfg = Cfg(self.tmp.name, "sidecar", ours=False)
        self.assertEqual(T.srt_state(cfg, "ep"), "foreign")
        self.assertIsNone(M.polish_subtitle_for(cfg, "ep"))
        self.assertIsNone(M.sync_sidecar(cfg, "ep"))

    def test_sidecar_lands_beside_the_film_and_follows_later_edits(self):
        cfg = Cfg(self.tmp.name, "sidecar")
        side = M.sync_sidecar(cfg, "ep")
        self.assertEqual(side, cfg.input_dir / "ep.pl.srt")
        self.assertEqual(side.read_text(encoding="utf-8"), SRT)
        (cfg.work_dir / "ep.srt").write_text(SRT.replace("Cześć", "Hej"), encoding="utf-8")
        M.sync_sidecar(cfg, "ep")
        self.assertIn("Hej", side.read_text(encoding="utf-8"))

    def test_off_and_mux_write_no_sidecar(self):
        for mode in ("off", "mux"):
            cfg = Cfg(self.tmp.name, mode)
            self.assertIsNone(M.sync_sidecar(cfg, "ep"))
        self.assertFalse((Path(self.tmp.name) / "ep.pl.srt").exists())

    def test_a_misspelled_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            M.subtitles_mode(Cfg(self.tmp.name, "both"))

    def test_mux_passes_the_translation_only_in_mux_mode(self):
        for mode, want in (("mux", True), ("sidecar", False), ("off", False)):
            cfg = Cfg(self.tmp.name, mode)
            track = cfg.work_dir / "ep.lektor.eac3"
            track.write_bytes(b"a")
            with mock.patch.object(M, "track_extension", return_value="eac3"), \
                 mock.patch.object(M, "mux", return_value=None) as mx:
                M.stage_mux(cfg, "ep", True)
            subs = mx.call_args.args[6]
            self.assertEqual(subs is not None, want, mode)
            if want:
                self.assertEqual(subs, cfg.work_dir / "ep.srt")
                self.assertEqual(mx.call_args.args[7], "Polski (AI)")


class TestPackSyncsTheSidecar(unittest.TestCase):
    """The two call sites that make a hand fix reach the copy beside the film: stage_pack, before
    its own "exists, skipping", and the chain's pack step when it skips."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(M, "say").start()
        mock.patch.object(M, "track_extension", return_value="eac3").start()
        self.addCleanup(mock.patch.stopall)
        self.cfg = Cfg(self.tmp.name, "sidecar")
        (self.cfg.work_dir / "ep.lektor.eac3").write_bytes(b"a")
        M.pack_path(self.cfg, "ep").write_bytes(b"mka")
        self.side = M.sidecar_srt_path(self.cfg, "ep")

    def test_a_skipped_pack_still_refreshes_the_sidecar(self):
        with mock.patch.object(M, "run_ffmpeg") as ff:
            M.stage_pack(self.cfg, "ep", False)
            (self.cfg.work_dir / "ep.srt").write_text(SRT.replace("Cześć", "Hej"),
                                                      encoding="utf-8")
            M.stage_pack(self.cfg, "ep", False)
        ff.assert_not_called()
        self.assertIn("Hej", self.side.read_text(encoding="utf-8"))

    def test_a_suffixed_pack_writes_no_sidecar(self):
        (self.cfg.work_dir / "ep.lektor.b.eac3").write_bytes(b"a")
        def fake_ffmpeg(ffmpeg, args, what):
            Path(args[-1]).write_bytes(b"mka")

        with mock.patch.object(M, "run_ffmpeg", side_effect=fake_ffmpeg):
            M.stage_pack(self.cfg, "ep", True, ".b")
        self.assertTrue(M.pack_path(self.cfg, "ep", ".b").exists())
        self.assertFalse(self.side.exists())


class TestCliFlag(unittest.TestCase):
    def test_subs_overrides_the_config_for_one_run(self):
        seen = {}

        def fake_pack(cfg, only, force):
            seen["subs"] = cfg.raw["translate"]["subtitles"]

        with mock.patch.object(lektor.media, "stage_pack", side_effect=fake_pack):
            lektor.main(["pack", "--subs", "sidecar"])
        self.assertEqual(seen["subs"], "sidecar")

    def test_translate_hands_force_and_redraw_to_the_stage(self):
        with mock.patch.object(lektor.translate, "stage_translate", return_value={}) as st:
            lektor.main(["translate", "--only", "ep", "--force", "--redraw"])
        cfg, only, force, redraw = st.call_args.args
        self.assertEqual((only, force, redraw), ("ep", True, True))
        with mock.patch.object(lektor.translate, "stage_translate", return_value={}) as st:
            lektor.main(["translate", "--only", "ep", "--redraw"])
        self.assertEqual(st.call_args.args[1:], ("ep", False, True))

    def test_a_stage_that_writes_no_subtitles_refuses_the_flag(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                lektor.main(["script", "--subs", "mux"])
        self.assertEqual(cm.exception.code, 2)

    def test_a_bad_value_in_the_config_is_a_usage_error_in_run(self):
        cfg = SimpleNamespace(raw={"translate": {"subtitles": "always"}})
        args = SimpleNamespace(from_step="extract", to_step="pack", skip="", redo="",
                               force=False, redraw=False, dry_run=True, require_speech=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(P.stage_run(cfg, "ep", args), 2)
        self.assertIn("translate.subtitles", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
