"""The mix's layout logic, and the two ways it is allowed to fail.

These are the parts of the 5.1 renderer that used to live in `tools/render51.py` with no tests at
all. What is pinned here is what a mistake would cost: a level 3 dB out on the only thing the
listener is there for, a config typo that shears every frame, or - the worst one - a mix that
looks finished and has no film underneath the voice.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lektorlib import mix as M  # noqa: E402


class Cfg:
    def __init__(self, **over):
        self.raw = {
            "mix": {
                "sample_rate": 48000,
                "channel_layout": "5.1(side)",
                "voice_channels": [2],
                "voice_channel_comp_db": 3.0,
                "level_reference": "stereo_fold",
                "duck_mode": "spectral",
                "duck_band_hz": [300, 4000],
                "duck_to_db": -10.0,
                "duck_mid_db": -14.0,
                "duck_rest_db": -3.0,
                "program_gain_db": 4.0,
                "voice_over_program_db": 7.0,
                "attack_ms": 90,
                "release_ms": 600.0,
                "pre_roll_ms": 450,
                "post_roll_ms": 350,
                "hold_ms": 1500.0,
                "limiter_ceiling_db": -1.0,
                **over.pop("mix", {}),
            },
            "encode": {"codec": "ac3", "bitrate": "256k", "extension": "ac3",
                       "surround": {"codec": "eac3", "bitrate": "768k", "extension": "eac3"}},
            "paths": {"ffmpeg_wsl": "ffmpeg", "ffprobe_wsl": "ffprobe"},
        }
        self.raw["mix"].update(over)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def ffmpeg(self):
        return "ffmpeg"


class TestLayout(unittest.TestCase):
    def test_surround_is_surround_and_stereo_is_not(self):
        self.assertTrue(M.is_surround(Cfg()))
        self.assertFalse(M.is_surround(Cfg(channel_layout="stereo")))
        self.assertFalse(M.is_surround(Cfg(channel_layout="")))

    def test_the_encode_profile_follows_the_layout(self):
        """ac3 at 256k cannot carry six channels credibly, so the profile is not shared."""
        self.assertEqual(M.encode_profile(Cfg())["extension"], "eac3")
        self.assertEqual(M.encode_profile(Cfg(channel_layout="stereo"))["extension"], "ac3")

    def test_the_voice_goes_to_the_centre_in_surround_and_to_both_in_stereo(self):
        self.assertEqual(M.voice_channels(Cfg()), [2])
        self.assertEqual(M.voice_channels(Cfg(channel_layout="stereo")), [0, 1])

    def test_the_centre_compensation_applies_only_when_one_channel_carries_the_voice(self):
        """Two correlated channels sum 3 dB; one does not, and the levels were tuned on the fold."""
        self.assertEqual(M.voice_comp_db(Cfg()), 3.0)
        self.assertEqual(M.voice_comp_db(Cfg(channel_layout="stereo")), 0.0)
        self.assertEqual(M.voice_comp_db(Cfg(voice_channels=[0, 1, 2])), 0.0)

    def test_normalize_voice_includes_the_compensation(self):
        track = np.ones(1000, dtype=np.float32)
        spans = [(0, 1000)]
        centre = M.normalize_voice(Cfg(), track, spans, -20.0)
        stereo = M.normalize_voice(Cfg(channel_layout="stereo"), track, spans, -20.0)
        self.assertAlmostEqual(20.0 * np.log10(centre / stereo), 3.0, places=6)

    def test_an_unbuildable_layout_is_refused_rather_than_guessed(self):
        cfg = Cfg(channel_layout="7.1")
        with self.assertRaises(RuntimeError) as caught:
            M.write_mix(cfg, Path("/nonexistent.mkv"), np.zeros(10, dtype=np.float32),
                        {"all": np.zeros(10, dtype=np.float32)}, Path("/dev/null"), 10)
        self.assertIn("7.1", str(caught.exception))


class TestEnvelopes(unittest.TestCase):
    def test_broadband_gives_one_envelope(self):
        env = M.build_duck_envelopes(Cfg(duck_mode="broadband"), [(0, 48000)], 96000)
        self.assertEqual(list(env), ["all"])

    def test_spectral_gives_two_with_different_floors(self):
        env = M.build_duck_envelopes(Cfg(), [(0, 48000)], 96000)
        self.assertEqual(sorted(env), ["mid", "rest"])
        # The speech band is pulled further down than the rest; that is the whole idea.
        self.assertLess(env["mid"].min(), env["rest"].min())

    def test_the_config_is_left_exactly_as_it_was_found(self):
        """The renderer this replaced built the second envelope by mutating the config in place."""
        cfg = Cfg()
        M.build_duck_envelopes(cfg, [(0, 48000)], 96000)
        self.assertEqual(cfg["mix"]["duck_to_db"], -10.0)


class TestDecoderFailures(unittest.TestCase):
    """A decode that dies must never pass for a mix. It is the one failure nothing else catches."""

    def test_reap_raises_with_what_ffmpeg_said(self):
        proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", "/nonexistent-file.mkv",
                                 "-f", "null", "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdout.read()
        with self.assertRaises(RuntimeError) as caught:
            M.reap(proc, "a file that is not there")
        self.assertIn("a file that is not there", str(caught.exception))

    def test_reap_is_quiet_on_a_clean_exit(self):
        proc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                                 "anullsrc=r=48000:cl=stereo", "-t", "0.1", "-f", "null", "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdout.read()
        M.reap(proc, "silence")

    def test_measure_program_level_refuses_to_invent_a_number(self):
        with self.assertRaises(RuntimeError):
            M.measure_program_level(Cfg(), Path("/nonexistent-file.mkv"))


if __name__ == "__main__":
    unittest.main()
