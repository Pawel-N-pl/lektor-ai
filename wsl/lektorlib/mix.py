"""Timeline assembly, ducking, normalisation and the streaming encode.

Measured behaviour that must not drift: while the lektor speaks the original sits exactly
duck_db under its own natural level, and between lines the envelope releases back to the
program_gain_db plateau and stays there. That plateau is a level, not a no-op: at the shipped
program_gain_db of +4 the gaps are 4 dB above the source, so they are never bit-identical to it,
and the one-pole release needs several seconds to settle, which most inter-cue gaps do not last.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import say

CTRL_HZ = 200  # control rate of the duck envelope; see build_duck_envelope


@dataclass
class Clip:
    cue: dict
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    speed: float = 1.0
    atempo: float = 1.0
    # What the backend was asked for and what came back, kept per line so the voice stage can
    # report fits and overruns from measured audio instead of from the backend's own opinion.
    target_ms: int = 0
    achieved_ms: float = 0.0
    billable_chars: int = 0
    cached: bool = False

    @property
    def fits(self) -> bool:
        """Did the reading land inside the window it was given? Undershoot is a fit."""
        return self.target_ms <= 0 or self.achieved_ms <= self.target_ms

    @property
    def overrun_pct(self) -> float:
        if self.target_ms <= 0 or self.achieved_ms <= self.target_ms:
            return 0.0
        return 100.0 * (self.achieved_ms - self.target_ms) / self.target_ms


def db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def assemble_voice(cfg, clips: list[Clip], total_samples: int) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Lay the clips onto one timeline. Voice never overlaps voice: a long line pushes the next."""
    sr = cfg["mix"]["sample_rate"]
    track = np.zeros(total_samples, dtype=np.float32)
    spans: list[tuple[int, int]] = []
    cursor = 0
    pushed = 0

    for clip in clips:
        if clip.audio.size == 0:
            continue
        start = int(clip.cue["start_ms"] / 1000.0 * sr)
        if start < cursor:
            pushed += 1
            start = cursor
        end = start + clip.audio.size
        if end > total_samples:
            clip.audio = clip.audio[: max(0, total_samples - start)]
            end = start + clip.audio.size
            if clip.audio.size == 0:
                continue
        track[start:end] += clip.audio
        spans.append((start, end))
        cursor = end + int(0.06 * sr)  # a beat of air between lines

    if pushed:
        say(f"    {pushed} line(s) pushed later to avoid overlapping the previous one")
    return track, spans


def build_duck_envelope(cfg, spans: list[tuple[int, int]], total_samples: int) -> np.ndarray:
    """Gain curve for the original audio, derived from where the lektor actually speaks.

    Built at a 200 Hz control rate and interpolated up, so a 40 minute episode costs a couple
    of megabytes instead of a couple of gigabytes.
    """
    m = cfg["mix"]
    sr = m["sample_rate"]
    ctrl_hz = CTRL_HZ
    n = int(total_samples / sr * ctrl_hz) + 2
    active = np.zeros(n, dtype=np.float32)

    pre = int(m["pre_roll_ms"] / 1000.0 * ctrl_hz)
    post = int(m["post_roll_ms"] / 1000.0 * ctrl_hz)
    for s, e in spans:
        a = max(0, int(s / sr * ctrl_hz) - pre)
        b = min(n, int(e / sr * ctrl_hz) + post)
        active[a:b] = 1.0

    # Hold: keep the original down through a gap too short to be worth restoring it for.
    # Without this the envelope pumps - measured on one episode, 324 full recoveries of which 198
    # lasted under a second - and the film audio being yanked up and back down 11 dB between two
    # lines is far more audible than simply staying under the lektor until he is done talking.
    hold = int(m.get("hold_ms", 0.0) / 1000.0 * ctrl_hz)
    if hold > 0:
        edges = np.diff(active)
        rises = np.flatnonzero(edges > 0) + 1   # silence -> speech
        falls = np.flatnonzero(edges < 0) + 1   # speech -> silence
        for f in falls:
            nxt = rises[rises > f]
            if nxt.size and nxt[0] - f <= hold:
                active[f:nxt[0]] = 1.0

    # One-pole attack/release: fast to duck, slow to come back, the way a real ducker behaves.
    atk = math.exp(-1.0 / (m["attack_ms"] / 1000.0 * ctrl_hz))
    rel = math.exp(-1.0 / (m["release_ms"] / 1000.0 * ctrl_hz))
    env = np.zeros(n, dtype=np.float32)
    y = 0.0
    for i in range(n):
        target = active[i]
        coef = atk if target > y else rel
        y = target + coef * (y - target)
        env[i] = y

    # Both ends of the curve are levels for the original, expressed against its own natural
    # loudness: program_gain_db where the lektor is silent, duck_to_db underneath him.
    quiet = db_to_lin(m["program_gain_db"])
    ducked = db_to_lin(m["duck_to_db"])
    return (quiet + (ducked - quiet) * env).astype(np.float32)


def _decay_max(x: np.ndarray, coef: float, carry: float) -> np.ndarray:
    """Running max with exponential decay: y[n] = max(x[n], coef * y[n-1]), starting from carry.

    Written as a cumulative maximum rather than a sample loop: y[n] is max(x[k] * coef**(n-k)) over
    every k up to n, so the coef**-k factor can be lifted out, accumulated, and put back. That
    factor grows without bound over a long span, hence the segments – they are an overflow guard,
    not a reset, because each one is seeded with the value the previous one ended on.
    """
    if not 0.0 < coef < 1.0:
        return np.maximum(x, 0.0)
    seg = max(1, int(-20.0 / math.log(coef)))  # coef**-seg stays under e**20
    out = np.empty(x.size, dtype=np.float64)
    prev = carry
    for start in range(0, x.size, seg):
        part = x[start : start + seg]
        k = np.arange(part.size, dtype=np.float64)
        up = np.power(coef, -k)
        y = np.maximum.accumulate(part * up) * np.power(coef, k)
        y = np.maximum(y, prev * np.power(coef, k + 1.0))
        out[start : start + seg] = y
        prev = float(y[-1])
    return out


def limit_block(mixed: np.ndarray, ceiling: float, release_coef: float,
                carry: float) -> tuple[np.ndarray, float]:
    """Hold the mix under the ceiling, sample by sample, with the state carried between blocks.

    Instantaneous attack and a one-pole release, tracked as a gain REDUCTION so the release is a
    decay towards zero: r[n] = max(needed[n], release * r[n-1]), gain 1 - r. Because r is never
    below what the sample itself needs, the ceiling is exact.

    The scalar-per-block normalisation this replaced was neither: one transient pulled its whole
    10 s block down, the neighbouring block was scaled by an unrelated number, and the seam between
    them was a step in the gain – a click, in a stretch the ducker never touched.
    """
    peak = np.max(np.abs(mixed), axis=1).astype(np.float64)
    needed = 1.0 - ceiling / np.maximum(peak, 1e-12)
    np.maximum(needed, 0.0, out=needed)
    r = _decay_max(needed, release_coef, carry)
    mixed *= (1.0 - r).astype(np.float32)[:, None]
    return mixed, float(r[-1]) if r.size else carry


def measure_program_level(orig_path: Path) -> float:
    """RMS of the whole programme in dB.

    Every level in the mix is set against this rather than against dBFS. An absolute voice target
    made the lektor 20 dB louder than a quiet film and would have buried a loud one; a relative one
    behaves the same way on both.
    """
    acc = 0.0
    n = 0
    with sf.SoundFile(str(orig_path)) as f:
        while True:
            block = f.read(1 << 20, dtype="float32", always_2d=True)
            if block.shape[0] == 0:
                break
            acc += float(np.sum(np.square(block, dtype=np.float64)))
            n += block.size
    if n == 0:
        return -60.0
    return 20.0 * math.log10(max(1e-9, math.sqrt(acc / n)))


def normalize_voice(cfg, track: np.ndarray, spans: list[tuple[int, int]],
                    program_db: float) -> float:
    """Scale factor putting the spoken parts voice_over_program_db above the programme.

    Silence between lines is excluded, so a sparse episode does not push the voice up.
    """
    if not spans:
        return 1.0
    chunks = [track[s:e] for s, e in spans if e > s]
    speech = np.concatenate(chunks) if chunks else track
    rms = float(np.sqrt(np.mean(np.square(speech, dtype=np.float64))))
    if rms <= 1e-9:
        return 1.0
    target = db_to_lin(program_db + cfg["mix"]["voice_over_program_db"])
    return target / rms


def write_mix(cfg, orig_path: Path, voice: np.ndarray, env: np.ndarray,
              out_path: Path, total_samples: int) -> None:
    """Stream the mix straight into ffmpeg. Blockwise, so RAM stays flat regardless of runtime."""
    m = cfg["mix"]
    sr = m["sample_rate"]
    ceiling = db_to_lin(m["limiter_ceiling_db"])
    release_coef = math.exp(-1.0 / max(1.0, float(m.get("limiter_release_ms", 120.0)) / 1000.0 * sr))
    ctrl_hz = CTRL_HZ

    args = [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "pipe:0",
        "-c:a", cfg["encode"]["codec"], "-b:a", cfg["encode"]["bitrate"],
        str(out_path),
    ]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    block = sr * 10
    pos = 0
    reduction = 0.0  # limiter state, carried across blocks so the seam is not a step
    try:
        with sf.SoundFile(str(orig_path)) as src:
            resample = src.samplerate != sr
            while pos < total_samples:
                want = min(block, total_samples - pos)
                need = int(round(want * src.samplerate / sr)) if resample else want
                chunk = src.read(need, dtype="float32", always_2d=True)
                if chunk.shape[0] == 0:
                    chunk = np.zeros((want, 2), dtype=np.float32)
                if chunk.shape[1] == 1:
                    chunk = np.repeat(chunk, 2, axis=1)
                elif chunk.shape[1] > 2:
                    chunk = chunk[:, :2]
                if chunk.shape[0] != want:
                    idx = np.linspace(0, chunk.shape[0] - 1, want)
                    chunk = np.stack(
                        [np.interp(idx, np.arange(chunk.shape[0]), chunk[:, ch]) for ch in range(2)],
                        axis=1,
                    ).astype(np.float32)

                ctrl_idx = np.linspace(pos / sr * ctrl_hz, (pos + want) / sr * ctrl_hz, want)
                gain = np.interp(ctrl_idx, np.arange(env.size), env).astype(np.float32)

                mixed = chunk * gain[:, None]
                mixed += voice[pos : pos + want, None]

                if mixed.size:
                    mixed, reduction = limit_block(mixed, ceiling, release_coef, reduction)

                proc.stdin.write(mixed.astype(np.float32).tobytes())
                pos += want
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode() if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError(f"encode failed: {err[:400]}")
