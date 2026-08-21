"""The condensed-text reuse cache: what may be reused, and what must never be.

The cache is not a speed optimisation. Every line it reuses is a line whose text does not change,
and text that does not change is a line the TTS backend does not have to be paid to read again --
which is 14 to 65 times the money the LLM call itself costs. So the tests that matter are the ones
about the KEY: a reused line must have been produced under exactly the conditions in force now.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import script as S  # noqa: E402
from lektorlib.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

SCRIPT_TBL = {
    "openrouter_model": "test/model",
    "reasoning_effort": "medium",
}


class FakeCfg:
    def __init__(self, work_dir, **script_over):
        self.raw = {"script": dict(SCRIPT_TBL, **script_over)}
        self._work = Path(work_dir)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def work_dir(self):
        return self._work


class Cue:
    def __init__(self, idx, text, budget, condensed=False):
        self.idx, self.text, self.budget_chars = idx, text, budget
        self.condensed = condensed


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


class TestCacheKey(unittest.TestCase):
    def key(self, text="Kibice wciąż wspierają klub.", budget=30, **over):
        return S.cache_key(text, budget, dict(SCRIPT_TBL, **over))

    def test_stable_for_the_same_input(self):
        self.assertEqual(self.key(), self.key())

    def test_text_is_part_of_it(self):
        self.assertNotEqual(self.key(), self.key(text="Kibice wspierają klub."))

    def test_budget_is_part_of_it(self):
        # The same sentence shortened to 30 characters is not the same answer as to 20.
        self.assertNotEqual(self.key(), self.key(budget=20))

    def test_model_and_effort_are_part_of_it(self):
        self.assertNotEqual(self.key(), self.key(openrouter_model="other/model"))
        self.assertNotEqual(self.key(), self.key(reasoning_effort="high"))

    def test_prompt_is_part_of_it(self):
        """Editing CONDENSE_SYSTEM must miss on every line.

        This is the failure the whole design exists to prevent: half an episode written by a
        prompt that no longer exists, shipped under a new one, with nothing in the output saying
        so. The fingerprint is taken from the prompt text itself rather than from a hand-bumped
        number, because the number is what gets forgotten.
        """
        before = self.key()
        with mock.patch.object(S, "CONDENSE_SYSTEM", S.CONDENSE_SYSTEM + "\nNowa zasada."):
            after = self.key()
        self.assertNotEqual(before, after)

    def test_temperature_and_rerolls_are_not_part_of_it(self):
        # They change the draw, not the contract. `--redraw` is how a different draw is asked for.
        self.assertEqual(self.key(), self.key(temperature=0.9, max_rerolls=9,
                                              fallback_model="other/model"))


# ---------------------------------------------------------------------------
# The book on disk
# ---------------------------------------------------------------------------


class TestBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = FakeCfg(self.tmp.name)

    def test_round_trip(self):
        S.write_condensed(self.cfg, "EP", {"k1": {"text": "Krótka kwestia."}})
        self.assertEqual(S.load_condensed(self.cfg, "EP")["k1"]["text"], "Krótka kwestia.")

    def test_missing_file_is_an_empty_book(self):
        self.assertEqual(S.load_condensed(self.cfg, "EP"), {})

    def test_entries_without_text_are_ignored(self):
        S.write_condensed(self.cfg, "EP", {"k1": {"text": ""}, "k2": {"id": 3}})
        self.assertEqual(S.load_condensed(self.cfg, "EP"), {})

    def test_a_corrupt_book_is_kept_aside_not_overwritten(self):
        p = S.condensed_path(self.cfg, "EP")
        p.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(S.load_condensed(self.cfg, "EP"), {})
        self.assertFalse(p.exists())
        kept = list(Path(self.tmp.name).glob("EP.condensed.corrupt-*.json"))
        self.assertEqual(len(kept), 1)

    def test_the_file_records_what_produced_it(self):
        S.write_condensed(self.cfg, "EP", {"k": {"text": "x"}})
        data = json.loads(S.condensed_path(self.cfg, "EP").read_text(encoding="utf-8"))
        self.assertEqual(data["prompt"], S.prompt_fingerprint())
        self.assertEqual(data["model"], "test/model")


# ---------------------------------------------------------------------------
# Splitting the work
# ---------------------------------------------------------------------------


class TestSplit(unittest.TestCase):
    def setUp(self):
        self.cues = [Cue(1, "Pierwsza długa kwestia do skrócenia.", 20),
                     Cue(2, "Druga długa kwestia do skrócenia.", 20)]
        self.s = dict(SCRIPT_TBL)

    def book_with(self, cue, text):
        return {S.cache_key(cue.text, cue.budget_chars, self.s): {"text": text}}

    def test_a_hit_is_not_sent(self):
        hits, misses, _ = S.split_by_cache(self.cues, [1, 2],
                                           self.book_with(self.cues[0], "Pierwsza."), self.s)
        self.assertEqual(hits, {1: "Pierwsza."})
        self.assertEqual(misses, [2])

    def test_an_edited_line_misses(self):
        """A cue whose source text changed by one character is a different question."""
        book = self.book_with(self.cues[0], "Pierwsza.")
        self.cues[0].text = self.cues[0].text.replace("długa", "bardzo długa")
        hits, misses, _ = S.split_by_cache(self.cues, [1, 2], book, self.s)
        self.assertEqual(hits, {})
        self.assertEqual(misses, [1, 2])

    def test_a_changed_budget_misses(self):
        book = self.book_with(self.cues[0], "Pierwsza.")
        self.cues[0].budget_chars = 25
        hits, _, _ = S.split_by_cache(self.cues, [1], book, self.s)
        self.assertEqual(hits, {})

    def test_an_empty_entry_is_a_miss_not_a_hit(self):
        hits, misses, _ = S.split_by_cache(self.cues, [1], self.book_with(self.cues[0], "   "),
                                           self.s)
        self.assertEqual((hits, misses), ({}, [1]))

    def test_a_cached_line_that_still_has_digits_is_poison(self):
        """The rule is never to trust the reply, and a reply off disk is still a reply.

        The line was sent to have its digits spelled out. An entry that still carries them would
        ship a line the lektor physically cannot read, so it is dropped and redrawn.
        """
        cue = Cue(1, "Mamy 3 mecze do końca sezonu i 2 wyjazdy.", 30)
        book = {S.cache_key(cue.text, cue.budget_chars, self.s): {"text": "Mamy 3 mecze."}}
        hits, misses, poisoned = S.split_by_cache([cue], [1], book, self.s)
        self.assertEqual((hits, misses, poisoned), ({}, [1], 1))

    def test_a_spelled_out_line_is_a_normal_hit(self):
        cue = Cue(1, "Mamy 3 mecze do końca sezonu i 2 wyjazdy.", 30)
        book = {S.cache_key(cue.text, cue.budget_chars, self.s):
                {"text": "Mamy trzy mecze i dwa wyjazdy."}}
        hits, misses, poisoned = S.split_by_cache([cue], [1], book, self.s)
        self.assertEqual((misses, poisoned), ([], 0))
        self.assertEqual(hits[1], "Mamy trzy mecze i dwa wyjazdy.")


class TestTakeCondensed(unittest.TestCase):
    """One acceptance rule, whether the line came from the model or from the book."""

    def cue(self, text="Kibice Richmond wciąż wspierają swój klub."):
        c = Cue(1, text, 20)
        c.reused = c.edited = False
        return c

    def test_shorter_is_taken(self):
        c = self.cue()
        self.assertTrue(S.take_condensed(c, "Kibice wspierają klub."))
        self.assertTrue(c.condensed)
        self.assertFalse(c.reused)

    def test_longer_is_refused(self):
        c = self.cue()
        self.assertFalse(S.take_condensed(c, c.text + " Naprawdę."))
        self.assertFalse(c.condensed)

    def test_a_line_that_grew_because_its_digits_were_spelled_out_is_taken(self):
        c = self.cue("Mamy 3 mecze.")
        self.assertTrue(S.take_condensed(c, "Mamy trzy mecze do rozegrania."))

    def test_a_reused_line_is_marked_as_reused(self):
        c = self.cue()
        S.take_condensed(c, "Kibice wspierają klub.", reused=True)
        self.assertTrue(c.reused)

    def test_nothing_is_not_a_condensation(self):
        c = self.cue()
        self.assertFalse(S.take_condensed(c, None))
        self.assertFalse(S.take_condensed(c, ""))


class TestHandEdits(unittest.TestCase):
    """What the user typed into the script file outranks everything this module can produce."""

    def setUp(self):
        self.s = dict(SCRIPT_TBL)
        self.cues = [Cue(1, "Kibice Richmond wciąż wspierają swój klub.", 20)]

    def prev(self, text, condensed, source=None):
        return {"cues": [{"id": 1, "text": text, "condensed": condensed,
                          "text_original": source or self.cues[0].text}]}

    def test_an_edit_on_a_line_the_model_never_touched_is_kept(self):
        keep = S.hand_edited(self.cues, self.prev("Kibice wciąż wspierają klub.", False), {},
                             self.s)
        self.assertEqual(keep, {1: "Kibice wciąż wspierają klub."})

    def test_an_untouched_line_is_not_an_edit(self):
        self.assertEqual(S.hand_edited(self.cues, self.prev(self.cues[0].text, False), {},
                                       self.s), {})

    def test_an_edit_on_top_of_a_condensation_is_kept(self):
        book = {S.cache_key(self.cues[0].text, 20, self.s): {"text": "Kibice wspierają klub."}}
        keep = S.hand_edited(self.cues, self.prev("Kibice ciągle z klubem.", True), book, self.s)
        self.assertEqual(keep, {1: "Kibice ciągle z klubem."})

    def test_the_models_own_condensation_is_not_an_edit(self):
        book = {S.cache_key(self.cues[0].text, 20, self.s): {"text": "Kibice wspierają klub."}}
        self.assertEqual(S.hand_edited(self.cues, self.prev("Kibice wspierają klub.", True),
                                       book, self.s), {})

    def test_a_condensed_line_with_nothing_to_compare_against_is_left_alone(self):
        """Without the book there is no way to tell an edit from a redraw, and guessing wrong
        would freeze a line the model must never see again."""
        self.assertEqual(S.hand_edited(self.cues, self.prev("Kibice wspierają klub.", True), {},
                                       self.s), {})

    def test_a_line_whose_source_changed_is_not_matched_at_all(self):
        prev = self.prev("Cokolwiek.", False, source="Zupełnie inne zdanie źródłowe.")
        self.assertEqual(S.hand_edited(self.cues, prev, {}, self.s), {})

    def test_an_edit_is_never_stamped_onto_an_identical_line_elsewhere(self):
        """An episode carries a dozen cards reading "Tak." – editing one is not editing all."""
        cues = [Cue(3, "Tak.", 20), Cue(4, "Tak.", 20)]
        prev = {"cues": [{"id": 3, "text": "No tak.", "condensed": False, "text_original": "Tak."},
                         {"id": 4, "text": "Tak.", "condensed": False, "text_original": "Tak."}]}
        self.assertEqual(S.hand_edited(cues, prev, {}, self.s), {3: "No tak."})

    def test_an_edit_on_the_second_duplicate_is_also_found(self):
        cues = [Cue(3, "Tak.", 20), Cue(4, "Tak.", 20)]
        prev = {"cues": [{"id": 3, "text": "Tak.", "condensed": False, "text_original": "Tak."},
                         {"id": 4, "text": "No tak.", "condensed": False, "text_original": "Tak."}]}
        self.assertEqual(S.hand_edited(cues, prev, {}, self.s), {4: "No tak."})

    def test_a_moved_cue_id_is_still_matched_when_the_source_is_unique(self):
        """Re-extracting the subtitles or changing the merge gate renumbers everything."""
        prev = {"cues": [{"id": 1, "text": "Cokolwiek.", "condensed": False,
                          "text_original": "Krótka kwestia."},
                         {"id": 2, "text": "Kibice wciąż wspierają klub.", "condensed": False,
                          "text_original": self.cues[0].text}]}
        self.assertEqual(S.hand_edited(self.cues, prev, {}, self.s),
                         {1: "Kibice wciąż wspierają klub."})


# ---------------------------------------------------------------------------
# The stage, end to end, with the network scripted
# ---------------------------------------------------------------------------


SRT = """1
00:00:01,000 --> 00:00:03,000
Kibice Richmond, którzy przeszli przez to wszystko, wciąż tu są.

2
00:00:04,000 --> 00:00:06,000
Mamy 3 mecze do końca sezonu.

3
00:00:08,000 --> 00:00:12,000
Krótka.
"""


def reply(cues, cost=0.01):
    return {
        "choices": [{"message": {"content": json.dumps({"cues": cues}, ensure_ascii=False)}}],
        "usage": {"cost": cost, "prompt_tokens": 10, "completion_tokens": 5},
    }


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class TestStageReuse(unittest.TestCase):
    """The whole point, measured: the second run of an unchanged episode calls nobody."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        raw = Config(REPO / "lektor.toml").raw
        raw["paths"] = dict(raw["paths"], input_dir_wsl=str(root), work_subdir="_lektor")
        raw["script"] = dict(raw["script"], request_gap_ms=0, max_rerolls=0)
        # Written as JSON on purpose: Config still reads that, and the standard library has no
        # TOML writer. What this file exercises is the condensation cache, not the format.
        (root / "lektor.json").write_text(json.dumps(raw), encoding="utf-8")
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}).start()
        self.addCleanup(mock.patch.stopall)
        self.cfg = Config(root / "lektor.json")
        (self.cfg.work_dir / "EP01.srt").write_text(SRT, encoding="utf-8")
        self.answers = [{"id": 1, "text": "Kibice Richmond wciąż tu są."},
                        {"id": 2, "text": "Mamy trzy mecze do końca."}]

    def run_stage(self, **kw):
        calls = []

        def post(url, headers=None, json=None, timeout=None):  # noqa: A002
            calls.append(json)
            return FakeResponse(reply(self.answers))

        with mock.patch.object(S.requests, "post", post):
            S.stage_script(self.cfg, None, force=True, **kw)
        return calls, json_loads(self.cfg.work_dir / "EP01.lektor.json")

    def test_second_run_is_free_and_identical(self):
        calls, first = self.run_stage()
        self.assertEqual(len(calls), 1)
        self.assertTrue(S.condensed_path(self.cfg, "EP01").exists())

        calls2, second = self.run_stage()
        self.assertEqual(calls2, [], "an unchanged episode must not cost a single call")
        self.assertEqual([c["text"] for c in second["cues"]],
                         [c["text"] for c in first["cues"]])
        self.assertTrue(any(c["reused"] for c in second["cues"]))

    def test_a_hand_edit_survives_a_forced_rerun(self):
        """The stage ends by telling you to open the file and fix what reads badly."""
        self.run_stage()
        path = self.cfg.work_dir / "EP01.lektor.json"
        data = json_loads(path)
        data["cues"][0]["text"] = "Zupełnie inna kwestia, moja."
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        calls, after = self.run_stage()
        self.assertEqual(after["cues"][0]["text"], "Zupełnie inna kwestia, moja.")
        self.assertTrue(after["cues"][0]["edited"])
        body = "".join(c["messages"][-1]["content"] for c in calls)
        self.assertNotIn("[1] >>>", body, "an edited line is never sent to the model again")

    def test_a_hand_edit_survives_redraw_too(self):
        """--redraw asks to pay for new text, not to throw away what the user typed."""
        self.run_stage()
        path = self.cfg.work_dir / "EP01.lektor.json"
        data = json_loads(path)
        data["cues"][0]["text"] = "Ręcznie poprawiona linia."
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        calls, after = self.run_stage(redraw=True)
        self.assertEqual(after["cues"][0]["text"], "Ręcznie poprawiona linia.")
        body = "".join(c["messages"][-1]["content"] for c in calls)
        self.assertNotIn("[1] >>>", body)

    def test_redraw_ignores_the_book(self):
        self.run_stage()
        calls, _ = self.run_stage(redraw=True)
        self.assertEqual(len(calls), 1)

    def test_reuse_previous_false_ignores_the_book(self):
        self.run_stage()
        self.cfg.raw["script"]["reuse_previous"] = False
        calls, _ = self.run_stage()
        self.assertEqual(len(calls), 1)

    def test_a_new_prompt_redraws_everything(self):
        self.run_stage()
        with mock.patch.object(S, "CONDENSE_SYSTEM", S.CONDENSE_SYSTEM + "\nNowa zasada."):
            calls, _ = self.run_stage()
        self.assertEqual(len(calls), 1, "a changed prompt must miss on every line")

    def test_a_failed_cue_is_not_cached_as_a_failure(self):
        """The model returning nothing for a cue must not make that permanent."""
        self.answers = [{"id": 1, "text": "Kibice Richmond wciąż tu są."}]
        self.run_stage()
        calls, _ = self.run_stage()
        # More than one call, because a cue that never came back is also retried, re-rolled and
        # escalated - which is the point: a cue with digits in it cannot be read aloud at all, so
        # a draw that drops it is a failed draw and not a result.
        self.assertGreater(len(calls), 1)
        body = calls[0]["messages"][-1]["content"]
        self.assertIn("[2] >>>", body, "the cue that failed must be asked for again")
        self.assertNotIn("[1] >>>", body, "the cue that succeeded must not be")


def json_loads(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
