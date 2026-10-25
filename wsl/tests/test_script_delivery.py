"""Handing a script to an interpreter that is not this one: the argv it gets and what comes back.

The forced aligner and the transcriber both run as a string fed on stdin to another python - a
venv's, or a container's - because the models they load must never be imported here. No such
interpreter exists on the machine these tests run on, so subprocess is replaced and only the
contract is checked: inputs, then the output path, then the script's own arguments; a non-zero
exit keeps its code; the container's scratch directory goes whatever happened.

    .venv/bin/python -m unittest discover -s wsl/tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import speech as SP  # noqa: E402


def done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.wav = self.dir / "a.wav"
        self.wav.write_bytes(b"RIFF")
        self.out = self.dir / "out.json"


class TestLocal(Base):
    def test_argv_is_inputs_then_output_then_the_scripts_own_and_the_script_goes_on_stdin(self):
        seen = {}

        def run(argv, input=None, **kw):
            seen["argv"], seen["input"] = argv, input
            self.out.write_text(json.dumps({"ok": 1}), encoding="utf-8")
            return done()

        with mock.patch.object(SP.subprocess, "run", side_effect=run):
            got = SP.run_script_local(Path("/v/bin/python"), "print(1)", [self.wav], self.out,
                                      ["7", "x"], 10, "thing")
        self.assertEqual(got, {"ok": 1})
        self.assertEqual(seen["argv"], ["/v/bin/python", "-", str(self.wav), str(self.out),
                                        "7", "x"])
        self.assertEqual(seen["input"], "print(1)")

    def test_a_non_zero_exit_keeps_its_code_and_the_tail_of_stderr(self):
        with mock.patch.object(SP.subprocess, "run", return_value=done(3, stderr="boom\nlast")):
            with self.assertRaises(SP.ScriptFailed) as cm:
                SP.run_script_local(Path("p"), "", [], self.out, [], 10, "thing")
        self.assertEqual(cm.exception.code, 3)
        self.assertIn("thing failed: boom\nlast", str(cm.exception))

    def test_no_output_is_an_error_not_an_empty_result(self):
        with mock.patch.object(SP.subprocess, "run", return_value=done()):
            with self.assertRaisesRegex(RuntimeError, "wrote no output"):
                SP.run_script_local(Path("p"), "", [], self.out, [], 10, "thing")


class TestDocker(Base):
    def test_no_docker_is_said_before_anything_is_copied(self):
        with mock.patch.object(SP.shutil, "which", return_value=None), \
             mock.patch.object(SP, "_run") as run:
            self.assertEqual(SP.docker_problem("asr"), "docker is not on PATH")
            with self.assertRaisesRegex(RuntimeError, "docker is not on PATH"):
                SP.run_script_docker("asr", "", [self.wav], self.out, [], 10, 10, "thing")
        run.assert_not_called()

    def test_a_stopped_container_is_named(self):
        with mock.patch.object(SP.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(SP, "_run", return_value=done(stdout="false\n")):
            self.assertEqual(SP.docker_problem("asr"), "container 'asr' is not running")

    def test_paths_are_rewritten_into_the_container_and_the_scratch_dir_always_goes(self):
        calls = []

        def _run(cmd, timeout):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"]:
                return done(stdout="true\n")
            if cmd[:2] == ["docker", "cp"] and cmd[2].startswith("asr:"):
                Path(cmd[3]).write_text("{\"n\": 2}", encoding="utf-8")
            return done()

        seen = {}

        def run(argv, input=None, **kw):
            seen["argv"] = argv
            return done()

        with mock.patch.object(SP.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(SP, "_run", side_effect=_run), \
             mock.patch.object(SP.subprocess, "run", side_effect=run):
            got = SP.run_script_docker("asr", "s", [self.wav], self.out, ["9"], 10, 10, "thing")
        self.assertEqual(got, {"n": 2})
        remote = calls[1][-1]
        self.assertTrue(remote.startswith("/tmp/lektor-out-"))
        self.assertEqual(seen["argv"], ["docker", "exec", "-i", "asr", "python3", "-",
                                        f"{remote}/a.wav", f"{remote}/out.json", "9"])
        self.assertEqual(calls[-1], ["docker", "exec", "asr", "rm", "-rf", remote])

    def test_a_failing_script_still_cleans_up(self):
        calls = []

        def _run(cmd, timeout):
            calls.append(cmd)
            return done(stdout="true\n")

        with mock.patch.object(SP.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(SP, "_run", side_effect=_run), \
             mock.patch.object(SP.subprocess, "run", return_value=done(3, stderr="nope")):
            with self.assertRaises(SP.ScriptFailed) as cm:
                SP.run_script_docker("asr", "s", [self.wav], self.out, [], 10, 10, "thing")
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(calls[-1][:5], ["docker", "exec", "asr", "rm", "-rf"])


if __name__ == "__main__":
    unittest.main()
