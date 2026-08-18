#!/usr/bin/env python3
"""Audition what the mix does to the FILM, on one short passage, many settings at a time.

The question this answers is not "how does the lektor sound" but "what do we do to the film that
the film did not already do to itself". So the voice is silent by default and the variants differ
only in the handful of numbers that touch the programme.

The source passage is decoded ONCE into a cache next to the film and every variant is rendered from
that cache, because pulling 82 s of 5.1 out of a 2.7 GB mkv over a 9p mount costs more than the
render does. Re-running with the same --at/--len reuses the cache and starts encoding immediately.

    tools/ab_mix.py --film <path.mkv> --at 26:36 --len 82
    tools/ab_mix.py --film <path.mkv> --at 26:36 --len 82 --verify
    tools/ab_mix.py --film <path.mkv> --at 26:36 --len 82 --only A_ORYGINAL,C_OBECNE

--verify nulls every variant against the source, per channel, searching for the best integer
offset. That is the check that catches an encoder delay, a swapped channel or a gain that is not
what the config says it is - all three of which have shipped in this project at least once.
"""

import argparse
import json
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.config import Config          # noqa: E402
from lektorlib import mix as MX              # noqa: E402

SR = 48000
NAMES = ["FL", "FR", "FC", "LFE", "SL", "SR"]
# Two of this machine's sixteen cores belong to other projects' containers. Owner rule, not tuning.
WORKERS = 14


def parse_at(s: str) -> float:
    """Accept 1596, 26:36 or 0:26:36."""
    parts = [float(p) for p in str(s).split(":")]
    out = 0.0
    for p in parts:
        out = out * 60 + p
    return out


def cache_segment(cfg, film: pathlib.Path, at: float, length: float, force: bool) -> pathlib.Path:
    """Decode the passage once, as float32 5.1, and keep it."""
    cache = film.parent / "_lektor" / "test" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    seg = cache / f"{film.stem[:40]}_{int(at)}_{int(length)}.wav"
    if seg.is_file() and not force:
        print(f"  cache: {seg.name} ({seg.stat().st_size/1048576:.0f} MB)")
        return seg
    # Input seeking is fine here: every variant is built from THIS file, so whatever frame the seek
    # landed on is the shared zero for all of them. It would only matter if the cache were compared
    # against a fresh decode of the mkv, which is exactly what --verify avoids doing.
    subprocess.run([cfg.ffmpeg, "-v", "error", "-y", "-threads", str(WORKERS),
                    "-ss", str(at), "-t", str(length), "-i", str(film),
                    "-map", "0:a:0", "-c:a", "pcm_f32le", str(seg)], check=True)
    print(f"  wyciete raz: {seg.name} ({seg.stat().st_size/1048576:.0f} MB)")
    return seg


def decode(path: pathlib.Path, channels: int = 6) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                          "-ac", str(channels), "-ar", str(SR), "-f", "f32le", "pipe:1"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, channels)


# name -> (program_gain_db, duck_mode, note shown to the listener)
VARIANTS = {
    "A_ORYGINAL":      (None, None, "film nietkniety, kopia strumienia"),
    "B_PRZELOT_0dB":   (0.0, "broadband", "przez nasz mikser, bez podbicia"),
    "C_OBECNE_+4dB":   (4.0, "broadband", "obecne ustawienia"),
    "D_PODBICIE_+2dB": (2.0, "broadband", "polowa podbicia"),
}


def render(seg: pathlib.Path, out_dir: pathlib.Path, name: str, gain, mode, frames: int):
    if gain is None:                      # the control: no encode of ours at all
        out = out_dir / f"{name}.mka"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(seg), "-map", "0:a:0",
                        "-c:a", "eac3", "-b:a", "768k", str(out)], check=True)
        return out
    cfg = Config(ROOT / "lektor.config.json")
    cfg["mix"]["program_gain_db"] = gain
    cfg["mix"]["duck_mode"] = mode
    ctrl = int(frames / SR * MX.CTRL_HZ) + 2
    envs = {"all": np.full(ctrl, MX.db_to_lin(gain), dtype=np.float32)}
    tmp = out_dir / f"{name}.{cfg['encode']['surround']['extension']}"
    MX.write_mix(cfg, seg, np.zeros(frames, dtype=np.float32), envs, tmp, frames)
    out = out_dir / f"{name}.mka"
    if tmp != out:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp), "-c:a", "copy",
                        "-metadata:s:a:0", "language=pol", str(out)], check=True)
        tmp.unlink()
    return out


def verify(seg: pathlib.Path, made: dict):
    film = decode(seg)
    print("\n  weryfikacja: null wzgledem zrodla, kanal po kanale")
    for name, (path, gain) in made.items():
        y = decode(path)
        n = min(len(y), len(film))
        g = 1.0 if gain is None else MX.db_to_lin(gain)
        offs, nulls = [], []
        for i in range(6):
            ref = film[:, i] * g
            lvl = 20 * np.log10(max(1e-12, float(np.sqrt(np.mean(ref[:n] ** 2)))))
            if lvl < -80:
                continue
            # Cross-correlation through the FFT, not a loop over candidate offsets. The brute-force
            # version was 1200 offsets x 6 channels x 4 variants of full-length MSE and took four
            # minutes on 82 s of audio; this is one transform pair per channel.
            LIM = 600
            a = y[LIM:n - LIM, i].astype(np.float64)
            b = ref[:n].astype(np.float64)
            size = 1 << int(np.ceil(np.log2(len(a) + len(b))))
            corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
            # Lag k means a[j] lines up with b[j + LIM + k]; only +-LIM is physically plausible.
            cand = np.concatenate([corr[:LIM + 1], corr[-LIM:]])
            lags = np.concatenate([np.arange(0, LIM + 1), np.arange(-LIM, 0)])
            k = int(lags[int(np.argmax(cand))]) - LIM
            seg = b[LIM + k:n - LIM + k]
            m = min(len(a), len(seg))
            e = float(np.mean(np.square(a[:m] - seg[:m], dtype=np.float64)))
            offs.append(k)
            nulls.append(10 * np.log10(max(1e-12, e)) - lvl)
        same = "OK" if len(set(offs)) <= 1 else f"ROZJAZD {sorted(set(offs))}"
        print(f"    {name:16s} przesuniecie {offs[0]:+5d} probek [{same}]   "
              f"null {min(nulls):+6.1f} do {max(nulls):+6.1f} dB")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--film", required=True)
    ap.add_argument("--at", required=True, help="start, seconds or mm:ss")
    ap.add_argument("--len", type=float, default=82.0)
    ap.add_argument("--only", default="", help="comma-separated variant names")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="re-cut the cached passage")
    args = ap.parse_args()

    cfg = Config(ROOT / "lektor.config.json")
    film = pathlib.Path(args.film)
    at = parse_at(args.at)
    seg = cache_segment(cfg, film, at, args.len, args.refresh)
    frames = decode(seg).shape[0]

    out_dir = film.parent / "_lektor" / "test"
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = [w for w in (args.only.split(",") if args.only else VARIANTS) if w in VARIANTS]

    made = {}
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(wanted))) as pool:
        futures = {name: pool.submit(render, seg, out_dir, name, *VARIANTS[name][:2], frames)
                   for name in wanted}
        for name, fut in futures.items():
            made[name] = (fut.result(), VARIANTS[name][0])
            print(f"  -> {name}.mka   {VARIANTS[name][2]}")

    if args.verify:
        verify(seg, made)
    (out_dir / "_opis.json").write_text(
        json.dumps({k: VARIANTS[k][2] for k in wanted}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
