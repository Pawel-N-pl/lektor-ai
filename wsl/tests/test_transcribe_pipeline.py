"""`run` with a transcribe step in it: what the plan promises, what the step reports, what it says
it cost - for a film with no text subtitle at all.

Built like test_translate_pipeline: a REAL Config from the shipped template and tuning file, and
the stages that would touch ffmpeg, a GPU or the network replaced where a test needs them to have
run. Nothing here transcribes anything.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import pipeline as P  # noqa: E402
from lektorlib import transcribe as TR  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n"


def make_cfg(root: Path) -> Config:
    films = root / "films"
    films.mkdir()
    text = (REPO / "lektor.toml.example").read_text(encoding="utf-8")
    text = text.replace("input_dir = ''", f"input_dir = '{films.as_posix()}'", 1)
    (root / "lektor.toml").write_text(text, encoding="utf-8")
    shutil.copy(REPO / "lektor.tuning.toml", root / "lektor.tuning.toml")
    return Config(root / "lektor.toml")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name))
        self.work = self.cfg.work_dir
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}).start()
        # The machine these run on has a docker binary or not; the plan must not depend on it.
        mock.patch.object(TR, "runtime_problem", return_value="").start()
        self.addCleanup(mock.patch.stopall)

    def heard(self, transcribed=False, translated=False, language="eng"):
        """A work dir extract left for a film with no text subtitle at all."""
        (self.work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "source": str(self.cfg.input_dir / "ep.mkv"), "language": "pol",
            "subtitle_origin": "asr", "source_language": "", "audio_language": language}),
            encoding="utf-8")
        if transcribed:
            (self.work / "subs").mkdir(exist_ok=True)
            srt = self.work / "subs" / f"ep.asr.{language}.srt"
            srt.write_text(SRT, encoding="utf-8")
            (self.work / "ep.asr.json").write_text(json.dumps({
                "srt": f"subs/{srt.name}", "language": language, "cues": 1,
                "language_requested": TR.whisper_code(language), "language_source": "tag",
                "segments": [{"text": "Hello.", "dropped": None},
                             {"text": "Thanks for watching!", "dropped": "blacklisted"}]}),
                encoding="utf-8")
        if translated:
            (self.work / "ep.srt").write_text(SRT, encoding="utf-8")

    def plan(self, steps=None, redo=()):
        steps = steps or list(P.STEPS[:P.STEPS.index("pack") + 1])
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(P, "_predicts_duration", return_value=False):
            P.print_plan(self.cfg, "ep", steps, set(redo),
                         {s: "outside --from/--to" for s in P.STEPS if s not in steps})
        return buf.getvalue()

    def quiet(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()


class TestSteps(unittest.TestCase):
    def test_transcribe_sits_between_extract_and_translate(self):
        self.assertEqual(P.STEPS[:3], ("extract", "transcribe", "translate"))
        self.assertEqual(P.select_steps("transcribe", "transcribe", ""), ["transcribe"])


class TestSatisfied(Base):
    def test_a_film_with_any_text_subtitle_has_nothing_to_transcribe(self):
        (self.work / "ep.source.json").write_text(json.dumps({"base": "ep"}), encoding="utf-8")
        self.assertTrue(P.satisfied(self.cfg, "ep", "transcribe"))
        (self.work / "ep.source.json").write_text(json.dumps(
            {"base": "ep", "subtitle_origin": "subs"}), encoding="utf-8")
        self.assertTrue(P.satisfied(self.cfg, "ep", "transcribe"))

    def test_before_extract_it_is_not_known_to_be_done(self):
        self.assertFalse(P.satisfied(self.cfg, "ep", "transcribe"))

    def test_a_heard_film_is_done_once_its_transcript_is_on_disk(self):
        self.heard()
        self.assertFalse(P.satisfied(self.cfg, "ep", "transcribe"))
        self.assertEqual(P.artefact(self.cfg, "ep", "extract"), self.work / "ep.source.json")
        self.assertEqual(P.artefact(self.cfg, "ep", "transcribe"), self.work / "ep.asr.json")
        self.heard(transcribed=True)
        self.assertTrue(P.satisfied(self.cfg, "ep", "transcribe"))
        self.assertFalse(P.satisfied(self.cfg, "ep", "translate"))

    def test_a_heard_episode_is_a_candidate_before_it_has_any_srt(self):
        self.heard()
        self.assertEqual(P.candidate_bases(self.cfg), ["ep"])


class TestPlan(Base):
    def test_before_extract_it_runs_only_if_there_is_no_text_subtitle_at_all(self):
        out = self.plan()
        self.assertIn("transcribe run  -> ep.asr.json, only if extract finds no text subtitle "
                      "at all", out)
        self.assertIn("audio    transcribed by faster-whisper large-v3 (docker: asr), local and "
                      "free – only if the film has no text subtitle at all", out)

    def test_source_subs_never_transcribes(self):
        self.cfg.raw["translate"]["source"] = "subs"
        out = self.plan()
        self.assertIn("transcribe skip translate.source is 'subs'", out)
        self.assertNotIn("faster-whisper", out)

    def test_source_asr_runs_whenever_there_is_no_polish_track(self):
        self.cfg.raw["translate"]["source"] = "asr"
        self.assertIn("transcribe run  -> ep.asr.json, only if extract finds no Polish text "
                      "track", self.plan())

    def test_a_heard_film_runs_it_then_skips_it(self):
        self.heard()
        out = self.plan()
        self.assertIn("transcribe run  -> ep.asr.json", out)
        self.assertIn("audio    transcribed by faster-whisper large-v3 (docker: asr), local and "
                      "free\n", out)
        self.assertIn("translate  run  -> ep.srt", out)
        self.heard(transcribed=True)
        self.assertIn("transcribe skip ep.asr.json is already there", self.plan())
        self.assertIn("transcribe REDO ep.asr.json will be rebuilt",
                      self.plan(redo={"transcribe"}))

    def test_a_transcript_for_another_language_is_planned_as_run(self):
        self.heard(transcribed=True)
        self.cfg.raw["translate"]["source_language"] = "ger"
        self.assertIn("transcribe run  it was transcribed as 'en' and the language asked for "
                      "is now 'de'", self.plan())

    def test_a_film_with_subtitles_or_a_polish_track_says_why_it_skips(self):
        (self.work / "ep.source.json").write_text(json.dumps(
            {"base": "ep", "subtitle_origin": "subs", "source_srt": "subs/x.srt"}),
            encoding="utf-8")
        self.assertIn("transcribe skip the film has a text subtitle to translate from",
                      self.plan())
        (self.work / "ep.source.json").write_text(json.dumps({"base": "ep"}), encoding="utf-8")
        self.assertIn("transcribe skip the Polish text comes from the film's own track",
                      self.plan())

    def test_an_english_transcript_to_come_is_not_warned_about_as_a_missing_reference(self):
        self.heard()
        self.assertNotIn("no English reference subtitle", self.plan())
        self.heard(language="ger")
        with mock.patch.object(P, "reference_stream", return_value=None):
            self.assertIn("no English reference subtitle", self.plan())

    def test_a_missing_runtime_is_said_before_minute_two(self):
        self.heard()
        with mock.patch.object(TR, "runtime_problem", return_value="docker is not on PATH"):
            out = self.plan()
        self.assertIn("no runtime for transcription: docker is not on PATH", out)
        self.assertIn("The chain will stop at transcribe.", out)

    def test_a_missing_runtime_before_extract_is_conditional(self):
        with mock.patch.object(TR, "runtime_problem", return_value="docker is not on PATH"):
            out = self.plan()
        self.assertIn("If extract finds no text subtitle to translate from, the chain will stop "
                      "at transcribe.", out)

    def test_a_bad_runner_is_a_usage_error_before_anything_runs(self):
        self.cfg.raw["transcribe"]["runner"] = "gpu"
        args = SimpleNamespace(from_step="extract", to_step="pack", skip="", redo="",
                               force=False, redraw=False, dry_run=True, require_speech=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(P.stage_run(self.cfg, "ep", args), 2)
        self.assertIn("transcribe.runner", buf.getvalue())


class TestRunStep(Base):
    def step(self, force=False):
        with mock.patch.object(P, "say"), mock.patch.object(TR, "say"):
            return P._run_step(self.cfg, "ep", "transcribe", force, False, False, {})

    def test_a_film_with_subtitles_skips_without_calling_the_stage(self):
        (self.work / "ep.source.json").write_text(json.dumps(
            {"base": "ep", "subtitle_origin": "subs"}), encoding="utf-8")
        with mock.patch.object(TR, "stage_transcribe") as st:
            status, note = self.step()
        st.assert_not_called()
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("nothing to transcribe", note)

    def test_before_extract_it_is_a_skip_and_translate_is_the_step_that_stops(self):
        status, note = self.step()
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("extract has not sent this film here", note)

    def test_a_transcription_reports_the_file_the_language_and_what_was_dropped(self):
        self.heard()

        def fake(cfg, only, force=False):
            self.heard(transcribed=True)
            return {"ep": "written"}

        with mock.patch.object(TR, "stage_transcribe", side_effect=fake):
            status, note = self.step()
        self.assertEqual(status, P.OK)
        self.assertEqual(note, "subs/ep.asr.eng.srt, 1 cues in 'eng' (tag), 1 segment(s) "
                               "dropped by the filters")

    def test_a_kept_transcript_is_a_skip_and_redo_passes_force(self):
        self.heard(transcribed=True)
        seen = {}

        def fake(cfg, only, force=False):
            seen["force"] = force
            return {"ep": "written" if force else "kept"}

        with mock.patch.object(TR, "stage_transcribe", side_effect=fake):
            status, note = self.step()
            self.assertEqual(status, P.SKIPPED)
            self.assertIn("ep.asr.json was already there", note)
            status, _ = self.step(force=True)
        self.assertEqual((status, seen["force"]), (P.OK, True))

    def test_a_failed_or_impossible_transcription_stops_the_chain(self):
        self.heard()
        with mock.patch.object(TR, "stage_transcribe",
                               side_effect=SystemExit("no runtime for transcription: x")):
            with self.assertRaises(P.HardFail) as cm:
                self.step()
        self.assertIn("no runtime for transcription", str(cm.exception))
        with mock.patch.object(TR, "stage_transcribe", return_value={"ep": "written"}):
            with self.assertRaises(P.HardFail) as cm:
                self.step()
        self.assertIn("transcribe wrote no usable ep.asr.json", str(cm.exception))

    def test_extract_that_sent_the_film_to_transcribe_is_a_success(self):
        def fake(cfg, only, force=False):
            self.heard()

        with mock.patch.object(P.media, "stage_extract", side_effect=fake), \
             mock.patch.object(P, "say"):
            status, note = P._run_step(self.cfg, "ep", "extract", False, False, False, {})
        self.assertEqual(status, P.OK)
        self.assertIn("ep.source.json", note)


class TestFooterCostStatus(Base):
    def test_the_footer_names_the_transcript_and_says_it_was_free(self):
        self.heard(transcribed=True, translated=True)
        out = self.quiet(P.print_summary, self.cfg, "ep", [], ["transcribe", "translate"],
                         {"translate_usd": 0.0}, 1.0)
        self.assertIn("subs/ep.asr.eng.srt   (the transcript of the film's audio", out)
        self.assertIn("transcription $0.00 – faster-whisper runs locally", out)
        self.assertIn("translation  $0.0000 this run", out)

    def test_cost_says_transcription_is_free_and_which_are_still_waiting(self):
        from lektorlib import pricing
        self.heard()
        out = self.quiet(pricing.stage_cost, self.cfg, "creator")
        self.assertIn("transcription via faster-whisper large-v3: 1 episode(s) with no text "
                      "subtitle at all, $0.00 - it runs locally. 1 not transcribed yet", out)
        self.assertIn("translation via", out)
        self.heard(transcribed=True)
        out = self.quiet(pricing.stage_cost, self.cfg, "creator")
        self.assertNotIn("not transcribed yet", out)
        self.assertIn("1 cues / 6 chars", out)

    def test_status_follows_a_heard_film_from_asr_to_src_to_tr(self):
        from lektorlib import stages
        self.heard()
        self.assertRegex(self.quiet(stages.stage_status, self.cfg), r"\nep\s+asr ")
        self.heard(transcribed=True)
        self.assertRegex(self.quiet(stages.stage_status, self.cfg), r"\nep\s+src ")
        self.heard(transcribed=True, translated=True)
        self.assertRegex(self.quiet(stages.stage_status, self.cfg), r"\nep\s+tr ")


if __name__ == "__main__":
    unittest.main()
