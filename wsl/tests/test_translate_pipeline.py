"""`run` with a translate step in it: what the plan promises, what the step reports, what it costs.

These run against a REAL Config, built from the shipped template and tuning file in a temp
directory, because the plan and the footer read a dozen keys between them and a stand-in config
would only test itself. No stage that touches ffmpeg or the network is called: extract and
translate are replaced where a test needs them to have run.

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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import pipeline as P  # noqa: E402
from lektorlib import script as S  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello.\n"


def make_cfg(root: Path, extra: str = "") -> Config:
    films = root / "films"
    films.mkdir()
    text = (REPO / "lektor.toml.example").read_text(encoding="utf-8")
    text = text.replace("input_dir = ''", f"input_dir = '{films.as_posix()}'", 1)
    (root / "lektor.toml").write_text(text + extra, encoding="utf-8")
    shutil.copy(REPO / "lektor.tuning.toml", root / "lektor.tuning.toml")
    return Config(root / "lektor.toml")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name))
        self.work = self.cfg.work_dir
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}).start()
        self.addCleanup(mock.patch.stopall)

    def translated(self, with_srt=False):
        """A work dir extract left for a film with no Polish track."""
        (self.work / "subs").mkdir(exist_ok=True)
        (self.work / "subs" / "ep.src.eng.srt").write_text(SRT, encoding="utf-8")
        (self.work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "source": str(self.cfg.input_dir / "ep.mkv"), "language": "pol",
            "subtitle_origin": "subs", "source_language": "eng",
            "source_srt": "subs/ep.src.eng.srt"}), encoding="utf-8")
        if with_srt:
            (self.work / "ep.srt").write_text(SRT, encoding="utf-8")

    def polish(self):
        (self.work / "ep.srt").write_text(SRT, encoding="utf-8")
        (self.work / "ep.source.json").write_text(json.dumps({"base": "ep", "language": "pol"}),
                                                  encoding="utf-8")

    def plan(self, steps=None, redo=()):
        steps = steps or list(P.STEPS[:P.STEPS.index("pack") + 1])
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(P, "_predicts_duration", return_value=False):
            P.print_plan(self.cfg, "ep", steps, set(redo),
                         {s: "outside --from/--to" for s in P.STEPS if s not in steps})
        return buf.getvalue()


class TestSteps(unittest.TestCase):
    def test_translate_sits_between_extract_and_speech(self):
        self.assertEqual(P.STEPS[:4], ("extract", "transcribe", "translate", "speech"))
        self.assertEqual(P.DEFAULT_LAST_STEP, "pack")

    def test_it_can_be_named_in_every_range_flag(self):
        self.assertEqual(P.select_steps("translate", "translate", ""), ["translate"])
        self.assertNotIn("translate", P.select_steps("extract", "pack", "translate"))


class TestArtefacts(Base):
    def test_a_polish_film_keeps_its_old_artefact_and_has_nothing_to_translate(self):
        self.polish()
        self.assertEqual(P.artefact(self.cfg, "ep", "extract"), self.work / "ep.srt")
        self.assertTrue(P.satisfied(self.cfg, "ep", "translate"))

    def test_before_extract_the_answer_is_the_polish_file(self):
        self.assertEqual(P.artefact(self.cfg, "ep", "extract"), self.work / "ep.srt")
        self.assertFalse(P.satisfied(self.cfg, "ep", "translate"))

    def test_a_translated_film_is_extracted_once_its_source_is_there(self):
        self.translated()
        self.assertEqual(P.artefact(self.cfg, "ep", "extract"),
                         self.work / "subs" / "ep.src.eng.srt")
        # Surround as shipped: no downmix is wanted, so the source alone is the whole step.
        self.assertTrue(P.satisfied(self.cfg, "ep", "extract"))
        self.assertFalse(P.satisfied(self.cfg, "ep", "translate"))
        (self.work / "ep.srt").write_text(SRT, encoding="utf-8")
        self.assertTrue(P.satisfied(self.cfg, "ep", "translate"))

    def test_a_translated_episode_is_a_candidate_before_it_has_any_srt(self):
        self.translated()
        self.assertEqual(P.candidate_bases(self.cfg), ["ep"])
        self.assertEqual(P.resolve_episode(self.cfg, "ep"), "ep")


class TestPlan(Base):
    def test_a_translated_film_shows_the_model_and_the_step_that_will_run(self):
        self.translated()
        out = self.plan()
        self.assertIn("text     translated by openai/gpt-5.6-luna-pro (fallback "
                      "openai/gpt-5.6-terra), subtitles off", out)
        self.assertIn("translate  run  -> ep.srt", out)

    def test_a_polish_film_says_why_there_is_nothing_to_translate(self):
        self.polish()
        out = self.plan()
        self.assertIn("translate  skip the Polish text comes from the film's own track", out)
        self.assertNotIn("translated by", out)

    def test_before_extract_it_says_the_step_is_conditional(self):
        out = self.plan()
        self.assertIn("only if extract finds no Polish text track", out)

    def test_a_hand_edited_translation_is_announced_as_kept(self):
        self.translated(with_srt=True)
        T.provenance_path(self.cfg, "ep").write_text(json.dumps({"srt_sha256": "0" * 64}),
                                                    encoding="utf-8")
        self.assertIn("ep.srt was edited by hand – kept", self.plan())

    def test_no_key_is_said_before_minute_two(self):
        self.translated()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            self.assertIn("OPENROUTER_API_KEY is not set: translate cannot run", self.plan())

    def test_no_key_before_extract_is_said_as_conditional(self):
        # A film with a Polish track never calls translate, so nothing is known to stop yet.
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            out = self.plan()
        self.assertIn("OPENROUTER_API_KEY is not set: if extract finds no Polish text track", out)
        self.assertNotIn("the chain will stop at that step", out)

    def test_subtitles_meant_for_a_remux_the_chain_will_not_do_are_flagged(self):
        self.translated()
        self.cfg.raw["translate"]["subtitles"] = "mux"
        self.assertIn("go into a .mkv only with `--to mux`", self.plan())
        self.assertNotIn("only with `--to mux`", self.plan(steps=list(P.STEPS)))

    def test_mode_never(self):
        self.cfg.raw["translate"]["mode"] = "never"
        self.translated()
        self.assertIn("translate  skip translate.mode is 'never'", self.plan())

    def test_mode_always_on_a_polish_extract_says_both_steps_run(self):
        # extract runs whatever the plan says, takes the translation branch under 'always' and
        # moves <base>.srt aside; translate then bills. "skip" on either line hid that.
        self.polish()
        self.cfg.raw["translate"]["mode"] = "always"
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            out = self.plan()
        self.assertIn("extract    run  translate.mode is 'always'", out)
        self.assertIn("translate  run  -> ep.srt, unless extract finds no text track in another "
                      "language", out)
        self.assertIn("translated by openai/gpt-5.6-luna-pro", out)
        self.assertNotIn("comes from the film's own track", out)
        self.assertIn("OPENROUTER_API_KEY is not set: translate.mode is 'always'", out)
        # Without extract in range nothing turns the episode over, and the old skip is true.
        out = self.plan(steps=["translate", "speech"])
        self.assertIn("translate  skip the Polish text comes from the film's own track", out)

    def test_mode_always_before_extract_does_not_promise_polish_first(self):
        self.cfg.raw["translate"]["mode"] = "always"
        out = self.plan()
        self.assertIn("unless extract finds no text track in another language", out)
        self.assertNotIn("only if the film has no Polish text track", out)
        self.assertNotIn("only if extract finds no Polish text track", out)

    def test_a_translation_from_a_replaced_source_is_planned_as_run(self):
        self.translated(with_srt=True)
        T.provenance_path(self.cfg, "ep").write_text(json.dumps({
            "srt_sha256": T.file_sha256(self.work / "ep.srt"), "source_language": "ger",
            "source_srt": "subs/ep.src.ger.srt"}), encoding="utf-8")
        self.assertFalse(P.satisfied(self.cfg, "ep", "translate"))
        self.assertIn("translate  run  ep.srt was translated from subs/ep.src.ger.srt, and "
                      "extract now names subs/ep.src.eng.srt", self.plan())

    def test_a_translation_made_before_a_glossary_change_is_planned_as_run(self):
        self.translated(with_srt=True)
        T.provenance_path(self.cfg, "ep").write_text(json.dumps({
            "srt_sha256": T.file_sha256(self.work / "ep.srt"), "source_language": "eng",
            "source_srt": "subs/ep.src.eng.srt"}), encoding="utf-8")
        self.assertTrue(P.satisfied(self.cfg, "ep", "translate"))
        # A glossary entry this episode never meets leaves it finished...
        glossary = self.cfg.input_dir / "lektor.glossary.toml"
        glossary.write_text("[terms]\n'Goodbye' = 'Do widzenia'\n", encoding="utf-8")
        self.assertTrue(P.satisfied(self.cfg, "ep", "translate"))
        # ...and one it does say makes it a paid step again, said as such.
        glossary.write_text("[terms]\n'Hello' = 'Dzień dobry'\n", encoding="utf-8")
        self.assertFalse(P.satisfied(self.cfg, "ep", "translate"))
        self.assertIn("translate  run  ep.srt was translated from ep.src.eng.srt without the "
                      "series glossary", self.plan())


class TestRunStep(Base):
    def step(self, step, force=False):
        state = {}
        with mock.patch.object(T, "say"), mock.patch.object(P, "say"):
            status, note = P._run_step(self.cfg, "ep", step, force, False, False, state)
        return status, note, state

    def test_redraw_reaches_translate_only_when_it_is_redone(self):
        self.translated()
        for force, redraw in ((True, True), (False, False)):
            with self.subTest(redo=force), \
                    mock.patch.object(T, "stage_translate", return_value={}) as st, \
                    mock.patch.object(T, "say"), mock.patch.object(P, "say"):
                with self.assertRaises(P.HardFail):   # the double writes no ep.srt
                    P._run_step(self.cfg, "ep", "translate", force, True, False, {})
            self.assertEqual(st.call_args.args[2:], (force, redraw))

    def test_redraw_on_a_translate_only_range_is_accepted(self):
        from types import SimpleNamespace
        self.translated()
        args = SimpleNamespace(from_step="translate", to_step="translate", skip="", redo="",
                               force=False, redraw=True, dry_run=True, require_speech=False)
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(P, "resolve_episode", return_value="ep"):
            self.assertEqual(P.stage_run(self.cfg, "ep", args), 0)
        self.assertIn("translate  run  -> ep.srt", buf.getvalue())
        args.from_step = args.to_step = "speech"
        with redirect_stdout(buf), mock.patch.object(P, "resolve_episode", return_value="ep"):
            self.assertEqual(P.stage_run(self.cfg, "ep", args), 2)
        self.assertIn("--redraw only means anything for the translate and script", buf.getvalue())

    def test_a_polish_film_skips_without_calling_the_stage(self):
        self.polish()
        with mock.patch.object(T, "stage_translate") as st:
            status, note, _ = self.step("translate")
        st.assert_not_called()
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("own track", note)

    def test_a_translation_reports_its_language_and_this_runs_bill(self):
        self.translated()

        def fake(cfg, only, force=False, redraw=False):
            (self.work / "ep.srt").write_text(SRT, encoding="utf-8")
            S.append_spend(cfg, "ep", {"stage": "translate", "usd": 0.0123})
            return {"ep": "written"}

        S.append_spend(self.cfg, "ep", {"usd": 0.5})   # an older condensation run
        with mock.patch.object(T, "stage_translate", side_effect=fake):
            status, note, state = self.step("translate")
        self.assertEqual(status, P.OK)
        self.assertIn("from 'eng'", note)
        self.assertAlmostEqual(state["translate_usd"], 0.0123)

    def test_a_failed_translation_stops_the_chain_and_still_books_the_spend(self):
        self.translated()

        def fake(cfg, only, force=False, redraw=False):
            S.append_spend(cfg, "ep", {"stage": "translate", "usd": 0.004})
            raise SystemExit("translation did not finish for:\n  ep: 2 of 9 cues untranslated")

        with mock.patch.object(T, "stage_translate", side_effect=fake):
            with self.assertRaises(P.HardFail) as cm:
                self.step("translate")
        self.assertIn("untranslated", str(cm.exception))

    def test_a_kept_hand_edit_is_a_skip_that_says_so(self):
        self.translated(with_srt=True)
        with mock.patch.object(T, "stage_translate", return_value={"ep": "edited"}):
            status, note, _ = self.step("translate")
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("edited by hand", note)

    def test_redo_passes_force_to_the_stage(self):
        self.translated(with_srt=True)
        seen = {}

        def fake(cfg, only, force=False, redraw=False):
            seen["force"] = force
            return {"ep": "written"}

        with mock.patch.object(T, "stage_translate", side_effect=fake):
            self.step("translate", force=True)
        self.assertIs(seen["force"], True)

    def test_a_foreign_srt_is_a_skip_that_says_so(self):
        self.translated(with_srt=True)
        with mock.patch.object(T, "stage_translate", return_value={"ep": "foreign"}):
            status, note, _ = self.step("translate")
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("translate did not write it", note)

    def test_a_kept_translation_is_a_skip(self):
        self.translated(with_srt=True)
        with mock.patch.object(T, "stage_translate", return_value={"ep": "kept"}):
            status, note, _ = self.step("translate")
        self.assertEqual((status, note), (P.SKIPPED, "ep.srt was already there"))

    def test_a_stage_that_writes_nothing_is_a_hard_fail(self):
        self.translated()
        with mock.patch.object(T, "stage_translate", return_value={"ep": "written"}):
            with self.assertRaises(P.HardFail) as cm:
                self.step("translate")
        self.assertIn("translate wrote no ep.srt", str(cm.exception))

    def test_translate_before_extract_is_a_hard_fail_not_a_skip(self):
        # `--from translate` on a film extract never saw: no sidecar, no .srt.
        with mock.patch.object(T, "stage_translate") as st:
            with self.assertRaises(P.HardFail) as cm:
                self.step("translate")
        st.assert_not_called()
        self.assertIn("extract has not run", str(cm.exception))

    def test_a_hand_placed_srt_with_no_sidecar_is_still_taken(self):
        (self.work / "ep.srt").write_text(SRT, encoding="utf-8")
        status, note, _ = self.step("translate")
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("is taken as the Polish text", note)

    def test_a_skipped_pack_still_refreshes_the_sidecar(self):
        self.cfg.raw["translate"]["subtitles"] = "sidecar"
        self.translated(with_srt=True)
        T.provenance_path(self.cfg, "ep").write_text(json.dumps(
            {"srt_sha256": T.file_sha256(self.work / "ep.srt")}), encoding="utf-8")
        P.artefact(self.cfg, "ep", "pack").write_bytes(b"mka")
        with mock.patch.object(P.media, "say"):
            status, note, _ = self.step("pack")
        self.assertEqual(status, P.SKIPPED)
        self.assertIn("ep.pl.srt up to date", note)
        self.assertEqual(P.media.sidecar_srt_path(self.cfg, "ep").read_text(encoding="utf-8"), SRT)

    def test_extract_that_found_nothing_to_translate_either_says_so(self):
        with mock.patch.object(P.media, "stage_extract"):
            with self.assertRaises(P.HardFail) as cm:
                self.step("extract")
        self.assertIn("in another language to translate from", str(cm.exception))

    def test_extract_that_switched_a_film_to_translation_is_not_a_skip(self):
        self.polish()

        def fake(cfg, only, force=False):
            self.translated()

        with mock.patch.object(P.media, "stage_extract", side_effect=fake):
            status, note, _ = self.step("extract")
        self.assertEqual(status, P.OK)
        self.assertIn("ep.src.eng.srt", note)


class TestFooter(Base):
    def test_the_footer_names_the_source_and_splits_the_bill(self):
        self.translated(with_srt=True)
        S.append_spend(self.cfg, "ep", {"usd": 0.02})
        S.append_spend(self.cfg, "ep", {"stage": "translate", "usd": 0.01})
        buf = io.StringIO()
        with redirect_stdout(buf):
            P.print_summary(self.cfg, "ep", [], ["extract", "translate"],
                            {"translate_usd": 0.01}, 1.0)
        out = buf.getvalue()
        self.assertIn("ep.src.eng.srt", out)
        self.assertIn("$0.0300 for this episode so far", out)
        self.assertIn("translation  $0.0100 this run, $0.0100 of that total", out)

    def test_a_polish_film_footer_has_no_translation_line(self):
        self.polish()
        buf = io.StringIO()
        with redirect_stdout(buf):
            P.print_summary(self.cfg, "ep", [], ["extract"], {}, 1.0)
        self.assertNotIn("translation", buf.getvalue())


class TestCostAndStatus(Base):
    def run_quiet(self, fn, *a):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_cost_prices_translation_apart_and_admits_it_has_no_measured_figure(self):
        from lektorlib import pricing
        self.translated()
        out = self.run_quiet(pricing.stage_cost, self.cfg, "creator")
        self.assertIn("translation via openai/gpt-5.6-luna-pro: 1 episode(s) with no Polish "
                      "track, 1 cues / 6 chars", out)
        self.assertIn("no measured per-episode figure", out)
        self.assertIn("leaves out these 1", out)

    def test_cost_estimates_only_the_untranslated_ones(self):
        from lektorlib import pricing
        self.cfg.raw["translate"]["measured_usd_per_episode"] = 0.05
        self.translated(with_srt=True)
        (self.work / "subs" / "ep2.src.eng.srt").write_text(SRT, encoding="utf-8")
        (self.work / "ep2.source.json").write_text(json.dumps({
            "base": "ep2", "language": "pol", "subtitle_origin": "subs",
            "source_language": "eng", "source_srt": "subs/ep2.src.eng.srt"}), encoding="utf-8")
        out = self.run_quiet(pricing.stage_cost, self.cfg, "creator")
        self.assertIn("about $0.050 for the 1 not translated yet", out)

    def test_cost_keeps_the_two_bills_apart(self):
        from lektorlib import pricing
        self.translated(with_srt=True)
        S.append_spend(self.cfg, "ep", {"usd": 0.02})
        S.append_spend(self.cfg, "ep", {"stage": "translate", "usd": 0.01})
        out = self.run_quiet(pricing.stage_cost, self.cfg, "creator")
        self.assertIn("$0.0200 already billed", out)
        self.assertIn("$0.0100 already billed", out)
        self.assertNotIn("not translated yet", out)

    def test_cost_says_nothing_about_translation_on_a_polish_season(self):
        from lektorlib import pricing
        self.polish()
        self.assertNotIn("translation", self.run_quiet(pricing.stage_cost, self.cfg, "creator"))

    def test_status_lists_an_episode_still_waiting_for_its_translation(self):
        from lektorlib import stages
        self.translated()
        out = self.run_quiet(stages.stage_status, self.cfg)
        self.assertRegex(out, r"\nep\s+src ")
        (self.work / "ep.srt").write_text(SRT, encoding="utf-8")
        self.assertRegex(self.run_quiet(stages.stage_status, self.cfg), r"\nep\s+tr ")


if __name__ == "__main__":
    unittest.main()
