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
    """Lay the clips onto one timeline. Voice never overlaps voice: a long line pushes the next.

    `delay_ms` on a cue is the only thing that moves a line off its card, and it only ever moves
    it LATER: it is written by the script stage for the handful of lines whose actor had not
    started speaking yet, and it is already bounded by the room left in that line's own window, so
    it must not increase the number of lines pushed by the cursor below.
    """
    sr = cfg["mix"]["sample_rate"]
    track = np.zeros(total_samples, dtype=np.float32)
    spans: list[tuple[int, int]] = []
    cursor = 0
    pushed = 0
    delayed = 0

    for clip in clips:
        if clip.audio.size == 0:
            continue
        delay = max(0, int(clip.cue.get("delay_ms", 0) or 0))
        start = int((clip.cue["start_ms"] + delay) / 1000.0 * sr)
        if start < cursor:
            pushed += 1
            start = cursor
        elif delay:
            # Counted only when the delay is what actually placed the line. A delayed line that
            # still lands inside the previous line's tail was placed by the cursor, and reporting
            # it as both would be reporting it twice.
            delayed += 1
        end = start + clip.audio.size
        if end > total_samples:
            clip.audio = clip.audio[: max(0, total_samples - start)]
            end = start + clip.audio.size
            if clip.audio.size == 0:
                continue
        track[start:end] += clip.audio
        spans.append((start, end))
        cursor = end + int(0.06 * sr)  # a beat of air between lines

    if delayed:
        say(f"    {delayed} line(s) placed on the actor's first word rather than on their card")
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


def build_duck_envelopes(cfg, spans: list[tuple[int, int]], total_samples: int
                         ) -> dict[str, np.ndarray]:
    """One envelope for broadband ducking, two for spectral.

    Spectral ducking pulls the band the lektor competes for much further down than the rest, so
    the film keeps its weight and its air while the midrange gets out of the way. The two curves
    have the same shape and differ only in how far their floor sits, which is a level the builder
    above reads out of the config - so they are produced by pointing it at each floor in turn,
    with the config restored afterwards. Passing the floor as an argument would be cleaner still,
    but `build_duck_envelope` is also the A/B rig's entry point and its signature is not ours to
    break.
    """
    m = cfg["mix"]
    if m.get("duck_mode", "broadband") != "spectral":
        return {"all": build_duck_envelope(cfg, spans, total_samples)}
    keep = m["duck_to_db"]
    try:
        m["duck_to_db"] = m["duck_mid_db"]
        mid = build_duck_envelope(cfg, spans, total_samples)
        m["duck_to_db"] = m["duck_rest_db"]
        rest = build_duck_envelope(cfg, spans, total_samples)
    finally:
        m["duck_to_db"] = keep
    return {"mid": mid, "rest": rest}


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


def source_channels(cfg, source: Path) -> int:
    """How many channels the source audio really has, straight from ffprobe."""
    probe = cfg.raw["paths"].get("ffprobe_wsl") or "ffprobe"
    r = subprocess.run([probe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=channels", "-of", "csv=p=0", str(source)],
                       capture_output=True, text=True)
    try:
        return int((r.stdout.strip().splitlines() or ["0"])[0])
    except ValueError:
        return 0


def decode_stream(cfg, source: Path, sr: int, channels: int, filtergraph: str = "",
                  label: str = "") -> subprocess.Popen:
    """One decode of the source audio as raw interleaved float32, optionally through a filter.

    ffmpeg rather than soundfile, because the source is as likely to be the film's own E-AC-3 5.1
    inside an mkv as it is to be a FLAC downmix, and the mix should not care which.
    """
    args = [cfg.ffmpeg, "-v", "error", "-i", str(source)]
    if filtergraph:
        args += ["-filter_complex", filtergraph, "-map", label]
    else:
        args += ["-map", "0:a:0"]
    args += ["-ar", str(sr), "-ac", str(channels), "-f", "f32le", "pipe:1"]
    # stderr is kept, not discarded. A decoder that dies mid-file would otherwise be indis-
    # tinguishable from one that reached the end: the reader below zero-pads what it did not get,
    # and the result is a full-length, perfectly encoded track with no film underneath the lektor.
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def reap(proc: subprocess.Popen, what: str) -> None:
    """Wait for a decoder and raise with what ffmpeg said if it failed."""
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"decoding {what} failed: {err.strip()[-400:] or 'no output'}")


def crossover_graphs(band: list[int]) -> tuple[tuple[str, str], tuple[str, str]]:
    """DEPRECATED - ffmpeg's `acrossover` does not sum back to its input. Use `BandSplitter`.

    The theory says a Linkwitz-Riley crossover sums to allpass, so the two halves recombine to the
    original whenever their gains are equal. Measured on real film audio, this filtergraph does
    not: summing the bands leaves a residue 4.4 dB LOUDER than the signal itself, at identical
    RMS - the signature of a phase rotation, not of a level error. Summing all three bands inside
    one ffmpeg process gives the same 4.4 dB, so it is `acrossover` itself and not the two-decode
    split. Optimally time-aligned, the sum still only nulls 5.5 dB against the input.

    Audibly that is comb filtering over the entire programme, ducked or not, and it is what this
    pipeline shipped until it was measured. Kept only so the measurement above can be reproduced.
    """
    split = f"acrossover=split={band[0]} {band[1]}"
    mid = f"[0:a]{split}[a][b][c];[a]anullsink;[c]anullsink"
    rest = f"[0:a]{split}[a][b][c];[b]anullsink;[a][c]amix=inputs=2:normalize=0[r]"
    return (mid, "[b]"), (rest, "[r]")


def design_bandpass(band: list[int], sr: int, taps: int = 1025) -> np.ndarray:
    """A linear-phase FIR band-pass: windowed sinc, odd length, so the delay is an integer.

    Built here rather than pulled from scipy, which is not a dependency of this project and would
    be a large one to add for thirty lines of arithmetic.
    """
    if taps % 2 == 0:                      # an even length delays by half a sample, which cannot
        taps += 1                          # be cancelled by shifting an integer number of samples
    n = np.arange(taps) - (taps - 1) / 2.0
    lo, hi = float(band[0]) / sr, float(band[1]) / sr
    # sinc(0) is 1 in numpy's normalised sinc, so the centre tap needs no special case.
    lp_hi = 2.0 * hi * np.sinc(2.0 * hi * n)
    lp_lo = 2.0 * lo * np.sinc(2.0 * lo * n)
    h = (lp_hi - lp_lo) * np.blackman(taps)
    return h.astype(np.float64)


class BandSplitter:
    """Split a stream into the speech band and everything else, so that the two sum back exactly.

    `mid + rest` is the input, sample for sample, to floating-point rounding - not "flat in
    magnitude", not "allpass", but the same signal. That is the property the duck needs and the
    property `acrossover` turned out not to have (see `crossover_graphs`).

    It is guaranteed by construction rather than by the filter's theory: the band-pass is linear
    phase, so its only effect on timing is a delay of exactly `(taps - 1) / 2` samples, and `rest`
    is defined as the input delayed by that much MINUS the band. Whatever the band-pass does to
    the midrange, the difference undoes. With unequal gains the result is a clean spectral dip:

        mid * gm + rest * gr  ==  x * gr + mid * (gm - gr)

    The earlier code warned that band-passing the middle and calling the difference "the rest"
    does not work. That warning was right about an IIR band-pass, which shifts phase by a
    different amount at every frequency, and wrong to conclude that a crossover was the fix. With
    linear phase the difference trick is not a shortcut; it is the only construction that makes
    the reconstruction exact.

    The delay is paid up front by priming with the first `delay` samples, so the output timeline
    matches the input and nothing downstream has to know this class exists.
    """

    # Overlap-save on a fixed transform, sized once. Sizing it to the caller's block instead would
    # mean a millionpoint FFT per ten-second block and a fresh transform of the kernel each time;
    # on a machine with 2 GB free that was enough to make the encoder downstream fail to allocate.
    NFFT = 4096

    def __init__(self, band: list[int], sr: int, channels: int, taps: int = 1025):
        self.h = design_bandpass(band, sr, taps)
        self.taps = self.h.size
        self.delay = (self.taps - 1) // 2
        self.channels = channels
        self.step = self.NFFT - self.taps + 1
        self.H = np.fft.rfft(self.h, self.NFFT)
        # Overlap-save history: the last taps-1 input samples, so block boundaries are seamless.
        self.hist = np.zeros((self.taps - 1, channels), dtype=np.float32)
        # The dry path has to come out as delayed as the filtered one.
        self.dry = np.zeros((self.delay, channels), dtype=np.float32)

    def process(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """One block in, (mid, rest) out, both the same length as the block."""
        L = int(x.shape[0])
        if L == 0:
            e = np.zeros((0, self.channels), dtype=np.float32)
            return e, e
        x = x.astype(np.float32, copy=False)
        buf = np.concatenate([self.hist, x])
        self.hist = buf[-(self.taps - 1):].copy()

        mid = np.empty((L, self.channels), dtype=np.float32)
        done = 0
        while done < L:
            n = min(self.step, L - done)
            # 'valid' part of a linear convolution: the first taps-1 outputs of each transform are
            # wrap-around and get thrown away, which is exactly what the history is there to feed.
            chunk = buf[done : done + self.taps - 1 + n]
            for c in range(self.channels):
                y = np.fft.irfft(np.fft.rfft(chunk[:, c], self.NFFT) * self.H, self.NFFT)
                mid[done : done + n, c] = y[self.taps - 1 : self.taps - 1 + n]
            done += n

        # The dry signal, delayed by the same amount the band-pass delays.
        delayed = np.concatenate([self.dry, x])
        self.dry = (delayed[L:] if L < self.delay else delayed[-self.delay:]).copy()
        return mid, delayed[:L] - mid

    def prime(self, read) -> None:
        """Swallow the filter's own delay so the output lines up with the source.

        `read(frames)` must deliver from the same stream the mix will go on reading.
        """
        if self.delay:
            self.process(read(self.delay))


def measure_program_level(cfg, source: Path) -> float:
    """RMS of the whole programme in dB, measured on a stereo fold of it.

    Every level in the mix is set against this rather than against dBFS. An absolute voice target
    made the lektor 20 dB louder than a quiet film and would have buried a loud one; a relative one
    behaves the same way on both.

    The fold is not incidental. Every level in this pipeline was tuned by ear against a stereo
    fold, and an RMS taken across six raw channels is a different number - the LFE and the
    surrounds move it - so measuring the source "as it is" would quietly shift the balance of a
    mix nobody had changed.
    """
    sr = cfg["mix"]["sample_rate"]
    channels = 2 if cfg["mix"].get("level_reference", "stereo_fold") == "stereo_fold" else \
        max(1, source_channels(cfg, source))
    proc = decode_stream(cfg, source, sr, channels)
    acc = 0.0
    n = 0
    assert proc.stdout is not None
    while True:
        raw = proc.stdout.read(1 << 22)
        if not raw:
            break
        block = np.frombuffer(raw, dtype=np.float32)
        acc += float(np.sum(np.square(block, dtype=np.float64)))
        n += block.size
    proc.stdout.close()
    # A failed decode must not be mistaken for a quiet film. Every level in the mix hangs off this
    # number, so inventing one would mis-set all of them and say nothing about it.
    reap(proc, source.name)
    if n == 0:
        raise RuntimeError(f"{source.name} decoded to no audio at all")
    return 20.0 * math.log10(max(1e-9, math.sqrt(acc / n)))


def level_clips(cfg, clips) -> int:
    """Bring every line to the same loudness before any of them is placed.

    The track-wide normalisation that follows sets ONE gain from the RMS of all speech together, so
    a voice whose delivery wanders takes the listener with it. Measured over a sample of each: the
    voice this was written for spreads 4.7 LU between its 10th and 90th percentile line against
    2.7 LU for the previous one, and 10.8 LU end to end - loud enough to be heard as the lektor
    drifting rather than as the film changing.

    The correction is bounded. A line that is quiet because it is a whisper should stay a whisper;
    only the drift is taken out, so the gain is clamped and a near-silent clip is left alone.
    """
    m = cfg["mix"]
    if not bool(m.get("per_cue_level", False)):
        return 0
    limit = db_to_lin(float(m.get("per_cue_level_max_db", 4.0)))
    spoken = [c for c in clips if c.audio.size]
    if len(spoken) < 4:
        return 0
    rms = np.array([float(np.sqrt(np.mean(np.square(c.audio, dtype=np.float64)))) for c in spoken])
    ok = rms > 1e-5
    if ok.sum() < 4:
        return 0
    target = float(np.median(rms[ok]))
    moved = 0
    for clip, r in zip(spoken, rms):
        if r <= 1e-5:
            continue
        g = min(limit, max(1.0 / limit, target / r))
        if abs(20.0 * math.log10(g)) >= 0.5:
            moved += 1
        clip.audio = (clip.audio * g).astype(np.float32)
    return moved


def normalize_voice(cfg, track: np.ndarray, spans: list[tuple[int, int]],
                    program_db: float) -> float:
    """Scale factor putting the spoken parts voice_over_program_db above the programme.

    Silence between lines is excluded, so a sparse episode does not push the voice up.

    voice_channel_comp_db is added on top, and it is a correction rather than a preference: with
    the lektor on the centre channel alone, one channel is doing the work two channels did in the
    mix these levels were tuned on, and two correlated channels sum 3 dB. Without it the voice
    lands 3 dB below the number voice_over_program_db promises.
    """
    if not spans:
        return 1.0
    chunks = [track[s:e] for s, e in spans if e > s]
    speech = np.concatenate(chunks) if chunks else track
    rms = float(np.sqrt(np.mean(np.square(speech, dtype=np.float64))))
    if rms <= 1e-9:
        return 1.0
    m = cfg["mix"]
    target = db_to_lin(program_db + m["voice_over_program_db"] + voice_comp_db(cfg))
    return target / rms


def voice_comp_db(cfg) -> float:
    """The centre-channel compensation, and 0 for any layout that keeps the voice on two."""
    m = cfg["mix"]
    if len(voice_channels(cfg)) > 1:
        return 0.0
    return float(m.get("voice_channel_comp_db", 0.0))


# Layouts the mix loop can actually build, and how wide a frame each one is. Anything else is
# refused rather than half-built: the channel count decides the shape of every buffer in the loop,
# and guessing 6 for a layout that is not six channels would send ffmpeg frames of the wrong width
# and silently shear the mix.
LAYOUT_CHANNELS = {"stereo": 2, "2.0": 2, "5.1": 6, "5.1(side)": 6}


def is_surround(cfg) -> bool:
    return str(cfg["mix"].get("channel_layout", "stereo")).lower() not in ("stereo", "2.0", "")


def voice_channels(cfg) -> list[int]:
    """Which output channels carry the lektor. Both of them, in stereo."""
    if not is_surround(cfg):
        return [0, 1]
    got = cfg["mix"].get("voice_channels") or [2]
    return [int(i) for i in got]


def encode_profile(cfg) -> dict:
    """Codec, bitrate and extension for the layout in force. 5.1 needs its own; ac3 at 256k
    cannot carry six channels credibly."""
    enc = cfg["encode"]
    if is_surround(cfg) and isinstance(enc.get("surround"), dict):
        return dict(enc, **enc["surround"])
    return enc


def write_mix(cfg, source: Path, voice: np.ndarray, envelopes: dict[str, np.ndarray],
              out_path: Path, total_samples: int) -> int:
    """Stream the mix straight into ffmpeg. Blockwise, so RAM stays flat regardless of runtime.

    One loop for both layouts and both ducking modes. The source is decoded through ffmpeg, so it
    can be the film's own 5.1 inside an mkv or a stereo FLAC downmix and the mix does not care;
    the lektor lands on whichever channels the layout says carry him, and everything else keeps
    its own material with the duck riding on top.

    In spectral mode the source is decoded twice, once band-passed to the speech range and once as
    everything else, and the two are recombined at different gains. Only those two: the whole
    signal is their sum by construction, so a third decode of the untouched source would be one
    more full read of a 2.7 GB file over a slow mount for nothing.

    Returns how many blocks the limiter had to pull down, which is worth reporting - a mix that
    limits constantly is a mix whose levels are wrong.
    """
    m = cfg["mix"]
    sr = m["sample_rate"]
    ceiling = db_to_lin(m["limiter_ceiling_db"])
    release_coef = math.exp(-1.0 / max(1.0, float(m.get("limiter_release_ms", 120.0)) / 1000.0 * sr))
    ctrl_hz = CTRL_HZ
    layout = str(m.get("channel_layout", "stereo")) if is_surround(cfg) else "stereo"
    ch = LAYOUT_CHANNELS.get(layout.lower())
    if ch is None:
        raise RuntimeError(
            f"mix.channel_layout {layout!r} is not a layout this mix can build – "
            f"pick one of {', '.join(sorted(LAYOUT_CHANNELS))}")
    voice_ch = [c for c in voice_channels(cfg) if c < ch]
    prof = encode_profile(cfg)
    spectral = "mid" in envelopes

    # One decode either way. The bands are split here, in linear phase, because they have to sum
    # back to the film exactly - ffmpeg's crossover does not, and the difference is audible as
    # comb filtering across the whole programme, including every stretch the lektor is silent for.
    decoders = {"all": decode_stream(cfg, source, sr, ch)}
    splitter = BandSplitter(m.get("duck_band_hz", [300, 4000]), sr, ch) if spectral else None

    args = [
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(sr), "-channel_layout", layout, "-i", "pipe:0",
        "-c:a", prof["codec"],
    ]
    # A lossless profile has no bitrate to set, and passing one makes ffmpeg fail rather than
    # ignore it. sample_fmt matters for flac, which cannot store float and would otherwise be
    # given 16 bits by default - enough to hear on a quiet passage.
    if prof.get("bitrate"):
        args += ["-b:a", prof["bitrate"]]
    if prof.get("sample_fmt"):
        args += ["-sample_fmt", prof["sample_fmt"]]
    args.append(str(out_path))
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None

    block = sr * 10
    pos = 0
    reduction = 0.0  # limiter state, carried across blocks so the seam is not a step
    limited = 0

    padded = {name: 0 for name in decoders}

    def read_block(name: str, frames: int) -> np.ndarray:
        """`frames` frames from one decoder, zero-padded if the stream ended early.

        How much had to be padded is remembered. Padding a few frames at the very end is normal -
        the container's duration and the stream's do not agree to the sample - but padding a
        stretch means the decoder stopped, and a mix that quietly replaces the film with silence
        is worse than a mix that fails.
        """
        raw = decoders[name].stdout.read(frames * ch * 4)
        got = np.frombuffer(raw, dtype=np.float32).reshape(-1, ch).copy() if raw else \
            np.zeros((0, ch), dtype=np.float32)
        if got.shape[0] < frames:
            padded[name] += frames - got.shape[0]
            got = np.pad(got, ((0, frames - got.shape[0]), (0, 0)))
        return got

    if splitter is not None:
        # Pay the filter's delay before the first block, so `pos` still means the same instant in
        # the film that it means everywhere else in this pipeline.
        splitter.prime(lambda n: read_block("all", n))
        padded["all"] = 0        # those frames were consumed on purpose, not missing from the file

    # Encoder delay compensation. A lossy encoder emits its first frame late - measured at 256
    # samples for ffmpeg's eac3 - so without this the finished track carries the film 5.3 ms behind
    # the source. On its own that is inaudible, but it stops the track being a sample-exact copy of
    # the film, and any player that renders BOTH the internal track and this one sums the film with
    # a delayed copy of itself: a comb filter with notches every 188 Hz, 12.9 dB deep, measured. So
    # the source is advanced by exactly the delay the encoder will add, and the two cancel.
    delay = int(prof.get("delay_samples", 0))
    if delay > 0:
        read_block("all", delay)
        padded["all"] = 0

    try:
        while pos < total_samples:
            want = min(block, total_samples - pos)
            ctrl_idx = np.linspace(pos / sr * ctrl_hz, (pos + want) / sr * ctrl_hz, want)

            def gain_of(name: str) -> np.ndarray:
                env = envelopes[name]
                return np.interp(ctrl_idx, np.arange(env.size), env).astype(np.float32)

            if spectral:
                mid, rest = splitter.process(read_block("all", want))
                mixed = (rest * gain_of("rest")[:, None] + mid * gain_of("mid")[:, None])
            else:
                mixed = read_block("all", want) * gain_of("all")[:, None]

            block_voice = voice[pos : pos + want]
            for c in voice_ch:
                mixed[:, c] += block_voice

            if mixed.size:
                before = float(np.max(np.abs(mixed)))
                mixed, reduction = limit_block(mixed, ceiling, release_coef, reduction)
                limited += 1 if before > ceiling else 0

            proc.stdin.write(mixed.astype(np.float32).tobytes())
            pos += want
    finally:
        proc.stdin.close()
        for d in decoders.values():
            if d.stdout:
                d.stdout.close()
        err = proc.stderr.read().decode() if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError(f"encode failed: {err[:400]}")
    # After the encoder, so a genuine encode failure is reported as one - but before returning, so
    # a decode that died half way cannot pass for a finished mix. The exit status alone cannot say
    # which happened: rendering a 60 s sample of a 35 min episode closes the pipe on a healthy
    # ffmpeg, and it dies of that every time. What distinguishes the two is whether the frames we
    # needed actually arrived.
    for name, d in decoders.items():
        if padded[name] > sr // 2:
            reap(d, f"{source.name} ({name}), {padded[name] / sr:.1f} s short")
        else:
            d.wait()
    return limited
