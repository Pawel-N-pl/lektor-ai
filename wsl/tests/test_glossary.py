"""The series glossary, offline: what the file may say, what one episode gets of it, what it costs.

The rule worth a test of its own is the cost of an edit, because it fails silently in both
directions: too loose, and a changed gender ships under last week's translation; too tight, and
renaming one term pays for a season again. A term reaches the lines that say it, the cast reaches
every line of an episode it is in, and an entry an episode cannot use reaches nothing there.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import glossary as G  # noqa: E402

FILE = """
[characters.Ted]
gender = 'm'
aliases = ['Coach', 'Lasso']
note = 'amerykański trener'

[characters.Rebecca]
gender = 'f'

[characters.Nate]

[address]
default = 'ty'
'Nate -> Ted' = 'pan'
'Ted -> Rebecca' = 'pani'

[terms]
'Greyhounds' = 'Charty'
'gaffer' = 'szef'
"""


def parse(text=FILE):
    return G.parse_glossary(tomllib.loads(text), Path("lektor.glossary.toml"))


class Cfg:
    def __init__(self, input_dir, glossary=None):
        self.raw = {"translate": {} if glossary is None else {"glossary": glossary}}
        self.input_dir = Path(input_dir)


class TestFile(unittest.TestCase):
    def test_every_table_is_read(self):
        g = parse()
        self.assertEqual(set(g.characters), {"Ted", "Rebecca", "Nate"})
        self.assertEqual(g.characters["Ted"].gender, "m")
        self.assertEqual(g.characters["Ted"].aliases, ("Coach", "Lasso"))
        self.assertEqual(g.characters["Nate"].gender, "")
        self.assertEqual(g.address_default, "ty")
        self.assertEqual(g.address, {("Nate", "Ted"): "pan", ("Ted", "Rebecca"): "pani"})
        self.assertEqual(g.terms, {"Greyhounds": "Charty", "gaffer": "szef"})

    def test_a_name_is_found_by_any_of_its_names_in_any_case(self):
        g = parse()
        self.assertEqual(g.lookup("COACH").name, "Ted")
        self.assertEqual(g.canonical("rebecca"), "Rebecca")
        self.assertIsNone(g.lookup("Roy"))
        self.assertEqual(g.canonical("Roy"), "Roy")

    def test_a_pair_written_with_an_alias_is_filed_under_the_name(self):
        g = parse(FILE + "\n")
        g2 = G.parse_glossary(tomllib.loads(FILE.replace("'Nate -> Ted'", "'Nate > Coach'")))
        self.assertEqual(g2.address, g.address)

    def test_what_it_cannot_read_is_an_error_not_a_guess(self):
        for bad, said in [
            ("[characters.Ted]\ngender = 'male'\n", "gender"),
            ("[characters.Ted]\nsex = 'm'\n", "unknown key"),
            ("[address]\n'Ted -> Roy' = 'sir'\n", "pick from"),
            ("[address]\n'Ted and Roy' = 'ty'\n", "Who -> To whom"),
            ("[people.Ted]\n", "unknown table"),
            ("[characters]\nTed = 'm'\n", "must be a table"),
            ("[characters.Ted]\naliases = 'Coach'\n", "list"),
        ]:
            with self.subTest(bad=bad), self.assertRaisesRegex(G.GlossaryError, said):
                parse(bad)

    def test_an_empty_glossary_is_false(self):
        self.assertFalse(G.Glossary())
        self.assertTrue(parse())

    def test_what_it_writes_it_reads_back_unchanged(self):
        g = parse()
        g.characters["Zoë \"Z\""] = G.Character("Zoë \"Z\"", "f", ("Z",), "mówi: \"no\"")
        text = G.dump_glossary(g, "# a header")
        back = G.parse_glossary(tomllib.loads(text))
        self.assertTrue(text.startswith("# a header\n"))
        self.assertEqual(back.characters, g.characters)
        self.assertEqual((back.address_default, back.address, back.terms),
                         (g.address_default, g.address, g.terms))


class TestWhere(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_auto_reads_the_file_beside_the_episodes_and_nothing_when_it_is_not_there(self):
        self.assertIsNone(G.load_glossary(Cfg(self.dir)))
        (self.dir / "lektor.glossary.toml").write_text(FILE, encoding="utf-8")
        self.assertEqual(set(G.load_glossary(Cfg(self.dir)).characters), {"Ted", "Rebecca", "Nate"})

    def test_off_ignores_the_file(self):
        (self.dir / "lektor.glossary.toml").write_text(FILE, encoding="utf-8")
        self.assertIsNone(G.load_glossary(Cfg(self.dir, "off")))

    def test_a_named_file_is_relative_to_the_episodes_and_must_exist(self):
        (self.dir / "show.toml").write_text(FILE, encoding="utf-8")
        self.assertTrue(G.load_glossary(Cfg(self.dir, "show.toml")))
        with self.assertRaisesRegex(G.GlossaryError, "no such file"):
            G.load_glossary(Cfg(self.dir, "missing.toml"))

    def test_a_file_that_will_not_parse_says_where(self):
        (self.dir / "lektor.glossary.toml").write_text("[terms\n", encoding="utf-8")
        with self.assertRaisesRegex(G.GlossaryError, "lektor.glossary.toml"):
            G.load_glossary(Cfg(self.dir))

    def test_a_config_with_no_input_dir_has_no_auto_glossary(self):
        self.assertIsNone(G.glossary_path(SimpleNamespace(raw={})))


class TestEpisode(unittest.TestCase):
    TEXT = ["Where's Coach?", "The Greyhounds won!", "I'm ready."]

    def test_only_what_the_episode_says_is_kept(self):
        g = parse().for_episode(self.TEXT)
        # Ted by his alias; Rebecca and Nate are never named, so neither is their pair with Ted.
        self.assertEqual(set(g.characters), {"Ted"})
        self.assertEqual(g.address, {})
        self.assertEqual(g.address_default, "ty")
        self.assertEqual(g.terms, {"Greyhounds": "Charty"})

    def test_a_speaker_label_brings_its_character_in(self):
        g = parse().for_episode(self.TEXT, speakers={"REBECCA"})
        self.assertEqual(set(g.characters), {"Ted", "Rebecca"})
        self.assertEqual(g.address, {("Ted", "Rebecca"): "pani"})

    def test_names_keep_their_case_terms_do_not(self):
        g = parse("[characters.Will]\n[terms]\n'gaffer' = 'szef'\n")
        got = g.for_episode(["I will ask the GAFFER."])
        self.assertEqual(got.characters, {})
        self.assertEqual(got.terms, {"gaffer": "szef"})

    def test_a_term_matches_from_the_left_edge_of_a_word(self):
        self.assertTrue(G.mentions("Go Greyhounds!", "Greyhound"))
        self.assertFalse(G.mentions("the Megreyhound", "greyhound", fold=True))

    def test_the_rendered_glossary_names_genders_pairs_and_terms(self):
        text = parse().render()
        self.assertIn("- Ted (też: Coach, Lasso): mężczyzna. amerykański trener", text)
        self.assertIn("- Rebecca: kobieta", text)
        self.assertIn('- Nate do: Ted – "pan"', text)
        self.assertIn('- wszyscy pozostali do siebie – "ty"', text)
        self.assertIn("- Greyhounds → Charty", text)


class TestWhatAnEditCosts(unittest.TestCase):
    """Glossary.key_part on an episode's glossary is what a line's cache key carries."""

    TEXT = ["Where's Coach?", "The Greyhounds won!", "I'm ready."]

    def parts(self, text=FILE):
        g = parse(text).for_episode(self.TEXT)
        return [g.key_part(t) for t in self.TEXT]

    def test_a_changed_term_reaches_only_the_line_that_says_it(self):
        before = self.parts()
        after = self.parts(FILE.replace("'Charty'", "'Chartowie'"))
        self.assertEqual([b != a for b, a in zip(before, after)], [False, True, False])

    def test_a_changed_gender_reaches_every_line_of_an_episode_the_character_is_in(self):
        before = self.parts()
        after = self.parts(FILE.replace("gender = 'm'", "gender = 'f'"))
        self.assertEqual([b != a for b, a in zip(before, after)], [True, True, True])

    def test_a_character_the_episode_never_meets_reaches_nothing(self):
        before = self.parts()
        after = self.parts(FILE.replace("gender = 'f'", "note = 'szefowa'\ngender = 'f'"))
        self.assertEqual(before, after)
        self.assertEqual(self.parts(FILE + "\n[characters.Roy]\ngender = 'm'\n"), before)

    def test_a_pair_the_episode_cannot_use_reaches_nothing(self):
        self.assertEqual(self.parts(FILE.replace("'pani'", "'ty'")), self.parts())

    def test_no_glossary_is_no_key_part(self):
        self.assertEqual(G.Glossary().key_part("anything"), "")
        g = parse("[terms]\n'gaffer' = 'szef'\n")
        self.assertEqual(g.key_part("No terms here."), "")

    def test_the_episode_fingerprint_moves_with_what_it_uses_and_only_that(self):
        g = parse().for_episode(self.TEXT).fingerprint()
        self.assertEqual(parse(FILE + "\n[characters.Roy]\n").for_episode(self.TEXT).fingerprint(),
                         g)
        self.assertNotEqual(parse(FILE.replace("'Charty'", "'Psy'")).for_episode(
            self.TEXT).fingerprint(), g)
        self.assertEqual(G.Glossary().fingerprint(), "")


if __name__ == "__main__":
    unittest.main()
