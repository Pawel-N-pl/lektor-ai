"""The translate stage with a series glossary and speaker hints, offline, end to end.

What a test has to pin here is what a glossary edit costs on a real rerun - which lines are asked
for again and which come out of <base>.translated.json - and that an episode with neither a
glossary nor a labelled source is sent and keyed exactly as it was before either existed, so the
book a season already paid for still answers.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lektorlib import script as S  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.srt import parse_srt  # noqa: E402
from test_translate import TRANSLATE_TBL, Provider  # noqa: E402

SOURCE = """1
00:00:01,000 --> 00:00:02,500
Where's Coach?

2
00:00:03,000 --> 00:00:05,000
The Greyhounds won again.

3
00:00:08,000 --> 00:00:10,400
I'm ready.
"""

LABELLED = """1
00:00:01,000 --> 00:00:02,500
REBECCA: Where's Coach?

2
00:00:03,000 --> 00:00:05,000
- TED: Right here.
- REBECCA: Good.

3
00:00:08,000 --> 00:00:10,400
[Laughs] I'm ready.
"""

POLISH = {1: "Gdzie jest trener?", 2: "Charty znowu wygrały.", 3: "Jestem gotowa."}
POLISH_LABELLED = {1: "Gdzie jest trener?", 2: "- Tutaj. - Dobrze.", 3: "[śmiech] Jestem gotowa."}

GLOSSARY = """
[characters.Ted]
gender = 'm'
aliases = ['Coach']

[characters.Rebecca]
gender = 'f'

[address]
'Ted -> Rebecca' = 'pani'

[terms]
'Greyhounds' = 'Charty'
"""


class Cfg:
    def __init__(self, root):
        self.raw = {"translate": dict(TRANSLATE_TBL), "script": {"openrouter_model": "x"}}
        self.input_dir = Path(root)
        self._work = Path(root) / "_lektor"
        self._work.mkdir(exist_ok=True)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def work_dir(self):
        return self._work


class Base(unittest.TestCase):
    TEXT = SOURCE
    ANSWERS = POLISH

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Cfg(self.root)
        self.work = self.cfg.work_dir
        (self.work / "subs").mkdir()
        (self.work / "subs" / "ep.src.eng.srt").write_text(self.TEXT, encoding="utf-8")
        (self.work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "subtitle_origin": "subs", "source_language": "eng",
            "source_srt": "subs/ep.src.eng.srt"}), encoding="utf-8")
        mock.patch.object(T, "say").start()
        mock.patch.object(S, "say").start()
        mock.patch.object(S.time, "sleep").start()
        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}).start()
        self.addCleanup(mock.patch.stopall)

    def glossary(self, text=GLOSSARY):
        (self.root / "lektor.glossary.toml").write_text(text, encoding="utf-8")

    def run_stage(self, answers=None, **kw):
        answers = answers or self.ANSWERS
        p = Provider(lambda model, ids: {i: answers[i] for i in ids if i in answers})
        with mock.patch.object(S.requests, "post", p):
            got = T.stage_translate(self.cfg, None, **kw)
        return got, p

    def prov(self):
        return json.loads((self.work / "ep.translate.json").read_text(encoding="utf-8"))


class TestNothingNew(Base):
    def test_without_glossary_or_labels_the_request_is_the_old_one(self):
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        msgs = p.calls[0]["body"]["messages"]
        self.assertEqual(msgs[0]["content"], T.TRANSLATE_SYSTEM)
        self.assertEqual(msgs[1]["content"], "[1] Where's Coach?\n[2] The Greyhounds won again.\n"
                                             "[3] I'm ready.")
        self.assertEqual(self.prov()["glossary_sha"], "")

    def test_without_glossary_or_hint_the_cache_key_is_the_old_one(self):
        """The formula phase 1 shipped, spelled out: the book a season paid for still answers."""
        cue = SimpleNamespace(idx=3, text="I'm ready.", start_ms=8000)
        s = {"openrouter_model": "m", "reasoning_effort": "medium"}
        fp = hashlib.sha256("\n".join([
            T.TRANSLATE_SYSTEM, T.LINE_FORMAT,
            json.dumps(T.TRANSLATE_SCHEMA, sort_keys=True, ensure_ascii=False)]).encode()
        ).hexdigest()[:12]
        old = hashlib.sha256(json.dumps([1, "eng", 8000, "I'm ready.", "m", "medium", fp],
                                        ensure_ascii=False, sort_keys=True).encode()
                             ).hexdigest()[:24]
        self.assertEqual(T.cache_key(cue, "eng", s), old)
        self.assertEqual(T.cache_key(cue, "eng", s, T.Brief()), old)


class TestGlossary(Base):
    def test_the_episode_glossary_and_its_rules_go_into_the_system_prompt(self):
        self.glossary(GLOSSARY + "\n[characters.Roy]\ngender = 'm'\n")
        _, p = self.run_stage()
        sent = p.calls[0]["body"]["messages"][0]["content"]
        self.assertTrue(sent.startswith(T.TRANSLATE_SYSTEM))
        self.assertIn(T.GLOSSARY_RULES, sent)
        self.assertIn("- Ted (też: Coach): mężczyzna", sent)
        self.assertIn("- Greyhounds → Charty", sent)
        # Roy is never in this episode, and Rebecca never named, so neither is sent.
        self.assertNotIn("Roy", sent)
        self.assertNotIn("Rebecca", sent)
        self.assertNotIn(T.HINT_RULES, sent)

    def test_the_provenance_records_the_glossary_this_episode_used(self):
        self.glossary()
        self.run_stage()
        prov = self.prov()
        self.assertTrue(prov["glossary"].endswith("lektor.glossary.toml"))
        self.assertEqual(len(prov["glossary_sha"]), 12)

    def rerun_after(self, new_text):
        self.glossary()
        self.run_stage()
        self.glossary(new_text)
        got, p = self.run_stage()
        return got, [c["ids"] for c in p.calls]

    def test_a_changed_term_retranslates_only_the_line_that_says_it(self):
        got, asked = self.rerun_after(GLOSSARY.replace("'Charty'", "'Chartowie'"))
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(asked, [[2]])

    def test_a_changed_gender_retranslates_the_whole_episode(self):
        got, asked = self.rerun_after(GLOSSARY.replace("gender = 'm'", "gender = 'f'"))
        self.assertEqual(asked, [[1, 2, 3]])

    def test_an_entry_the_episode_cannot_use_changes_nothing(self):
        got, asked = self.rerun_after(GLOSSARY + "\n[characters.Roy]\ngender = 'm'\n")
        self.assertEqual((got, asked), ({"ep": "kept"}, []))
        got, asked = self.rerun_after(GLOSSARY.replace("'pani'", "'ty'"))
        self.assertEqual((got, asked), ({"ep": "kept"}, []))

    def test_a_glossary_added_later_makes_the_translation_stale(self):
        self.run_stage()
        self.assertEqual(T.stale_source(self.cfg, "ep"), "")
        self.glossary()
        self.assertIn("without the series glossary", T.stale_source(self.cfg, "ep"))
        got, p = self.run_stage()
        self.assertEqual([c["ids"] for c in p.calls], [[1, 2, 3]])
        self.assertEqual(T.stale_source(self.cfg, "ep"), "")

    def test_a_hand_edited_srt_is_still_kept_when_the_glossary_changes(self):
        self.glossary()
        self.run_stage()
        srt = self.work / "ep.srt"
        srt.write_text(srt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.glossary(GLOSSARY.replace("'Charty'", "'Psy'"))
        got, p = self.run_stage()
        self.assertEqual((got, p.calls), ({"ep": "edited"}, []))

    def test_a_glossary_that_will_not_load_stops_before_anything_is_paid_for(self):
        self.glossary("[characters.Ted]\ngender = 'male'\n")
        with self.assertRaisesRegex(SystemExit, "gender"):
            self.run_stage()
        self.assertFalse((self.work / "ep.srt").exists())

    def test_the_plan_does_not_crash_on_a_broken_glossary(self):
        self.run_stage()
        self.glossary("[terms\n")
        self.assertEqual(T.stale_source(self.cfg, "ep"), "")


class TestHints(Base):
    TEXT = LABELLED
    ANSWERS = POLISH_LABELLED

    def test_labels_become_hints_and_never_reach_the_polish_file(self):
        self.glossary()
        got, p = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        msgs = p.calls[0]["body"]["messages"]
        self.assertIn(T.HINT_RULES, msgs[0]["content"])
        self.assertEqual(msgs[1]["content"].split("\n"), [
            "[1] {mówi: Rebecca, kobieta} Where's Coach?",
            "[2] {mówią po kolei: Ted (mężczyzna), Rebecca (kobieta)} - Right here. - Good.",
            # A sound is not a speaker.
            "[3] [Laughs] I'm ready.",
        ])
        cards = parse_srt(self.work / "ep.srt")
        self.assertEqual([c.raw for c in cards],
                         ["Gdzie jest trener?", "- Tutaj.\n- Dobrze.", "[śmiech] Jestem gotowa."])
        self.assertEqual(self.prov()["speaker_hints"], 2)

    def test_a_hint_the_model_copies_back_is_taken_off_again(self):
        echoed = {**POLISH_LABELLED, 1: "{mówi: Rebecca, kobieta} Gdzie jest trener?"}
        self.run_stage(echoed)
        self.assertEqual(parse_srt(self.work / "ep.srt")[0].raw, "Gdzie jest trener?")

    def test_a_hinted_line_is_keyed_apart_from_the_same_line_unhinted(self):
        cue = SimpleNamespace(idx=1, text="Where's Coach?", start_ms=1000)
        s = {"openrouter_model": "m"}
        brief = T.Brief(hints=T.SpeakerHints(text={1: "{mówi: REBECCA}"}))
        other = T.Brief(hints=T.SpeakerHints(text={1: "{mówi: TED}"}))
        keys = {T.cache_key(cue, "eng", s), T.cache_key(cue, "eng", s, brief),
                T.cache_key(cue, "eng", s, other)}
        self.assertEqual(len(keys), 3)

    def test_labels_on_lines_with_no_dashes_come_back_as_two_turns(self):
        (self.work / "subs" / "ep.src.eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nTED: Ready?\nREBECCA: Always.\n", encoding="utf-8")
        p = Provider(lambda model, ids: {1: "- Gotowa? - Zawsze."})
        with mock.patch.object(S.requests, "post", p):
            T.stage_translate(self.cfg, None)
        self.assertIn("[1] {mówią po kolei: TED, REBECCA} - Ready? - Always.",
                      p.calls[0]["body"]["messages"][1]["content"])
        self.assertEqual(parse_srt(self.work / "ep.srt")[0].raw, "- Gotowa?\n- Zawsze.")

    def test_a_card_that_was_only_a_label_is_dropped(self):
        (self.work / "subs" / "ep.src.eng.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nTED:\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nTED: Hi.\n", encoding="utf-8")
        p = Provider(lambda model, ids: {2: "Cześć."})
        with mock.patch.object(S.requests, "post", p):
            T.stage_translate(self.cfg, None)
        self.assertEqual([c.raw for c in parse_srt(self.work / "ep.srt")], ["Cześć."])


if __name__ == "__main__":
    unittest.main()
