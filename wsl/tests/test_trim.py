"""The end-trim, and the one rule in it that a one-syllable line disagrees with.

trim_silence throws away a final run of voiced frames when it looks like an artefact the engine
appended. The shape it hunts - short, and behind a stretch of silence - is also the shape of a
Polish final plosive, whose closure is near-silence and whose release is a few tens of
milliseconds. These tests pin both sides: the measured artefact still goes, the released consonant
stays.
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lektorlib.audio import trim_silence

SR = 48000
FRAME = SR // 100          # trim_silence works in 10 ms frames


def build(*spans: tuple[float, int]) -> np.ndarray:
    """Audio from (amplitude, frames) pairs. Alternating tone and silence, nothing subtler."""
    out = []
    for amp, frames in spans:
        n = frames * FRAME
        if amp == 0.0:
            out.append(np.zeros(n, dtype=np.float32))
        else:
            t = np.arange(n, dtype=np.float32) / SR
            out.append((amp * np.sin(2 * np.pi * 200.0 * t)).astype(np.float32))
    return np.concatenate(out)


def ends_after_ms(audio: np.ndarray) -> float:
    return audio.size / SR * 1000.0


class FakeConfig:
    def __init__(self, trim):
        self.raw = {"voice": {"trim": trim}}

    def __getitem__(self, key):
        return self.raw[key]


def test_the_measured_artefact_is_still_dropped():
    """Speech, 200 ms of true silence, then 60 ms of level appended by the engine. That burst is
    what the rule was written for and it has to keep going: it plays as a hiss cut off mid-way."""
    audio = build((1.0, 100), (0.0, 20), (0.2, 6))
    out = trim_silence(audio, SR)
    assert ends_after_ms(out) < 1100.0          # 1000 ms of speech plus the keep padding


def test_a_final_plosive_release_is_not_an_artefact():
    """220 ms of vowel, a 110 ms closure, a 30 ms release. Under the old 100 ms gap and with no
    floor under what may be left standing, this dropped the release and "tak" played as "ta"."""
    audio = build((1.0, 22), (0.0, 11), (0.3, 3))
    out = trim_silence(audio, SR)
    assert ends_after_ms(out) > 350.0           # the release survived

    was = trim_silence(audio, SR, blip_gap_ms=100.0, blip_min_keep_ms=0.0)
    assert ends_after_ms(was) < 300.0           # ...and would not have, before


def test_nothing_is_dropped_when_too_little_speech_would_be_left():
    """The gap and the length both say artefact, but 200 ms is a syllable, not a line. Dropping
    here eats the word rather than something appended after it."""
    audio = build((1.0, 20), (0.0, 25), (0.3, 4))
    assert ends_after_ms(trim_silence(audio, SR)) > 400.0
    assert ends_after_ms(trim_silence(audio, SR, blip_min_keep_ms=0.0)) < 300.0


def test_the_config_reaches_the_rule():
    """[voice.trim] is what makes this tunable per project rather than per edit of audio.py."""
    audio = build((1.0, 22), (0.0, 11), (0.3, 3))
    loose = FakeConfig({"blip_gap_ms": 100.0, "blip_min_keep_ms": 0.0})
    assert ends_after_ms(trim_silence(audio, SR, cfg=loose)) < 300.0
    assert ends_after_ms(trim_silence(audio, SR, cfg=FakeConfig({}))) > 350.0
