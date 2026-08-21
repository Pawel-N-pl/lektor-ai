#!/usr/bin/env python3
"""A/B the merge gate by ear, on piper, for nothing.

The gate is justified by measurement - half of all merges join across a real pause or a shot cut -
but no measurement in this repo can answer whether refusing a merge actually SOUNDS better. This
renders the seams the gate refuses, both ways, over the real film audio.

The two variants differ in exactly one thing: whether the cards are spoken as one utterance placed
at the first card's start, or as two placed at their own. Same words, same voice, same mix. The
condensation stage is deliberately not involved - it would give the two variants different text and
confound the only question being asked.

    tools/ab_merge.py [--pairs 22] [--pad 2.5]
"""

import argparse
import json
import pathlib
import subprocess
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.backends import get_backend            # noqa: E402
from lektorlib.backends.base import Utterance         # noqa: E402
from lektorlib.config import Config                   # noqa: E402
from lektorlib.audio import apply_shelf                # noqa: E402
from lektorlib.mix import (build_duck_envelopes, crossover_graphs, db_to_lin,  # noqa: E402
                           limit_block)
from lektorlib.speech import load_events               # noqa: E402
from lektorlib.srt import clean_text, compute_windows, merge_contiguous, parse_srt, should_skip  # noqa: E402

SR = 48000


def cues_for(path, cfg, ev):
    cues = [c for c in parse_srt(path) if not should_skip(c.raw, cfg)]
    for c in cues:
        c.text = clean_text(c.raw, cfg)
    cues = [c for c in cues if c.text]
    merged = merge_contiguous(cues, cfg, ev)
    compute_windows(merged, cfg)
    return merged


def refused_seams(cfg, work, media):
    """Seams the gate declined: merged when it is off, separate when it is on."""
    out = []
    for srt in sorted(work.glob("*.srt")):
        if ".eng" in srt.name:
            continue
        mkv = media / f"{srt.stem}.mkv"
        if not mkv.exists():
            continue
        ev = load_events(work, srt.stem, cfg)
        if ev is None:
            continue
        ungated = {tuple(c.merged_from): c for c in cues_for(srt, cfg, None) if c.merged_from}
        gated = {tuple(c.merged_from) for c in cues_for(srt, cfg, ev) if c.merged_from}
        for group, cue in ungated.items():
            if group not in gated:
                out.append((mkv, srt, group, cue, ev))
    return out


def parts_of(srt, cfg, ids):
    raw = [c for c in parse_srt(srt) if not should_skip(c.raw, cfg)]
    for c in raw:
        c.text = clean_text(c.raw, cfg)
    raw = [c for c in raw if c.text]
    by = {c.idx: c for c in raw}
    parts = [by[i] for i in ids if i in by]
    compute_windows(raw, cfg)
    return parts


def _at_rate(env, total):
    return np.interp(np.linspace(0, total / SR * 200, total),
                     np.arange(env.size), env).astype(np.float32)


def render_window(cfg, backend, mkv, parts, merged_cue, start_s, dur_s, out_path, as_one,
                  gain=None):
    """One variant of one seam, mixed over the real audio."""
    total = int(dur_s * SR)
    voice = np.zeros(total, dtype=np.float32)
    spans = []

    utterances = ([(merged_cue.start_ms, merged_cue.text, merged_cue.window_ms)] if as_one
                  else [(p.start_ms, p.text, p.window_ms) for p in parts])
    cursor = 0
    for start_ms, text, window_ms in utterances:
        r = backend.render(Utterance(index=0, text=text,
                                     target_ms=int(window_ms * cfg["timing"]["safety"]) or None))
        audio = r.audio
        if r.sample_rate != SR:
            idx = np.linspace(0, audio.size - 1, int(audio.size * SR / r.sample_rate))
            audio = np.interp(idx, np.arange(audio.size), audio).astype(np.float32)
        at = max(int((start_ms / 1000.0 - start_s) * SR), cursor)
        end = min(total, at + audio.size)
        if end <= at:
            continue
        voice[at:end] += audio[: end - at]
        spans.append((at, end))
        cursor = end + int(0.06 * SR)

    m = cfg["mix"]
    prog = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start_s), "-t", str(dur_s),
                           "-i", str(mkv), "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
                           "-f", "f32le", "pipe:1"], capture_output=True).stdout
    bed = np.frombuffer(prog, dtype=np.float32).reshape(-1, 2).copy()
    if bed.shape[0] < total:
        bed = np.pad(bed, ((0, total - bed.shape[0]), (0, 0)))
    bed = bed[:total]

    # Tone before level, exactly as production does it.
    voice = apply_shelf(cfg, voice, SR, float(m.get("voice_presence_db", 0.0)),
                        float(m.get("voice_presence_hz", 4000.0)))

    ref = float(np.sqrt(np.mean(np.square(bed, dtype=np.float64))))
    prog_db = 20 * np.log10(max(1e-9, ref))
    if spans:
        # The gain is computed once for the pair and handed to the second variant. Computed twice it
        # would differ, because one variant has a single speech span and the other has two - and the
        # listener would be comparing loudness instead of merging.
        if gain is None:
            speech = np.concatenate([voice[a:b] for a, b in spans])
            rms = float(np.sqrt(np.mean(np.square(speech, dtype=np.float64))))
            gain = (db_to_lin(prog_db + m["voice_over_program_db"]) / max(1e-9, rms)
                    * db_to_lin(m.get("voice_trim_db", 0.0)))
        voice *= gain

    # Same spectral duck as a real episode, built by the same code, or the A/B would be judged on a
    # mix the pipeline does not produce. Both bands come out of the Linkwitz-Riley crossover: the
    # "rest" band is NOT the source minus the mid band, however obvious that shortcut looks. An IIR
    # band-pass shifts phase, so the difference still carries the midrange at full level and the
    # dip vanishes the moment the two are given different gains - measured at 6.8 dB LOUDER in the
    # speech band than plain broadband ducking.
    def band_of(graph: str, label: str) -> np.ndarray:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(start_s), "-t", str(dur_s), "-i", str(mkv),
             "-filter_complex", graph, "-map", label,
             "-ac", "2", "-ar", str(SR), "-f", "f32le", "pipe:1"], capture_output=True).stdout
        x = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()
        if x.shape[0] < total:
            x = np.pad(x, ((0, total - x.shape[0]), (0, 0)))
        return x[:total]

    envelopes = build_duck_envelopes(cfg, spans, total)
    if "mid" in envelopes:
        (fg_mid, lab_mid), (fg_rest, lab_rest) = crossover_graphs(m.get("duck_band_hz", [300, 4000]))
        gm = _at_rate(envelopes["mid"], total)
        gr = _at_rate(envelopes["rest"], total)
        mixed = band_of(fg_rest, lab_rest) * gr[:, None] + band_of(fg_mid, lab_mid) * gm[:, None]
    else:
        mixed = bed * _at_rate(envelopes["all"], total)[:, None]
    mixed[:, 0] += voice * 0.7071
    mixed[:, 1] += voice * 0.7071
    mixed, _ = limit_block(mixed, db_to_lin(m["limiter_ceiling_db"]),
                           float(np.exp(-1.0 / (0.120 * SR))), 0.0)

    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR),
                            "-ac", "2", "-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "160k",
                            str(out_path)], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc.communicate(mixed.astype(np.float32).tobytes())
    return gain


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", type=int, default=22)
    ap.add_argument("--pad", type=float, default=2.5)
    ap.add_argument("--backend", default="elevenlabs",
                    help="piper is free but its length_scale search swings the speaking rate so hard "
                         "between a wide window and a tight one that it masks the thing being judged")
    args = ap.parse_args()

    cfg = Config(ROOT / "lektor.toml")
    media = cfg.input_dir
    work = cfg.work_dir
    out = work / "ab"
    out.mkdir(exist_ok=True)

    seams = refused_seams(cfg, work, media)
    print(f"seams the gate refused: {len(seams)}")
    if not seams:
        return

    # Spread the sample across episodes rather than taking the first N of one.
    seams.sort(key=lambda s: (s[2][0]))
    per = max(1, args.pairs // max(1, len({s[0] for s in seams})))
    picked, seen = [], {}
    for s in seams:
        k = s[0].name
        if seen.get(k, 0) >= per and len(picked) >= args.pairs:
            continue
        seen[k] = seen.get(k, 0) + 1
        picked.append(s)
        if len(picked) >= args.pairs:
            break

    cfg.raw["voice"]["backend"] = args.backend
    backend = get_backend(args.backend, cfg)
    backend.prepare()
    index = []
    try:
        for n, (mkv, srt, group, cue, ev) in enumerate(picked, 1):
            parts = parts_of(srt, cfg, group)
            if len(parts) < 2:
                continue
            start_s = max(0.0, parts[0].start_ms / 1000.0 - args.pad)
            dur_s = (parts[-1].end_ms / 1000.0 + args.pad) - start_s
            tag = f"{n:02d}"
            ep = "".join(ch for ch in mkv.stem if ch.isdigit())[:6]
            g = render_window(cfg, backend, mkv, parts, cue, start_s, dur_s,
                              out / f"AB{tag}_A_scalone.mp3", True)
            render_window(cfg, backend, mkv, parts, cue, start_s, dur_s,
                          out / f"AB{tag}_B_osobno.mp3", False, gain=g)
            gap = parts[1].start_ms - parts[0].end_ms
            pause = ev.pause_at(parts[0].end_ms, parts[1].start_ms)
            cut = ev.cut_in(parts[0].end_ms, parts[1].start_ms)
            index.append({"pair": tag, "episode": ep, "at_s": round(parts[0].start_ms / 1000.0, 1),
                          "ids": list(group), "card_gap_ms": gap,
                          "real_pause": bool(pause), "shot_cut": bool(cut),
                          "text": cue.text})
            print(f"  {tag}  {ep} @{parts[0].start_ms/1000:7.1f}s  "
                  f"{'pauza' if pause else '     '} {'ciecie' if cut else '      '}  {cue.text[:58]}")
    finally:
        backend.close()

    (out / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    print(f"\n{len(index)} pairs -> {out}")


if __name__ == "__main__":
    main()
