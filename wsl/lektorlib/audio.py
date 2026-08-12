"""ffmpeg helpers shared by every backend: decode, time-stretch, resample.

All of it is free. A backend that misjudged a duration should stretch here rather than pay for a
second generation.
"""

from __future__ import annotations

import subprocess

import numpy as np


def atempo_chain(factor: float) -> list[str]:
    """atempo is only valid in [0.5, 2.0]; chain filters for anything beyond."""
    chain = []
    remaining = factor
    while remaining > 2.0:
        chain.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    chain.append(f"atempo={remaining:.4f}")
    return chain


def decode_mono(cfg, data: bytes, sr: int, atempo: float | None = None) -> np.ndarray:
    """Encoded audio bytes -> mono float32 at sr, optionally time-stretched. Costs no credits."""
    args = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if atempo and abs(atempo - 1.0) > 1e-3:
        args += ["-filter:a", ",".join(atempo_chain(atempo))]
    args += ["-f", "f32le", "-ar", str(sr), "-ac", "1", "pipe:1"]

    p = subprocess.run(args, input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"decode failed: {p.stderr.decode()[:300]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def trim_silence(audio: np.ndarray, sr: int, floor_db: float = -45.0,
                 keep_ms: float = 40.0, fade_ms: float = 8.0) -> np.ndarray:
    """Cut the dead air a TTS engine pads around an utterance.

    Measured over 180 real lines: 44 ms of silence before the speech and 323 ms after it. That
    tail is most of the fixed per-utterance cost in the duration model, and it is pure loss here,
    because the gap between two lektor lines is set by the subtitle timings, not by the engine.
    Trimming it takes the overhead from 414 ms to about 47 ms and hands the difference back to
    every short cue, which is where the timing actually fails.

    keep_ms of padding stays on both ends so nothing is clipped into a hard edge.
    """
    if audio.size == 0:
        return audio
    frame = max(1, int(0.01 * sr))
    n = audio.size // frame
    if n < 3:
        return audio
    peaks = np.abs(audio[: n * frame].reshape(n, frame)).max(axis=1)
    top = float(peaks.max())
    if top <= 1e-6:
        return audio
    voiced = np.where(peaks > top * (10.0 ** (floor_db / 20.0)))[0]
    if voiced.size == 0:
        return audio
    pad = int(keep_ms / 1000.0 * sr)
    start = max(0, voiced[0] * frame - pad)
    end = min(audio.size, (voiced[-1] + 1) * frame + pad)
    out = audio[start:end].copy()

    # Trimming lands on an arbitrary sample, not a zero crossing: measured over 60 real clips, 4 of
    # them ended on an amplitude up to 0.138. Dropped into the silence between lines that is a step,
    # and a step is a click. A few ms of cosine taper costs nothing audible and removes it.
    fade = min(int(fade_ms / 1000.0 * sr), out.size // 2)
    if fade > 1:
        ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade))).astype(np.float32)
        out[:fade] *= ramp
        out[-fade:] *= ramp[::-1]
    return out


def stretch_pcm(cfg, audio: np.ndarray, sr: int, atempo: float) -> np.ndarray:
    """Same stretch, for backends that hand back samples instead of an encoded blob."""
    if abs(atempo - 1.0) <= 1e-3 or audio.size == 0:
        return audio
    args = [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
        "-filter:a", ",".join(atempo_chain(atempo)),
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "pipe:1",
    ]
    p = subprocess.run(args, input=audio.astype(np.float32).tobytes(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"stretch failed: {p.stderr.decode()[:300]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def resample_linear(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Rate-match a rendered clip to the mix rate.

    Linear interpolation is enough here: the local backends run at 22.05 kHz, the mix at 48 kHz,
    and upsampling invents no detail whichever kernel is used.
    """
    if src_sr == dst_sr or audio.size == 0:
        return audio
    n = int(round(audio.size * dst_sr / src_sr))
    idx = np.linspace(0, audio.size - 1, n)
    return np.interp(idx, np.arange(audio.size), audio).astype(np.float32)
