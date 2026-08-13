"""The one interface every TTS engine implements.

Duration fitting is backend knowledge, not pipeline knowledge. The pipeline hands over a line and
the window it must fit in; how that is achieved differs per engine and the pipeline must not care:

  elevenlabs  every call costs money -> PREDICT the duration from a measured chars/second, set
              voice_settings.speed, make ONE call, then atempo off the CACHED bytes if it missed.
  piper       free, deterministic and ~instant, and length_scale controls duration directly, so
              binary search length_scale until it hits target_ms. No calibration, no atempo.
  chatterbox  free but very slow, with no exact length control: generate naturally, then atempo.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import numpy as np


@dataclass
class Utterance:
    index: int
    text: str
    target_ms: int | None      # window the speech must fit; None = unconstrained
    # What the lektor said just before and says just after. An engine given them reads the line
    # as part of a continuous delivery instead of as an isolated sentence, which is what a lektor
    # is; a backend that bills per call must also put them in its cache key, because they change
    # the audio that comes back.
    prev_text: str = ""
    next_text: str = ""
    # A speed decided for the whole episode at once rather than by this line alone. None means
    # the backend decides for itself, which is the right answer for an engine that controls
    # duration directly.
    speed_hint: float | None = None


@dataclass
class Rendered:
    audio: np.ndarray          # float32 MONO at Rendered.sample_rate
    sample_rate: int
    speed: float               # rate the backend spoke at, 1.0 = natural
    atempo: float              # post-hoc stretch applied, 1.0 = none
    cached: bool
    billable_chars: int        # 0 for local backends


class Backend(abc.ABC):
    name: str
    billable: bool
    needs_calibration: bool    # only backends that must PREDICT duration need chars/second
    # Does this engine want to be told how fast to read, planned across the whole episode? True
    # for anything that predicts a duration and cannot revise it for free; False for an engine
    # that solves each line for the rate that fits, where a planned speed would only get in the
    # way of a measurement it can make itself.
    wants_speed_plan: bool = False

    def __init__(self, cfg):
        self.cfg = cfg

    @abc.abstractmethod
    def prepare(self) -> None:
        """Load the model or validate credentials. Called ONCE, before any render."""

    @abc.abstractmethod
    def render(self, u: Utterance) -> Rendered:
        """Speak one line. Called from several threads at once, up to max_concurrency."""

    @property
    @abc.abstractmethod
    def max_concurrency(self) -> int:
        ...

    def close(self) -> None:
        """Release whatever prepare() acquired. Safe to call twice."""

    def set_chars_per_second(self, cps: float) -> None:
        """Tell a predicting backend the rate the script was budgeted against.

        The script file records the chars/second in force when it was written, and a backend that
        predicts durations must use that same number or its speed factors will not match the
        budgets. Backends with needs_calibration = False ignore this.
        """
