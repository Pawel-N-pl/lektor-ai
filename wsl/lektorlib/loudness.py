"""Loudness measured the way a listener hears it, over time, and referenced to DIALOGUE.

Why this module exists. The level of the lektor used to be set against the RMS of the whole film,
silence and music and explosions included. On a film with sparse dialogue that average sits far
below the dialogue itself, so "the lektor is 9.5 dB above the programme" came out as roughly 18 dB
above the scenes it actually has to be heard over. The reference has to be the dialogue, not the
film.

Two consequences follow, and both are unit questions rather than taste:

  * The measure is BS.1770 (K-weighted), not RMS. This project has already been bitten once: a
    +1.5 dB change in RMS moved perceived loudness by only 0.5 LU when the voice changed. RMS
    measures energy, LUFS measures what is heard, and matching speech to speech is exactly the job
    LUFS was designed for.
  * A DIFFERENCE between two loudness values is in LU, not LUFS and not dB. LUFS is an absolute
    scale, LU is a distance along it - the same relationship dBFS has with dB. So the knob is
    `voice_over_dialogue_lu`, while anything that merely multiplies a signal (the duck, the trim,
    the limiter ceiling) stays in dB.

The K-weighting itself is done by ffmpeg's `ebur128`, not reimplemented here. Two biquads over two
billion samples is not something to run as a Python loop, and ffmpeg's implementation is both fast
and the one the standard describes. One decode feeds both measurements through `asplit`, because
the source is typically several gigabytes on a slow mount and reading it twice costs more than
every computation in this file put together.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# Two of this machine's sixteen cores belong to other projects' containers. Owner's rule.
WORKERS = 14
BIN_MS = 100.0            # ebur128 emits a momentary value every 100 ms
SILENCE_LUFS = -70.0      # BS.1770's absolute gate; below this there is nothing to measure


def _parse(path: Path) -> np.ndarray:
    """Momentary loudness values, in order, from an ametadata dump.

    `nan` and `inf` have to be matched, not skipped. ebur128 prints `nan` for a block of true
    digital silence, and a pattern that only accepts digits drops those lines - which does not
    leave a gap, it CLOSES one. The array is positional, index times 100 ms is the timestamp, so
    every value after the first dropped line refers to the wrong moment in the film and the error
    grows all the way to the end.

    Measured on a real render: 73149 frames written, 73149 values present, continuous to the last
    frame - and this function returned 21018 of them, silently, so the verification downstream
    scored 234 cues of 810 and every level it reported was read off the wrong part of the film.
    Non-finite becomes -inf, which the silence gate then treats as the silence it is.
    """
    if not path.is_file():
        return np.zeros(0, dtype=np.float64)
    vals = re.findall(r"lavfi\.r128\.M=(-?(?:\d+\.?\d*|nan|inf))",
                      path.read_text(errors="replace"), flags=re.IGNORECASE)
    out = np.array([float(v) for v in vals], dtype=np.float64)
    return np.where(np.isfinite(out), out, -np.inf)


def profiles(cfg, source: Path, centre_channel: str = "FC",
             cache_dir: Path | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(whole mix, centre only) momentary loudness, one value per 100 ms, from ONE decode.

    The centre profile is not a second opinion on level - it answers a different question: whether
    a subtitle cue has anybody speaking under it. Screen text, song lyrics and translated signs all
    carry cue timings and no dialogue, and averaging them into the reference would drag it down.

    Cached beside the work directory, keyed on the source's size and mtime. The film does not
    change between renders and neither does its loudness, while reading it costs a full pass over
    several gigabytes on a slow mount - which on a two-hour film is most of the time a re-render
    takes. Neither the eac3 decoder nor its encoder has any threading to fall back on ("Threading
    capabilities: none" for both), so cutting passes is the only lever there is.
    """
    key = None
    if cache_dir is not None:
        st = source.stat()
        key = cache_dir / f".lufs-{source.stem[:40]}-{st.st_size}-{int(st.st_mtime)}.npz"
        if key.is_file():
            with np.load(key) as z:
                return z["mix"], z["fc"]
    # A stereo source has no FC to pan from, and asking for one yields silence - which does not
    # fail, it just quietly empties the speech gate and lets title cards back into the dialogue
    # reference. Its dialogue lives in what the two channels share, so take the mid signal instead.
    probe = subprocess.run(
        [cfg.raw["paths"].get("ffprobe_wsl") or "ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(source)],
        capture_output=True, text=True)
    try:
        channels = int((probe.stdout.strip().splitlines() or ["2"])[0])
    except ValueError:
        channels = 2
    centre = f"c0={centre_channel}" if channels >= 6 else "c0=0.5*FL+0.5*FR"

    with tempfile.TemporaryDirectory(prefix="lektor-lufs-") as tmp:
        mix_txt, fc_txt = Path(tmp) / "mix.txt", Path(tmp) / "fc.txt"
        graph = (
            f"[0:a]asplit=2[m][c];"
            f"[m]ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file={mix_txt}[mo];"
            f"[c]pan=mono|{centre},"
            f"ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file={fc_txt}[co]"
        )
        subprocess.run(
            [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-threads", str(WORKERS),
             "-i", str(source), "-filter_complex", graph,
             "-map", "[mo]", "-f", "null", "-", "-map", "[co]", "-f", "null", "-"],
            capture_output=True, check=False)
        mix, fc = _parse(mix_txt), _parse(fc_txt)
    if key is not None and mix.size:
        key.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(key, mix=mix, fc=fc)
    return mix, fc


def profile_of(cfg, samples: np.ndarray, sr: int) -> np.ndarray:
    """Momentary loudness of an in-memory mono signal - used for the finished lektor track.

    The samples go to a file rather than down a pipe. `subprocess.run(input=...)` buffers the whole
    payload in memory and writes it in one go, and a two-hour voice track is 1.4 GB of float32:
    measured, the first attempt silently delivered about half the film, so the verification below
    scored 389 cues of 810 and reported the first half as if it were the whole thing.
    """
    with tempfile.TemporaryDirectory(prefix="lektor-lufs-") as tmp:
        out, raw = Path(tmp) / "v.txt", Path(tmp) / "v.f32"
        with open(raw, "wb") as fh:
            for i in range(0, samples.size, 1 << 22):
                fh.write(samples[i:i + (1 << 22)].astype(np.float32).tobytes())
        written = raw.stat().st_size
        r = subprocess.run(
            [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", str(raw),
             "-af", f"ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file={out}",
             "-f", "null", "-"],
            capture_output=True, check=False)
        prof = _parse(out)
        want_bins = int(round(samples.size / sr * 1000.0 / BIN_MS))
        if prof.size < want_bins * 0.95:
            # Keep the evidence. Reproducing this from a synthetic signal failed three times -
            # silence, over-unity peaks and full length all measure correctly on their own - so the
            # only way forward is the actual array that provokes it.
            keep = Path("/tmp/lektor-lufs-debug")
            keep.mkdir(exist_ok=True)
            for src, dst in ((raw, keep / "voice.f32"), (out, keep / "voice.txt")):
                if src.is_file():
                    dst.write_bytes(src.read_bytes())

    # A measurement that quietly returns a third of the film is worse than one that fails: it looks
    # like a result. This exact call came back with 21018 bins of a 73140-bin track and the
    # verification downstream reported the first 35 minutes as if they were the whole film - the
    # same failure mode as an empty API key that loads the .env anyway.
    want = int(round(samples.size / sr * 1000.0 / BIN_MS))
    if written != samples.size * 4 or prof.size < want * 0.95:
        bad = int(np.count_nonzero(~np.isfinite(samples)))
        first = int(np.argmax(~np.isfinite(samples))) if bad else -1
        raise RuntimeError(
            f"loudness profile is short: {prof.size} bins of about {want}, from "
            f"{written / 2**30:.2f} GB of {samples.size * 4 / 2**30:.2f} GB written; "
            f"input has {bad} non-finite sample(s)"
            + (f", first at {first / sr:.1f} s" if bad else "")
            + f", peak {float(np.nanmax(np.abs(samples))):.3f}"
            + (f"; ffmpeg said: {r.stderr.decode(errors='replace').strip()[-300:]}"
               if r.stderr else "; ffmpeg said nothing"))
    return prof


def _energy_mean(prof: np.ndarray, a: int, b: int) -> float:
    """Mean loudness over a slice, averaged in ENERGY and returned in LUFS.

    Averaging decibels directly is the classic mistake: it would let one silent bin drag a loud
    passage down by tens of units, because -70 LUFS is an enormous number on a log scale and a
    vanishing one in energy.
    """
    seg = prof[max(0, a):max(0, b)]
    seg = seg[seg > SILENCE_LUFS]
    if seg.size == 0:
        return float("nan")
    return float(10.0 * np.log10(np.mean(np.power(10.0, seg / 10.0))))


def cue_loudness(prof: np.ndarray, cues: list[dict]) -> np.ndarray:
    """Loudness under each cue, in LUFS, NaN where the cue covers nothing measurable."""
    out = np.full(len(cues), np.nan)
    for i, c in enumerate(cues):
        a = int((c["start_ms"] + c.get("delay_ms", 0)) / BIN_MS)
        b = int(c["end_ms"] / BIN_MS) + 1
        out[i] = _energy_mean(prof, a, b)
    return out


def span_loudness(prof: np.ndarray, spans: list[tuple[int, int]], sr: int) -> np.ndarray:
    """Loudness over each span of REAL signal, in LUFS.

    For measuring the lektor this is the only honest window. A cue is a slot in the subtitle grid
    and the line usually fills a fraction of it - a six-second card carrying a second and a half of
    speech is ordinary - so averaging across the cue measures mostly the silence either side, and
    the gate then throws the whole cue away. Scored that way the verification covered 234 cues of
    810 and quietly reported a biased subset as if it were the film.
    """
    out = np.full(len(spans), np.nan)
    for i, (s, e) in enumerate(spans):
        out[i] = _energy_mean(prof, int(s / sr * 1000.0 / BIN_MS),
                              int(e / sr * 1000.0 / BIN_MS) + 1)
    return out


def window_loudness(prof: np.ndarray, spans: list[tuple[int, int]], sr: int) -> np.ndarray:
    """Loudness of another signal over the same spans - so the two are compared like with like."""
    return span_loudness(prof, spans, sr)


def dialogue_anchor(mix: np.ndarray, fc: np.ndarray, cues: list[dict],
                    speech_gate_lu: float = 12.0) -> tuple[float, np.ndarray, int]:
    """(anchor LUFS, per-cue mix loudness, cues that counted).

    The anchor is the MEDIAN, not the mean: a handful of cues sitting under an explosion should not
    define what "dialogue level" means for the whole film.
    """
    per_cue = cue_loudness(mix, cues)
    centre = cue_loudness(fc, cues)
    ok = np.isfinite(per_cue) & np.isfinite(centre)
    if ok.sum() == 0:
        return float("nan"), per_cue, 0
    # A cue whose centre channel is far below the typical cue's centre has no one speaking under
    # it - a title card, a sign, a lyric - and is not evidence about dialogue level.
    speaking = ok & (centre > np.nanmedian(centre[ok]) - speech_gate_lu)
    if speaking.sum() < 8:
        speaking = ok
    return float(np.median(per_cue[speaking])), per_cue, int(speaking.sum())


def ride_curve(mix: np.ndarray, cues: list[dict], anchor: float, sr: int, total: int,
               window_s: float, tracking: float, max_db: float, smooth_s: float,
               within_cue: bool) -> np.ndarray:
    """A slow gain ride for the voice, in dB, one value per sample.

    The lektor follows the scene PARTIALLY - `tracking` below 1.0 - because following it fully
    would just copy the film's dynamics and undo the point of having a reference at all. At 0.5, a
    scene 6 LU louder than the film's dialogue average lifts the voice by 3 dB.

    Held flat across each cue by default. A real lektor reads a sentence at one level and it is the
    mixer who rides between sentences; modulating inside a line changes the voice's own dynamics
    and starts to sound like breathing. `within_cue` follows the film continuously instead, for
    long lines that sit under a swell, and the same smoothing and the same ceiling still apply.
    """
    ctrl_hz = 1000.0 / BIN_MS
    n = max(1, int(total / sr * ctrl_hz) + 2)
    half = max(1, int(window_s * ctrl_hz / 2))

    # Local loudness of the film, in energy, as a moving average over `window_s`.
    lin = np.where(mix > SILENCE_LUFS, np.power(10.0, mix / 10.0), np.nan)
    lin = np.pad(lin, (0, max(0, n - lin.size)), constant_values=np.nan)[:n]
    csum = np.nancumsum(np.nan_to_num(lin))
    cnt = np.cumsum(np.isfinite(lin).astype(np.float64))
    lo = np.clip(np.arange(n) - half, 0, n - 1)
    hi = np.clip(np.arange(n) + half, 0, n - 1)
    tot = csum[hi] - csum[lo]
    num = np.maximum(cnt[hi] - cnt[lo], 1.0)
    local = 10.0 * np.log10(np.maximum(tot / num, 1e-12))

    target = np.clip(tracking * (local - anchor), -max_db, max_db)
    if not within_cue:
        # One value per cue, held across it; between cues the curve simply travels from one to the
        # next, which is what the smoothing below turns into an eased ramp.
        held = np.full(n, np.nan)
        for c in cues:
            a = int((c["start_ms"] + c.get("delay_ms", 0)) / BIN_MS)
            b = min(n, int(c["end_ms"] / BIN_MS) + 1)
            if b > a:
                held[a:b] = np.nanmedian(target[max(0, a):b])
        idx = np.arange(n)
        good = np.isfinite(held)
        target = np.interp(idx, idx[good], held[good]) if good.any() else np.zeros(n)

    # Zero-phase smoothing: one pole forward, the same backward. The easing is symmetric, so the
    # ride neither anticipates nor lags the picture, and there are no corners left to hear.
    a = float(np.exp(-1.0 / max(1e-6, smooth_s * ctrl_hz)))
    for _ in range(2):
        target = target[::-1].copy()
        for i in range(1, target.size):
            target[i] = target[i] * (1 - a) + target[i - 1] * a
    return np.interp(np.linspace(0, n - 1, total), np.arange(n), target).astype(np.float32)
