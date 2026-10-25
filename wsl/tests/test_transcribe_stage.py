"""The transcribe stage around the model: which films it runs on, what it writes, when it refuses.

The model itself never runs here - there is no GPU and no faster-whisper on the machine these
tests were written on - so the two things that would touch the outside world are replaced: the
centre-channel cut (`extract_centre`) and the call into the other interpreter (`run_asr`, or the
delivery functions under it). Everything between them and the files on disk is the real code,
against a real Config built from the shipped template and tuning file.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import speech as SP  # noqa: E402
from lektorlib import transcribe as TR  # noqa: E402
from lektorlib import translate as T  # noqa: E402
from lektorlib.config import Config  # noqa: E402
from lektorlib.srt import parse_srt  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def make_cfg(root: Path, extra: str = "") -> Config:
    films = root / "films"
    films.mkdir()
    text = (REPO / "lektor.toml.example").read_text(encoding="utf-8")
    text = text.replace("input_dir = ''", f"input_dir = '{films.as_posix()}'", 1)
    (root / "lektor.toml").write_text(text + extra, encoding="utf-8")
    shutil.copy(REPO / "lektor.tuning.toml", root / "lektor.tuning.toml")
    return Config(root / "lektor.toml")


def words(text, start):
    out, at = [], start
    for tok in text.split(" "):
        out.append([at, at + 250, " " + tok, 0.9])
        at += 300
    return out


def reply(language="en", texts=("Where were you?", "I was at home.")):
    segs = []
    for i, text in enumerate(texts):
        start = 1000 + i * 4000
        segs.append({"id": i, "start": start, "end": start + 1500, "text": " " + text,
                     "avg_logprob": -0.2, "no_speech_prob": 0.01, "compression_ratio": 1.3,
                     "words": words(text, start)})
    return {"meta": {"language": language, "language_probability": 0.98, "device": "cuda",
                     "transcribe_s": 1.0, "rtf_x": 60.0},
            "segments": segs, "speech": [[0, 60000]]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name))
        self.work = self.cfg.work_dir
        self.cfg.raw["speech"]["tmp_dir"] = str(Path(self.tmp.name) / "tmp")
        self.video = self.cfg.input_dir / "ep.mkv"
        self.video.write_bytes(b"not really an mkv")
        for mod in (TR, T):
            mock.patch.object(mod, "say").start()
        self.addCleanup(mock.patch.stopall)

    def sidecar(self, origin="asr", **extra):
        data = {"base": "ep", "source": str(self.video), "language": "pol", **extra}
        if origin:
            data["subtitle_origin"] = origin
        (self.work / "ep.source.json").write_text(json.dumps(data), encoding="utf-8")

    def run_stage(self, raw=None, force=False, problem=""):
        calls = []

        def asr(cfg, wav, tmp, language):
            calls.append(language)
            return raw if raw is not None else reply()

        with mock.patch.object(TR, "extract_centre", return_value="centre") as centre, \
             mock.patch.object(TR, "runtime_problem", return_value=problem), \
             mock.patch.object(TR, "run_asr", side_effect=asr):
            got = TR.stage_transcribe(self.cfg, None, force)
        return got, calls, centre

    @property
    def asr_json(self):
        return json.loads((self.work / "ep.asr.json").read_text(encoding="utf-8"))


class TestWhichFilms(Base):
    def test_a_film_with_a_subtitle_or_a_polish_track_is_not_transcribed(self):
        for origin, said in (("subs", "subs"), (None, "track")):
            with self.subTest(origin=origin):
                self.sidecar(origin)
                got, calls, _ = self.run_stage()
                self.assertEqual((got, calls), ({"ep": said}, []))

    def test_mode_never_skips_it(self):
        self.sidecar(audio_language="eng")
        self.cfg.raw["translate"]["mode"] = "never"
        got, calls, _ = self.run_stage()
        self.assertEqual((got, calls), ({"ep": "never"}, []))


class TestWriting(Base):
    def test_it_writes_the_srt_and_the_raw_record_and_translate_then_finds_it(self):
        self.sidecar(audio_language="eng", source_language="")
        got, calls, _ = self.run_stage()
        self.assertEqual(got, {"ep": "written"})
        self.assertEqual(calls, ["en"])
        srt = self.work / "subs" / "ep.asr.eng.srt"
        self.assertEqual([c.raw for c in parse_srt(srt)], ["Where were you?", "I was at home."])
        d = self.asr_json
        self.assertEqual((d["language"], d["language_requested"], d["language_source"]),
                         ("eng", "en", "tag"))
        self.assertEqual(d["srt"], "subs/ep.asr.eng.srt")
        self.assertEqual(d["srt_sha256"], T.file_sha256(srt))
        self.assertEqual(d["source"], {"video": "ep.mkv", "audio": "centre"})
        self.assertEqual(len(d["segments"]), 2)
        self.assertTrue(all(s["dropped"] is None for s in d["segments"]))
        self.assertEqual(T.source_srt(self.cfg, "ep"), srt)
        self.assertEqual(T.source_info(self.cfg, "ep")["source_language"], "eng")

    def test_a_detected_language_names_the_file_and_is_said_to_be_detected(self):
        self.sidecar(audio_language="")
        got, calls, _ = self.run_stage(reply(language="de", texts=("Wo warst du?",)))
        self.assertEqual(calls, [""])
        self.assertTrue((self.work / "subs" / "ep.asr.ger.srt").is_file())
        self.assertEqual(self.asr_json["language_source"], "detected")

    def test_dropped_segments_are_recorded_with_their_reason(self):
        self.sidecar(audio_language="eng")
        raw = reply(texts=("Where were you?", "Thanks for watching!"))
        self.run_stage(raw)
        reasons = [s["dropped"] for s in self.asr_json["segments"]]
        self.assertIsNone(reasons[0])
        self.assertIn("blacklisted", reasons[1])

    def test_nothing_left_after_the_filters_writes_nothing_and_exits_non_zero(self):
        self.sidecar(audio_language="eng")
        with self.assertRaises(SystemExit) as cm:
            self.run_stage(reply(texts=("Thanks for watching!",)))
        self.assertIn("nothing survived the filters", str(cm.exception))
        self.assertFalse((self.work / "ep.asr.json").exists())

    def test_no_video_is_a_failure_named_per_episode(self):
        self.sidecar(audio_language="eng")
        self.video.unlink()
        with self.assertRaises(SystemExit) as cm:
            self.run_stage()
        self.assertIn("no source video", str(cm.exception))


class TestSkipping(Base):
    def test_a_second_run_touches_nothing_and_force_redoes_it(self):
        self.sidecar(audio_language="eng")
        self.run_stage()
        got, calls, centre = self.run_stage()
        self.assertEqual((got, calls), ({"ep": "kept"}, []))
        centre.assert_not_called()
        self.assertTrue(TR.is_done(self.cfg, "ep"))
        got, calls, _ = self.run_stage(force=True)
        self.assertEqual((got, calls), ({"ep": "written"}, ["en"]))

    def test_a_changed_language_is_transcribed_again(self):
        self.sidecar(audio_language="eng")
        self.run_stage()
        self.cfg.raw["translate"]["source_language"] = "ger"
        self.assertIn("'en'", TR.stale_transcript(self.cfg, "ep"))
        self.assertFalse(TR.is_done(self.cfg, "ep"))
        got, calls, _ = self.run_stage(reply(language="de", texts=("Hallo.",)))
        self.assertEqual((got, calls), ({"ep": "written"}, ["de"]))

    def test_a_transcript_in_a_new_language_replaces_the_old_file(self):
        self.sidecar(audio_language="eng")
        self.run_stage()
        self.cfg.raw["translate"]["source_language"] = "ger"
        self.run_stage(reply(language="de", texts=("Hallo.",)))
        self.assertEqual(sorted(p.name for p in (self.work / "subs").iterdir()),
                         ["ep.asr.ger.srt"])

    def test_polish_audio_is_not_sent_on_to_be_translated_into_itself(self):
        self.sidecar(audio_language="")
        with self.assertRaises(SystemExit) as cm:
            self.run_stage(reply(language="pl", texts=("Gdzie byłeś?",)))
        self.assertIn("Whisper hears 'pol'", str(cm.exception))
        self.assertFalse((self.work / "ep.asr.json").exists())

    def test_a_missing_srt_is_not_a_finished_transcript(self):
        self.sidecar(audio_language="eng")
        self.run_stage()
        (self.work / "subs" / "ep.asr.eng.srt").unlink()
        self.assertFalse(TR.is_done(self.cfg, "ep"))
        got, _, _ = self.run_stage()
        self.assertEqual(got, {"ep": "written"})

    def test_a_hand_corrected_transcript_is_copied_aside_before_force_replaces_it(self):
        self.sidecar(audio_language="eng")
        self.run_stage()
        srt = self.work / "subs" / "ep.asr.eng.srt"
        srt.write_text(srt.read_text(encoding="utf-8").replace("home", "Hulme"), encoding="utf-8")
        self.run_stage(force=True)
        kept = list(srt.parent.glob("ep.asr.eng.srt.bak-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn("Hulme", kept[0].read_text(encoding="utf-8"))


class TestRuntime(Base):
    def test_no_runtime_stops_before_any_audio_is_cut_with_one_actionable_message(self):
        self.sidecar(audio_language="eng")
        with mock.patch.object(TR, "extract_centre") as centre, \
             mock.patch.object(TR.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                TR.stage_transcribe(self.cfg, None)
        centre.assert_not_called()
        msg = str(cm.exception)
        self.assertIn("docker is not on PATH", msg)
        self.assertIn("transcribe.venv", msg)
        self.assertIn("docs/install.md", msg)
        self.assertFalse((self.work / "ep.asr.json").exists())

    def test_auto_takes_a_configured_venv_and_the_container_otherwise(self):
        venv = Path(self.tmp.name) / "venv"
        self.assertEqual(TR.runtime(self.cfg), ("docker", "asr"))
        self.cfg.raw["transcribe"]["container"] = "whisper"
        self.assertEqual(TR.runtime(self.cfg), ("docker", "whisper"))
        self.cfg.raw["transcribe"]["venv"] = str(venv)
        # Configured but not there: auto does not pretend, it falls back to the container.
        self.assertEqual(TR.runtime(self.cfg)[0], "docker")
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        self.assertEqual(TR.runtime(self.cfg), ("local", str(venv)))
        self.assertEqual(TR.runtime_problem(self.cfg), "")

    def test_local_without_a_venv_is_named(self):
        self.cfg.raw["transcribe"]["runner"] = "local"
        self.assertIn("transcribe.venv is empty", TR.runtime_problem(self.cfg))
        self.cfg.raw["transcribe"]["venv"] = "/nowhere/venv"
        self.assertIn("no interpreter at", TR.runtime_problem(self.cfg))

    def test_a_misspelled_runner_is_an_error(self):
        self.cfg.raw["transcribe"]["runner"] = "gpu"
        with self.assertRaises(ValueError):
            TR.runtime(self.cfg)

    def test_the_plan_never_waits_on_docker(self):
        with mock.patch.object(TR, "docker_problem") as probe, \
             mock.patch.object(TR.shutil, "which", return_value=None):
            self.assertEqual(TR.runtime_problem(self.cfg, probe=False), "docker is not on PATH")
        probe.assert_not_called()


class TestRunAsr(Base):
    def test_the_options_go_to_the_venvs_python_as_one_json_argument(self):
        venv = Path(self.tmp.name) / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        self.cfg.raw["transcribe"].update(venv=str(venv), model="medium", beam_size=3)
        seen = {}

        def local(python, script, inputs, out, extra, timeout, what):
            seen.update(python=python, script=script, inputs=inputs, opts=json.loads(extra[0]))
            return {"segments": []}

        wav = Path(self.tmp.name) / "a.wav"
        with mock.patch.object(TR, "run_script_local", side_effect=local):
            TR.run_asr(self.cfg, wav, Path(self.tmp.name), "en")
        self.assertEqual(seen["python"], venv / "bin" / "python")
        self.assertIs(seen["script"], TR._ASR_SCRIPT)
        self.assertEqual(seen["inputs"], [wav])
        o = seen["opts"]
        self.assertEqual((o["model"], o["beam_size"], o["language"]), ("medium", 3, "en"))
        self.assertTrue(o["vad"])
        self.assertFalse(o["condition_on_previous_text"])
        self.assertEqual(o["vad_parameters"]["min_silence_duration_ms"], 2000)

    def test_faster_whisper_missing_in_the_container_is_the_runtime_message(self):
        fail = SP.ScriptFailed("transcription", TR.NO_RUNTIME_EXIT, "No module named x")
        with mock.patch.object(TR, "runtime_problem", return_value=""), \
             mock.patch.object(TR, "run_script_docker", side_effect=fail):
            with self.assertRaises(TR.RuntimeMissing) as cm:
                TR.run_asr(self.cfg, Path("a.wav"), Path(self.tmp.name), "")
        self.assertIn("faster-whisper is not installed in the container 'asr'", str(cm.exception))

    def test_any_other_script_failure_stays_an_episode_failure(self):
        fail = SP.ScriptFailed("transcription", 1, "CUDA out of memory")
        with mock.patch.object(TR, "runtime_problem", return_value=""), \
             mock.patch.object(TR, "run_script_docker", side_effect=fail):
            with self.assertRaises(SP.ScriptFailed):
                TR.run_asr(self.cfg, Path("a.wav"), Path(self.tmp.name), "")

    def test_the_script_is_valid_python_and_imports_nothing_heavy_at_the_top_of_the_pipeline(self):
        compile(TR._ASR_SCRIPT, "<asr>", "exec")
        import lektorlib.transcribe as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        head = src.split("_ASR_SCRIPT = ")[0]
        for heavy in ("import torch", "faster_whisper", "ctranslate2"):
            self.assertNotIn(f"\nimport {heavy}", head)
            self.assertNotIn(f"\nfrom {heavy}", head)


class TestAlignmentReference(Base):
    def transcribed(self, language="eng"):
        self.sidecar(audio_language=language)
        (self.work / "subs").mkdir(exist_ok=True)
        srt = self.work / "subs" / f"ep.asr.{language}.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n", encoding="utf-8")
        (self.work / "ep.asr.json").write_text(json.dumps(
            {"srt": f"subs/{srt.name}", "language": language}), encoding="utf-8")
        return srt

    def test_an_english_transcript_is_the_reference_even_before_a_configured_pattern(self):
        """retime matches cards by start time, and only the transcript shares their grid."""
        srt = self.transcribed("eng")
        (self.work / "subs" / "ep.eng.srt").write_text("x", encoding="utf-8")
        self.assertEqual(SP.find_reference_srt(self.cfg, "ep"), srt)

    def test_a_transcript_in_another_language_is_not(self):
        self.transcribed("ger")
        self.assertIsNone(SP.find_reference_srt(self.cfg, "ep"))

    def test_an_sdh_stream_in_the_film_is_not_extracted_over_it(self):
        srt = self.transcribed("eng")
        with mock.patch.object(SP, "reference_stream") as rs, \
             mock.patch.object(SP, "extract_reference_srt") as ex:
            self.assertEqual(SP.choose_reference(self.cfg, "ep", self.video), srt)
        rs.assert_not_called()
        ex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
