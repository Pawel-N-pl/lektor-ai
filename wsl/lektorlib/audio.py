"""ffmpeg helpers shared by every backend: decode, time-stretch, resample.

All of it is free. A backend that misjudged a duration should stretch here rather than pay for a
second generation.
"""

from __future__ import annotations

import re
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

def apply_shelf(cfg, audio: np.ndarray, sr: int, gain_db: float, freq: float = 4000.0) -> np.ndarray:
    """High shelf on the lektor, so a voice can be matched in tone and not just in level.

    Two voices at the same loudness do not sound equally present: measured band by band at equal
    RMS, one candidate had 3.6 dB less energy above 4 kHz than the one it replaced, which is where
    consonants live. The result reads as "further away and quieter" and cannot be fixed with the
    fader - turning it up only makes the mud louder. Shelving the top back restores the balance at
    the same loudness.
    """
    if abs(gain_db) < 0.05 or audio.size == 0:
        return audio
    args = [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
        "-af", f"highshelf=f={freq:g}:g={gain_db:g}",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "pipe:1",
    ]
    p = subprocess.run(args, input=audio.astype(np.float32).tobytes(), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"shelf failed: {p.stderr.decode()[:300]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def measure_lufs(cfg, audio: np.ndarray, sr: int) -> float:
    """Integrated loudness (BS.1770 K-weighted), which is what a listener compares, not RMS."""
    if audio.size == 0:
        return float("nan")
    args = [cfg.ffmpeg, "-hide_banner", "-nostats", "-f", "f32le", "-ar", str(sr), "-ac", "1",
            "-i", "pipe:0", "-af", "ebur128=peak=none", "-f", "null", "-"]
    p = subprocess.run(args, input=audio.astype(np.float32).tobytes(), capture_output=True)
    hits = re.findall(r"I:\s*(-?\d+\.\d+)\s*LUFS", p.stderr.decode())
    return float(hits[-1]) if hits else float("nan")


def find_pauses(audio: np.ndarray, sr: int, floor_db: float = -30.0,
                min_ms: float = 60.0) -> list[tuple[int, int]]:
    """Low-level stretches INSIDE an utterance, in samples.

    Not digital silence: the engine's pauses sit around -30 dB of the clip's peak, breath and room
    rather than nothing. A -42 dB threshold finds none of them, which is how they went unnoticed -
    measured at the right threshold, a line with two commas spends 23% of its length not speaking.
    """
    if audio.size == 0:
        return []
    frame = max(1, int(0.01 * sr))
    n = audio.size // frame
    if n < 5:
        return []
    peaks = np.abs(audio[: n * frame].reshape(n, frame)).max(axis=1)
    top = float(peaks.max())
    if top <= 1e-6:
        return []
    quiet = peaks <= top * (10.0 ** (floor_db / 20.0))
    out: list[tuple[int, int]] = []
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * 10 >= min_ms:
                out.append((start * frame, i * frame))
            start = None
    if start is not None and (n - start) * 10 >= min_ms:
        out.append((start * frame, n * frame))
    return out


def shorten_pauses(cfg, audio: np.ndarray, sr: int) -> np.ndarray:
    """Compress the excess of every internal pause above the floor where a pause stops being heard.

    Scaling every pause by the same fraction would be wrong: it drags a sentence boundary down to
    the length of a comma and the sentence stops ending. Instead each pause keeps the floor and
    gives up a fraction of whatever it has ABOVE it, so a long pause stays longer than a short one
    and the hierarchy the engine built survives.

        new = floor + (old - floor) * ratio

    Splices are crossfaded, because a cut at an arbitrary sample is a step and a step is a click -
    the same lesson the end-trimming already learned.
    """
    m = cfg["mix"]
    ratio = float(m.get("pause_ratio", 1.0))
    if ratio >= 0.999 or audio.size == 0:
        return audio
    floor = int(float(m.get("pause_floor_ms", 120.0)) / 1000.0 * sr)
    fade = max(2, int(0.006 * sr))
    spans = find_pauses(audio, sr, float(m.get("pause_floor_db", -30.0)),
                        float(m.get("pause_min_ms", 60.0)))
    if not spans:
        return audio

    pieces: list[np.ndarray] = []
    prev = 0
    for a, b in spans:
        keep = floor + int((b - a - floor) * ratio) if b - a > floor else b - a
        keep = max(0, min(keep, b - a))
        pieces.append(audio[prev:a])
        if keep:
            pieces.append(audio[a : a + keep])
        prev = b
    pieces.append(audio[prev:])

    out = pieces[0]
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade))).astype(np.float32)
    for nxt in pieces[1:]:
        if out.size < fade or nxt.size < fade:
            out = np.concatenate([out, nxt])
            continue
        head = out[-fade:] * ramp[::-1] + nxt[:fade] * ramp
        out = np.concatenate([out[:-fade], head, nxt[fade:]])
    return out.astype(np.float32)
