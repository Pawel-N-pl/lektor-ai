"""`lektor.py glossary`, offline: one call, one draft, and the file translate reads left alone.

The rule worth a test is the one the command exists around: a model's guess at a cast is never
applied. It goes to lektor.glossary.suggested.toml, an earlier draft is not overwritten without
--force, and the real glossary is never written at all.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lektorlib import glossary as G  # noqa: E402
from lektorlib import script as S  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from test_translate import Resp  # noqa: E402
from test_translate_glossary import LABELLED, Cfg  # noqa: E402

DRAFT = {
    "characters": [
        {"name": "Ted", "gender": "m", "aliases": ["Coach", "Ted"], "note": "trener"},
        {"name": "Rebecca", "gender": "f", "aliases": [], "note": ""},
        {"name": "ted", "gender": "f", "aliases": [], "note": "a duplicate"},
        {"name": "", "gender": "m", "aliases": [], "note": "no name"},
    ],
    "address": [
        {"from": "Coach", "to": "Rebecca", "form": "pani"},
        {"from": "Ted", "to": "Ted", "form": "ty"},
        {"from": "Rebecca", "to": "Ted", "form": "sir"},
    ],
    "terms": [{"source": "Greyhounds", "polish": "Charty"}, {"source": "", "polish": "x"}],
}


def reply(payload, cost=0.01):
    return Resp(200, {"choices": [{"message": {"content": json.dumps(payload)}}],
                      "usage": {"cost": cost, "prompt_tokens": 10, "completion_tokens": 5}})


class TestParse(unittest.TestCase):
    def test_what_would_load_is_kept_and_the_rest_left_out(self):
        g = G.parse_suggestion(json.dumps(DRAFT))
        self.assertEqual(list(g.characters), ["Ted", "Rebecca"])
        self.assertEqual(g.characters["Ted"].aliases, ("Coach",))
        self.assertEqual(g.address, {("Ted", "Rebecca"): "pani"})
        self.assertEqual(g.terms, {"Greyhounds": "Charty"})

    def test_an_empty_draft_is_worth_another_roll(self):
        with self.assertRaises(S.ReplyError):
            G.parse_suggestion(json.dumps({"characters": [], "address": [], "terms": []}))

    def test_a_fenced_reply_is_still_read(self):
        g = G.parse_suggestion("```json\n" + json.dumps(DRAFT) + "\n```")
        self.assertIn("Ted", g.characters)


class TestStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Cfg(self.root)
        work = self.cfg.work_dir
        (work / "subs").mkdir()
        (work / "subs" / "ep.src.eng.srt").write_text(LABELLED, encoding="utf-8")
        (work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "subtitle_origin": "subs", "source_language": "eng",
            "source_srt": "subs/ep.src.eng.srt"}), encoding="utf-8")
        for mod in (G, S, T):
            if hasattr(mod, "say"):
                mock.patch.object(mod, "say").start()
        mock.patch("lektorlib.say").start()
        mock.patch.object(S.time, "sleep").start()
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}).start()
        self.addCleanup(mock.patch.stopall)
        self.sent = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        self.sent.append(json)
        return reply(DRAFT)

    def draft(self, **kw):
        with mock.patch.object(S.requests, "post", self.post):
            return G.stage_glossary(self.cfg, None, **kw)

    def test_one_call_one_draft_and_the_real_glossary_untouched(self):
        live = self.root / G.DEFAULT_NAME
        live.write_text("[terms]\n'gaffer' = 'szef'\n", encoding="utf-8")
        out = self.draft()
        self.assertEqual(out, self.root / G.SUGGESTED_NAME)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(live.read_text(encoding="utf-8"), "[terms]\n'gaffer' = 'szef'\n")
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# DRAFT by test/primary from ep"))
        back = G.load_glossary(self.cfg_for(out))
        self.assertEqual(back.address, {("Ted", "Rebecca"): "pani"})
        self.assertEqual(back.characters["Ted"].gender, "m")

    def cfg_for(self, path):
        cfg = Cfg(self.root)
        cfg.raw["translate"]["glossary"] = str(path)
        return cfg

    def test_the_model_reads_the_episode_with_its_speaker_hints(self):
        self.draft()
        body = self.sent[0]
        self.assertEqual(body["response_format"]["json_schema"]["name"], "lektor_glossary_draft")
        self.assertIn("[1] {mówi: REBECCA} Where's Coach?", body["messages"][1]["content"])

    def test_the_call_is_billed_to_the_episode_as_its_own_stage(self):
        self.draft()
        self.assertAlmostEqual(S.read_spend(self.cfg, "ep", "glossary"), 0.01)
        self.assertEqual(S.read_spend(self.cfg, "ep", "translate"), 0.0)
        self.assertAlmostEqual(S.read_spend(self.cfg, "ep"), 0.01)

    def test_an_earlier_draft_is_replaced_only_with_force(self):
        out = self.root / G.SUGGESTED_NAME
        out.write_text("# my edits\n", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "--force"):
            self.draft()
        self.assertEqual(self.sent, [])
        self.draft(force=True)
        self.assertIn("DRAFT", out.read_text(encoding="utf-8"))

    def test_no_translated_episode_is_a_stop_not_a_call(self):
        (self.cfg.work_dir / "ep.source.json").write_text(json.dumps({"base": "ep"}),
                                                          encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "no translated episode"):
            self.draft()
        self.assertEqual(self.sent, [])

    def test_no_key_is_a_stop_not_a_call(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
                self.assertRaisesRegex(SystemExit, "OPENROUTER_API_KEY"):
            self.draft()

    def test_a_failed_call_writes_no_draft_and_still_books_the_spend(self):
        def dead(url, headers=None, json=None, timeout=None):  # noqa: A002
            return Resp(402, {"error": {"code": 402, "message": "no credits"}})

        with mock.patch.object(S.requests, "post", dead), \
                self.assertRaisesRegex(SystemExit, "402"):
            G.stage_glossary(self.cfg, None)
        self.assertFalse((self.root / G.SUGGESTED_NAME).exists())
        runs = json.loads((self.cfg.work_dir / "ep.spend.json").read_text())["runs"]
        self.assertEqual(runs[0]["stage"], "glossary")


if __name__ == "__main__":
    unittest.main()
