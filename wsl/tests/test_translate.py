"""The translate stage, offline: what it sends, what it accepts, what it writes and what it refuses.

Nothing here touches the network. `requests.post` is replaced by a fake provider that reads the
prompt it was sent and answers it - every cue, or a chosen few, or an error - so the retry, the
fallback model, the reuse cache and the hand-edit guard are all exercised end to end on a real
work dir in a temp directory.

The two rules worth a test each, because both fail silently:

  * a cue that never came back must never produce a `<base>.srt` with a hole in it - the lektor
    would simply fall silent there, and nothing downstream could tell that from a quiet scene;
  * a `<base>.srt` somebody edited after it was translated must never be overwritten without
    --force, and a Polish track's `<base>.srt` must never be touched at all.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import script as S  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.srt import parse_srt  # noqa: E402

SOURCE = """1
00:00:01,000 --> 00:00:02,500
<i>Where have you been?</i>

2
00:00:03,000 --> 00:00:05,000
- I was at the pub.
- Again?

3
00:00:06,000 --> 00:00:07,000
{\\an8}

4
00:00:08,000 --> 00:00:10,400
I'm ready.

5
00:01:00,000 --> 00:01:02,000
I'm ready.
"""

POLISH = {1: "Gdzie byłeś?", 2: "- Byłem w pubie. - Znowu?", 4: "Jestem gotowa.", 5: "Jestem gotowy."}

TRANSLATE_TBL = {
    "mode": "auto",
    "source": "auto",
    "openrouter_model": "test/primary",
    "fallback_model": "test/fallback",
    "openrouter_base": "https://example.invalid/api/v1",
    "reasoning_effort": "medium",
    "max_retries": 1,
    "timeout_s": 5,
    "request_gap_ms": 0,
    "max_prompt_chars": 200000,
    "max_len_ratio": 3.0,
    "min_len_ratio": 0.25,
    "min_check_chars": 24,
    "reuse_previous": True,
}


class FakeCfg:
    def __init__(self, work_dir, **over):
        self.raw = {"translate": dict(TRANSLATE_TBL, **over),
                    "script": {"openrouter_model": "test/condense"}}
        self._work = Path(work_dir)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def work_dir(self):
        return self._work


def body(cues, cost=0.002, model=None):
    payload = {"choices": [{"message": {"content": json.dumps({"cues": cues},
                                                              ensure_ascii=False)}}],
               "usage": {"cost": cost, "prompt_tokens": 100, "completion_tokens": 50}}
    if model:
        payload["model"] = model
    return payload


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


_LINE = re.compile(r"^\[(\d+)\] (.*)$", re.M)


class Provider:
    """A requests.post double that answers what it was asked.

    It reads the cue list out of the prompt and, when a third message names cues, answers only
    those. `answer(model, ids)` decides what comes back: a dict id -> text, or a Resp to return as
    is. The default is the POLISH table for every id asked.
    """

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or (lambda model, ids: {i: POLISH[i] for i in ids if i in POLISH})

    def __call__(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        msgs = json["messages"]
        ids = [int(m) for m, _ in _LINE.findall(msgs[1]["content"])]
        if len(msgs) > 2:
            ids = [int(m) for m, _ in _LINE.findall(msgs[2]["content"])]
        self.calls.append({"model": json["model"], "ids": ids, "body": json})
        got = self.answer(json["model"], ids)
        if isinstance(got, Resp):
            return got
        return Resp(200, body([{"id": i, "text": t} for i, t in got.items()],
                              model=json["model"]))


class WorkDir(unittest.TestCase):
    """A real work dir: a sidecar saying 'translate me', and the foreign subtitle it points at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.cfg = FakeCfg(self.work)
        (self.work / "subs").mkdir()
        self.src = self.work / "subs" / "ep.src.eng.srt"
        self.src.write_text(SOURCE, encoding="utf-8")
        self.write_sidecar({"subtitle_origin": "subs", "source_language": "eng",
                            "source_srt": "subs/ep.src.eng.srt"})
        mock.patch.object(T, "say").start()
        mock.patch.object(S, "say").start()
        mock.patch.object(S.time, "sleep").start()
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}).start()
        self.addCleanup(mock.patch.stopall)

    def write_sidecar(self, extra):
        data = {"base": "ep", "source": "/films/ep.mkv", "language": "pol", **extra}
        (self.work / "ep.source.json").write_text(json.dumps(data), encoding="utf-8")

    def run_stage(self, provider=None, **kw):
        provider = provider or Provider()
        with mock.patch.object(S.requests, "post", provider):
            got = T.stage_translate(self.cfg, kw.pop("only", None), **kw)
        return got, provider

    @property
    def srt(self):
        return self.work / "ep.srt"


# ---------------------------------------------------------------------------
# Pure parts
# ---------------------------------------------------------------------------


class TestSourceCues(unittest.TestCase):
    def test_markup_goes_lines_join_and_empty_cards_drop_with_their_ids_kept(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.srt"
            p.write_text(SOURCE, encoding="utf-8")
            cues = T.source_cues(p)
        self.assertEqual([c.idx for c in cues], [1, 2, 4, 5])
        self.assertEqual(cues[0].text, "Where have you been?")
        # The speaker dash survives the join, so the model can put the change of speaker back.
        self.assertEqual(cues[1].text, "- I was at the pub. - Again?")

    def test_the_prompt_is_one_numbered_line_per_cue(self):
        cues = [SimpleNamespace(idx=1, text="Hi."), SimpleNamespace(idx=4, text="Bye.")]
        self.assertEqual(T.translate_prompt(cues), "[1] Hi.\n[4] Bye.")

    def test_a_request_note_names_each_cue_and_why(self):
        note = T.request_note({7, 3}, {3: "brak w odpowiedzi"})
        self.assertIn("[3] brak w odpowiedzi", note)
        self.assertIn("[7] do przetłumaczenia", note)
        self.assertLess(note.index("[3]"), note.index("[7]"))


class TestWrapAndFormat(unittest.TestCase):
    def test_a_speaker_change_goes_back_on_its_own_line(self):
        self.assertEqual(T.wrap_subtitle("- Byłem w pubie. - Znowu?"), "- Byłem w pubie.\n- Znowu?")

    def test_a_long_line_breaks_once_near_the_middle(self):
        text = "Nie mam pojęcia, o czym mówisz, ale brzmi to całkiem rozsądnie."
        lines = T.wrap_subtitle(text).split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(" ".join(lines), text)
        self.assertLess(abs(len(lines[0]) - len(lines[1])), 12)

    def test_a_short_line_and_a_hyphenated_word_are_left_alone(self):
        self.assertEqual(T.wrap_subtitle("Tak."), "Tak.")
        self.assertEqual(T.wrap_subtitle("To jest biało-czerwone."), "To jest biało-czerwone.")

    def test_timings_survive_one_for_one_and_numbering_restarts(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "x.srt"
            src.write_text(SOURCE, encoding="utf-8")
            cues = T.source_cues(src)
            out = Path(td) / "pl.srt"
            out.write_text(T.format_srt(cues, POLISH), encoding="utf-8")
            back = parse_srt(out)
        self.assertEqual([c.idx for c in back], [1, 2, 3, 4])
        self.assertEqual([(c.start_ms, c.end_ms) for c in back],
                         [(c.start_ms, c.end_ms) for c in cues])
        self.assertEqual(back[1].raw, "- Byłem w pubie.\n- Znowu?")

    def test_timestamps_over_an_hour(self):
        self.assertEqual(T._ts(3_723_004), "01:02:03,004")


class TestCheckLine(unittest.TestCase):
    T = {"max_len_ratio": 3.0, "min_len_ratio": 0.25, "min_check_chars": 24}

    def check(self, src, text):
        return T.check_line(src, text, self.T)

    def test_a_plain_translation_passes(self):
        self.assertEqual(self.check("Where have you been?", "Gdzie byłeś?"), "")

    def test_empty_is_refused(self):
        self.assertEqual(self.check("Hi.", "  "), "empty")

    def test_absurdly_long_is_refused(self):
        self.assertIn("too long", self.check("Hi.", "x" * 40))

    def test_a_condensed_long_line_is_refused_but_a_short_one_is_exempt(self):
        src = "I have absolutely no idea what you are talking about, mate."
        self.assertIn("condensed", self.check(src, "Nie wiem."))
        self.assertEqual(self.check("Yeah, sure, whatever.", "No."), "")

    def test_a_sentence_handed_back_untranslated_is_refused_but_a_name_is_not(self):
        self.assertEqual(self.check("I don't know what you mean.", "I don't know what you mean."),
                         "came back untranslated")
        self.assertEqual(self.check("Ted!", "Ted!"), "")

    def test_validate_sorts_every_id_into_one_bucket(self):
        v = T.validate({1: "Gdzie byłeś?", 2: "", 9: "obce"},
                       {1: "Where have you been?", 2: "Again?", 4: "I'm ready."}, self.T)
        self.assertEqual(v.accepted, {1: "Gdzie byłeś?"})
        self.assertEqual(v.rejected, {2: "empty"})
        self.assertEqual(v.missing, [4])
        self.assertEqual(v.extra, [9])
        self.assertEqual(T.miss_reasons(v), {4: "brak w odpowiedzi", 2: "odrzucona (empty)"})


class TestCacheKey(unittest.TestCase):
    S = {"openrouter_model": "m", "reasoning_effort": "medium"}

    def key(self, text="I'm ready.", start=8000, lang="eng", **over):
        return T.cache_key(SimpleNamespace(text=text, start_ms=start), lang, dict(self.S, **over))

    def test_the_same_card_said_twice_is_two_entries(self):
        """Polish puts the speaker's gender into "I'm ready." - one entry would stamp it on both."""
        self.assertNotEqual(self.key(start=8000), self.key(start=60000))

    def test_text_model_effort_and_language_are_in_the_key(self):
        base = self.key()
        self.assertNotEqual(base, self.key(text="I'm ready!"))
        self.assertNotEqual(base, self.key(openrouter_model="other"))
        self.assertNotEqual(base, self.key(reasoning_effort="high"))
        self.assertNotEqual(base, self.key(lang="ger"))

    def test_the_draw_settings_and_the_fallback_are_not(self):
        base = self.key()
        self.assertEqual(base, self.key(temperature=0.9, fallback_model="x", max_retries=9))

    def test_a_prompt_edit_misses(self):
        base = self.key()
        with mock.patch.object(T, "TRANSLATE_SYSTEM", T.TRANSLATE_SYSTEM + " "):
            self.assertNotEqual(base, self.key())


class TestChunks(unittest.TestCase):
    def test_one_chunk_while_it_fits_contiguous_slices_when_it_does_not(self):
        cues = [SimpleNamespace(idx=i, text="x" * 40) for i in range(1, 101)]
        chunks, path = T.plan_chunks(cues, 1_000_000)
        self.assertEqual((len(chunks), path), (1, "whole_episode"))
        chunks, path = T.plan_chunks(cues, 1500)
        self.assertEqual(path, f"chunked:{len(chunks)}")
        self.assertGreater(len(chunks), 1)
        self.assertEqual([c.idx for ch in chunks for c in ch], list(range(1, 101)))


# ---------------------------------------------------------------------------
# The call: retry, fallback, degradation
# ---------------------------------------------------------------------------


class TestTranslateEpisode(WorkDir):
    def cues(self):
        return T.source_cues(self.src)

    def go(self, provider, todo=None):
        cues = self.cues()
        with mock.patch.object(S.requests, "post", provider):
            return T.translate_episode(cues, set(todo or {c.idx for c in cues}), self.cfg, "k")

    def test_one_call_for_the_whole_episode(self):
        p = Provider()
        out = self.go(p)
        self.assertEqual(len(p.calls), 1)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual(out.path, "whole_episode")
        self.assertEqual(out.by_model, {"test/primary": 4})
        self.assertAlmostEqual(out.usd, 0.002)
        # The schema is the translation one, not condensation's.
        fmt = p.calls[0]["body"]["response_format"]
        self.assertEqual(fmt["json_schema"]["name"], "lektor_translated_cues")
        self.assertEqual(p.calls[0]["body"]["max_tokens"], 64000)

    def test_a_missed_cue_gets_one_targeted_retry_naming_only_it(self):
        first = {"n": 0}

        def answer(model, ids):
            first["n"] += 1
            if first["n"] == 1:
                return {i: POLISH[i] for i in ids if i != 4}
            return {i: POLISH[i] for i in ids}

        p = Provider(answer)
        out = self.go(p)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2, 4, 5], [4]])
        self.assertIn("[4] brak w odpowiedzi", p.calls[1]["body"]["messages"][2]["content"])

    def test_the_fallback_model_gets_what_the_primary_could_not_do(self):
        def answer(model, ids):
            if model == "test/primary":
                return {i: POLISH[i] for i in ids if i != 2}
            return {i: POLISH[i] for i in ids}

        p = Provider(answer)
        out = self.go(p)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual([(c["model"], c["ids"]) for c in p.calls],
                         [("test/primary", [1, 2, 4, 5]), ("test/primary", [2]),
                          ("test/fallback", [2])])
        self.assertEqual(out.by_model, {"test/primary": 3, "test/fallback": 1})

    def test_a_primary_that_fails_outright_goes_straight_to_the_fallback(self):
        def answer(model, ids):
            if model == "test/primary":
                return Resp(401, {"error": {"code": 401, "message": "bad key"}})
            return {i: POLISH[i] for i in ids}

        p = Provider(answer)
        out = self.go(p)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual(out.error, "")
        self.assertEqual([c["model"] for c in p.calls], ["test/primary", "test/fallback"])

    def test_both_failing_outright_is_an_error_with_everything_missing(self):
        p = Provider(lambda model, ids: Resp(402, {"error": {"code": 402, "message": "no credits"}}))
        out = self.go(p)
        self.assertIn("402", out.error)
        self.assertEqual(out.missing, [1, 2, 4, 5])

    def test_a_refused_schema_degrades_to_json_object(self):
        def answer(model, ids):
            if answer.n == 0:
                answer.n += 1
                return Resp(400, {"error": {"code": 400,
                                            "message": "response_format json_schema unsupported"}})
            return {i: POLISH[i] for i in ids}

        answer.n = 0
        p = Provider(answer)
        out = self.go(p)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual(out.response_format, "json_object")
        self.assertEqual(p.calls[1]["body"]["response_format"], {"type": "json_object"})

    def test_a_partial_send_shows_the_whole_episode_and_names_the_wanted_cues(self):
        p = Provider()
        out = self.go(p, todo={5})
        self.assertEqual(out.texts, {5: POLISH[5]})
        msgs = p.calls[0]["body"]["messages"]
        self.assertIn("[1] Where have you been?", msgs[1]["content"])
        self.assertIn("[5]", msgs[2]["content"])


# ---------------------------------------------------------------------------
# The stage: what is written, what is kept, what is refused
# ---------------------------------------------------------------------------


class TestStage(WorkDir):
    def test_it_writes_the_polish_srt_with_the_source_timings_and_records_provenance(self):
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        cues = parse_srt(self.srt)
        self.assertEqual([c.raw for c in cues],
                         ["Gdzie byłeś?", "- Byłem w pubie.\n- Znowu?", "Jestem gotowa.",
                          "Jestem gotowy."])
        self.assertEqual([(c.start_ms, c.end_ms) for c in cues],
                         [(1000, 2500), (3000, 5000), (8000, 10400), (60000, 62000)])
        prov = json.loads((self.work / "ep.translate.json").read_text(encoding="utf-8"))
        self.assertEqual(prov["srt_sha256"], T.file_sha256(self.srt))
        self.assertEqual(prov["source_language"], "eng")
        self.assertEqual(prov["source_srt"], "subs/ep.src.eng.srt")
        self.assertEqual(T.srt_state(self.cfg, "ep"), "ours")

    def test_spend_is_appended_to_the_episodes_one_record_without_clobbering_condensation(self):
        S.append_spend(self.cfg, "ep", {"model": "test/condense", "usd": 0.01})
        self.run_stage()
        d = json.loads((self.work / "ep.spend.json").read_text(encoding="utf-8"))
        self.assertEqual(len(d["runs"]), 2)
        self.assertEqual(d["runs"][1]["stage"], "translate")
        self.assertAlmostEqual(d["usd_total"], 0.012)
        self.assertAlmostEqual(S.read_spend(self.cfg, "ep"), 0.012)
        self.assertAlmostEqual(S.read_spend(self.cfg, "ep", "script"), 0.01)
        self.assertAlmostEqual(S.read_spend(self.cfg, "ep", "translate"), 0.002)

    def test_a_second_run_touches_nothing(self):
        self.run_stage()
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "kept"})
        self.assertEqual(p.calls, [])

    def test_force_redoes_it_from_the_book_without_paying_again(self):
        self.run_stage()
        got, p = self.run_stage(force=True)
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(p.calls, [])

    def test_redraw_ignores_the_book(self):
        self.run_stage()
        got, p = self.run_stage(force=True, redraw=True)
        self.assertEqual(len(p.calls), 1)

    def test_a_hand_edit_is_never_overwritten_without_force(self):
        self.run_stage()
        edited = self.srt.read_text(encoding="utf-8").replace("Gdzie byłeś?", "Gdzieś ty był?")
        self.srt.write_text(edited, encoding="utf-8")
        self.assertEqual(T.srt_state(self.cfg, "ep"), "edited")
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "edited"})
        self.assertEqual(self.srt.read_text(encoding="utf-8"), edited)
        self.assertEqual(p.calls, [])

    def test_force_on_a_hand_edit_copies_it_aside_first(self):
        self.run_stage()
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nmoje\n", encoding="utf-8")
        got, _ = self.run_stage(force=True)
        self.assertEqual(got, {"ep": "written"})
        kept = list(self.work.glob("ep.srt.bak-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("moje", kept[0].read_text(encoding="utf-8"))

    def test_an_srt_this_stage_did_not_write_is_left_alone(self):
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPolski track\n", encoding="utf-8")
        self.assertEqual(T.srt_state(self.cfg, "ep"), "foreign")
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "foreign"})
        self.assertIn("Polski track", self.srt.read_text(encoding="utf-8"))

    def test_an_episode_from_a_polish_track_is_skipped_entirely(self):
        self.write_sidecar({})
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPolski\n", encoding="utf-8")
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "track"})
        self.assertEqual(p.calls, [])
        self.assertEqual(T.origin(self.cfg, "ep"), "track")

    def test_mode_never_translates_nothing(self):
        self.cfg.raw["translate"]["mode"] = "never"
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "never"})
        self.assertFalse(self.srt.exists())

    def test_a_hole_writes_no_file_and_the_rerun_pays_only_for_the_hole(self):
        def answer(model, ids):
            return {i: POLISH[i] for i in ids if i != 4}

        with self.assertRaises(SystemExit) as cm:
            self.run_stage(Provider(answer))
        self.assertIn("1 of 4 cues untranslated", str(cm.exception))
        self.assertFalse(self.srt.exists())
        book = json.loads((self.work / "ep.translated.json").read_text(encoding="utf-8"))
        self.assertEqual(len(book["entries"]), 3)

        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual([c["ids"] for c in p.calls], [[4]])

    def test_no_key_writes_nothing_and_exits_non_zero(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.assertRaises(SystemExit) as cm:
                self.run_stage()
        self.assertIn("OPENROUTER_API_KEY", str(cm.exception))
        self.assertFalse(self.srt.exists())

    def test_a_missing_source_subtitle_is_a_failure_that_names_extract(self):
        self.src.unlink()
        with self.assertRaises(SystemExit) as cm:
            self.run_stage()
        self.assertIn("extract", str(cm.exception))

    def test_a_cached_line_is_checked_like_a_fresh_one(self):
        self.run_stage()
        book_p = self.work / "ep.translated.json"
        book = json.loads(book_p.read_text(encoding="utf-8"))
        for e in book["entries"].values():
            if e["id"] == 1:
                # Handed back untranslated: four words, identical to the source.
                e["text"] = "Where have you been?"
        book_p.write_text(json.dumps(book), encoding="utf-8")
        got, p = self.run_stage(force=True)
        self.assertEqual([c["ids"] for c in p.calls], [[1]])

    def test_only_filters_like_every_other_stage(self):
        got, p = self.run_stage(only="other")
        self.assertEqual(got, {})
        self.assertEqual(p.calls, [])


if __name__ == "__main__":
    unittest.main()
