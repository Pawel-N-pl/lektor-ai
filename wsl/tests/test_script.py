"""Offline tests for the condensation stage: reply parsing, validation, retry and spend.

Nothing here touches the network. `requests.post` is replaced by a scripted list of canned
responses, so the retry logic, the json_schema -> json_object degradation and the spend record are
exercised against exactly the failures a provider produces in the wild.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import script as S  # noqa: E402
from lektorlib.script import (  # noqa: E402
    CondenseError,
    ReplyError,
    SchemaRejected,
    Target,
    parse_reply,
    retry_note,
    validate,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def ok_body(cues, cost=0.001, prompt=100, completion=50, reasoning=10):
    return {
        "choices": [{"message": {"content": json.dumps({"cues": cues}, ensure_ascii=False)}}],
        "usage": {
            "cost": cost,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def raw_body(content, cost=0.001):
    return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost}}


class Cue:
    """The two fields the prompt builder needs, without dragging in srt.Cue."""

    def __init__(self, idx, text, budget):
        self.idx, self.text, self.budget_chars = idx, text, budget


class FakeCfg:
    def __init__(self, work_dir, **script_over):
        self.raw = {
            "script": {
                "openrouter_model": "test/model",
                "openrouter_base": "https://example.invalid/api/v1",
                "reasoning_effort": "medium",
                "max_retries": 3,
                "timeout_s": 5,
                **script_over,
            }
        }
        self._work = Path(work_dir)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def work_dir(self):
        return self._work


def scripted(*responses):
    """A requests.post double that returns each canned response in turn."""
    seq = list(responses)
    calls = []

    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "headers": headers, "body": json})
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    post.calls = calls
    return post


SCRIPT = {
    "openrouter_model": "test/model",
    "openrouter_base": "https://example.invalid/api/v1",
    "reasoning_effort": "medium",
    "max_retries": 3,
    "timeout_s": 5,
}


# ---------------------------------------------------------------------------
# parse_reply
# ---------------------------------------------------------------------------


class TestParseReply(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_reply('{"cues":[{"id":3,"text":" a "}]}'), {3: "a"})

    def test_fenced(self):
        self.assertEqual(parse_reply('```json\n{"cues":[{"id":1,"text":"b"}]}\n```'), {1: "b"})

    def test_prose_around_object(self):
        self.assertEqual(parse_reply('Oto wynik: {"cues":[{"id":2,"text":"c"}]} gotowe'), {2: "c"})

    def test_string_id_is_coerced(self):
        self.assertEqual(parse_reply('{"cues":[{"id":"7","text":"d"}]}'), {7: "d"})

    def test_duplicate_id_keeps_the_last(self):
        self.assertEqual(
            parse_reply('{"cues":[{"id":1,"text":"first"},{"id":1,"text":"second"}]}'),
            {1: "second"},
        )

    def test_bad_items_are_dropped_not_fatal(self):
        got = parse_reply(
            '{"cues":[{"id":1},{"text":"no id"},{"id":"x","text":"y"},'
            '{"id":2,"text":5},{"id":3,"text":"ok"}]}'
        )
        self.assertEqual(got, {3: "ok"})

    def test_empty_content(self):
        with self.assertRaises(ReplyError):
            parse_reply("   ")

    def test_not_json(self):
        with self.assertRaises(ReplyError):
            parse_reply("przepraszam, nie mogę tego zrobić")

    def test_truncated_json(self):
        with self.assertRaises(ReplyError):
            parse_reply('{"cues":[{"id":1,"text":"urwane')

    def test_wrong_shape(self):
        for bad in ('{"lines":[]}', '{"cues":{"1":"a"}}', "[1,2,3]", '{"cues":[]}'):
            with self.assertRaises(ReplyError, msg=bad):
                parse_reply(bad)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


TARGETS = [
    Target(1, "Oryginalna kwestia numer jeden, stanowczo za długa.", 20),
    Target(2, "Druga kwestia, też za długa jak na okno czasowe.", 20),
    Target(3, "Trzecia kwestia do skrócenia.", 15),
]


class TestValidate(unittest.TestCase):
    def test_all_good(self):
        v = validate({1: "Kwestia jeden.", 2: "Kwestia dwa.", 3: "Kwestia trzy."}, TARGETS)
        self.assertEqual(len(v.accepted), 3)
        self.assertEqual(v.misses, [])

    def test_missing_cue(self):
        v = validate({1: "Kwestia jeden.", 3: "Kwestia trzy."}, TARGETS)
        self.assertEqual(v.missing, [2])
        self.assertEqual(v.misses, [2])

    def test_grown_cue_is_never_accepted(self):
        grown = TARGETS[0].original + " i jeszcze dopisek"
        v = validate({1: grown, 2: "ok", 3: "ok"}, TARGETS)
        self.assertNotIn(1, v.accepted)
        self.assertIn("grew", v.rejected[1])

    def test_equal_length_is_allowed_but_useless(self):
        v = validate({1: TARGETS[0].original}, TARGETS)
        self.assertIn(1, v.accepted)
        self.assertIn(1, v.over_budget)

    def test_empty_cue_rejected(self):
        v = validate({1: "", 2: "   ".strip()}, TARGETS)
        self.assertEqual(v.rejected[1], "empty")
        self.assertEqual(v.rejected[2], "empty")

    def test_over_budget_is_accepted_but_flagged(self):
        v = validate({1: "Wciąż odrobinę za długa kwestia"}, TARGETS)
        self.assertIn(1, v.accepted)
        self.assertIn(1, v.over_budget)
        self.assertIn(1, v.misses)

    def test_unasked_cue_is_extra_not_accepted(self):
        v = validate({99: "nieproszona"}, TARGETS)
        self.assertEqual(v.extra, [99])
        self.assertEqual(v.accepted, {})

    def test_retry_note_names_every_miss_and_reason(self):
        v = validate({1: TARGETS[0].original + "!", 2: "Wciąż stanowczo za długa."}, TARGETS)
        note = retry_note(v, TARGETS)
        self.assertIn("[1] limit 20 znaków - odrzucona (grew", note)
        self.assertIn("[2] limit 20 znaków - za długa: 25 znaków przy limicie 20", note)
        self.assertIn("[3] limit 15 znaków - brak w odpowiedzi", note)


# ---------------------------------------------------------------------------
# call_openrouter: retries, fail-fast, HTTP-200 errors, schema rejection
# ---------------------------------------------------------------------------


class TestCall(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(S.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def call(self, *responses, fmt=None):
        post = scripted(*responses)
        with mock.patch.object(S.requests, "post", post):
            got, stats = S.call_openrouter(
                SCRIPT, "k", [{"role": "user", "content": "x"}], fmt or S.CUES_SCHEMA, "condense"
            )
        return got, stats, post

    def test_happy_path_reports_usage_cost(self):
        got, stats, post = self.call(FakeResponse(200, ok_body([{"id": 1, "text": "a"}], cost=0.0077)))
        self.assertEqual(got, {1: "a"})
        self.assertEqual(stats.cost, 0.0077)
        self.assertEqual(stats.reasoning_tokens, 10)
        self.assertEqual(stats.response_format, "json_schema")

    def test_attribution_headers_present(self):
        _, _, post = self.call(FakeResponse(200, ok_body([{"id": 1, "text": "a"}])))
        h = post.calls[0]["headers"]
        self.assertTrue(h["HTTP-Referer"])
        self.assertEqual(h["X-OpenRouter-Title"], "lektor-ai")
        self.assertEqual(post.calls[0]["body"]["response_format"]["json_schema"]["strict"], True)

    def test_429_retries_and_honours_retry_after(self):
        got, _, post = self.call(
            FakeResponse(429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "7"}),
            FakeResponse(200, ok_body([{"id": 1, "text": "a"}])),
        )
        self.assertEqual(got, {1: "a"})
        self.assertEqual(len(post.calls), 2)
        self.sleep.assert_called_once_with(7.0)

    def test_401_fails_fast(self):
        with self.assertRaises(CondenseError) as cm:
            self.call(
                FakeResponse(401, {"error": {"code": 401, "message": "bad key"}}),
                FakeResponse(200, ok_body([{"id": 1, "text": "a"}])),
            )
        self.assertIn("401", str(cm.exception))
        self.sleep.assert_not_called()

    def test_error_arriving_as_http_200(self):
        with self.assertRaises(CondenseError) as cm:
            self.call(FakeResponse(200, {"error": {"code": 402, "message": "no credits"}}))
        self.assertIn("no credits", str(cm.exception))

    def test_schema_rejection_is_its_own_signal(self):
        with self.assertRaises(SchemaRejected):
            self.call(FakeResponse(400, {"error": {"code": 400, "message": "response_format json_schema not supported"}}))

    def test_schema_rejection_at_http_200(self):
        with self.assertRaises(SchemaRejected):
            self.call(FakeResponse(200, {"error": {"code": 400, "message": "structured outputs unsupported"}}))

    def test_json_object_mode_does_not_raise_schema_rejected(self):
        with self.assertRaises(CondenseError) as cm:
            self.call(
                FakeResponse(400, {"error": {"code": 400, "message": "response_format bad"}}),
                fmt=S.JSON_OBJECT_FORMAT,
            )
        self.assertNotIsInstance(cm.exception, SchemaRejected)

    def test_malformed_content_is_retried_then_given_up(self):
        with self.assertRaises(CondenseError) as cm:
            self.call(
                FakeResponse(200, raw_body("nie ma tu jsona")),
                FakeResponse(200, raw_body("nadal nie ma")),
                FakeResponse(200, raw_body("dalej nic")),
            )
        self.assertIn("unusable reply after 3 tries", str(cm.exception))

    def test_malformed_then_good(self):
        got, _, post = self.call(
            FakeResponse(200, raw_body("nie ma tu jsona")),
            FakeResponse(200, ok_body([{"id": 1, "text": "a"}])),
        )
        self.assertEqual(got, {1: "a"})
        self.assertEqual(len(post.calls), 2)

    def test_network_error_is_retried(self):
        got, _, post = self.call(
            S.requests.ConnectionError("boom"),
            FakeResponse(200, ok_body([{"id": 1, "text": "a"}])),
        )
        self.assertEqual(got, {1: "a"})
        self.assertEqual(len(post.calls), 2)


# ---------------------------------------------------------------------------
# condense_episode: the whole flow, still offline
# ---------------------------------------------------------------------------


CUES = [
    Cue(1, "Oryginalna kwestia numer jeden, stanowczo za długa.", 20),
    Cue(2, "Druga kwestia, też za długa jak na okno czasowe.", 20),
    Cue(3, "Kontekst, ten się mieści.", 200),
    Cue(4, "Trzecia kwestia do skrócenia, długa.", 15),
]
MARKED = {1, 2, 4}


class TestCondenseEpisode(unittest.TestCase):
    def setUp(self):
        mock.patch.object(S.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = FakeCfg(self.tmp.name)

    def run_with(self, *responses, **over):
        self.cfg.raw["script"].update(over)
        post = scripted(*responses)
        with mock.patch.object(S.requests, "post", post):
            out = S.condense_episode(CUES, MARKED, self.cfg, "k")
        return out, post

    def test_prompt_marks_only_the_overflowing_cues(self):
        p = S.whole_prompt(CUES, MARKED)
        self.assertIn("[1] >>> SKROC DO 20 ZNAKOW <<< Oryginalna", p)
        self.assertIn("[3] Kontekst, ten się mieści.", p)
        self.assertNotIn("[3] >>>", p)
        self.assertEqual(len(p.splitlines()), 4)

    def test_clean_run_one_call(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body(
                [{"id": 1, "text": "Kwestia jeden."},
                 {"id": 2, "text": "Kwestia dwa."},
                 {"id": 4, "text": "Kwestia cztery."}], cost=0.0077))
        )
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(out.path, "whole_episode")
        self.assertEqual(out.response_format, "json_schema")
        self.assertEqual((out.marked, out.returned, out.accepted), (3, 3, 3))
        self.assertEqual(out.over_budget, [])
        self.assertAlmostEqual(out.usd, 0.0077)

    def test_missing_and_grown_cues_trigger_one_retry(self):
        out, post = self.run_with(
            # cue 2 grew, cue 4 never came back
            FakeResponse(200, ok_body(
                [{"id": 1, "text": "Kwestia jeden."},
                 {"id": 2, "text": CUES[1].text + " dopisek"}], cost=0.005)),
            FakeResponse(200, ok_body(
                [{"id": 2, "text": "Kwestia dwa."},
                 {"id": 4, "text": "Kwestia cztery."}], cost=0.001)),
        )
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(out.accepted, 3)
        self.assertEqual(out.missing, [])
        self.assertEqual(out.rejected, {})
        self.assertAlmostEqual(out.usd, 0.006)
        note = post.calls[1]["body"]["messages"][-1]["content"]
        self.assertIn("[2]", note)
        self.assertIn("[4] limit 15 znaków - brak w odpowiedzi", note)
        # The retry prompt re-marks only the misses, never the cues that already worked.
        self.assertNotIn("[1] >>>", post.calls[1]["body"]["messages"][1]["content"])

    def test_retry_that_fails_keeps_the_best_available(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body(
                [{"id": 1, "text": "Kwestia jeden."},
                 {"id": 2, "text": "Wciąż stanowczo za długa kwestia."},
                 {"id": 4, "text": "Kwestia cztery."}], cost=0.005)),
            FakeResponse(500, {"error": {"code": 500, "message": "upstream died"}}),
            FakeResponse(500, {"error": {"code": 500, "message": "upstream died"}}),
            FakeResponse(500, {"error": {"code": 500, "message": "upstream died"}}),
        )
        self.assertEqual(out.accepted, 3)
        self.assertEqual(out.over_budget, [2])
        self.assertEqual(out.texts[2], "Wciąż stanowczo za długa kwestia.")

    def test_retry_only_replaces_when_it_is_shorter(self):
        out, _ = self.run_with(
            FakeResponse(200, ok_body(
                [{"id": 1, "text": "Kwestia jeden."},
                 {"id": 2, "text": "Nadal ciut za długa."},
                 {"id": 4, "text": "Kwestia cztery."}])),
            FakeResponse(200, ok_body([{"id": 2, "text": "Nadal ciut za długa a nawet dłuższa."}])),
        )
        self.assertEqual(out.texts[2], "Nadal ciut za długa.")

    def test_retry_can_be_switched_off(self):
        # max_rerolls off as well, or the one deliberately bad draw below is re-rolled instead of
        # being the end of the run, which is what this test is about.
        out, post = self.run_with(
            FakeResponse(200, ok_body([{"id": 1, "text": "Kwestia jeden."}])),
            retry_misses=False, max_rerolls=0,
        )
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(out.missing, [2, 4])
        self.assertEqual(out.accepted, 1)

    def test_degrades_to_json_object_and_stays_there(self):
        out, post = self.run_with(
            FakeResponse(400, {"error": {"code": 400, "message": "json_schema is not supported here"}}),
            FakeResponse(200, ok_body(
                [{"id": 1, "text": "Kwestia jeden."},
                 {"id": 2, "text": "Kwestia dwa."}])),
            FakeResponse(200, ok_body([{"id": 4, "text": "Kwestia cztery."}])),
        )
        self.assertEqual(out.response_format, "json_object")
        self.assertEqual(out.accepted, 3)
        fmts = [c["body"]["response_format"]["type"] for c in post.calls]
        self.assertEqual(fmts, ["json_schema", "json_object", "json_object"])

    def test_hard_failure_returns_empty_and_flags_the_error(self):
        out, post = self.run_with(
            FakeResponse(402, {"error": {"code": 402, "message": "insufficient credits"}}),
        )
        self.assertEqual(out.texts, {})
        self.assertIn("402", out.error)
        self.assertEqual(len(post.calls), 1)

    def test_batching_fallback_when_the_prompt_will_not_fit(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body([{"id": 1, "text": "Jeden."}, {"id": 2, "text": "Dwa."}])),
            FakeResponse(200, ok_body([{"id": 4, "text": "Cztery."}])),
            max_prompt_chars=90,
            retry_misses=False,
        )
        self.assertEqual(out.path, "batched:2")
        self.assertEqual(out.accepted, 3)
        self.assertEqual(len(post.calls), 2)
        # Each segment carries its own cues and only its own marks.
        self.assertIn("[1] >>>", post.calls[0]["body"]["messages"][1]["content"])
        self.assertNotIn("[4]", post.calls[0]["body"]["messages"][1]["content"])


# ---------------------------------------------------------------------------
# Spend record
# ---------------------------------------------------------------------------


class TestSpend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = FakeCfg(self.tmp.name)

    def test_record_is_written_and_accumulates(self):
        out = S.CondenseOutcome(marked=3, returned=3, accepted=3, usd=0.0077,
                                over_budget=[2], response_format="json_schema")
        out.calls.append(S.CallStats("condense", "json_schema", cost=0.0077, seconds=67.0))
        p = S.write_spend(self.cfg, "Ep01", out)
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["cost_source"], "openrouter usage.cost")
        self.assertEqual(d["runs"][0]["cues_in_budget"], 2)
        self.assertEqual(d["usd_total"], 0.0077)
        self.assertEqual(S.read_spend(self.cfg, "Ep01"), 0.0077)

        S.write_spend(self.cfg, "Ep01", S.CondenseOutcome(usd=0.001))
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(d["runs"]), 2)
        self.assertAlmostEqual(d["usd_total"], 0.0087)

    def test_missing_record_reads_as_zero(self):
        self.assertEqual(S.read_spend(self.cfg, "nigdy-nie-bylo"), 0.0)


class TestRerolls(unittest.TestCase):
    """The model decides for itself how hard to think, so a bad draw is re-rolled.

    The targeted retry is switched off throughout, so every call counted here is a whole-episode
    draw rather than a mixture of the two mechanisms.
    """

    def setUp(self):
        mock.patch.object(S.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = FakeCfg(self.tmp.name)

    def run_with(self, *responses, **over):
        # accept_fit_ratio 1.0 because three cues cannot express 0.9: int(3 * 0.9) is 2, so the
        # deliberately-bad draws below would pass the bar they are here to fail.
        self.cfg.raw["script"].update(dict(retry_misses=False, accept_fit_ratio=1.0, **over))
        post = scripted(*responses)
        with mock.patch.object(S.requests, "post", post):
            out = S.condense_episode(CUES, MARKED, self.cfg, "k")
        return out, post

    # Cue 4's budget is 15 characters; this reply blows it, so the draw scores 2 of 3.
    BAD = [{"id": 1, "text": "Kwestia jeden."},
           {"id": 2, "text": "Kwestia dwa."},
           {"id": 4, "text": "Kwestia numer cztery."}]
    GOOD = [{"id": 1, "text": "Kwestia jeden."},
            {"id": 2, "text": "Kwestia dwa."},
            {"id": 4, "text": "Kwestia cztery."}]

    def test_a_bad_draw_is_rerolled_and_the_better_one_wins(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body(self.BAD, cost=0.004)),
            FakeResponse(200, ok_body(self.GOOD, cost=0.006)),
        )
        self.assertEqual(len(post.calls), 2)
        self.assertEqual(out.over_budget, [])
        # The bill is every attempt, not just the winning one.
        self.assertAlmostEqual(out.usd, 0.010)
        self.assertEqual(len(out.calls), 2)

    def test_a_good_first_draw_is_never_rerolled(self):
        out, post = self.run_with(FakeResponse(200, ok_body(self.GOOD, cost=0.006)))
        self.assertEqual(len(post.calls), 1)
        self.assertAlmostEqual(out.usd, 0.006)

    def test_rerolls_can_be_switched_off(self):
        out, post = self.run_with(FakeResponse(200, ok_body(self.BAD)), max_rerolls=0)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(out.over_budget, [4])

    def test_the_best_of_a_bad_run_is_kept_and_reported(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body(self.BAD[:1], cost=0.001)),          # 1 of 3
            FakeResponse(200, ok_body(self.BAD, cost=0.002)),              # 2 of 3
            FakeResponse(200, ok_body(self.BAD[:2], cost=0.003)),          # 2 of 3
            max_rerolls=2,
        )
        self.assertEqual(len(post.calls), 3)
        self.assertEqual(out.accepted, 3)          # the 2-of-3 draw, not the last one
        self.assertEqual(out.over_budget, [4])
        self.assertAlmostEqual(out.usd, 0.006)

    def test_a_hard_failure_stops_the_rerolling(self):
        out, post = self.run_with(
            FakeResponse(200, ok_body(self.BAD[:1], cost=0.001)),
            FakeResponse(402, {"error": {"code": 402, "message": "insufficient credits"}}),
            max_rerolls=3,
        )
        self.assertEqual(len(post.calls), 2)
        self.assertIn("402", out.error)


if __name__ == "__main__":
    unittest.main()
