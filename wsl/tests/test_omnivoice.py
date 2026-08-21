"""The two pieces of omnivoice that are arithmetic rather than a model, and must not drift.

Everything else in that backend needs a GPU and a 3 GiB checkpoint. These two do not, and they are
the two that decide whether a line lands in its window and whether it is paid for twice.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lektorlib.backends.omnivoice import OmniVoiceBackend, cache_key


class FakeConfig:
    """Just enough config for the parts that never touch the model."""

    def __init__(self, **omni):
        base = {"venv": "/nowhere", "reference_wav": "", "duration_pad_ms": 200.0,
                "duration_floor_ms": 400.0}
        base.update(omni)
        self.raw = {"voice": {"backend": "omnivoice", "omnivoice": base},
                    "timing": {"safety": 0.96, "max_atempo": 1.0},
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

def test_ask_subtracts_the_pad_and_applies_safety():
    """The window is not the ask: the model appends a constant 200 ms of silence."""
    b = backend()
    assert b._ask_for(3000) == (3000 * 0.96 - 200) / 1000.0


def test_ask_never_goes_below_the_floor():
    b = backend()
    # 500 ms of card would ask for 280 ms, which is a scramble rather than a sentence.
    assert b._ask_for(500) == 0.4


def test_no_window_means_no_duration_at_all():
    """target_ms None is 'read it naturally', not 'read it in zero seconds'."""
    b = backend()
    assert b._ask_for(None) is None
    assert b._ask_for(0) is None


def test_pad_is_configurable_because_it_is_a_measurement():
    b = backend(duration_pad_ms=0.0)
    assert b._ask_for(3000) == 3000 * 0.96 / 1000.0


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
