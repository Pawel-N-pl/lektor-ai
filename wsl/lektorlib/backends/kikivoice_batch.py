#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The module that actually buys an episode: plan -> one paid task -> cut -> cache.

Why this file exists at all
---------------------------
Credits are not the scarce resource on this API. SLOTS ARE. Every accepted task
spends one of the ~30 between two GeeTest challenges, and every challenge is a
human being walking to a browser. A 759-cue episode rendered line by line is
~25 interruptions; the same episode at 12 cues per request is three. Nothing
else in this project moves that number.

The trick is that the API honours a pause marker inside the text, so twelve
subtitle lines separated by ``SEP`` come back as ONE clip with eleven very
regular ~1300 ms silences in it. This module groups the lines, buys the clip,
cuts it on those silences and drops the pieces into exactly the cache
``KikiVoiceBackend.fetch()`` reads from. After a successful prefetch every
render() is a disk read.

What it refuses to do
---------------------
* It never guesses a cut. ``split_batch`` either finds exactly N-1 markers in
  the absolute band around the ordered pause length, or it raises. A wrong cut
  puts the tail of one line onto the head of the next, and nothing downstream
  can notice.
* It never creates a task except through ``backend.request_task``, which reserves
  the slot, applies the model's character limit, handles 777 and books the
  credits - so both budget gates see every request, batches included.
* It never assumes a batch worked. ``prefetch`` returns a report that names
  every cue it failed to cover, and the caller checks the CACHE, not the report,
  before believing the episode is bought.

The cost of failure, stated plainly
-----------------------------------
When a batch cannot be cut, the characters are already paid for. The cascade
(halve, then render singly) buys the same text a second time. That is why every
failure is logged with its character count and why the report carries
``chars_wasted``: this is the one number that says whether batching is still
worth it on this material.

Standalone by design: the standard library plus ``kikivoice_client``. No
lektorlib import, so the whole cascade is testable offline.
"""

from __future__ import annotations

import array
import io
import sys
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

try:  # copied into lektorlib/backends/ next to the adapter
    from .kikivoice_client import (
        SEP, PAUSE_MS, CUT_MARGIN_MS, BatchLimits, Cue, KikiError, SplitError,
        BatchPlanError, plan_batches, split_batch,
    )
except ImportError:  # standalone, same directory
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from kikivoice_client import (  # noqa: F401
        SEP, PAUSE_MS, CUT_MARGIN_MS, BatchLimits, Cue, KikiError, SplitError,
        BatchPlanError, plan_batches, split_batch,
    )


def _stderr(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
#  WAV in, WAV out
# ---------------------------------------------------------------------------

class BatchAudioError(KikiError):
    """The batch clip is not something this module can cut."""


def decode_wav_mono(blob: bytes) -> tuple[array.array, int]:
    """PCM bytes -> (float samples in [-1, 1], sample rate).

    ``array('f')`` rather than a list: four bytes per sample instead of the
    eight-plus a Python float object costs, which is the difference between
    7 MB and 46 MB for one 76-second batch. It slices and indexes like a list,
    which is all ``split_batch`` needs.

    Only PCM WAV is accepted, and that is deliberate. The measured output of
    this API is WAV PCM s16le mono 24000 Hz; anything else means the config
    asked for mp3, and decoding mp3 would drag ffmpeg into a module that is
    otherwise pure standard library. Batching simply requires audio_format=wav.
    """
    try:
        with contextlib_closing(wave.open(io.BytesIO(blob), "rb")) as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception as exc:  # noqa: BLE001 - wave raises half a dozen types
        raise BatchAudioError(
            f"batch nie jest plikiem WAV PCM ({exc}). Batchowanie wymaga "
            f"voice.kikivoice.audio_format = \"wav\"."
        ) from None
    if width != 2:
        raise BatchAudioError(f"batch ma {width * 8} bitow na probke, obslugiwane jest 16.")
    pcm = array.array("h")
    pcm.frombytes(frames)
    if sys.byteorder == "big":
        pcm.byteswap()
    out = array.array("f")
    if channels == 1:
        out.extend(v / 32768.0 for v in pcm)
    else:
        # Downmix, so a stereo answer does not silently become half-speed audio.
        for i in range(0, len(pcm) - channels + 1, channels):
            out.append(sum(pcm[i:i + channels]) / (32768.0 * channels))
    return out, sr


def encode_wav_mono(samples, sr: int) -> bytes:
    """Float samples -> a PCM s16le mono RIFF file, the shape the cache holds."""
    pcm = array.array("h")
    for v in samples:
        x = int(round(v * 32767.0))
        pcm.append(-32768 if x < -32768 else (32767 if x > 32767 else x))
    if sys.byteorder == "big":
        pcm.byteswap()
    buf = io.BytesIO()
    with contextlib_closing(wave.open(buf, "wb")) as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def contextlib_closing(thing):
    """wave.Wave_read/Wave_write are context managers on 3.11+ but not everywhere."""
    import contextlib
    return contextlib.closing(thing)


# ---------------------------------------------------------------------------
#  The report
# ---------------------------------------------------------------------------

@dataclass
class PrefetchReport:
    """What a prefetch actually achieved. Never just "how many tasks".

    ``prewarm`` used to take an ``int`` back and print it, which says nothing
    about whether the episode is covered - a batch that failed to split spends
    a task and leaves its cues uncached. The caller must be able to see the
    hole, so this object names it: ``missing`` is the list of cue indices that
    are still not in the cache when prefetch returns.
    """

    cues_total: int = 0
    cues_covered: int = 0
    missing: list = field(default_factory=list)      # cue indices still uncached
    batches_planned: int = 0
    batches_cached: int = 0                          # skipped, already on disk
    tasks: int = 0                                   # API tasks actually created
    chars_bought: int = 0                            # characters sent, markers included
    chars_wasted: int = 0                            # characters paid for twice
    split_failures: int = 0
    singles: int = 0                                 # cues that fell back to one task each
    notes: list = field(default_factory=list)        # one line per failure, in order

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def overhead_pct(self) -> float:
        base = self.chars_bought - self.chars_wasted
        return 100.0 * self.chars_wasted / base if base > 0 else 0.0

    def summary(self) -> str:
        head = (f"{self.cues_covered}/{self.cues_total} kwestii w cache, "
                f"{self.tasks} zadan API"
                + (f", {self.batches_cached} batchy pominietych (juz kupione)"
                   if self.batches_cached else ""))
        if self.split_failures or self.singles:
            head += (f"; {self.split_failures} batchy nie dalo sie pociac, "
                     f"{self.singles} kwestii poszlo pojedynczo, narzut "
                     f"{self.chars_wasted} znakow ({self.overhead_pct:.0f}%)")
        if self.missing:
            head += f"; NIEPOKRYTE kwestie: {self.missing[:12]}"
            if len(self.missing) > 12:
                head += f" (+{len(self.missing) - 12})"
        return head

    # Legacy seam: the first version of attach_batcher documented `-> int`.
    def __int__(self) -> int:
        return int(self.tasks)


# ---------------------------------------------------------------------------
#  The batcher
# ---------------------------------------------------------------------------

class KikiBatcher:
    """Buys an episode in batches and files the pieces in the backend's cache.

    Usage, and the whole public surface:

        batcher = KikiBatcher()
        backend.attach_batcher(batcher)
        backend.prewarm(utterances)          # calls batcher.prefetch(backend, us)

    What it needs from the backend, all of it public and all of it exercised
    offline by test_kikivoice.py (R12, against a real KikiVoiceBackend):

        backend.max_text_length          int, the model's own limit
        backend.segment_cached(text)     bool, is this cue already bought
        backend.put_segment(text, wav)   file one cue's audio, atomically
        backend.request_task(text)       buy one task, through both budget gates

    ``request_task`` reserves the slot internally, so the batcher must NOT wrap
    it in ``reserve_task`` as well - that would book two slots for one request
    and bring the CAPTCHA forward by half.
    """

    def __init__(
        self,
        *,
        limits: Optional[BatchLimits] = None,
        separator: str = SEP,
        pause_ms: float = PAUSE_MS,
        cut_margin_ms: float = CUT_MARGIN_MS,
        max_cues: int = 12,
        min_cues: int = 4,
        gap_break_ms: int = 1500,
        budget_ratio: float = 0.85,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.limits = limits
        self.separator = separator
        self.pause_ms = float(pause_ms)
        self.cut_margin_ms = float(cut_margin_ms)
        self.max_cues = int(max_cues)
        self.min_cues = int(min_cues)
        self.gap_break_ms = int(gap_break_ms)
        self.budget_ratio = float(budget_ratio)
        self.log = log or _stderr
        self._lock = threading.Lock()

    # -- planning ---------------------------------------------------------

    def limits_for(self, backend) -> BatchLimits:
        """The batch geometry, derived from the model's own limit when possible.

        ``char_budget`` counts the separators, because ``max_text_length`` is
        checked by the server on the text as SENT. (Credits are charged on the
        text with the markers stripped - measured: 1198 characters with 23
        separators were billed as 922 - so the markers are free in credits and
        not free in budget. The conservative direction is the safe one.)
        """
        if self.limits is not None:
            return self.limits
        limit = int(getattr(backend, "max_text_length", 0) or 1000)
        return BatchLimits(
            char_budget=max(1, int(limit * self.budget_ratio)),
            hard_limit=limit,
            max_cues=self.max_cues,
            min_cues=self.min_cues,
            gap_break_ms=self.gap_break_ms,
            separator=self.separator,
        )

    def _cues_from(self, utterances: Iterable) -> tuple[list, dict, bool]:
        """(cues, original text by index, whether the subtitle grid is usable).

        The original text is kept beside the stripped one because the cache is
        keyed on what ``fetch()`` will ask for, which is ``u.text`` verbatim.
        Filing a stripped variant under a different key means paying twice for
        every line with a trailing space.
        """
        cues, originals = [], {}
        saw_gap = False
        for u in utterances:
            text = (getattr(u, "text", "") or "")
            if not text.strip():
                continue
            idx = int(getattr(u, "index", len(cues)))
            gap = getattr(u, "gap_before_ms", None)
            if gap is None:
                gap = 0
            elif int(gap) > 0:
                saw_gap = True
            originals[idx] = text
            cues.append(Cue(index=idx, text=text.strip(),
                            target_ms=getattr(u, "target_ms", None),
                            gap_before_ms=int(gap)))
        return cues, originals, saw_gap

    def _warn_about_grid(self, saw_gap: bool, cues: list) -> None:
        """The most expensive silent failure in this design, said out loud.

        Rule 4 of ``plan_batches`` puts batch boundaries on the SUBTITLE GRID,
        which is what makes the cache survive editing: retouching one line
        invalidates that line's batch and nothing else. It needs
        ``Utterance.gap_before_ms``, and lektor-ai's ``Utterance`` does not have
        that field (base.py). Without it every boundary is a function of
        ``max_cues`` alone, and deleting a single cue shifts the whole grid -
        measured on 759 cues: 0 of 64 batches survive, i.e. 18 549 characters
        re-bought at 2.0x for one deleted line.
        """
        if saw_gap or len(cues) <= self.min_cues:
            return
        self.log(
            "    kikivoice: !! Utterance nie niesie gap_before_ms (albo wszystkie sa zerowe).\n"
            "       Granice batchy oprze sie wtedy wylacznie o max_cues, wiec SKASOWANIE albo\n"
            "       DODANIE jednej kwestii uniewazni CALY odcinek, nie jeden batch.\n"
            "       Latka na stages.py jest w INTEGRACJA.md; edycja samego TEKSTU jednej\n"
            "       kwestii kosztuje jeden batch i bez latki."
        )

    # -- the entry point --------------------------------------------------

    def prefetch(self, backend, utterances) -> PrefetchReport:
        """Buy every uncached cue, in batches, and file the pieces in the cache."""
        cues, originals, saw_gap = self._cues_from(utterances)
        report = PrefetchReport(cues_total=len(cues))
        if not cues:
            return report
        self._warn_about_grid(saw_gap, cues)

        lim = self.limits_for(backend)
        try:
            batches = plan_batches(cues, lim)
        except BatchPlanError as exc:
            # Planning refused the episode. Nothing has been sent, so the honest
            # thing is to say so and let every cue go through single renders,
            # which is what the backend does with no batcher at all.
            self.log(f"    kikivoice: planowanie batchy odmowilo ({exc}) – "
                     f"odcinek pojdzie kwestia po kwestii")
            report.missing = [c.index for c in cues]
            return report
        report.batches_planned = len(batches)

        for batch in batches:
            todo = [c for c in batch.cues
                    if not backend.segment_cached(originals.get(c.index, c.text))]
            if not todo:
                # Resumability, and the reason Ctrl+C is cheap: this batch was bought and cut in
                # an earlier run, every piece is already on disk, so it costs nothing to skip.
                # The check is per CUE, not per batch key, because the cache is keyed on the cue
                # text - which is also what makes it survive a change to the batch grid.
                report.batches_cached += 1
                continue
            if len(todo) < len(batch.cues):
                # A run was killed between two put_segment calls of the same batch. The missing
                # pieces exist only inside that batch's clip, so the batch is bought again; the
                # pieces that are already on disk are simply overwritten with identical audio.
                # The window is the few milliseconds of a write loop, and the alternative - buying
                # the leftovers as a smaller batch - changes their prosody against their cached
                # neighbours, which is worse than the rare repeat.
                self.log(f"    kikivoice: batch {batch.indices[:4]}... byl pociety tylko "
                         f"czesciowo ({len(batch.cues) - len(todo)}/{len(batch.cues)} w cache) – "
                         f"kupuje go ponownie w calosci")
            self._buy(backend, batch.cues, originals, report, depth=0)

        covered = [c.index for c in cues
                   if backend.segment_cached(originals.get(c.index, c.text))]
        report.cues_covered = len(covered)
        report.missing = [c.index for c in cues if c.index not in set(covered)]
        self.log("    kikivoice: " + report.summary())
        if report.chars_wasted:
            self.log(f"    kikivoice: !! {report.chars_wasted} znakow kupionych po raz drugi "
                     f"przez nieudane ciecie – to sa realne kredyty, nie statystyka")
        return report

    # -- the cascade ------------------------------------------------------

    def _buy(self, backend, cues: list, originals: dict, report: PrefetchReport,
             depth: int) -> None:
        """Buy one group of cues, and fall back in halves when the cut fails.

        The cascade is: one task for the whole group -> two tasks for the halves
        -> one task per cue. Every step down re-buys characters that were
        already paid for, so every step down is logged with its size. There is
        no fourth step: a single cue that will not render is a cue the server
        refuses, not a cutting problem.
        """
        if not cues:
            return
        if len(cues) == 1:
            self._buy_single(backend, cues[0], originals, report)
            return

        text = self.separator.join(c.text for c in cues)
        try:
            blob = self._task(backend, text, report)
        except SystemExit:
            raise
        except KikiError as exc:
            report.notes.append(f"batch {[c.index for c in cues]}: zadanie padlo ({exc})")
            self.log(f"    kikivoice: batch {len(cues)} cue padl na zadaniu ({exc}) – "
                     f"dziele na polowy")
            self._halve(backend, cues, originals, report, depth)
            return

        try:
            samples, sr = decode_wav_mono(blob)
            segments = split_batch(
                samples, sr, len(cues),
                texts=[c.text for c in cues],
                pause_ms=self.pause_ms,
                cut_margin_ms=self.cut_margin_ms,
            )
        except (SplitError, BatchAudioError) as exc:
            report.split_failures += 1
            report.chars_wasted += len(text)
            note = (f"batch {[c.index for c in cues]} ({len(text)} zn.): {exc}")
            report.notes.append(note)
            self.log(f"    kikivoice: !! ciecie batcha nie wyszlo, {len(text)} znakow "
                     f"do odkupienia. {exc}")
            self._halve(backend, cues, originals, report, depth)
            return

        for cue, seg in zip(cues, segments):
            backend.put_segment(originals.get(cue.index, cue.text), encode_wav_mono(seg, sr))
        # Drop the batch audio before the next one is bought: one clip at a
        # time is the whole memory budget of this module.
        del segments, samples, blob

    def _halve(self, backend, cues: list, originals: dict, report: PrefetchReport,
               depth: int) -> None:
        if not cues:
            return
        if len(cues) == 1:
            self._buy_single(backend, cues[0], originals, report)
            return
        mid = len(cues) // 2
        for half in (cues[:mid], cues[mid:]):
            todo = [c for c in half
                    if not backend.segment_cached(originals.get(c.index, c.text))]
            if not todo:
                continue
            if len(half) == 1 or depth >= 1:
                # Two failed cuts on the same material is enough evidence: stop
                # paying for batches of it and render the remaining cues singly.
                for cue in todo:
                    self._buy_single(backend, cue, originals, report)
            else:
                self._buy(backend, half, originals, report, depth + 1)

    def _buy_single(self, backend, cue, originals: dict, report: PrefetchReport) -> None:
        text = originals.get(cue.index, cue.text)
        if backend.segment_cached(text):
            return
        try:
            blob = self._task(backend, text, report)
        except SystemExit:
            raise
        except KikiError as exc:
            report.notes.append(f"kwestia {cue.index}: {exc}")
            self.log(f"    kikivoice: kwestia {cue.index} nie wyszla ({exc}) – "
                     f"zostaje do pojedynczego renderu")
            return
        report.singles += 1
        report.chars_wasted += len(text)
        backend.put_segment(text, blob)

    def _task(self, backend, text: str, report: PrefetchReport) -> bytes:
        """One paid task, through the backend's own gates and accounting."""
        res = backend.request_task(text)
        audio = getattr(res, "audio", None) or getattr(res, "audio_bytes", None)
        if not audio:
            raise KikiError("zadanie skonczylo sie bez audio")
        with self._lock:
            report.tasks += 1
            report.chars_bought += len(text)
        return audio


__all__ = ["KikiBatcher", "PrefetchReport", "BatchAudioError",
           "decode_wav_mono", "encode_wav_mono"]
