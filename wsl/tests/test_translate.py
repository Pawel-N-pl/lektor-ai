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
        self.assertEqual(T.wrap_subtitle("- Byłem w pubie. - Znowu?", speakers=2),
                         "- Byłem w pubie.\n- Znowu?")

    def test_the_source_card_says_how_many_speakers_there_are(self):
        self.assertEqual(T.speaker_turns("- I was at the pub.\n- Again?"), 2)
        self.assertEqual(T.speaker_turns("-Hi.\n-Hello."), 2)
        # Some rips leave the first speaker's dash off.
        self.assertEqual(T.speaker_turns("Who?\n- Me."), 2)
        self.assertEqual(T.speaker_turns("A long line that the\nripper wrapped."), 0)
        self.assertEqual(T.speaker_turns("- A lone dash, one line."), 0)

    def test_a_dash_inside_one_speakers_line_is_not_a_third_speaker(self):
        src = "- Who did it?\n- I was - I mean, me."
        self.assertEqual(T.wrap_subtitle("- Kto to zrobił? - Ja - to znaczy, ja.",
                                         speakers=T.speaker_turns(src)),
                         "- Kto to zrobił?\n- Ja - to znaczy, ja.")

    def test_a_dash_with_no_space_after_it_still_splits(self):
        turns = T.speaker_turns("-Hi.\n-Hello.")
        self.assertEqual(T.wrap_subtitle("-Cześć. -Witaj.", speakers=turns), "-Cześć.\n-Witaj.")

    def test_a_card_with_one_speaker_is_never_split_on_a_dash(self):
        self.assertEqual(T.wrap_subtitle("- Ja - to znaczy, ja."), "- Ja - to znaczy, ja.")

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

    def test_retry_misses_off_goes_straight_to_the_fallback(self):
        self.cfg.raw["translate"]["retry_misses"] = False

        def answer(model, ids):
            if model == "test/primary":
                return {i: POLISH[i] for i in ids if i != 4}
            return {i: POLISH[i] for i in ids}

        p = Provider(answer)
        out = self.go(p)
        self.assertEqual(out.texts, POLISH)
        self.assertEqual([(c["model"], c["ids"]) for c in p.calls],
                         [("test/primary", [1, 2, 4, 5]), ("test/fallback", [4])])

    def test_a_fallback_equal_to_the_primary_is_not_called_twice(self):
        self.cfg.raw["translate"]["fallback_model"] = "test/primary"
        p = Provider(lambda model, ids: {i: POLISH[i] for i in ids if i != 4})
        out = self.go(p)
        self.assertEqual(len(p.calls), 2)          # the call and its targeted retry
        self.assertEqual(out.missing, [4])
        # The retry asked for cue 4 alone and got a reply with no cue in it, which _ask reports
        # as a failed call - and a failed retry is reported like a failed first call.
        self.assertIn("no usable cue", out.error)
        self.assertEqual(out.rejected, {})

    def chunked(self):
        # Small enough that SOURCE's four cues go out as [1, 2] and [4, 5].
        self.cfg.raw["translate"]["max_prompt_chars"] = 60
        _, path = T.plan_chunks(self.cues(), 60)
        self.assertEqual(path, "chunked:2")

    def test_an_episode_over_the_limit_is_sent_in_contiguous_chunks(self):
        self.chunked()
        p = Provider()
        out = self.go(p)
        self.assertEqual(out.path, "chunked:2")
        self.assertEqual(out.texts, POLISH)
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2], [4, 5]])
        # Each chunk sees only itself, and asks for all of it, so there is no request note.
        second = p.calls[1]["body"]["messages"]
        self.assertNotIn("[1]", second[1]["content"])
        self.assertEqual(len(second), 2)

    def test_a_chunk_with_nothing_wanted_is_not_sent(self):
        self.chunked()
        p = Provider()
        out = self.go(p, todo={5})
        self.assertEqual([c["ids"] for c in p.calls], [[5]])
        # Its own chunk is the context: cue 4 is shown, only cue 5 is asked for.
        self.assertIn("[4] I'm ready.", p.calls[0]["body"]["messages"][1]["content"])
        self.assertEqual(out.texts, {5: POLISH[5]})

    def test_an_outright_failure_in_one_chunk_stops_the_rest(self):
        self.chunked()
        dead = Resp(402, {"error": {"code": 402, "message": "no credits"}})
        p = Provider(lambda model, ids: dead)
        out = self.go(p)
        # Chunk 1's primary and fallback only: chunk 2 would meet the same empty account.
        self.assertEqual([(c["model"], c["ids"]) for c in p.calls],
                         [("test/primary", [1, 2]), ("test/fallback", [1, 2])])
        self.assertIn("402", out.error)
        self.assertEqual(out.missing, [1, 2, 4, 5])

    def test_a_validation_miss_in_one_chunk_does_not_stop_the_next(self):
        self.chunked()
        # Cue 1 comes back every time, untranslated: a reply that parses, with a line that fails
        # validation. (A reply with no usable cue at all is an error to _ask, and does stop.)
        p = Provider(lambda model, ids: {i: ("Where have you been?" if i == 1 else POLISH[i])
                                         for i in ids})
        out = self.go(p)
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2], [1], [1], [4, 5]])
        self.assertEqual(out.missing, [1])
        # In the validator's words, not the retry note's Polish meant for the model.
        self.assertEqual(out.rejected, {1: "came back untranslated"})
        self.assertEqual(out.error, "")

    def test_a_cue_that_never_came_back_is_missing_not_rejected(self):
        self.cfg.raw["translate"]["fallback_model"] = ""
        dropped = {i: POLISH[i] for i in (1, 2, 5)}
        p = Provider(lambda model, ids: {i: dropped[i] for i in ids if i in dropped}
                     if ids != [4] else Resp(200, body([{"id": 1, "text": POLISH[1]}])))
        out = self.go(p)
        self.assertEqual(out.missing, [4])
        self.assertEqual(out.rejected, {})

    def test_a_failed_retry_is_a_failed_call(self):
        # The first call does most of chunk 1, the retry meets a dead key, and there is no
        # fallback: the error is the episode's, and chunk 2 is not sent to the same dead key.
        self.chunked()
        self.cfg.raw["translate"]["fallback_model"] = ""

        def answer(model, ids):
            answer.n += 1
            if answer.n == 1:
                return {1: POLISH[1]}
            return Resp(401, {"error": {"code": 401, "message": "bad key"}})

        answer.n = 0
        p = Provider(answer)
        out = self.go(p)
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2], [2]])
        self.assertIn("401", out.error)
        self.assertEqual(out.missing, [2, 4, 5])
        self.assertEqual(out.rejected, {})

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

    def test_redraw_alone_implies_force(self):
        # As in script: a clean draw that then skips the episode as "exists" cannot mean anything.
        self.run_stage()
        got, p = self.run_stage(redraw=True)
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(len(p.calls), 1)

    def test_a_full_book_needs_no_key(self):
        # Rebuilding from lines already paid for is offline work: no key, no call.
        self.run_stage()
        self.srt.unlink()
        T.provenance_path(self.cfg, "ep").unlink()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            got, p = self.run_stage(Provider(lambda model, ids: {}))
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(p.calls, [])
        self.assertTrue(self.srt.exists())

    def test_a_changed_source_language_is_translated_again(self):
        self.run_stage()
        ger = self.work / "subs" / "ep.src.ger.srt"
        ger.write_text(SOURCE, encoding="utf-8")
        self.write_sidecar({"subtitle_origin": "subs", "source_language": "ger",
                            "source_srt": "subs/ep.src.ger.srt"})
        self.assertIn("ep.src.eng.srt", T.stale_source(self.cfg, "ep"))
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        # The language is in the cache key, so nothing English-sourced is reused.
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2, 4, 5]])
        prov = json.loads((self.work / "ep.translate.json").read_text(encoding="utf-8"))
        self.assertEqual((prov["source_language"], prov["source_srt"]),
                         ("ger", "subs/ep.src.ger.srt"))
        self.assertEqual(T.stale_source(self.cfg, "ep"), "")

    def test_a_source_rewritten_on_the_same_path_is_translated_again_for_what_changed(self):
        # extract --force picking another stream of the same language lands on the same path.
        self.run_stage()
        self.src.write_text(SOURCE.replace("Where have you been?", "Where were you?"),
                            encoding="utf-8")
        self.assertNotEqual(T.stale_source(self.cfg, "ep"), "")
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual([c["ids"] for c in p.calls], [[1]])

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

    def test_a_failed_force_leaves_the_hand_edit_where_it_was(self):
        # Copied aside only once there is a complete translation to put in its place: a forced
        # run that fails must not leave the episode with a .bak and no <base>.srt.
        self.run_stage()
        mine = "1\n00:00:01,000 --> 00:00:02,000\nmoje\n"
        self.srt.write_text(mine, encoding="utf-8")
        drop4 = Provider(lambda model, ids: {i: POLISH[i] for i in ids if i != 4})
        with self.assertRaises(SystemExit):
            # --redraw too: with the book full, --force alone would never call the provider.
            self.run_stage(drop4, force=True, redraw=True)
        self.assertEqual(self.srt.read_text(encoding="utf-8"), mine)
        self.assertEqual(list(self.work.glob("ep.srt.bak-*")), [])
        self.assertEqual(T.srt_state(self.cfg, "ep"), "edited")

    def test_a_failed_force_leaves_a_foreign_srt_where_it_was(self):
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPolski track\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_stage(Provider(lambda model, ids: {i: POLISH[i] for i in ids if i != 4}),
                           force=True)
        self.assertIn("Polski track", self.srt.read_text(encoding="utf-8"))
        self.assertEqual(list(self.work.glob("ep.srt.bak-*")), [])
        self.assertEqual(T.srt_state(self.cfg, "ep"), "foreign")

    def test_one_failed_episode_does_not_stop_the_next(self):
        # 'a' has a cue no model ever answers; 'b' is fine and comes after it.
        (self.work / "ep.source.json").unlink()
        self.src.unlink()
        for base, text in (("a", SOURCE + "\n9\n00:02:00,000 --> 00:02:01,000\nNever.\n"),
                           ("b", SOURCE)):
            (self.work / "subs" / f"{base}.src.eng.srt").write_text(text, encoding="utf-8")
            (self.work / f"{base}.source.json").write_text(json.dumps({
                "base": base, "language": "pol", "subtitle_origin": "subs",
                "source_language": "eng", "source_srt": f"subs/{base}.src.eng.srt"}),
                encoding="utf-8")
        # Asked for the extra cue alone, the double answers with one nobody asked for: a reply
        # that parses and leaves the cue absent, rather than one _ask would call a failed call.
        p = Provider(lambda model, ids: {i: POLISH[i] for i in ids if i in POLISH}
                     or {1: POLISH[1]})
        with self.assertRaises(SystemExit) as cm:
            self.run_stage(p)
        msg = str(cm.exception)
        # Absent, said in English, and not as a rejection.
        self.assertRegex(msg, r"a: 1 of 5 cues untranslated \(\[\d+\] absent\)")
        self.assertNotIn("b:", msg)
        self.assertTrue((self.work / "b.srt").exists())
        self.assertFalse((self.work / "a.srt").exists())

    def test_an_srt_this_stage_did_not_write_is_left_alone(self):
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPolski track\n", encoding="utf-8")
        self.assertEqual(T.srt_state(self.cfg, "ep"), "foreign")
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "foreign"})
        self.assertIn("Polski track", self.srt.read_text(encoding="utf-8"))

    def test_force_on_a_foreign_srt_copies_it_aside_first(self):
        self.srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nPolski track\n", encoding="utf-8")
        got, _ = self.run_stage(force=True)
        self.assertEqual(got, {"ep": "written"})
        kept = list(self.work.glob("ep.srt.bak-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("Polski track", kept[0].read_text(encoding="utf-8"))
        self.assertEqual(T.srt_state(self.cfg, "ep"), "ours")

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
        # Merged, not replaced: the book holds what both runs bought, so no later run pays for
        # cues 1, 2 and 5 again.
        book = json.loads((self.work / "ep.translated.json").read_text(encoding="utf-8"))
        self.assertEqual(len(book["entries"]), 4)
        got, p = self.run_stage(force=True)
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(p.calls, [])

    def test_with_reuse_off_a_hole_says_the_rerun_pays_for_everything(self):
        self.cfg.raw["translate"]["reuse_previous"] = False
        with self.assertRaises(SystemExit) as cm:
            self.run_stage(Provider(lambda model, ids: {i: POLISH[i] for i in ids if i != 4}))
        self.assertNotIn("will not be paid for again", str(cm.exception))
        self.assertIn("a rerun pays for every line again", str(cm.exception))
        self.assertFalse((self.work / "ep.translated.json").exists())

    def test_a_corrupt_book_is_kept_aside_and_the_episode_is_translated_fresh(self):
        for bad in ("{not json", '{"base": "ep"}', "[1]"):
            with self.subTest(book=bad):
                for f in self.work.glob("ep.translate*"):
                    f.unlink()
                self.srt.unlink(missing_ok=True)
                (self.work / "ep.translated.json").write_text(bad, encoding="utf-8")
                got, p = self.run_stage()
                self.assertEqual(got, {"ep": "written"})
                self.assertEqual(len(p.calls), 1)
                kept = list(self.work.glob("ep.translated.corrupt-*.json"))
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0].read_text(encoding="utf-8"), bad)
                kept[0].unlink()
                book = json.loads((self.work / "ep.translated.json").read_text(encoding="utf-8"))
                self.assertEqual(len(book["entries"]), 4)

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


class TestStageFromTranscript(WorkDir):
    """Path 3: the source is the transcript `transcribe` wrote, found through <base>.asr.json."""

    def setUp(self):
        super().setUp()
        self.src.unlink()
        self.write_sidecar({"subtitle_origin": "asr", "source_language": "", "audio_language": ""})

    def transcribed(self, language="eng", text=SOURCE):
        asr = self.work / "subs" / f"ep.asr.{language}.srt"
        asr.write_text(text, encoding="utf-8")
        (self.work / "ep.asr.json").write_text(json.dumps({
            "srt": f"subs/{asr.name}", "language": language}), encoding="utf-8")
        return asr

    def test_it_is_translated_exactly_like_a_subtitle_track(self):
        self.transcribed()
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual([c.start_ms for c in parse_srt(self.srt)], [1000, 3000, 8000, 60000])
        prov = json.loads((self.work / "ep.translate.json").read_text(encoding="utf-8"))
        self.assertEqual(prov["source_srt"], "subs/ep.asr.eng.srt")
        self.assertEqual(prov["source_language"], "eng")
        self.assertEqual(prov["subtitle_origin"], "asr")
        self.assertTrue(T.is_translated(self.cfg, "ep"))

    def test_before_transcribe_has_run_it_fails_and_says_which_stage_is_missing(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_stage()
        self.assertIn("run transcribe first", str(cm.exception))
        self.assertFalse(self.srt.exists())

    def test_a_new_transcript_is_translated_again(self):
        self.transcribed()
        self.run_stage()
        self.transcribed(text=SOURCE.replace("I'm ready.", "I am ready.", 1))
        self.assertTrue(T.stale_source(self.cfg, "ep"))
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(len(p.calls), 1)

    def test_the_sidecar_is_never_rewritten_with_what_the_transcript_learned(self):
        self.transcribed()
        info = T.source_info(self.cfg, "ep")
        self.assertEqual((info["source_srt"], info["source_language"]),
                         ("subs/ep.asr.eng.srt", "eng"))
        on_disk = json.loads((self.work / "ep.source.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_srt", on_disk)


if __name__ == "__main__":
    unittest.main()
