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
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir

from . import say
from .srt import parse_srt, predicted_ms
from .venvs import venv_python

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
# Onsets: when the actor actually started, per card
# ---------------------------------------------------------------------------
#
# The other half of what the artefact is for. The gate above asks the evidence about the SEAM
# between two cards; this asks it about the START of one, because a handful of lines every episode
# have the lektor talking before anyone on screen does.
#
# The prize is real and small, and both halves of that matter. Measured over all six episodes at
# forced-alignment score >= 0.8: 182 of 3443 lines lead the first spoken word by more than 300 ms,
# 35 by more than 500 ms, 8 by more than 800 ms - about thirty lines an episode.
#
# The score threshold is higher than the pause gate's 0.5 because the two failures are not
# symmetrical: a pause gate acting on a bad alignment refuses a merge, while a delay acting on one
# moves a line that was never early. But 0.8 does NOT bound the lead on its own - the worst one
# passing it is +2526 ms, on a two-word cue - so what actually contains a bad anchor is the
# max_delay_ms cap. That cap is load-bearing, not cosmetic.


@dataclass
class Onsets:
    """Forced-aligned first-word times, indexed by the card they belong to.

    Cards are matched by their start time rather than by their id, because the ids belong to two
    different subtitle files - the aligner reads the English track, the lektor is written from the
    Polish one. Measured over this release, 3191 of 3396 Polish cues start on a byte-identical
    timestamp to their English counterpart, so a small tolerance matches almost everything and
    anything outside it is a card that has no English counterpart at all.
    """

    rows: list[tuple[int, int, float]] = field(default_factory=list)  # card start, onset, score
    min_score: float = 0.8
    match_tol_ms: int = 120

    def __post_init__(self) -> None:
        self.rows = sorted((int(a), int(b), float(s)) for a, b, s in self.rows)
        self._starts = [r[0] for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def first_word_at(self, start_ms: int) -> int | None:
        """The onset of the card starting here, or None when there is no confident evidence."""
        if not self.rows:
            return None
        i = bisect_left(self._starts, start_ms)
        best: tuple[int, int, float] | None = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(self.rows):
                r = self.rows[j]
                if abs(r[0] - start_ms) <= self.match_tol_ms and (
                        best is None or abs(r[0] - start_ms) < abs(best[0] - start_ms)):
                    best = r
        if best is None or best[2] < self.min_score:
            return None
        return best[1]

    def describe(self) -> str:
        """Count the rows that could actually anchor a line, not every row in the file.

        The score filter lives in `first_word_at`, so `len(self.rows)` is the whole artefact -
        about twice the usable figure on this release, and reporting that under a label naming the
        threshold would make the matcher below look like it was losing half its evidence.
        """
        usable = sum(1 for r in self.rows if r[2] >= self.min_score)
        return (f"{usable} card onsets at score >= {self.min_score}, "
                f"of {len(self.rows)} aligned")


def onsets_from(data: dict | None, cfg) -> Onsets | None:
    """Build the onset index from a loaded artefact, or None to mean 'change nothing'."""
    if not data:
        return None
    t = cfg["timing"]
    if int(t.get("max_delay_ms", 0)) <= 0:
        return None
    rows = [(r["cue_start_ms"], r["fa_first_ms"], r.get("score", 0.0))
            for r in data.get("cues", [])
            if isinstance(r, dict) and r.get("fa_first_ms") is not None
            and r.get("cue_start_ms") is not None]
    if not rows:
        return None
    ons = Onsets(rows=rows,
                 min_score=float(t.get("delay_min_score", 0.8)),
                 match_tol_ms=int(t.get("delay_match_tol_ms", 120)))
    return ons if len(ons) else None


def load_onsets(work_dir: Path, base: str, cfg) -> Onsets | None:
    """Read the onsets out of `<base>.speech.json`. A missing or broken file is not an error."""
    p = speech_path(work_dir, base)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return onsets_from(data, cfg)


def plan_delays(cues, onsets: Onsets | None, t: dict, cps: float | None = None) -> dict:
    """Push the lines that speak before the actor does, and only those.

    Three limits, and each one is there for a different reason:

      lead - delay_keep_lead_ms   a lektor that starts exactly on the first syllable sounds late;
                                  the card's own lead is the working room, so some of it is kept
      max_delay_ms                past this it stops being a nudge and becomes a re-timing, which
                                  was measured to be a net loss (-50.8 s of room over an episode)
      room left in the window     a delay must never create an overrun the speed planner then has
                                  to buy back, so the line has to still fit after being moved

    Never negative: subtitles lead speech deliberately, and moving a line EARLIER than its card
    would be inventing timing evidence rather than following it.
    """
    report = {"considered": 0, "matched": 0, "early": 0, "delayed": 0, "total_ms": 0,
              "max_ms": 0, "clamped_by_window": 0, "clamped_by_max": 0}
    if onsets is None or not cues:
        return report
    max_delay = int(t.get("max_delay_ms", 600))
    min_lead = int(t.get("delay_min_lead_ms", 300))
    keep = int(t.get("delay_keep_lead_ms", 100))
    safety = float(t.get("safety", 1.0))
    if max_delay <= 0:
        return report

    for c in cues:
        c.delay_ms = 0
        report["considered"] += 1
        onset = onsets.first_word_at(c.start_ms)
        if onset is None:
            continue
        report["matched"] += 1
        lead = onset - c.start_ms
        if lead <= min_lead:
            continue
        report["early"] += 1
        room = c.window_ms * safety - predicted_ms(c.text, t, cps)
        want = lead - keep
        delay = int(min(want, float(max_delay), room))
        if delay <= 0:
            continue
        if delay < want:
            report["clamped_by_max" if max_delay <= room else "clamped_by_window"] += 1
        c.delay_ms = delay
        report["delayed"] += 1
        report["total_ms"] += delay
        report["max_ms"] = max(report["max_ms"], delay)
    return report


def lead_counts(cues, onsets: Onsets | None, thresholds=(300, 500, 800)) -> dict:
    """How many lines start before the actor does, before and after the delay is applied.

    This is the metric the feature was designed against, so it is also the one that has to be
    reported: a delay planner that quietly moved nothing would otherwise look exactly like one
    that worked.
    """
    out = {"matched": 0, "before": {t: 0 for t in thresholds}, "after": {t: 0 for t in thresholds}}
    if onsets is None:
        return out
    for c in cues:
        onset = onsets.first_word_at(c.start_ms)
        if onset is None:
            continue
        out["matched"] += 1
        for t in thresholds:
            if onset - c.start_ms > t:
                out["before"][t] += 1
            if onset - (c.start_ms + getattr(c, "delay_ms", 0)) > t:
                out["after"][t] += 1
    return out


def describe_leads(counts: dict) -> str:
    parts = [f">{t} ms: {counts['before'][t]} -> {counts['after'][t]}"
             for t in sorted(counts["before"])]
    return f"lines leading the first word, {counts['matched']} matched  " + ", ".join(parts)


def describe_delays(report: dict) -> str:
    if not report.get("delayed"):
        return (f"no line moved ({report.get('early', 0)} early of "
                f"{report.get('matched', 0)} matched)")
    mean = report["total_ms"] / max(1, report["delayed"])
    return (f"{report['delayed']} of {report['early']} early lines pushed later "
            f"(mean {mean:.0f} ms, worst {report['max_ms']} ms; "
            f"{report['clamped_by_window']} held back by their own window)")


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
    # `scdet` rather than select='gt(scene,...)': on the same ten minutes of the same film, at
    # settings that are each side's sensible default, scdet found 44 cuts against 29. Both compare
    # frames pixel by pixel rather than by average brightness - two scenes that share a mean but
    # not their content do score high - but both look at LUMINANCE only, so a cut from a red scene
    # to an equally bright green one is invisible to them. Hence three detectors, one per plane.
    #
    # Three costs the same as one: 13.5 s per ten minutes either way, because decoding H.264 is the
    # whole bill and the filters are noise beside it. Two things that look like savings and are
    # not, both measured: decoding on the 3090 took 20.7 s against the CPU's 13.3, because every
    # frame has to come back over the bus for a CPU filter; and scaling down first changes nothing,
    # because the frame has already been decoded at full size by the time it can be scaled.
    logs = {p: log.with_name(f"cuts_{p}.txt") for p in "yuv"}
    graph = "[0:v]split=3[y][u][v];" + ";".join(
        f"[{p}]extractplanes={p},scdet=threshold={s.get('scene_threshold_scdet', 5)},"
        f"metadata=print:file={logs[p]}[o{p}]" for p in "yuv")
    cmd = [cfg.ffmpeg, "-v", "error", "-y", "-threads", str(s.get("ffmpeg_threads", 14)),
           "-i", str(video), "-an", "-sn", "-filter_complex", graph]
    for p in "yuv":
        cmd += ["-map", f"[o{p}]", "-f", "null", "-"]
    t0 = time.perf_counter()
    r = _run(cmd, timeout=s["ffmpeg_timeout_s"])
    took = time.perf_counter() - t0
    found = {p: f for p, f in logs.items() if f.is_file()}
    if r.returncode != 0 or not found:
        say(f"  !! scene detection failed ({r.stderr.strip()[-200:]}) – continuing without cuts")
        return [], took
    # scdet writes a score for EVERY frame and a `time` only where it decided there was a cut, so
    # the time key is the one to count. Counting frames instead reads as 14386 cuts in ten minutes,
    # which is simply the frame rate.
    at: set[int] = set()
    for f in found.values():
        at |= {round(float(m) * 1000) for m in
               re.findall(r"lavfi\.scd\.time=([0-9.]+)",
                          f.read_text(encoding="utf-8", errors="replace"))}
    # Merge near-simultaneous hits from different planes into one cut.
    cuts: list[int] = []
    for t in sorted(at):
        if not cuts or t - cuts[-1] > 80:
            cuts.append(t)
    return cuts, took


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
    """Word times for the English reference, from whichever aligner this machine can offer.

    The alignment itself is one script (`_FA_SCRIPT`) taking four arguments, and it does not care
    where it runs. Only the delivery differs, so `speech.aligner` picks between two:

      docker  hand it to the container named by speech.asr_container. This is how it started, and
              the container is INCIDENTAL - it was recycled from a sibling project because it
              already had torch on the GPU, not because anything here needs a container.
      local   run it with the interpreter in speech.aligner_venv, which needs torch and torchaudio
              and nothing else. No container, no daemon, no copying: the wav and the cues are
              already on this filesystem, which is also why this path is the simpler of the two.
      auto    local when a usable venv is configured, docker otherwise. The default, so a machine
              with neither still degrades the documented way - the caller catches the failure and
              writes an artefact carrying shot cuts only.

    Docker Desktop runs Linux containers on a WSL2 or Hyper-V backend, so on Windows the docker
    path drags in the very thing a native install is trying to avoid. That is the reason `local`
    exists rather than a preference: it is the only aligner a Windows or macOS box without a
    container runtime can use, and it costs the same GPU either way.
    """
    s = cfg["speech"]
    choice = str(s.get("aligner", "auto")).strip().lower()
    venv = str(s.get("aligner_venv", "") or "")
    if choice == "local" or (choice == "auto" and venv and venv_python(venv).is_file()):
        return _align_local(cfg, wav, cues, tmp)
    return _align_docker(cfg, wav, cues, tmp)


def _align_local(cfg, wav: Path, cues: list[dict], tmp: Path) -> dict:
    """Same script, this machine, no container. Paths are local, so nothing is copied."""
    s = cfg["speech"]
    venv = str(s.get("aligner_venv", "") or "")
    if not venv:
        raise RuntimeError(
            "speech.aligner is 'local' but speech.aligner_venv is empty -- point it at a venv "
            "with torch and torchaudio installed"
        )
    python = venv_python(venv)
    if not python.is_file():
        raise RuntimeError(f"no interpreter at {python} -- speech.aligner_venv must be a venv")

    cues_json = tmp / "fa_cues.json"
    cues_json.write_text(json.dumps(cues), encoding="utf-8")
    out = tmp / "fa.json"
    run = subprocess.run(
        [str(python), "-", str(wav), str(cues_json), str(out), str(s["fa_window_s"])],
        input=_FA_SCRIPT, capture_output=True, text=True, timeout=s["fa_timeout_s"])
    if run.returncode != 0:
        raise RuntimeError(f"forced alignment failed: {run.stderr.strip()[-400:]}")
    if not out.is_file():
        raise RuntimeError("forced alignment wrote no output")
    return json.loads(out.read_text(encoding="utf-8"))


def _align_docker(cfg, wav: Path, cues: list[dict], tmp: Path) -> dict:
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
