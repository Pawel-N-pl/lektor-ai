"""What the actors actually did, as opposed to what the subtitle grid claims.

Every subtitle stream in this release is one authored display grid: measured over six episodes,
3191 of 3396 Polish cues start and end on byte-identical timestamps to the English track, the
minimum gap between any two cards is 82 ms and 55 % of all gaps are 83-84 ms. So the cards carry
no evidence at all about the delivery - not about where a speaker paused, and not about where the
edit cut. That evidence only exists in the picture and in the audio, and this module is the one
place that goes and gets it.

It produces one free local artefact per episode, `<base>.speech.json`:

  words   forced-aligned word spans, ms, from cues the aligner was confident about
  gaps    the silences between those words - the actors' own pauses
  cuts    shot changes, ms, from ffmpeg scene detection
  cues    per English cue: first/last aligned word and the aligner's confidence

Nothing here is paid and nothing here is installed. Forced alignment runs inside the `asr`
container that is already up, because torchaudio 2.6 ships the aligner; shot detection is ffmpeg.

Deliberately NOT here: Whisper. The running ASR service is not called at all. Its raw word
timestamps were measured 316 ms early at utterance starts (p50), which makes them useless for
anything time-critical, and the merge gate reads word spans from forced alignment instead. Skipping
it takes the episode from ~3.5 min of GPU to ~10 s.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from tempfile import gettempdir

from . import say
from .srt import parse_srt

# Runs INSIDE the asr container, fed on stdin to its python3. Kept as a string rather than a file
# because it is not our code's environment: it needs the container's torch, and shipping it as a
# file would invite someone to import it from the pipeline, where torch does not exist.
#
# Long sequential windows, not per-cue padding: with a short pad and no <star> token the aligner is
# free to drag a cue's first word backwards onto whatever speech sits in the pad, which fabricates
# exactly the statistic we care about. 30 s windows measured better than 120 s on a real episode -
# 475 vs 393 of 616 cues aligned, 2.6x less VRAM, 1.9x faster, medians agreeing to 10 ms.
_FA_SCRIPT = r'''
import json, sys, time, wave
import numpy as np, torch, torchaudio

WAV, CUES_JSON, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
WINDOW_S = float(sys.argv[4])
PAD = 0.35

def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "expected 16-bit mono"
        sr, raw = w.getframerate(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.from_numpy(x.copy()).unsqueeze(0), sr

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
bundle = torchaudio.pipelines.WAV2VEC2_ASR_LARGE_LV60K_960H
model = bundle.get_model().to(device)
lut = {c: i for i, c in enumerate(bundle.get_labels())}

wav, sr = read_wav(WAV)
assert sr == bundle.sample_rate, (sr, bundle.sample_rate)
dur = wav.shape[1] / sr
cues = json.load(open(CUES_JSON))

groups, cur = [], []
for c in cues:
    if cur and (c["end"] / 1000.0 - cur[0]["start"] / 1000.0) > WINDOW_S:
        groups.append(cur); cur = []
    cur.append(c)
if cur:
    groups.append(cur)

rows, errs = [], 0
t0 = time.perf_counter()
with torch.inference_mode():
    for g in groups:
        tokens, owner, kept = [], [], []
        for c in g:
            for w in c["words"]:
                tw = [lut[ch] for ch in w.upper() if ch in lut]
                if tw:
                    tokens.append(tw); owner.append(c["i"])
            kept.append(c)
        if not tokens:
            continue
        s = max(0.0, g[0]["start"] / 1000.0 - PAD)
        e = min(dur, g[-1]["end"] / 1000.0 + PAD)
        seg = wav[:, int(s * sr):int(e * sr)].to(device)
        if seg.shape[1] < sr // 2:
            continue
        emission, _ = model(seg)
        flat = torch.tensor([[t for tw in tokens for t in tw]], dtype=torch.int32)
        try:
            ali, sc = torchaudio.functional.forced_align(
                emission.cpu().float().log_softmax(-1), flat, blank=0)
        except Exception:
            errs += 1
            continue
        merged = torchaudio.functional.merge_tokens(ali[0], sc[0].exp())
        ratio = seg.shape[1] / emission.shape[1]
        spans, k = [], 0
        for tw in tokens:
            spans.append(merged[k:k + len(tw)]); k += len(tw)
        by_cue = {}
        for sp, oid in zip(spans, owner):
            by_cue.setdefault(oid, []).append(sp)
        for c in kept:
            sps = by_cue.get(c["i"])
            if not sps:
                continue
            score = float(np.mean([float(t.score) for sp in sps for t in sp]))
            rows.append({
                "i": c["i"], "cue_start_ms": c["start"], "cue_end_ms": c["end"],
                "score": round(score, 3), "n_words": len(sps),
                "word_times": [[round((sp[0].start * ratio / sr + s) * 1000),
                                round((sp[-1].end * ratio / sr + s) * 1000)] for sp in sps],
            })
align_s = time.perf_counter() - t0

json.dump({"meta": {"bundle": "WAV2VEC2_ASR_LARGE_LV60K_960H", "device": str(device),
                    "audio_dur_s": round(dur, 2), "window_s": WINDOW_S, "n_groups": len(groups),
                    "n_rows": len(rows), "n_group_errors": errs,
                    "align_s": round(align_s, 2),
                    "align_rtf_x": round(dur / align_s, 1) if align_s else None},
           "rows": rows}, open(OUT, "w"))
print(json.dumps({"ok": True, "n_rows": len(rows), "align_s": round(align_s, 1)}))
'''


# ---------------------------------------------------------------------------
# The artefact, as the rest of the pipeline sees it
# ---------------------------------------------------------------------------


@dataclass
class SpeechEvents:
    """The two questions the merge gate asks, and nothing else.

    Both are answered against a seam - the sliver of time between one card coming down and the
    next going up, which in this release is a fixed 83 ms. Everything is in milliseconds.
    """

    gaps: list[tuple[int, int]]
    cuts: list[int]
    mode: str = "both"
    min_pause_ms: int = 300
    max_pause_ms: int = 5000
    pause_tol_ms: int = 250
    cut_pad_ms: int = 0

    def __post_init__(self) -> None:
        self.gaps = sorted(tuple(g) for g in self.gaps)
        self.cuts = sorted(self.cuts)
        self._gap_starts = [g[0] for g in self.gaps]

    @property
    def uses_pause(self) -> bool:
        return self.mode in ("pause", "both") and bool(self.gaps)

    @property
    def uses_cut(self) -> bool:
        return self.mode in ("cut", "both") and bool(self.cuts)

    def cut_in(self, a: int, b: int) -> bool:
        """Did the edit cut between these two cards?"""
        if not self.uses_cut:
            return False
        lo, hi = a - self.cut_pad_ms, b + self.cut_pad_ms
        i = bisect_left(self.cuts, lo)
        return i < len(self.cuts) and self.cuts[i] <= hi

    def pause_at(self, a: int, b: int) -> bool:
        """Did the actors themselves pause across this seam?

        A silence counts when it is long enough to be a real pause in the delivery and short
        enough to still be one: past max_pause_ms it is far likelier to be a stretch the aligner
        never covered - a scene with no subtitled speech in it - than a breath between two halves
        of one sentence, and refusing a merge on a hole in the evidence is not the same as
        refusing it on evidence.
        """
        if not self.uses_pause:
            return False
        lo, hi = a - self.pause_tol_ms, b + self.pause_tol_ms
        # Any gap starting at or before hi may still overlap; walk back far enough to catch the
        # longest one that could reach lo.
        i = bisect_right(self._gap_starts, hi)
        for gs, ge in reversed(self.gaps[:i]):
            if gs < lo - self.max_pause_ms:
                break
            if ge <= lo or ge - gs < self.min_pause_ms or ge - gs > self.max_pause_ms:
                continue
            return True
        return False

    def describe(self) -> str:
        long_gaps = sum(1 for s, e in self.gaps if self.min_pause_ms <= e - s <= self.max_pause_ms)
        return (f"gate '{self.mode}': {long_gaps} pauses "
                f"{self.min_pause_ms}-{self.max_pause_ms} ms, {len(self.cuts)} shot cuts")


def speech_path(work_dir: Path, base: str) -> Path:
    return work_dir / f"{base}.speech.json"


def events_from(data: dict | None, cfg) -> SpeechEvents | None:
    """Build the gate from a loaded artefact, or return None to mean 'behave exactly as before'.

    None is the important return value: it is what keeps an episode with no speech.json, or a
    config with the gate switched off, byte-identical to what the pipeline did yesterday.
    """
    if not data:
        return None
    c = cfg["clean"]
    mode = str(c.get("merge_gate", "off")).lower()
    if mode not in ("cut", "pause", "both"):
        return None
    ev = SpeechEvents(
        gaps=[tuple(g) for g in data.get("gaps", [])],
        cuts=list(data.get("cuts", [])),
        mode=mode,
        min_pause_ms=int(c.get("merge_min_pause_ms", 300)),
        max_pause_ms=int(c.get("merge_max_pause_ms", 5000)),
        pause_tol_ms=int(c.get("merge_pause_tol_ms", 250)),
        cut_pad_ms=int(c.get("merge_cut_pad_ms", 0)),
    )
    # A gate with nothing to say is worse than no gate: it would look enabled in the report and
    # change nothing. Say so by returning None instead.
    return ev if (ev.uses_pause or ev.uses_cut) else None


def load_events(work_dir: Path, base: str, cfg) -> SpeechEvents | None:
    """Read `<base>.speech.json` if it is there. A missing or broken file is not an error."""
    p = speech_path(work_dir, base)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        say(f"  !! {p.name} is unreadable ({exc}) – merging falls back to the text rules")
        return None
    return events_from(data, cfg)


# ---------------------------------------------------------------------------
# Building the artefact
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"S\d{2}E\d{2}", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z']+")


def episode_tag(base: str) -> str:
    m = _TAG_RE.search(base)
    return m.group(0).upper() if m else ""


def find_reference_srt(cfg, base: str) -> Path | None:
    """The English text the aligner is given. English, because the acoustic model is English.

    Stream 3 is the near-verbatim transcript - it keeps "Ooh", the stutters and the reaction
    sounds - which is what is actually uttered, so it aligns better than the cleaned reading
    version in stream 2. Stream 2 is the fallback, and the Polish track is never a candidate.
    """
    tag = episode_tag(base)
    for pat in cfg["speech"]["reference_srt_patterns"]:
        p = cfg.work_dir / pat.format(tag=tag, base=base)
        if p.is_file():
            return p
    return None


def find_video(cfg, base: str) -> Path | None:
    for ext in (".mkv", ".mp4", ".m4v"):
        p = cfg.input_dir / f"{base}{ext}"
        if p.is_file():
            return p
    return None


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_centre(cfg, video: Path, wav: Path) -> str:
    """The dialogue stem, for free: on a 5.1 track the centre channel is where the voices are.

    No source separation. Demucs was measured to HURT alignment in the sibling project and was
    dropped there; the band-pass below is what survived. Falls back to a plain mono downmix when
    the track is not 5.1, and says which one it used.
    """
    s = cfg["speech"]
    for filt, kind in ((s["centre_filter"], "centre"), (s["downmix_filter"], "downmix")):
        cmd = [cfg.ffmpeg, "-v", "error", "-y", "-i", str(video), "-map", s["audio_stream"],
               "-af", filt, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)]
        r = _run(cmd, timeout=s["ffmpeg_timeout_s"])
        if r.returncode == 0 and wav.is_file() and wav.stat().st_size > 1024:
            return kind
        say(f"  !! ffmpeg {kind} extraction failed: {r.stderr.strip().splitlines()[-1:] or ''}")
    raise RuntimeError("could not extract audio")


def shot_cuts(cfg, video: Path, tmp: Path) -> tuple[list[int], float]:
    """Shot changes, in ms, from ffmpeg's own scene score. Decode only, nothing is written back."""
    s = cfg["speech"]
    log = tmp / "cuts.txt"
    cmd = [cfg.ffmpeg, "-v", "error", "-y", "-i", str(video), "-an", "-sn",
           "-vf", f"select='gt(scene,{s['scene_threshold']})',metadata=print:file={log}",
           "-f", "null", "-"]
    t0 = time.perf_counter()
    r = _run(cmd, timeout=s["ffmpeg_timeout_s"])
    took = time.perf_counter() - t0
    if r.returncode != 0 or not log.is_file():
        say(f"  !! scene detection failed ({r.stderr.strip()[-200:]}) – continuing without cuts")
        return [], took
    cuts = [round(float(m) * 1000) for m in
            re.findall(r"pts_time:([0-9.]+)", log.read_text(encoding="utf-8", errors="replace"))]
    return sorted(cuts), took


def cues_for_alignment(srt: Path) -> list[dict]:
    """Subtitle text reduced to what a CTC character model can be given: bare English words."""
    out = []
    for c in parse_srt(srt):
        body = re.sub(r"<[^>]+>|\[[^\]]*\]|\([^)]*\)", " ", c.raw)
        words = _WORD_RE.findall(body)
        if words:
            out.append({"i": c.idx, "start": c.start_ms, "end": c.end_ms, "words": words})
    return out


def forced_align(cfg, wav: Path, cues: list[dict], tmp: Path) -> dict:
    """Run the aligner inside the container that is already up. Nothing is installed anywhere.

    The container is treated as read-only apart from one scratch directory under its /tmp, which
    is removed afterwards. The resident Whisper model is never touched: this loads a second,
    smaller model, measured at +1461 MiB resident and +3617 MiB peak alongside it.
    """
    s = cfg["speech"]
    name = s["asr_container"]
    if not shutil.which("docker"):
        raise RuntimeError("docker is not on PATH")
    probe = _run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=30)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise RuntimeError(f"container {name!r} is not running")

    remote = f"/tmp/lektor-speech-{int(time.time())}"
    cues_json = tmp / "fa_cues.json"
    cues_json.write_text(json.dumps(cues), encoding="utf-8")
    try:
        _run(["docker", "exec", name, "mkdir", "-p", remote], timeout=60)
        for src in (wav, cues_json):
            cp = _run(["docker", "cp", str(src), f"{name}:{remote}/{src.name}"],
                      timeout=s["docker_timeout_s"])
            if cp.returncode != 0:
                raise RuntimeError(f"docker cp failed: {cp.stderr.strip()[:200]}")
        # `python3`, not `python`: the image has no `python` on PATH.
        run = subprocess.run(
            ["docker", "exec", "-i", name, "python3", "-",
             f"{remote}/{wav.name}", f"{remote}/{cues_json.name}", f"{remote}/fa.json",
             str(s["fa_window_s"])],
            input=_FA_SCRIPT, capture_output=True, text=True, timeout=s["fa_timeout_s"])
        if run.returncode != 0:
            raise RuntimeError(f"forced alignment failed: {run.stderr.strip()[-400:]}")
        got = _run(["docker", "cp", f"{name}:{remote}/fa.json", str(tmp / "fa.json")],
                   timeout=s["docker_timeout_s"])
        if got.returncode != 0:
            raise RuntimeError(f"docker cp back failed: {got.stderr.strip()[:200]}")
        return json.loads((tmp / "fa.json").read_text(encoding="utf-8"))
    finally:
        _run(["docker", "exec", name, "rm", "-rf", remote], timeout=60)


def words_and_gaps(fa: dict, min_score: float, store_min_gap_ms: int
                   ) -> tuple[list[list[int]], list[list[int]], list[dict]]:
    """Turn per-cue alignments into one word stream, its silences, and a per-cue summary.

    Only cues the aligner was confident about contribute words. That threshold is not decoration:
    cues scoring below 0.35 were measured at 0.314 mean ASR token recall against 0.901 for cues
    at 0.7 and above, i.e. the low scorers are shouts over crowd noise where the word times are
    guesses. A guessed word time invents a silence that was never there.
    """
    words: list[list[int]] = []
    rows: list[dict] = []
    for r in sorted(fa.get("rows", []), key=lambda x: x["cue_start_ms"]):
        wt = r.get("word_times") or []
        row = {"cue_start_ms": r["cue_start_ms"], "cue_end_ms": r["cue_end_ms"],
               "score": r["score"], "n_words": r["n_words"]}
        if wt:
            row["fa_first_ms"], row["fa_last_ms"] = wt[0][0], wt[-1][1]
        rows.append(row)
        if r["score"] >= min_score:
            words.extend([int(a), int(b)] for a, b in wt)
    words.sort()
    gaps = [[a[1], b[0]] for a, b in zip(words, words[1:])
            if b[0] - a[1] >= store_min_gap_ms]
    return words, gaps, rows


def stage_speech(cfg, only: str | None, force: bool) -> None:
    """Build `<base>.speech.json` for every episode that has a script's worth of subtitles.

    Every step is allowed to fail on its own. A missing container costs the pause signal, a
    missing video costs the cut signal, and either way the artefact that gets written says which
    signals it actually contains - so the gate can only ever act on evidence that exists.
    """
    s = cfg["speech"]
    tmp_root = Path(s["tmp_dir"]).expanduser() if s.get("tmp_dir") else Path(gettempdir())
    srts = sorted(cfg.work_dir.glob("*.srt"))
    if only:
        srts = [p for p in srts if only in p.name]
    if not srts:
        raise SystemExit(f"no .srt in {cfg.work_dir} – run the Windows extract stage first")

    for srt in srts:
        base = srt.stem
        out = speech_path(cfg.work_dir, base)
        if out.exists() and not force:
            say(f"{base}: {out.name} exists, skipping (--force to redo)")
            continue

        say(f"\n{base}")
        video = find_video(cfg, base)
        ref = find_reference_srt(cfg, base)
        if video is None:
            say(f"  !! no video beside the subtitles in {cfg.input_dir} – nothing to measure")
            continue

        tmp = tmp_root / f"lektor-speech-{base[:40]}"
        tmp.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "base": base,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"video": video.name, "reference_srt": ref.name if ref else None},
            "words": [], "gaps": [], "cuts": [], "cues": [],
        }
        try:
            # Each signal fails on its own and costs only itself. An episode that ends up with
            # neither is not written at all, so a half-built artefact can never gate a merge.
            if ref is None:
                say("  !! no English reference subtitle – no pause signal for this episode")
            else:
                try:
                    wav = tmp / "centre16k.wav"
                    t0 = time.perf_counter()
                    kind = extract_centre(cfg, video, wav)
                    extract_s = time.perf_counter() - t0
                    cues = cues_for_alignment(ref)
                    say(f"  {kind} audio in {extract_s:.1f} s, aligning {len(cues)} English cues "
                        f"in the '{s['asr_container']}' container")
                    fa = forced_align(cfg, wav, cues, tmp)
                except Exception as exc:  # noqa: BLE001 - every failure here is recoverable
                    say(f"  !! no alignment for this episode: {exc}")
                    say("     the artefact will carry shot cuts only, and the pause gate is off")
                else:
                    words, gaps, rows = words_and_gaps(
                        fa, float(s["min_score"]), int(s["store_min_gap_ms"]))
                    payload["words"], payload["gaps"], payload["cues"] = words, gaps, rows
                    meta = fa.get("meta", {})
                    payload["fa"] = dict(meta, min_score=s["min_score"],
                                         n_aligned=sum(1 for r in rows
                                                       if r["score"] >= s["min_score"]),
                                         extract_s=round(extract_s, 1))
                    payload["audio_dur_s"] = meta.get("audio_dur_s")
                    say(f"  aligned {payload['fa']['n_aligned']}/{len(cues)} cues at score "
                        f">= {s['min_score']} in {meta.get('align_s')} s "
                        f"({meta.get('align_rtf_x')}x realtime), {len(words)} words, "
                        f"{len(gaps)} silences >= {s['store_min_gap_ms']} ms")

            if s["detect_cuts"]:
                try:
                    cuts, took = shot_cuts(cfg, video, tmp)
                except Exception as exc:  # noqa: BLE001
                    say(f"  !! no shot cuts for this episode: {exc}")
                else:
                    payload["cuts"] = cuts
                    payload["cuts_meta"] = {"scene_threshold": s["scene_threshold"],
                                            "n": len(cuts), "ffmpeg_s": round(took, 1)}
                    if cuts:
                        shots = sorted(b - a for a, b in zip(cuts, cuts[1:]))
                        median = shots[len(shots) // 2] / 1000.0 if shots else 0.0
                        say(f"  {len(cuts)} shot cuts in {took:.0f} s, median shot {median:.2f} s")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if not payload["words"] and not payload["cuts"]:
            say("  !! nothing measurable came back – not writing an artefact")
            continue
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        say(f"  -> {out.name} ({out.stat().st_size / 1024:.0f} kB)")
