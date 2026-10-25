"""Forced alignment for a reference that is not English, offline.

There is no GPU, no torch and no MMS_FA model on the machine these tests were written on, so the
acoustic model never runs. What does run is everything around it - which text is the reference,
which bundle reads it, what reaches the script - and the real aligner script itself, fed to this
interpreter against a stand-in torch and torchaudio that check they were asked for the right
model and return a fixed alignment.

The two rules worth a test each: English goes to the aligner it always went to, with the
arguments it always had; and nothing goes to MMS_FA unless speech.multilingual_aligner says so,
because its weights are CC-BY-NC.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import speech as SP  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.config import Config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GERMAN = "1\n00:00:01,000 --> 00:00:02,000\nWo warst du, Jürgen?\n\n" \
         "2\n00:00:03,000 --> 00:00:04,500\n- In der Straße.\n- Schön.\n"


def make_cfg(root: Path, multilingual: bool = False) -> Config:
    films = root / "films"
    films.mkdir()
    text = (REPO / "lektor.toml.example").read_text(encoding="utf-8")
    text = text.replace("input_dir = ''", f"input_dir = '{films.as_posix()}'", 1)
    text = text.replace("multilingual_aligner = false",
                        f"multilingual_aligner = {str(multilingual).lower()}", 1)
    (root / "lektor.toml").write_text(text, encoding="utf-8")
    shutil.copy(REPO / "lektor.tuning.toml", root / "lektor.tuning.toml")
    return Config(root / "lektor.toml")


class Base(unittest.TestCase):
    MULTILINGUAL = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_cfg(self.root, self.MULTILINGUAL)
        self.work = self.cfg.work_dir
        (self.work / "subs").mkdir()
        self.video = self.cfg.input_dir / "ep.mkv"
        self.video.write_bytes(b"not really an mkv")
        mock.patch.object(SP, "say").start()
        self.addCleanup(mock.patch.stopall)

    def subs(self, language="ger", audio="ger", sdh=False):
        src = self.work / "subs" / f"ep.src.{language}.srt"
        src.write_text(GERMAN, encoding="utf-8")
        (self.work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "source": str(self.video), "subtitle_origin": "subs",
            "source_language": language, "audio_language": audio, "subtitle_stream": 3,
            "source_srt": f"subs/{src.name}", "source_sdh": sdh}), encoding="utf-8")
        return src

    def transcript(self, language="ger"):
        srt = self.work / "subs" / f"ep.asr.{language}.srt"
        srt.write_text(GERMAN, encoding="utf-8")
        (self.work / "ep.source.json").write_text(json.dumps({
            "base": "ep", "source": str(self.video), "subtitle_origin": "asr",
            "audio_language": language}), encoding="utf-8")
        (self.work / "ep.asr.json").write_text(json.dumps({
            "srt": f"subs/{srt.name}", "language": language,
            "language_requested": "de"}), encoding="utf-8")
        return srt

    def english_pattern(self):
        p = self.work / "subs" / "ep.eng.srt"
        p.write_text("1\n00:00:01,000 --> 00:00:02,000\nWhere were you?\n", encoding="utf-8")
        return p


# ---------------------------------------------------------------------------
# Which model reads which text
# ---------------------------------------------------------------------------


class TestWhichAligner(Base):
    def test_english_and_an_unknown_language_keep_the_english_model(self):
        for lang in ("eng", "en", ""):
            self.assertEqual(SP.aligner_for(self.cfg, lang), SP.ENGLISH_BUNDLE)

    def test_any_other_language_gets_mms_fa_only_when_allowed(self):
        self.assertEqual(SP.aligner_for(self.cfg, "ger"), SP.MULTILINGUAL_BUNDLE)
        self.cfg.raw["speech"]["multilingual_aligner"] = False
        self.assertEqual(SP.aligner_for(self.cfg, "ger"), "")

    def test_the_shipped_default_is_off(self):
        import tomllib
        for name in ("lektor.tuning.toml", "lektor.toml.example"):
            with self.subTest(name=name):
                data = tomllib.loads((REPO / name).read_text(encoding="utf-8"))
                self.assertIs(data["speech"]["multilingual_aligner"], False)


class TestReference(Base):
    def test_a_subtitle_in_the_language_spoken_comes_before_an_english_pattern(self):
        src = self.subs("ger", audio="ger")
        self.english_pattern()
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), src)
        self.assertEqual(SP.reference_language(self.cfg, "ep", src), "ger")

    def test_a_subtitle_in_a_language_nobody_speaks_is_no_reference(self):
        self.subs("spa", audio="eng")
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))
        eng = self.english_pattern()
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), eng)
        self.assertEqual(SP.reference_language(self.cfg, "ep", eng), "eng")

    def test_a_transcript_in_any_language_is_its_own_reference(self):
        srt = self.transcript("ger")
        self.english_pattern()
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), srt)

    def test_an_english_sdh_stream_is_not_swapped_in_for_the_spoken_language(self):
        src = self.subs("ger", audio="ger")
        with mock.patch.object(SP, "reference_stream") as rs, \
                mock.patch.object(SP, "extract_reference_srt") as ex:
            self.assertEqual(SP.choose_reference(self.cfg, "ep", self.video), src)
        rs.assert_not_called()
        ex.assert_not_called()


class TestReferenceWhenOff(Base):
    MULTILINGUAL = False

    def test_nothing_changes_and_the_reason_is_said(self):
        self.subs("ger", audio="ger")
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))
        why = SP.reference_problem(self.cfg, "ep")
        self.assertIn("'ger'", why)
        self.assertIn("CC-BY-NC", why)
        eng = self.english_pattern()
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), eng)

    def test_a_german_transcript_is_no_reference(self):
        self.transcript("ger")
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))

    def test_an_english_source_needs_no_reason(self):
        self.subs("eng", audio="eng")
        self.assertEqual(SP.reference_problem(self.cfg, "ep"), "")


# ---------------------------------------------------------------------------
# What the script is given
# ---------------------------------------------------------------------------


class TestRomanise(unittest.TestCase):
    def test_latin_script_folds_to_the_aligner_alphabet(self):
        for word, want in [("Straße", "strasse"), ("Łódź", "lodz"), ("über", "uber"),
                           ("Ærø", "aero"), ("l'homme", "l'homme"), ("geht’s", "geht's"),
                           ("Ça", "ca")]:
            with self.subTest(word=word):
                self.assertEqual(SP.latin_fold(word), want)

    def test_another_script_does_not_fold(self):
        for word in ("Привет", "Καλημέρα", "こんにちは"):
            with self.subTest(word=word):
                self.assertEqual(SP.latin_fold(word), "")

    def test_an_all_latin_episode_needs_no_romaniser(self):
        cues = [{"i": 1, "start": 0, "end": 900, "words": ["Schön", "Jürgen"]}]
        got, how = SP.romanise(cues)
        self.assertEqual((got[0]["words"], how), (["schon", "jurgen"], "none"))

    def test_one_word_in_another_script_sends_the_episode_to_uroman_as_it_is(self):
        cues = [{"i": 1, "start": 0, "end": 900, "words": ["Hallo"]},
                {"i": 2, "start": 1000, "end": 1900, "words": ["Привет"]}]
        got, how = SP.romanise(cues)
        self.assertEqual((got, how), (cues, "uroman"))

    def test_words_keep_every_letter_outside_english(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.srt"
            p.write_text(GERMAN, encoding="utf-8")
            self.assertEqual(SP.cues_for_alignment(p, "ger")[0]["words"],
                             ["Wo", "warst", "du", "Jürgen"])
            # English is read exactly as before: ASCII words.
            self.assertEqual(SP.cues_for_alignment(p)[0]["words"],
                             ["Wo", "warst", "du", "J", "rgen"])


class TestDelivery(Base):
    def setUp(self):
        super().setUp()
        self.cfg.raw["speech"]["aligner"] = "local"
        venv = self.root / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("", encoding="utf-8")
        self.cfg.raw["speech"]["aligner_venv"] = str(self.root / "venv")

    def deliver(self, *args):
        seen = {}

        def run(python, script, inputs, out, extra, timeout, what):
            seen["extra"] = extra
            return {"meta": {}, "rows": []}

        with mock.patch.object(SP, "run_script_local", side_effect=run):
            SP.forced_align(self.cfg, Path("a.wav"), [], self.root, *args)
        return seen["extra"]

    def test_english_gets_exactly_the_arguments_it_always_had(self):
        self.assertEqual(self.deliver(), [str(self.cfg["speech"]["fa_window_s"])])
        self.assertEqual(self.deliver(SP.ENGLISH_BUNDLE, "none", "eng"),
                         [str(self.cfg["speech"]["fa_window_s"])])

    def test_mms_fa_is_told_the_bundle_the_romaniser_and_the_language(self):
        self.assertEqual(self.deliver(SP.MULTILINGUAL_BUNDLE, "uroman", "rus"),
                         [str(self.cfg["speech"]["fa_window_s"]), "MMS_FA", "uroman", "rus"])

    def test_a_missing_romaniser_is_said_as_such(self):
        fail = SP.ScriptFailed("forced alignment", SP.NO_ROMANISER_EXIT,
                               "uroman cannot be imported: No module named 'uroman'")
        with mock.patch.object(SP, "run_script_local", side_effect=fail), \
                self.assertRaisesRegex(RuntimeError, "pip install uroman"):
            SP.forced_align(self.cfg, Path("a.wav"), [], self.root, SP.MULTILINGUAL_BUNDLE,
                            "uroman", "rus")


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


class StageBase(Base):
    def run_stage(self):
        calls = []

        def align(cfg, wav, cues, tmp, bundle=SP.ENGLISH_BUNDLE, romaniser="none", language=""):
            calls.append((bundle, romaniser, language, [c["words"] for c in cues]))
            return {"meta": {"bundle": bundle, "align_s": 1.0}, "rows": [
                {"i": 1, "cue_start_ms": 1000, "cue_end_ms": 2000, "score": 0.9, "n_words": 2,
                 "word_times": [[1100, 1300], [1400, 1700]]}]}

        def centre(cfg, video, wav):
            wav.write_bytes(b"x")
            return "centre"

        (self.work / "ep.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nGdzie byłeś?\n",
                                          encoding="utf-8")
        with mock.patch.object(SP, "extract_centre", side_effect=centre), \
                mock.patch.object(SP, "forced_align", side_effect=align), \
                mock.patch.object(SP, "shot_cuts", return_value=([5000], 0.1)), \
                mock.patch.object(SP, "reference_stream", return_value=None):
            SP.stage_speech(self.cfg, None, force=True)
        data = json.loads((self.work / "ep.speech.json").read_text(encoding="utf-8"))
        return calls, data


class TestStage(StageBase):
    def test_a_german_film_is_aligned_with_mms_fa_on_its_own_text(self):
        self.subs("ger", audio="ger")
        calls, data = self.run_stage()
        self.assertEqual(calls, [("MMS_FA", "none", "ger",
                                  [["wo", "warst", "du", "jurgen"], ["in", "der", "strasse", "schon"]])])
        self.assertEqual(data["source"]["reference_language"], "ger")
        self.assertEqual(data["fa"]["bundle"], "MMS_FA")
        self.assertEqual(data["words"], [[1100, 1300], [1400, 1700]])

    def test_an_english_reference_takes_the_old_road(self):
        self.subs("eng", audio="eng")
        calls, data = self.run_stage()
        self.assertEqual([c[:3] for c in calls], [(SP.ENGLISH_BUNDLE, "none", "eng")])


class TestStageWhenOff(StageBase):
    MULTILINGUAL = False

    def test_a_german_film_keeps_shot_cuts_only_and_says_why(self):
        """Off: the German text is no reference, the aligner is never called, and cuts remain."""
        self.subs("ger", audio="ger")
        said = []
        with mock.patch.object(SP, "say", side_effect=said.append):
            calls, data = self.run_stage()
        self.assertEqual(calls, [])
        self.assertEqual((data["cuts"], data["words"]), ([5000], []))
        self.assertTrue(any("multilingual_aligner" in s and "CC-BY-NC" in s for s in said))


# ---------------------------------------------------------------------------
# The real aligner script, against a stand-in torch and torchaudio
# ---------------------------------------------------------------------------

FAKE_TORCH = '''
import contextlib
import numpy as np

int32 = "int32"


class cuda:
    @staticmethod
    def is_available():
        return False


def device(name):
    return name


class Tensor:
    def __init__(self, a):
        self.a = np.asarray(a)

    @property
    def shape(self):
        return self.a.shape

    def unsqueeze(self, d):
        return Tensor(np.expand_dims(self.a, d))

    def __getitem__(self, k):
        return Tensor(self.a[k])

    def to(self, *_):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def log_softmax(self, _):
        return self

    def exp(self):
        return Tensor(np.exp(self.a))


def from_numpy(a):
    return Tensor(a)


def tensor(data, dtype=None):
    return Tensor(np.array(data))


inference_mode = contextlib.nullcontext
'''

FAKE_TORCHAUDIO = '''
from . import functional, pipelines  # noqa: F401
'''

FAKE_FUNCTIONAL = '''
from collections import namedtuple

import numpy as np
import torch

Span = namedtuple("Span", "start end score")


def forced_align(log_probs, targets, blank=0):
    n = targets.shape[1]
    return torch.Tensor(targets.a), torch.Tensor(np.zeros((1, n)))


def merge_tokens(tokens, scores):
    return [Span(2 * k, 2 * k + 1, 0.9) for k in range(tokens.shape[0])]
'''

FAKE_PIPELINES = '''
import torch

ENGLISH = ["-", "|", "E", "T", "A", "O", "N", "I", "H", "S", "R", "D", "L", "U", "M", "W", "C",
           "F", "G", "Y", "P", "B", "V", "K", "'", "X", "J", "Q", "Z"]
MMS = ["-", "a", "i", "e", "n", "o", "u", "t", "s", "r", "m", "k", "l", "d", "g", "h", "y", "b",
       "p", "w", "c", "v", "j", "z", "f", "'", "q", "x", "*"]


class Model:
    def to(self, _):
        return self

    def __call__(self, seg):
        return torch.Tensor([[[0.0] * 30] * (seg.shape[1] // 320)]), None


class English:
    sample_rate = 16000

    def get_model(self):
        return Model()

    def get_labels(self):
        return tuple(ENGLISH)


class Mms:
    sample_rate = 16000

    def get_model(self, with_star=True):
        assert with_star is False, "MMS_FA must be loaded without its <star> token"
        return Model()

    def get_labels(self, star="*", blank="-"):
        assert star is None, "MMS_FA's labels must be asked for without the <star> token"
        return tuple(MMS[:-1])


WAV2VEC2_ASR_LARGE_LV60K_960H = English()
MMS_FA = Mms()
'''

FAKE_UROMAN = '''
class Uroman:
    def romanize_string(self, s, lcode=None):
        return {"Привет": "Privet", "мир": "mir"}.get(s, s)
'''


class TestTheScriptItself(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = self.root / "lib"
        (self.lib / "torchaudio").mkdir(parents=True)
        (self.lib / "torch.py").write_text(FAKE_TORCH, encoding="utf-8")
        (self.lib / "torchaudio" / "__init__.py").write_text(FAKE_TORCHAUDIO, encoding="utf-8")
        (self.lib / "torchaudio" / "functional.py").write_text(FAKE_FUNCTIONAL, encoding="utf-8")
        (self.lib / "torchaudio" / "pipelines.py").write_text(FAKE_PIPELINES, encoding="utf-8")
        self.wav = self.root / "a.wav"
        with wave.open(str(self.wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000 * 6)

    def align(self, words, extra, uroman=False):
        if uroman:
            (self.lib / "uroman.py").write_text(FAKE_UROMAN, encoding="utf-8")
        cues = self.root / "cues.json"
        cues.write_text(json.dumps([{"i": 1, "start": 1000, "end": 3000, "words": words}]),
                        encoding="utf-8")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.lib)}):
            return SP.run_script_local(Path(sys.executable), SP._FA_SCRIPT, [self.wav, cues],
                                       self.root / "fa.json", extra, 60, "forced alignment")

    def test_the_english_call_reads_upper_case_letters_as_always(self):
        fa = self.align(["Where", "were", "you"], ["30"])
        self.assertEqual(fa["meta"]["bundle"], "WAV2VEC2_ASR_LARGE_LV60K_960H")
        self.assertEqual(fa["rows"][0]["n_words"], 3)

    def test_mms_fa_reads_lower_case_and_is_loaded_without_star(self):
        fa = self.align(["wo", "warst", "du"], ["30", "MMS_FA", "none", "ger"])
        self.assertEqual((fa["meta"]["bundle"], fa["meta"]["romaniser"]), ("MMS_FA", "none"))
        self.assertEqual(fa["rows"][0]["n_words"], 3)

    def test_uroman_romanises_what_does_not_fold(self):
        fa = self.align(["Привет", "мир"], ["30", "MMS_FA", "uroman", "rus"], uroman=True)
        self.assertEqual(fa["rows"][0]["n_words"], 2)

    def test_without_uroman_it_exits_with_the_code_that_means_not_installed(self):
        try:
            import uroman  # noqa: F401
            self.skipTest("uroman is installed in this interpreter")
        except ImportError:
            pass
        with self.assertRaises(SP.ScriptFailed) as cm:
            self.align(["Привет"], ["30", "MMS_FA", "uroman", "rus"])
        self.assertEqual(cm.exception.code, SP.NO_ROMANISER_EXIT)
        self.assertIn("uroman", cm.exception.stderr)


if __name__ == "__main__":
    unittest.main()
