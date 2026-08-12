"""The six things the CLI can ask for.

Nothing in here knows which TTS engine is in use: it asks the registry for a backend, asks that
backend how many threads it tolerates, and hands it one Utterance at a time.

Two stages are big enough to live in their own module and are only re-exported here so the CLI
keeps one import: `script` (LLM condensation, its validation and its spend record) and `pricing`
(what the run costs on the selected backend).
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import soundfile as sf

from . import say
from .audio import apply_shelf, resample_linear
from .config import Config, cps_path
from .backends import get_backend
from .backends.base import Backend, Utterance
from .mix import (Clip, assemble_voice, build_duck_envelope, db_to_lin, measure_program_level,
                  normalize_voice, write_mix)
from .pricing import stage_cost, tts_usd, usd_per_1k
from .script import CONDENSE_SYSTEM, condense_episode, stage_script, whole_prompt

__all__ = [
    "CONDENSE_SYSTEM",
    "condense_episode",
    "whole_prompt",
    "stage_script",
    "stage_cost",
    "stage_calibrate",
    "stage_voice",
    "stage_sample",
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


def generate_clips(cfg: Config, cues: list[dict], backend: Backend) -> list[Clip]:
    """Render every line through whichever backend is configured, in parallel where allowed."""
    sr = cfg["mix"]["sample_rate"]
    safety = cfg["timing"]["safety"]
    clips: list[Clip] = [Clip(cue=c) for c in cues]
    done = [0]

    def work(item) -> None:
        i, clip = item
        target_ms = int(clip.cue["window_ms"] * safety)
        r = backend.render(Utterance(index=i, text=clip.cue["text"], target_ms=target_ms or None))
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


def stage_voice(cfg: Config, only: str | None, force: bool, limit_s: float | None = None,
                suffix: str = "") -> None:
    m = cfg["mix"]
    sr = m["sample_rate"]
    ext = cfg["encode"]["extension"]

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
            orig_path = cfg.work_dir / f"{base}.orig.flac"
            out_path = cfg.work_dir / f"{base}.lektor{suffix}.{ext}"

            if out_path.exists() and not force:
                say(f"{base}: {out_path.name} exists, skipping (--force to redo)")
                continue
            if not orig_path.exists():
                say(f"{base}: !! no {orig_path.name} -- run the Windows extract stage")
                continue

            info = sf.info(str(orig_path))
            total_samples = int(info.frames * sr / info.samplerate)
            cues = data["cues"]
            if limit_s:
                total_samples = min(total_samples, int(limit_s * sr))
            # A cue starting past the end of the audio can only be cut back to nothing during
            # assembly, so it is dropped before it is spoken. That matters on a short test slice
            # against a full-episode script: without it the stage renders 598 lines for 60 s.
            end_ms = total_samples / sr * 1000.0
            cues = [c for c in cues if c["start_ms"] < end_ms]

            # The budgets in this file were computed against this rate; a predicting backend has
            # to speak to the same number or its speed factors will not match them.
            backend.set_chars_per_second(data["chars_per_second"])

            say(f"\n{base}  ({info.duration / 60:.1f} min, {len(cues)} lines, "
                f"backend {backend.name})")
            say("  generating speech")
            t0 = time.time()
            clips = generate_clips(cfg, cues, backend)
            gen_s = time.time() - t0

            say("  assembling")
            voice, spans = assemble_voice(cfg, clips, total_samples)
            # Tone first, level second: the shelf changes what the voice sounds like, so the
            # normalisation that follows has to see the finished sound.
            voice = apply_shelf(cfg, voice, sr, float(m.get("voice_presence_db", 0.0)),
                                float(m.get("voice_presence_hz", 4000.0)))
            program_db = measure_program_level(orig_path)
            voice *= normalize_voice(cfg, voice, spans, program_db)
            voice *= db_to_lin(float(m.get("voice_trim_db", 0.0)))

            say(f"  ducking + mixing (programme {program_db:.1f} dB, lektor "
                f"{program_db + m['voice_over_program_db']:.1f} dB, under it {m['duck_to_db']:.0f} dB)")
            env = build_duck_envelope(cfg, spans, total_samples)
            write_mix(cfg, orig_path, voice, env, out_path, total_samples)

            report_clips(cfg, backend, clips, gen_s)
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
# Status
# ---------------------------------------------------------------------------


def stage_status(cfg: Config) -> None:
    srts = sorted(cfg.work_dir.glob("*.srt"))
    ext = cfg["encode"]["extension"]
    out_dir = cfg.input_dir / cfg["paths"]["output_subdir"]
    say(f"\n{'EPISODE':<52} {'SRT':>4} {'AUDIO':>6} {'SCRIPT':>7} {'TRACK':>6} {'MUXED':>6}")
    for p in srts:
        b = p.stem
        mark = lambda q: "yes" if q.exists() else "-"  # noqa: E731
        say(f"{b[:52]:<52} {'yes':>4} "
            f"{mark(cfg.work_dir / f'{b}.orig.flac'):>6} "
            f"{mark(cfg.work_dir / f'{b}.lektor.json'):>7} "
            f"{mark(cfg.work_dir / f'{b}.lektor.{ext}'):>6} "
            f"{mark(out_dir / f'{b}_PL_LEKTOR.mkv'):>6}")
    say("")
