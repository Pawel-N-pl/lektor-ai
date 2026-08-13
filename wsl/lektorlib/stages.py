"""The six things the CLI can ask for.

Nothing in here knows which TTS engine is in use: it asks the registry for a backend, asks that
backend how many threads it tolerates, and hands it one Utterance at a time.

Two stages are big enough to live in their own module and are only re-exported here so the CLI
keeps one import: `script` (LLM condensation, its validation and its spend record) and `pricing`
(what the run costs on the selected backend).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import say
from .audio import apply_shelf, resample_linear
from .config import Config, cps_path
from .backends import get_backend
from .backends.base import Backend, Utterance
from .mix import (Clip, assemble_voice, build_duck_envelopes, db_to_lin, encode_profile,
                  is_surround, measure_program_level, normalize_voice, source_channels,
                  voice_channels, voice_comp_db, write_mix)
from .pricing import stage_cost, tts_usd, usd_per_1k
from .script import CONDENSE_SYSTEM, condense_episode, stage_script, whole_prompt
from .speech import (describe_delays, describe_leads, find_video, lead_counts, load_onsets,
                     plan_delays, stage_speech)
from .srt import Cue

__all__ = [
    "CONDENSE_SYSTEM",
    "condense_episode",
    "whole_prompt",
    "stage_script",
    "stage_speech",
    "stage_cost",
    "stage_calibrate",
    "stage_voice",
    "stage_sample",
    "stage_retime",
    "stage_status",
    "generate_clips",
    "report_clips",
]

# ---------------------------------------------------------------------------
# Calibration -- how fast does this voice actually talk?
# ---------------------------------------------------------------------------

CALIBRATION_LINES = [
    "Nie mam pojęcia, o czym mówisz, ale brzmi to całkiem rozsądnie.",
    "Zawodnicy wychodzą na murawę, a kibice zaczynają skandować.",
    "Powiedziałem mu wprost, że tak dalej być nie może.",
    "To był zdecydowanie najgorszy tydzień w moim życiu.",
    "Trener Lasso poprosił o rozmowę w cztery oczy.",
    "Poczekaj chwilę, muszę odebrać ten telefon.",
]


def stage_calibrate(cfg: Config) -> None:
    backend = get_backend(cfg.backend_name, cfg)
    if not backend.needs_calibration:
        say(f"\nbackend '{backend.name}' does not need calibration: it controls the duration of "
            f"each line directly, so there is nothing to predict and no rate to measure.")
        say(f"timing.chars_per_second feeds nothing on this backend: the script stage budgets "
            f"against the timing.overhead_ms / ms_per_char model, and only falls back to a flat "
            f"rate if ms_per_char is 0. Leave it at {cfg['timing']['chars_per_second']}.\n")
        return

    backend.prepare()
    say(f"calibrating backend '{backend.name}' on {len(CALIBRATION_LINES)} lines")
    total_chars = 0
    total_sec = 0.0
    try:
        for i, line in enumerate(CALIBRATION_LINES):
            r = backend.render(Utterance(index=i, text=line, target_ms=None))
            dur = len(r.audio) / r.sample_rate
            total_chars += len(line)
            total_sec += dur
            say(f"  {len(line):3d} chars in {dur:5.2f}s  ->  {len(line) / dur:5.2f} chars/s")
    finally:
        backend.close()

    cps = total_chars / total_sec
    b = cfg.backend_cfg()
    cps_path(cfg).write_text(
        json.dumps(
            {
                "chars_per_second": round(cps, 3),
                "backend": backend.name,
                "voice_id": b.get("voice_id", ""),
                "model_id": b.get("model_id", ""),
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    say(f"\nmeasured {cps:.2f} chars/s (config default was {cfg['timing']['chars_per_second']})")
    say(f"saved to {cps_path(cfg).name} – this is what the backend predicts each line's duration "
        f"from, so it sets how much faster the API is asked to read.")
    if float(cfg["timing"].get("ms_per_char", 0.0)):
        say(f"the character budgets do NOT move with it: timing.ms_per_char is "
            f"{cfg['timing']['ms_per_char']}, and the linear duration model wins over the flat "
            f"rate. Set ms_per_char to 0 to budget from the measured rate instead.")


# ---------------------------------------------------------------------------
# Voice stage -- generate, fit, duck, mix
# ---------------------------------------------------------------------------


def plan_speeds(cfg: Config, cues: list[dict], cps: float) -> list[float]:
    """Per-line reading speed, smoothed across neighbours instead of spiking on one line.

    A line at 1.2 between lines at 1.0 is heard as the lektor suddenly hurrying and then stopping.
    Spreading part of the speed-up over everything within speed_smooth_s turns that step into a
    gradient, and because the neighbours now finish earlier the crowded line has more room than it
    had alone - so the peak itself comes down, which is what speed_relax banks.

    The position used for smoothing is the cue's own card, not its delayed placement: speed is a
    question about fitting a window, and the delay does not change the window.

    Rounded to two decimals because that rounding is part of the TTS cache key. Changing it
    re-reads the episode.
    """
    t = cfg["timing"]
    max_speed = float(t["max_speed"])
    span = float(t.get("speed_smooth_s", 0.0))
    relax = float(t.get("speed_relax", 1.0))
    safety = float(t["safety"])
    raw = []
    for c in cues:
        window_s = c["window_ms"] / 1000.0 * safety
        raw.append(min(max_speed, max(1.0, (len(c["text"]) / cps) / window_s))
                   if window_s > 0 else 1.0)
    if span <= 0:
        return [round(x, 2) for x in raw]

    at = [c["start_ms"] / 1000.0 for c in cues]
    out = []
    for i in range(len(cues)):
        best = raw[i]
        j = i - 1
        while j >= 0 and at[i] - at[j] <= span:
            best = max(best, 1.0 + (raw[j] - 1.0) * (1.0 - (at[i] - at[j]) / span))
            j -= 1
        j = i + 1
        while j < len(cues) and at[j] - at[i] <= span:
            best = max(best, 1.0 + (raw[j] - 1.0) * (1.0 - (at[j] - at[i]) / span))
            j += 1
        out.append(round(min(max_speed, 1.0 + (best - 1.0) * relax), 2))
    return out


def generate_clips(cfg: Config, cues: list[dict], backend: Backend,
                   speeds: list[float] | None = None) -> list[Clip]:
    """Render every line through whichever backend is configured, in parallel where allowed."""
    sr = cfg["mix"]["sample_rate"]
    safety = cfg["timing"]["safety"]
    clips: list[Clip] = [Clip(cue=c) for c in cues]
    done = [0]

    def work(item) -> None:
        i, clip = item
        target_ms = int(clip.cue["window_ms"] * safety)
        r = backend.render(Utterance(
            index=i, text=clip.cue["text"], target_ms=target_ms or None,
            # The lektor's own neighbours, not the film's: this is what he just said and what he
            # is about to say, which is what makes the engine read a line as part of a delivery.
            # Taken from the cue if the caller decided them over the whole episode, because they
            # are part of the TTS cache key and must not depend on how much of the episode this
            # particular run happens to be rendering.
            prev_text=clip.cue.get("prev_text", cues[i - 1]["text"] if i > 0 else ""),
            next_text=clip.cue.get("next_text",
                                   cues[i + 1]["text"] if i + 1 < len(cues) else ""),
            speed_hint=speeds[i] if speeds else None,
        ))
        clip.audio = resample_linear(r.audio, r.sample_rate, sr)
        clip.speed, clip.atempo = r.speed, r.atempo
        clip.target_ms = target_ms
        # Measured off the audio that actually came back, not off what the backend intended.
        clip.achieved_ms = clip.audio.size / sr * 1000.0
        clip.billable_chars, clip.cached = r.billable_chars, r.cached
        done[0] += 1
        if done[0] % 25 == 0 or done[0] == len(clips):
            say(f"    {done[0]}/{len(clips)} lines")

    workers = max(1, backend.max_concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, enumerate(clips)))
    return clips


def report_clips(cfg: Config, backend: Backend, clips: list[Clip], seconds: float) -> None:
    """What the voice stage actually did, in numbers, per episode.

    Every figure here is measured from the audio that came back rather than from what the backend
    meant to do, so a backend that quietly misses its window cannot report a clean run.
    """
    spoken = [c for c in clips if c.audio.size]
    if not spoken:
        say("  !! no audio came back at all")
        return

    over = [c for c in spoken if not c.fits]
    faster = [c for c in spoken if c.speed > 1.005]
    stretched = [c for c in spoken if c.atempo > 1.01]
    cached = sum(1 for c in spoken if c.cached)
    chars = sum(c.billable_chars for c in clips)

    say(f"  backend {backend.name}: {len(spoken)} lines spoken"
        + (f", {len(clips) - len(spoken)} empty" if len(clips) != len(spoken) else ""))
    inside = len(spoken) - len(over)
    line = f"    {inside}/{len(spoken)} inside their window"
    if over:
        worst = max(over, key=lambda c: c.overrun_pct)
        line += (f", {len(over)} clamped and still over it "
                 f"(worst +{worst.overrun_pct:.1f}% on cue {worst.cue['id']})")
    say(line)
    say(f"    {len(faster)} read faster than natural"
        + (f" (up to {max(c.speed for c in faster):.2f}x)" if faster else "")
        + f", {len(stretched)} time-stretched"
        + (f" (up to {max(c.atempo for c in stretched):.2f}x)" if stretched else ""))

    if backend.billable:
        model = cfg.backend_cfg(backend.name).get("model_id", "")
        say(f"    {chars} billable characters -> ${tts_usd(chars, model):.2f} at "
            f"${usd_per_1k(model):.2f} per 1000 ({model or 'model unset'}); "
            f"{cached} line(s) came from cache and cost nothing")
    else:
        say(f"    0 billable characters, $0.00 -- {backend.name} is local; "
            f"{cached} line(s) came from cache")
    say(f"    {seconds:.1f} s wall clock for {spoken[-1].cue['end_ms'] / 60000.0:.1f} min "
        f"of programme")


def source_duration_s(cfg: Config, source: Path) -> float:
    """Length of the audio stream in seconds, from ffprobe. Falls back to the container's."""
    probe = cfg.raw["paths"].get("ffprobe_wsl") or "ffprobe"
    for args in (["-select_streams", "a:0", "-show_entries", "stream=duration"],
                 ["-show_entries", "format=duration"]):
        r = subprocess.run([probe, "-v", "error", *args, "-of", "csv=p=0", str(source)],
                           capture_output=True, text=True)
        raw = (r.stdout.strip().splitlines() or [""])[0]
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    raise RuntimeError(f"ffprobe could not measure {source.name}")


def pick_source(cfg: Config, base: str) -> Path | None:
    """The audio the lektor is mixed over.

    In a surround layout that is the film's own track, read straight out of the mkv: nothing else
    on this machine carries six channels, and the 2.7 GB file is decoded, never rewritten. In
    stereo it is the FLAC downmix the Windows extract stage leaves in the work dir, because a
    small local file beats decoding the film for a mix that only needs two channels.
    """
    flac = cfg.work_dir / f"{base}.orig.flac"
    video = find_video(cfg, base)
    order = [video, flac] if is_surround(cfg) else [flac, video]
    for path in order:
        if path is None or not path.exists():
            continue
        if not is_surround(cfg) or source_channels(cfg, path) >= 6:
            return path
    return None


def stage_voice(cfg: Config, only: str | None, force: bool, limit_s: float | None = None,
                suffix: str = "") -> None:
    m = cfg["mix"]
    sr = m["sample_rate"]
    ext = encode_profile(cfg)["extension"]

    scripts = sorted(cfg.work_dir.glob("*.lektor.json"))
    if only:
        scripts = [p for p in scripts if only in p.name]
    if not scripts:
        sys.exit(f"no *.lektor.json in {cfg.work_dir} -- run the script stage first")

    backend = get_backend(cfg.backend_name, cfg)
    backend.prepare()

    try:
        for script_path in scripts:
            data = json.loads(script_path.read_text(encoding="utf-8"))
            base = data["base"]
            out_path = cfg.work_dir / f"{base}.lektor{suffix}.{ext}"

            if out_path.exists() and not force:
                say(f"{base}: {out_path.name} exists, skipping (--force to redo)")
                continue
            source = pick_source(cfg, base)
            if source is None:
                say(f"{base}: !! no source audio with {'6 channels' if is_surround(cfg) else 'a track'} "
                    f"-- put the mkv beside the subtitles, run the Windows extract stage, or set "
                    f"mix.channel_layout to \"stereo\"")
                continue

            duration_s = source_duration_s(cfg, source)
            total_samples = int(duration_s * sr)
            if limit_s:
                total_samples = min(total_samples, int(limit_s * sr))

            # The budgets in this file were computed against this rate; a predicting backend has
            # to speak to the same number or its speed factors will not match them.
            cps = float(data["chars_per_second"])
            backend.set_chars_per_second(cps)

            # Everything that depends on a line's NEIGHBOURS is decided over the whole episode
            # before anything is cut away: the speed plan smooths across neighbours, and the
            # neighbours' text goes into the TTS request and therefore into its cache key. Deciding
            # them over a 60 s slice would give the boundary lines different keys from the same
            # lines in a full render, and a rendered sample would quietly buy audio the episode had
            # already paid for.
            everything = data["cues"]
            all_speeds = None
            if backend.wants_speed_plan and \
                    cfg.backend_cfg().get("speed_plan", "neighbour_smoothed") != "per_line":
                all_speeds = plan_speeds(cfg, everything, cps)
            for i, c in enumerate(everything):
                c["prev_text"] = everything[i - 1]["text"] if i > 0 else ""
                c["next_text"] = everything[i + 1]["text"] if i + 1 < len(everything) else ""

            # A cue starting past the end of the audio can only be cut back to nothing during
            # assembly, so it is dropped before it is spoken. That matters on a short test slice
            # against a full-episode script: without it the stage renders 598 lines for 60 s.
            end_ms = total_samples / sr * 1000.0
            keep = [i for i, c in enumerate(everything) if c["start_ms"] < end_ms]
            cues = [everything[i] for i in keep]
            speeds = [all_speeds[i] for i in keep] if all_speeds else None

            say(f"\n{base}  ({duration_s / 60:.1f} min, {len(cues)} lines, "
                f"backend {backend.name}, {m.get('channel_layout', 'stereo')} from {source.name})")
            if speeds:
                faster = sum(1 for s in speeds if s > 1.005)
                smoothed = sum(1 for i, c in enumerate(cues)
                               if speeds[i] > 1.005
                               and (len(c["text"]) / cps) <= c["window_ms"] / 1000.0
                               * cfg["timing"]["safety"])
                say(f"  speed plan: {faster} lines read faster, {smoothed} of them only to carry "
                    f"a crowded neighbour")

            say("  generating speech")
            t0 = time.time()
            clips = generate_clips(cfg, cues, backend, speeds)
            gen_s = time.time() - t0

            say("  assembling")
            voice, spans = assemble_voice(cfg, clips, total_samples)
            # Tone first, level second: the shelf changes what the voice sounds like, so the
            # normalisation that follows has to see the finished sound.
            voice = apply_shelf(cfg, voice, sr, float(m.get("voice_presence_db", 0.0)),
                                float(m.get("voice_presence_hz", 4000.0)))
            program_db = measure_program_level(cfg, source)
            voice *= normalize_voice(cfg, voice, spans, program_db)
            voice *= db_to_lin(float(m.get("voice_trim_db", 0.0)))

            envelopes = build_duck_envelopes(cfg, spans, total_samples)
            lektor_db = program_db + m["voice_over_program_db"] + voice_comp_db(cfg)
            under = (f"speech band {m['duck_mid_db']:.0f} dB, the rest {m['duck_rest_db']:.0f} dB"
                     if "mid" in envelopes else f"under it {m['duck_to_db']:.0f} dB")
            say(f"  ducking + mixing (programme {program_db:.1f} dB, lektor {lektor_db:.1f} dB on "
                f"{'/'.join(str(c) for c in voice_channels(cfg))}, {under})")
            limited = write_mix(cfg, source, voice, envelopes, out_path, total_samples)

            report_clips(cfg, backend, clips, gen_s)
            if limited:
                say(f"    {limited} block(s) the limiter had to pull down")
            say(f"  -> {out_path}")
    finally:
        backend.close()


def stage_sample(cfg: Config, only: str | None, seconds: float, models: list[str],
                 backends: list[str] | None = None) -> None:
    """Render the same seconds of the same episode several ways, into files you can A/B by ear.

    Two axes, never mixed in one run, because a file called `.sample.piper.eleven_flash_v2_5` would
    be a lie for two of the three backends:

      --backends piper,elevenlabs   one file per ENGINE, each with its own configured voice
      --models a,b                  one file per ElevenLabs MODEL on the current backend

    Whatever is being compared, everything else is held still: same cues, same window, same mix.
    """
    if backends:
        original = cfg.backend_name
        try:
            for name in backends:
                cfg.raw["voice"]["backend"] = name
                say(f"\n=== sample with backend {name} ===")
                stage_voice(cfg, only, force=True, limit_s=seconds, suffix=f".sample.{name}")
        finally:
            cfg.raw["voice"]["backend"] = original
        return

    b = cfg.raw["voice"].get(cfg.backend_name, {})
    if not models or "model_id" not in b:
        # A backend with no model to switch between (piper, chatterbox) samples exactly once.
        say(f"\n=== sample with backend {cfg.backend_name} ===")
        stage_voice(cfg, only, force=True, limit_s=seconds, suffix=".sample")
        return
    for model in models:
        b["model_id"] = model
        tag = "." + re.sub(r"[^a-z0-9]+", "-", model.lower())
        say(f"\n=== sample with {model} ===")
        stage_voice(cfg, only, force=True, limit_s=seconds, suffix=f".sample{tag}")


# ---------------------------------------------------------------------------
# Retime -- move the lines that speak before the actor, without touching a word
# ---------------------------------------------------------------------------


def stage_retime(cfg: Config, only: str | None) -> None:
    """Recompute `delay_ms` in a script that already exists, in place.

    Free, and free in the expensive sense: no text changes, so every ElevenLabs line stays in
    cache and the whole job bills nothing. It exists because the alternative -- re-running the
    script stage -- is a new condensation draw and a new set of texts, which is the one thing that
    does cost money. Run it after `speech`, or after changing any of the timing.delay_* keys.
    """
    scripts = sorted(cfg.work_dir.glob("*.lektor.json"))
    if only:
        scripts = [p for p in scripts if only in p.name]
    if not scripts:
        sys.exit(f"no *.lektor.json in {cfg.work_dir} -- run the script stage first")

    t = cfg["timing"]
    for path in scripts:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = data["base"]
        say(f"\n{base}")
        onsets = load_onsets(cfg.work_dir, base, cfg)
        if onsets is None:
            say("  no usable onsets in speech.json (or timing.max_delay_ms is 0) – nothing to do")
            continue

        # Only the four fields the planner reads are rebuilt; everything else in the document is
        # left exactly as it was, because this stage must never rewrite a line's text.
        cues = [Cue(idx=c["id"], start_ms=c["start_ms"], end_ms=c["end_ms"], raw="",
                    text=c["text"], window_ms=c["window_ms"], delay_ms=int(c.get("delay_ms", 0)))
                for c in data["cues"]]
        before = sum(1 for c in cues if c.delay_ms)
        moved = plan_delays(cues, onsets, t, data.get("chars_per_second"))
        say(f"  {onsets.describe()}; {describe_delays(moved)}")
        say(f"  {describe_leads(lead_counts(cues, onsets))}")

        by_id = {c.idx: c.delay_ms for c in cues}
        for c in data["cues"]:
            c["delay_ms"] = by_id.get(c["id"], 0)
        data["retimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = path.with_suffix(f".{os.getpid():x}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        say(f"  -> {path.name} ({before} delayed before, {moved['delayed']} now; no text changed)")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def stage_status(cfg: Config) -> None:
    srts = sorted(cfg.work_dir.glob("*.srt"))
    ext = encode_profile(cfg)["extension"]
    out_dir = cfg.input_dir / cfg["paths"]["output_subdir"]
    say(f"\n{'EPISODE':<52} {'SRT':>4} {'AUDIO':>6} {'SPEECH':>7} {'SCRIPT':>7} {'REUSE':>6} "
        f"{'TRACK':>6} {'MUXED':>6}")
    for p in srts:
        b = p.stem
        mark = lambda q: "yes" if q.exists() else "-"  # noqa: E731
        # AUDIO is whatever the mix would actually be laid over, which in a surround layout is the
        # film itself and not the FLAC downmix the stereo path wants.
        say(f"{b[:52]:<52} {'yes':>4} "
            f"{'yes' if pick_source(cfg, b) else '-':>6} "
            f"{mark(cfg.work_dir / f'{b}.speech.json'):>7} "
            f"{mark(cfg.work_dir / f'{b}.lektor.json'):>7} "
            f"{mark(cfg.work_dir / f'{b}.condensed.json'):>6} "
            f"{mark(cfg.work_dir / f'{b}.lektor.{ext}'):>6} "
            f"{mark(out_dir / f'{b}_PL_LEKTOR.mkv'):>6}")
    say("")
