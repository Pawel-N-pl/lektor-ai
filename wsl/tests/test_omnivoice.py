"""The two pieces of omnivoice that are arithmetic rather than a model, and must not drift.

Everything else in that backend needs a GPU and a 3 GiB checkpoint. These two do not, and they are
the two that decide whether a line lands in its window and whether it is paid for twice.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lektorlib.backends.base import Utterance
from lektorlib.backends.omnivoice import OmniVoiceBackend, cache_key


class FakeConfig:
    """Just enough config for the parts that never touch the model."""

    def __init__(self, **omni):
        base = {"venv": "/nowhere", "reference_wav": "", "duration_pad_ms": 0.0,
                "duration_floor_ms": 400.0}
        base.update(omni)
        self.raw = {"voice": {"backend": "omnivoice", "omnivoice": base},
                    "timing": {"safety": 0.96, "max_atempo": 1.0, "max_speed": 1.2,
                               "overhead_ms": 0.0, "ms_per_char": 67.0, "numeral_drag": 1.2},
                    "mix": {"sample_rate": 48000}}

    def __getitem__(self, key):
        return self.raw[key]

    def backend_cfg(self, name=None):
        return self.raw["voice"]["omnivoice"]

    @property
    def cache_dir(self):
        return pathlib.Path("/tmp")


def backend(**omni):
    return OmniVoiceBackend(FakeConfig(**omni))


# -- the ask -----------------------------------------------------------------

def utt(text, target_ms):
    return Utterance(index=0, text=text, target_ms=target_ms)


def test_the_ask_is_the_window_and_the_pad_is_not_subtracted():
    """The regression this exists for. The model's 200 ms tail is silence that trim_silence takes
    off, so subtracting it from the ask as well spends it twice and every line comes out short."""
    b = backend()
    long_line = "Zbliż się do Melindy lub córki, a dopilnuję, byś tego pożałował."
    assert b._ask_for(utt(long_line, 6129)) == 6129 * 0.96 / 1000.0


def test_a_tight_window_is_floored_at_max_speed_rather_than_scrambled():
    """A 539 ms card for 13 characters would need 24 chars/s. The floor is the same ceiling every
    other engine obeys: the fitted duration model at timing.max_speed."""
    b = backend()
    got = b._ask_for(utt("Carla Marino.", 539))
    assert got == pytest.approx(13 * 67.0 / 1.2 / 1000.0)
    assert got > 539 * 0.96 / 1000.0      # deliberately overruns rather than gabbling


def test_a_roomy_window_is_not_dragged_up_by_the_floor():
    b = backend()
    assert b._ask_for(utt("Tak.", 4000)) == 4000 * 0.96 / 1000.0


def test_no_window_means_no_duration_at_all():
    """target_ms None is 'read it naturally', not 'read it in zero seconds'."""
    b = backend()
    assert b._ask_for(utt("Tak.", None)) is None
    assert b._ask_for(utt("Tak.", 0)) is None


# -- the cache key -----------------------------------------------------------

PARAMS = {"model": "k2-fsa/OmniVoice", "dtype": "bfloat16", "language": "pl",
          "num_step": 32, "seed": 1234}


def test_duration_is_part_of_the_key():
    """Unlike chatterbox, the window changes the AUDIO here, so it cannot be left out.

    Chatterbox generates naturally and stretches afterwards, so one line is one take whatever
    window it lands in. This model is told the length and speaks differently to hit it; sharing a
    key between two windows would serve one window's take for the other's slot.
    """
    a = cache_key("Nie wyrobię.", PARAMS, "ref", 1.20)
    b = cache_key("Nie wyrobię.", PARAMS, "ref", 1.80)
    assert a != b


def test_key_survives_a_window_that_moved_by_a_hair():
    """Rounded to 10 ms: finer than the model's own accuracy, so a one-sample drift is free."""
    assert cache_key("Tak.", PARAMS, "ref", 1.2000) == cache_key("Tak.", PARAMS, "ref", 1.2004)


def test_reference_clip_is_part_of_the_key():
    """Re-cutting the reference is a different voice, and every cached line is stale."""
    assert cache_key("Tak.", PARAMS, "refA", 1.2) != cache_key("Tak.", PARAMS, "refB", 1.2)


def test_generation_settings_are_part_of_the_key():
    other = dict(PARAMS, num_step=16)
    assert cache_key("Tak.", PARAMS, "ref", 1.2) != cache_key("Tak.", other, "ref", 1.2)


def test_natural_and_windowed_are_different_takes():
    assert cache_key("Tak.", PARAMS, "ref", None) != cache_key("Tak.", PARAMS, "ref", 1.2)


# -- the contract the pipeline reads -----------------------------------------

def test_it_declares_itself_as_an_engine_that_needs_no_rate():
    """needs_calibration False is what makes `lektor.py calibrate` skip it, and wants_speed_plan
    False is what stops the episode-wide speed planner fighting the duration it is given."""
    b = backend()
    assert b.needs_calibration is False
    assert b.wants_speed_plan is False
    assert b.billable is False
    assert b.max_concurrency == 1


# -- the truncation guard ----------------------------------------------------

def test_a_stub_is_rejected_however_much_audio_it_technically_is():
    """110 ms of audio for a whole sentence is not brisk delivery, it is a failed generation.
    The old constant 50 ms floor waved 100 of 759 such takes through in one episode."""
    b = backend()
    assert not b._long_enough("Z całym szacunkiem,", 0.110)


def test_the_line_that_started_this_is_rejected():
    """"Nie. To było FBI." asked for 1113 ms, returned 870 ms, and plays as "to było F".
    17 characters need 17 * 67 / 1.2 = 949 ms at the fastest rate this project allows."""
    b = backend()
    assert not b._long_enough("Nie. To było FBI.", 0.870)
    assert b._long_enough("Nie. To było FBI.", 0.960)


def test_a_full_length_take_is_accepted():
    b = backend()
    assert b._long_enough("Nie. To było FBI.", 1.113)


def test_the_floor_scales_with_the_text_not_with_a_constant():
    """A long line needs proportionally more before it counts as spoken."""
    b = backend()
    long_line = "Zbliż się do Melindy lub córki, a dopilnuję, byś tego pożałował."
    assert not b._long_enough(long_line, 1.0)
    assert b._long_enough(long_line, 3.6)
