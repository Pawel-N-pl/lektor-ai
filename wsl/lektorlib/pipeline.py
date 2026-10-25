"""The whole pipeline for one episode, in one command, with nobody watching.

`lektor.py run --only S23E01` walks extract -> translate -> speech -> script -> retime -> voice
-> pack (-> mux) and stops where the chain would otherwise ship something wrong. translate only
does anything on a film extract found no Polish track in; on every other film it is a skip. It
adds no processing of its own: every step is the same function the single-stage CLI calls, so
`run` and the same stages invoked by hand produce byte-identical artefacts.

Three things make that safe to fire and forget:

  * One episode, resolved once. `--only` is a substring filter that every stage re-globs on its
    own, and nothing in them enforces a single match, so a loose filter half-renders two episodes
    and every number in the footer becomes a lie. Here it is resolved against the videos and the
    subtitles before anything runs, and 0 or >1 matches is a usage error with the candidate list.

  * Skipping is the stages' own business. Five of the six steps already refuse to redo a finished
    artefact, and this module calls them and reports what they decided rather than reimplementing
    the rule. Two are pre-checked: `voice`, whose guard sits behind `backend.prepare()` so a chain
    that trusted it would load a TTS model to print "skipping", and `mux`, which is the one step
    whose input another step produces - with no track it prints "run the voice stage first" and
    returns 0, and the chain has to say that rather than blame the film. retime has no artefact at
    all: it recomputes delay_ms from scratch, changes no text, bills nothing, so it always runs.

  * The failure policy is the one already written in the code. speech is allowed to fail in
    pieces - no container, no English reference, no video - because everything downstream degrades
    to the text-only rules and says so in its own log; the chain warns and carries on (`--require
    speech` turns that into an abort). Everything else stops the chain, and condensation stops it
    hardest: `stage_script` exits non-zero precisely so raw subtitles never reach a backend that
    bills per character.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import media, say, stages, translate
from .config import Config, cps_path
from .mix import encode_profile, is_surround
from .script import read_spend
from .speech import find_reference_srt, find_video, reference_stream

__all__ = ["STEPS", "stage_run", "select_steps", "resolve_episode", "satisfied"]

STEPS = ("extract", "translate", "speech", "script", "retime", "voice", "pack", "mux")
# Where a chain stops when nobody says otherwise. `pack`, not `mux`: the deliverable is the lektor
# track as a .mka beside the film, which a player loads by itself, while `mux` writes a second copy
# of the video - 3.6 GB against 300 MB on a 45-minute episode - for a file the viewer already has.
# `--to mux` is still there for whoever wants one file.
DEFAULT_LAST_STEP = "pack"

OK, SKIPPED, WARNED, FAILED = "ok", "skipped", "warned", "failed"

RULE = "=" * 78


class UsageError(Exception):
    """Wrong request, nothing ran. Exit 2, so a script can tell it from a step that failed."""


class HardFail(Exception):
    """A step failed in a way that must stop the chain. Exit 1, footer still printed."""


@dataclass
class StepResult:
    step: str
    status: str
    note: str = ""
    seconds: float = 0.0


# ---------------------------------------------------------------------------
# What to run, and on what
# ---------------------------------------------------------------------------


def _named_steps(value: str, flag: str) -> list[str]:
    """Comma-separated step names, validated against STEPS with the flag named in the error."""
    names = [s.strip().lower() for s in (value or "").split(",") if s.strip()]
    for name in names:
        if name not in STEPS:
            raise UsageError(f"{flag}: unknown step {name!r} – pick from {', '.join(STEPS)}")
    return names


def select_steps(from_step: str, to_step: str, skip: str) -> list[str]:
    """The steps `run` will walk, in pipeline order: [from..to] minus --skip.

    Names are matched the way --skip and --redo match them, case and padding folded, so the four
    flags cannot disagree about what `Voice` means.
    """
    bounds = []
    for flag, value in (("--from", from_step), ("--to", to_step)):
        name = (value or "").strip().lower()
        if name not in STEPS:
            raise UsageError(f"{flag}: unknown step {value!r} – pick from {', '.join(STEPS)}")
        bounds.append(name)
    lo, hi = STEPS.index(bounds[0]), STEPS.index(bounds[1])
    if lo > hi:
        raise UsageError(f"--from {bounds[0]} is after --to {bounds[1]}; the order is "
                         f"{' -> '.join(STEPS)}")
    dropped = set(_named_steps(skip, "--skip"))
    chosen = [s for s in STEPS[lo:hi + 1] if s not in dropped]
    if not chosen:
        # Otherwise the chain runs zero steps, prints a summary of nothing and exits 0, which a
        # script driving this would read as "the episode is done".
        raise UsageError(f"--skip {skip!r} leaves nothing to run between {bounds[0]} and "
                         f"{bounds[1]}")
    return chosen


def candidate_bases(cfg: Config) -> list[str]:
    """Every episode this work dir could run, by base name.

    Both halves of the pipeline are asked, because the chain can start on either side of extract:
    a work dir the Windows stage already filled has subtitles and may have no video beside them,
    and a fresh directory has videos and nothing else.
    """
    out_dir = media.output_path(cfg, "x").parent.resolve()
    bases = {p.stem for p in cfg.work_dir.glob("*.srt")}
    for video in media.find_sources(cfg.input_dir, cfg["paths"]["work_subdir"]):
        # A finished episode is not a candidate. `find_sources` skips the work dir only, and the
        # output dir sits inside it by convention alone: point paths.output_subdir elsewhere and
        # every remuxed <base>_PL_LEKTOR.mkv comes back as an episode whose name CONTAINS the real
        # base, which resolve_episode then refuses as ambiguous - so the chain would stop being
        # able to run any episode it had already finished.
        if out_dir in video.resolve().parents:
            continue
        bases.add(video.stem)
    # A translated episode has no <base>.srt until `translate` has run, and its film may not be
    # under input_dir any more; its sidecar is then the only thing on disk that names it.
    tail = ".source.json"
    for p in cfg.work_dir.glob(f"*{tail}"):
        base = p.name[: -len(tail)]
        if translate.origin(cfg, base) == "subs":
            bases.add(base)
    return sorted(bases)


def resolve_episode(cfg: Config, only: str | None) -> str:
    """Exactly one episode, or a usage error naming the candidates.

    A substring that matches two episodes is refused rather than run twice: each stage would apply
    the filter again on its own, so the chain would interleave two renders and the cost footer
    would describe neither.
    """
    bases = candidate_bases(cfg)
    if not bases:
        raise UsageError(f"nothing to run: no *.srt in {cfg.work_dir} and no video under "
                         f"{cfg.input_dir}")
    hits = [b for b in bases if only in b] if only else list(bases)
    if not hits:
        raise UsageError(f"--only {only!r} matched nothing. Candidates:\n  "
                         + "\n  ".join(bases))
    if len(hits) > 1:
        what = f"--only {only!r} matched {len(hits)} episodes" if only else \
            f"{len(hits)} episodes here and `run` does one at a time"
        raise UsageError(f"{what}; narrow it with --only:\n  " + "\n  ".join(hits))
    base = hits[0]
    # A base that is a prefix of another one cannot be handed to the stages as a filter: they
    # match on `only in p.name`, so `S01E01` would also pull in `S01E01.PROPER`.
    shadowed = [b for b in bases if b != base and base in b]
    if shadowed:
        raise UsageError(f"{base!r} is a substring of {', '.join(shadowed)}, so no stage can be "
                         f"told to run it alone – rename one of the files")
    return base


def extract_artefact(cfg: Config, base: str) -> Path:
    """What extract wrote for this episode: <base>.srt, or the foreign subtitle to translate from.

    Read off the sidecar, because only extract knows which of the two a film gets. Before extract
    has run there is no sidecar, and the answer is the Polish file - which is also the right
    answer for every work dir the Windows side filled, whose sidecars predate translation.
    """
    info = translate.source_info(cfg, base)
    if (info.get("subtitle_origin") or "track") == "subs" and info.get("source_srt"):
        return cfg.work_dir / str(info["source_srt"])
    return cfg.work_dir / f"{base}.srt"


def artefact(cfg: Config, base: str, step: str) -> Path | None:
    """The file whose existence lets a step skip. None for retime, which produces no new file."""
    work = cfg.work_dir
    return {
        "extract": extract_artefact(cfg, base),
        "translate": work / f"{base}.srt",
        "speech": work / f"{base}.speech.json",
        "script": work / f"{base}.lektor.json",
        "retime": None,
        "voice": work / f"{base}.lektor.{encode_profile(cfg)['extension']}",
        "pack": media.pack_path(cfg, base),
        "mux": media.output_path(cfg, base),
    }[step]


def video_for(cfg: Config, base: str) -> Path | None:
    """The film `extract` would read, found the way `extract` finds it.

    Recursively, as `media.find_sources` does, and NOT via `speech.find_video`, which only looks at
    `input_dir/<base>.<ext>`: a season laid out one directory per episode has a film to downmix
    even though nothing lies beside the subtitles.
    """
    hits = media.find_sources(cfg.input_dir, cfg["paths"]["work_subdir"], base)
    return hits[0] if hits else None


def satisfied(cfg: Config, base: str, step: str) -> bool:
    """Is this step's work already on disk? The plan and the run share this, or the plan lies.

    Only `extract` needs more than its named artefact: it writes two files, and a work dir the
    Windows side filled routinely has the .srt and not the 300 MB downmix, which is the expensive
    half. Two cases make the downmix beside the point, and in both of them the subtitle really is
    the whole step: a surround mix cannot use it at all (`pick_source` takes nothing under six
    channels, so the two-channel FLAC is dead weight there, and the audio comes from the film or
    from <base>.orig.mka), and where there is no film in the tree it can never be written anyway.
    Demanding it in either case prints "run" against a step whose work is already done - and in
    the surround case actually re-cuts 300 MB that nothing will read.

    `translate` is satisfied by <base>.srt, and also by having nothing to do: a film whose Polish
    text came from its own track is finished with this step before it starts.
    """
    if step == "translate" and translate.origin(cfg, base) == "track":
        return True
    art = artefact(cfg, base, step)
    if art is None or not art.exists():
        return False
    if step == "extract":
        if is_surround(cfg):
            return True
        return (cfg.work_dir / f"{base}.orig.flac").exists() or video_for(cfg, base) is None
    return True


# ---------------------------------------------------------------------------
# The plan header: everything worth knowing before minute two
# ---------------------------------------------------------------------------


def _mb(path: Path) -> str:
    """Size of a finished artefact. KB below a megabyte, because a script is ~200 KB and "0 MB"
    reads like an empty file."""
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    return f"{size / 1048576:.0f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def speech_signals(cfg: Config, base: str) -> dict | None:
    """Which signals `<base>.speech.json` actually carries, or None when there is no artefact."""
    p = cfg.work_dir / f"{base}.speech.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {k: len(data.get(k) or []) for k in ("words", "gaps", "cuts")}


def warnings_for(cfg: Config, base: str, will_run: set[str]) -> list[str]:
    """The degradations that are invisible in the output file, said at second zero instead.

    Every one of these produces a finished episode that nothing downstream complains about, which
    is exactly why they belong in the header: nobody discovers them by listening.

    Keyed on the steps that will really execute, not on the selected range: a finished episode
    re-run for its footer skips voice and speech, and warning there about an uncalibrated engine
    describes a render that is not going to happen.
    """
    out: list[str] = []
    if "translate" in will_run and not os.environ.get("OPENROUTER_API_KEY"):
        # Before extract has run nobody knows yet whether the film has a Polish track, and a film
        # that has one never calls translate - so the stop is only certain for a translated film.
        if translate.origin(cfg, base) == "subs":
            out.append("OPENROUTER_API_KEY is not set: translate cannot run, and a film with no "
                       "Polish track has no Polish text without it – the chain will stop at that "
                       "step.")
        else:
            out.append("OPENROUTER_API_KEY is not set: if extract finds no Polish text track, "
                       "translate cannot run and the chain will stop there.")
    # Gated on the ENGINE, not just on the missing file. Four of the five backends never predict a
    # duration - piper binary-searches length_scale, omnivoice is handed the length in seconds,
    # chatterbox measures after the fact and kikivoice meets the window locally - and all four
    # implement set_chars_per_second as an explicit no-op. Warning them that "the engine predicts
    # its speed from timing.chars_per_second" states something untrue and sends the reader off to
    # spend six TTS calls on a number nothing will read.
    if "voice" in will_run and not cps_path(cfg).exists() and _predicts_duration(cfg):
        # Only the engine's own duration prediction is affected. The character budgets come from
        # timing.ms_per_char, so an uncalibrated backend is not a reason to spend six TTS calls.
        out.append(f"no calibration for {cfg.backend_name} yet: the engine predicts its speed "
                   f"from timing.chars_per_second ({cfg['timing']['chars_per_second']}) instead "
                   f"of a measured one. The budgets come from timing.ms_per_char "
                   f"({cfg['timing']['ms_per_char']}) and are unaffected – `calibrate` is "
                   f"optional, and it costs six TTS calls.")
    if "speech" in will_run and find_reference_srt(cfg, base) is None:
        video = video_for(cfg, base)
        pick = reference_stream(cfg, video) if video is not None else None
        if pick is None:
            out.append("no English reference subtitle beside the episode and none in the file "
                       "either: forced alignment cannot run, so this episode gets shot cuts only "
                       "and the pause gate stays off.")
        else:
            out.append(f"no English reference subtitle yet; `speech` will pull stream "
                       f"{pick.index} ({pick.title or pick.language}) out of the episode itself.")
    subs = media.subtitles_mode(cfg)
    if subs == "mux" and "mux" not in will_run and translate.origin(cfg, base) != "track" \
            and media.translate_mode(cfg) != "never":
        out.append("translate.subtitles is 'mux', but this chain does not remux: the Polish "
                   "subtitles go into a .mkv only with `--to mux`. 'sidecar' puts them beside the "
                   "film instead.")
    track = artefact(cfg, base, "voice")
    if "voice" in will_run and track.exists():
        # <base>.lektor.<ext> carries no backend tag, so the file gives no hint of what made it.
        out.append(f"{track.name} exists and will be overwritten by {cfg.backend_name}; the "
                   f"filename carries no backend, so a track paid for on another engine looks "
                   f"exactly like this one.")
    return out


def print_plan(cfg: Config, base: str, steps: list[str], redo: set[str],
               excluded: dict[str, str]) -> None:
    m = cfg["mix"]
    b = cfg.backend_cfg()
    say(RULE)
    say(f"RUN  {base}")
    say(f"  config   {cfg.path}")
    say(f"  work     {cfg.work_dir}")
    say(f"  voice    {cfg.backend_name}"
        + (f" / {b['model_id']}" if b.get("model_id") else "")
        + f", {m.get('channel_layout') or 'stereo'} -> "
        + f"{base}.lektor.{encode_profile(cfg)['extension']}")
    org = translate.origin(cfg, base)
    mode = media.translate_mode(cfg)
    if "translate" in steps and mode != "never" and org != "track":
        t = media.translate_settings(cfg)
        fb = str(t.get("fallback_model") or "")
        say(f"  text     translated by {t.get('openrouter_model', '?')}"
            + (f" (fallback {fb})" if fb else "")
            + f", subtitles {t.get('subtitles', 'off') or 'off'}"
            + ("" if org == "subs" else " – only if the film has no Polish text track"))
    say("")
    will_run: set[str] = set()
    for step in STEPS:
        if step not in steps:
            say(f"  {step:<9} -    {excluded[step]}")
            continue
        art = artefact(cfg, base, step)
        done = satisfied(cfg, base, step)
        if step == "translate" and (mode == "never" or org != "subs"):
            # Said rather than printed as "skip <base>.srt is already there", which on a film with
            # a Polish track reads as if something had been translated.
            if mode == "never":
                state = "skip translate.mode is 'never'"
            elif org == "track" or art.exists():
                state = "skip the Polish text comes from the film's own track"
            else:
                state = f"run  -> {art.name}, only if extract finds no Polish text track"
                will_run.add(step)
            say(f"  {step:<9} {state}")
            continue
        if step == "translate" and done and step not in redo \
                and translate.srt_state(cfg, base) == "edited":
            say(f"  {step:<9} skip {art.name} was edited by hand – kept (--redo translate replaces "
                f"it, after copying it aside)")
            continue
        if step == "retime":
            state = "run  free: recomputes delay_ms, changes no text, bills nothing"
        elif done and step not in redo:
            state = f"skip {art.name} is already there"
        elif done:
            state = f"REDO {art.name} will be rebuilt"
        elif step == "extract" and art.exists():
            # The .srt is there and the downmix the stereo mix reads is not, which is the half
            # that costs 300 MB and the minutes. Calling that "skip" was the plan promising a
            # no-op it could not keep.
            state = f"run  -> {base}.orig.flac ({art.name} is already there)"
        else:
            state = f"run  -> {art.name}" if art is not None else "run"
        if step == "retime" or step in redo or not done:
            will_run.add(step)
        say(f"  {step:<9} {state}")
    for note in warnings_for(cfg, base, will_run):
        say(f"\n  !! {note}")
    say(RULE)


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def _no_audio_reason(cfg: Config, base: str) -> str:
    """Why `voice` wrote nothing. Its own missing-source path is a `continue`, not an exit, so
    without this the chain would fail two steps later inside mux with an unrelated message."""
    if find_video(cfg, base) is None and not (cfg.work_dir / f"{base}.orig.flac").exists():
        return (f"no audio to mix over: neither {base}.orig.flac in the work dir nor "
                f"{base}.mkv beside the subtitles in {cfg.input_dir}")
    if is_surround(cfg):
        return (f"no source with 6 channels for {cfg['mix']['channel_layout']} – the film's own "
                f"track is the only thing here that carries them; set mix.channel_layout to "
                f"\"stereo\" to mix over the downmix instead")
    return "the voice stage wrote no track; its own log above says why"


def _run_step(cfg: Config, base: str, step: str, force: bool, redraw: bool,
              require_speech: bool, state: dict) -> tuple[str, str]:
    art = artefact(cfg, base, step)
    existed = art.exists() if art is not None else False

    if step == "extract":
        # The downmix counts too: a work dir the Windows side filled has subtitles and often no
        # FLAC, and the FLAC is the 300 MB half of this step. stage_extract writes it in either
        # layout - the stereo mix reads it directly, and in surround it is still the fallback
        # source - so its absence is real work outstanding, not a satisfied artefact.
        flac = cfg.work_dir / f"{base}.orig.flac"
        existed = satisfied(cfg, base, step)
        media.stage_extract(cfg, base, force)
        # Asked again afterwards: extract is what decides whether this film gets its Polish
        # track or a foreign one to translate, and a skip only means something when the answer
        # did not change under it.
        art, before = artefact(cfg, base, step), art
        existed = existed and art == before
        if not art.exists():
            lang = cfg["extract"]["subtitle_language"]
            if media.translate_mode(cfg) == "never":
                raise HardFail(f"no {art.name}: nothing here has a '{lang}' subtitle stream to "
                               f"extract, and every later step needs one")
            raise HardFail(f"no {art.name}: nothing here has a '{lang}' text subtitle stream, nor "
                           f"one in another language to translate from (the extract log above "
                           f"says which), and every later step needs one")
        if existed and not force:
            if flac.exists():
                return SKIPPED, f"{art.name} + {flac.name} were already there"
            # The downmix is not outstanding work here, it is work with no consumer. Said as
            # "skipped" with the reason, because reporting a step nothing did as "ok" reads like
            # a downmix that was written.
            why = ("nothing reads a 2-channel downmix in this layout"
                   if is_surround(cfg) else "no video here to downmix from")
            return SKIPPED, f"{art.name} was already there; no {flac.name} ({why})"
        # Reported from what is on disk afterwards, not from what was asked for: a work dir the
        # Windows side filled has the .srt and no video beside it, so this step legitimately
        # produces nothing at all and must not claim a downmix it never wrote.
        have = " + ".join(p.name for p in (art, flac) if p.exists())
        if not flac.exists():
            # Two different absences, and calling the deliberate one "no video here" sent the
            # reader looking for a file that was sitting right next to the subtitles.
            why = ("nothing reads a 2-channel downmix in this layout"
                   if is_surround(cfg) else "no video here to downmix from")
            have += f" (no {flac.name}: {why})"
        return OK, have

    if step == "translate":
        org = translate.origin(cfg, base)
        if org == "track":
            return SKIPPED, ("the Polish text comes from the film's own track – nothing to "
                             "translate")
        if org != "subs":
            if not art.exists():
                # `--from translate` or `--skip extract` on a film extract never saw. Said here,
                # not one step later as speech's "no .srt", and not as a skip that names a file
                # nobody wrote.
                raise HardFail(f"no {base}.source.json and no {art.name}: extract has not run for "
                               f"this episode, and every later step needs its Polish text")
            return SKIPPED, f"no {base}.source.json; {art.name} is taken as the Polish text"
        before = read_spend(cfg, base, "translate")
        try:
            what = translate.stage_translate(cfg, base, force).get(base, "")
        except SystemExit as exc:
            # Same contract as script: no Polish file means nothing downstream may run, and the
            # footer still prints before the exit code.
            raise HardFail(str(exc).strip()) from exc
        finally:
            state["translate_usd"] = max(0.0, read_spend(cfg, base, "translate") - before)
        if what == "never":
            return SKIPPED, "translate.mode is 'never'"
        if not art.exists():
            raise HardFail(f"translate wrote no {art.name}; its own log above says why")
        if what == "edited":
            return SKIPPED, (f"{art.name} was edited by hand after it was translated – kept "
                             f"(--redo translate replaces it, after copying it aside)")
        if what == "foreign":
            return SKIPPED, (f"{art.name} was already there and translate did not write it – "
                             f"kept (--redo translate replaces it, after copying it aside)")
        if what == "kept":
            return SKIPPED, f"{art.name} was already there"
        info = translate.source_info(cfg, base)
        return OK, (f"{art.name} from '{info.get('source_language', '?')}', "
                    f"${state['translate_usd']:.4f}")

    if step == "speech":
        try:
            stages.stage_speech(cfg, base, force)
        except SystemExit as exc:                      # only ever "no .srt" – that is fatal
            raise HardFail(str(exc)) from exc
        except Exception as exc:                       # noqa: BLE001 - documented recoverable path
            if require_speech:
                raise HardFail(f"{type(exc).__name__}: {exc}") from exc
            return WARNED, (f"{type(exc).__name__}: {exc} – merge gate off, no onsets; "
                            f"the script stage falls back to its text rules")
        if not art.exists():
            note = ("nothing measurable came back (no video beside the subtitles, or neither "
                    "signal survived) – merge gate off, no onsets")
            if require_speech:
                raise HardFail(note)
            return WARNED, note
        sig = speech_signals(cfg, base) or {}
        note = (f"{sig.get('words', 0)} words, {sig.get('gaps', 0)} pauses, "
                f"{sig.get('cuts', 0)} cuts")
        return (SKIPPED, f"{art.name} was already there ({note})") if existed and not force else \
            (OK, note)

    if step == "script":
        before = read_spend(cfg, base)
        try:
            stages.stage_script(cfg, base, force, redraw)
        except SystemExit as exc:
            # script.py exits non-zero when condensation failed, on purpose: the script is valid
            # but uncondensed, and running voice on it would pay a backend to read the full
            # episode. Turned into a HardFail so the footer still prints before the exit code.
            raise HardFail(str(exc)) from exc
        state["condense_usd"] = max(0.0, read_spend(cfg, base) - before)
        return (SKIPPED, f"{art.name} was already there") if existed and not force \
            else (OK, art.name)

    if step == "retime":
        try:
            stages.stage_retime(cfg, base)
        except SystemExit as exc:
            raise HardFail(str(exc)) from exc
        return OK, "delay_ms recomputed in place; no text changed, so nothing re-bills"

    if step == "voice":
        # Pre-checked rather than delegated: stage_voice's own guard runs after get_backend() and
        # backend.prepare(), which on a local engine loads a model into VRAM just to print
        # "skipping" - and on a paid one opens a session for an episode it will not render.
        if existed and not force:
            return SKIPPED, f"{art.name} was already there ({_mb(art)})"
        stages.stage_voice(cfg, base, force)
        if not art.exists():
            raise HardFail(_no_audio_reason(cfg, base))
        return OK, f"{art.name} ({_mb(art)})"

    if step == "pack":
        if existed and not force:
            # The sidecar is synced on a skip too: a fix to the translation has to reach the copy
            # beside the film even when the .mka has nothing new in it.
            side = media.sync_sidecar(cfg, base)
            return SKIPPED, (f"{art.name} was already there ({_mb(art)})"
                             + (f"; {side.name} up to date" if side else ""))
        track = artefact(cfg, base, "voice")
        if not track.exists():
            raise HardFail(f"nothing to pack: {track.name} was never written, and pack only "
                           f"rewraps a track that already exists")
        media.stage_pack(cfg, base, force)
        if not art.exists():
            raise HardFail(f"pack produced no {art.name}")
        side = media.sidecar_srt_path(cfg, base)
        written = media.subtitles_mode(cfg) == "sidecar" and side.exists() \
            and media.polish_subtitle_for(cfg, base) is not None
        return OK, f"{art.name} ({_mb(art)})" + (f" + {side.name}" if written else "")

    if step == "mux":
        if existed and not force:
            return SKIPPED, f"{art.name} was already there ({_mb(art)})"
        # Checked before ffmpeg is asked for anything: with no track, `stage_mux` prints "run the
        # voice stage first" and returns 0, and the chain would then blame a missing source video
        # and send the reader to a source.json that is not the problem. This is the `--skip voice`
        # and the `--from mux` case, i.e. the one a chain actually produces.
        track = artefact(cfg, base, "voice")
        if not track.exists():
            raise HardFail(f"nothing to mux: {track.name} was never written. mux only appends a "
                           f"track that already exists, so the voice step has to be in the range "
                           f"and has to have produced one")
        media.stage_mux(cfg, base, force)
        if not art.exists():
            raise HardFail(f"mux produced no {art.name}: no source video to remux against – "
                           f"check {base}.source.json and {cfg.input_dir}")
        return OK, f"{art.name} ({_mb(art)})"

    raise UsageError(f"unknown step {step!r}")


# ---------------------------------------------------------------------------
# The footer: what happened, what it cost, where it is
# ---------------------------------------------------------------------------


def print_summary(cfg: Config, base: str, results: list[StepResult], steps: list[str],
                  state: dict, wall_s: float) -> None:
    say("")
    say(RULE)
    say(f"SUMMARY  {base}   {wall_s:.0f} s total")
    for step in STEPS:
        r = next((x for x in results if x.step == step), None)
        if r is None:
            why = "not reached – the chain stopped earlier" if step in steps else "not selected"
            say(f"  {step:<9} {'-':<8} {'':>8}  {why}")
            continue
        say(f"  {step:<9} {r.status:<8} {r.seconds:>7.1f}s  {r.note}")

    say("")
    ext = encode_profile(cfg)["extension"]
    translated = translate.origin(cfg, base) == "subs"
    if translated:
        say(f"  source     {extract_artefact(cfg, base)}   (the foreign text the Polish was "
            f"translated from)")
    for label, path in (("subtitles", cfg.work_dir / f"{base}.srt"),
                        ("speech   ", cfg.work_dir / f"{base}.speech.json"),
                        ("script   ", cfg.work_dir / f"{base}.lektor.json"),
                        ("track    ", cfg.work_dir / f"{base}.lektor.{ext}"),
                        ("output   ", media.output_path(cfg, base))):
        if label.startswith("output") and translated and media.subtitles_mode(cfg) == "sidecar":
            side = media.sidecar_srt_path(cfg, base)
            say(f"  pl subs    {side if side.exists() else '-  (' + side.name + ' not written)'}")
        say(f"  {label}  {path if path.exists() else '-  (' + path.name + ' not written)'}"
            + (f"   {_mb(path)}" if path.exists() else ""))

    sig = speech_signals(cfg, base)
    if not ({"speech", "script", "retime", "voice"} & set(steps)) \
            or not (cfg.work_dir / f"{base}.lektor.json").exists():
        # Nothing in this range consumed the measurement, or the chain died before there was a
        # script for it to have gated: either way "produced with the pause gate off" would be a
        # sentence about an episode that does not exist.
        pass
    elif sig is None:
        say("\n  !! produced with the pause gate off and no onsets: no <base>.speech.json, so the "
            "merge ran on the text rules alone and no line was moved onto the actor's first word. "
            "That difference is inaudible as a defect – it just reads slightly early.")
    elif not sig["words"]:
        say("\n  !! shot cuts only, no word onsets: the pause gate was off and retime had nothing "
            "to move lines onto.")

    run_usd = state.get("condense_usd", 0.0)
    total_usd = read_spend(cfg, base)
    say("")
    say(f"  cost  condensation ${run_usd:.4f} this run, ${total_usd:.4f} for this episode so far "
        f"({base}.spend.json)")
    if translated or "translate_usd" in state:
        # The so-far figure above already includes it: one spend record per episode, two stages
        # appending to it.
        say(f"        translation  ${state.get('translate_usd', 0.0):.4f} this run, "
            f"${read_spend(cfg, base, 'translate'):.4f} of that total")
    if cfg.backend_name in ("piper", "chatterbox", "omnivoice"):
        say(f"        speech $0.00 – {cfg.backend_name} runs locally")
    elif cfg.backend_name == "kikivoice":
        say("        speech billed in credits, not dollars; the backend printed its own "
            "credits/quota summary above")
    else:
        say(f"        speech on {cfg.backend_name}: the billable characters and the dollars for "
            f"this run are in the voice step's report above; cached lines cost nothing")

    out = media.output_path(cfg, base)
    say("")
    if out.exists():
        say(f"  result  {out}   {_mb(out)}")
        say(f"  next    play it, and if a line reads badly edit {base}.lektor.json and rerun "
            f"`run --only {base} --from voice --redo voice,mux`")
    elif "mux" not in steps:
        # Not a failure: the range simply stopped short of the remux, and saying "not produced"
        # under an exit code of 0 reads like one.
        say(f"  result  no mux in this range – the finished file would be {out}")
        say(f"  next    `run --only {base}` to carry on where this stopped")
    else:
        say(f"  result  not produced – {out} is missing")
    say(RULE)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------



def _predicts_duration(cfg) -> bool:
    """Does the selected backend have a speaking rate to calibrate at all?

    Asked of the class rather than of a running engine: this is a warning printed before anything
    is loaded, and constructing a backend to answer it would be the one place that opens a network
    session or a model just to decide whether to print a sentence.
    """
    try:
        from .backends import get_backend
        return bool(get_backend(cfg.backend_name, cfg).needs_calibration)
    except Exception:
        return True          # unknown engine: say the cautious thing rather than stay silent

def stage_run_all(cfg: Config, only: str | None, args) -> int:
    """Every episode the filter matches, one after another, through the chain `run` walks.

    `run` refuses a filter that matches two episodes, and that refusal is right: the stages each
    re-glob on their own, so one loose `--only` would interleave two renders and leave a cost
    footer describing neither. Sequencing them here keeps that guarantee - each episode is
    resolved, run and summarised on its own, in file-name order - and gives a season a single
    command.

    A failing episode does not stop the ones behind it. On a season the usual cause is local to
    the file (no Polish track, no video beside the subtitles), and the episodes that can be
    rendered should be; the exit code still reports that something failed.
    """
    bases = candidate_bases(cfg)
    if not bases:
        raise UsageError(f"nothing to run: no *.srt in {cfg.work_dir} and no video under "
                         f"{cfg.input_dir}")
    hits = [b for b in bases if only in b] if only else list(bases)
    if not hits:
        raise UsageError(f"--only {only!r} matched nothing. Candidates:\n  " + "\n  ".join(bases))
    say(f"\n{len(hits)} episode(s) to run, in this order:")
    for i, base in enumerate(hits, 1):
        say(f"  {i}. {base}")
    codes = []
    for i, base in enumerate(hits, 1):
        say(f"\n{'#' * 78}\n### {i}/{len(hits)}  {base}\n{'#' * 78}")
        codes.append(stage_run(cfg, base, args))
    done = sum(1 for c in codes if c == 0)
    say(f"\n{'=' * 78}\nALL  {done} of {len(hits)} episode(s) finished without a failed step")
    for base, code in zip(hits, codes):
        say(f"  {'ok  ' if code == 0 else 'FAIL'}  {base}")
    say("=" * 78)
    return 0 if all(c == 0 for c in codes) else 1


def stage_run(cfg: Config, only: str | None, args) -> int:
    """Walk the pipeline for exactly one episode. Returns the process exit code.

    0 when every selected step ran or was already satisfied, 1 when a step failed, 2 when the
    request itself was wrong. The footer prints in all three cases, because a chain that died in
    the middle is exactly when it matters where it got to.
    """
    try:
        steps = select_steps(args.from_step, args.to_step, args.skip)
        # Checked up front: a typo in either would otherwise surface as a traceback out of the
        # plan, or - for subtitles - only once the chain reached pack.
        try:
            media.translate_mode(cfg)
            media.subtitles_mode(cfg)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        base = resolve_episode(cfg, only)
        redo = set(steps) if (args.redo or "").strip().lower() == "all" \
            else set(_named_steps(args.redo or "", "--redo"))
        if args.force:
            # Kept because every other stage takes --force, but it is the expensive spelling: in a
            # chain it re-cuts the FLAC, re-pays the forced alignment, redraws the condensation and
            # re-reads the whole episode on a backend that may bill per character.
            say("--force in a chain redoes every selected step; --redo script,voice is the "
                "surgical version. Neither deletes anything already paid for: <base>.condensed"
                ".json and the TTS cache are consulted as usual, and only --redraw throws the "
                "condensation away. What does cost money is redoing speech, whose new merge gate "
                "changes the cue texts and therefore misses the TTS cache line by line. A "
                "hand-edited translation is replaced too, after being copied aside.")
            redo |= set(steps)
        if args.redraw:
            # --redraw already implies --force inside stage_script, so it has to pull script into
            # the redo set here too, or the episode would be skipped before paying for the draw.
            if "script" not in steps:
                raise UsageError("--redraw only means anything for the script step, which is not "
                                 "in this range; drop it or widen --from/--to")
            redo.add("script")
        outside = redo - set(steps)
        if outside:
            raise UsageError(f"--redo names {', '.join(sorted(outside))}, outside the selected "
                             f"range {' -> '.join(steps)}")
    except UsageError as exc:
        say(f"lektor.py run: {exc}")
        return 2

    # Two different reasons a step is not in the chain, and the difference is the user's typo.
    # Re-derived through select_steps so the same name folding applies; it cannot raise here,
    # having already validated these two flags above.
    in_range = set(select_steps(args.from_step, args.to_step, ""))
    excluded = {s: ("--skip" if s in in_range else "outside --from/--to")
                for s in STEPS if s not in steps}
    print_plan(cfg, base, steps, redo, excluded)
    if args.dry_run:
        say("\n--dry-run: nothing was touched.")
        return 0

    results: list[StepResult] = []
    state: dict = {}
    failed = False
    t_all = time.time()
    for i, step in enumerate(steps, 1):
        say(f"\n[{i}/{len(steps)}] {step}")
        t0 = time.time()
        try:
            status, note = _run_step(cfg, base, step, step in redo, args.redraw,
                                     args.require_speech, state)
        # SystemExit is named because it is not an Exception: the stages use it for their own
        # hard guards (a missing API key, a missing script), and swallowing it here only long
        # enough to print the footer is the difference between "it died" and "it died at voice,
        # here is what is on disk and what it cost". Everything else is caught for the same
        # reason - a JSONDecodeError out of a half-written script used to escape this handler and
        # take the footer with it, which is precisely the run whose footer was worth having.
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            note = str(exc) if isinstance(exc, (HardFail, SystemExit)) \
                else f"{type(exc).__name__}: {exc}"
            results.append(StepResult(step, FAILED, note, time.time() - t0))
            say(f"\n!! {step} failed: {note}")
            failed = True
            break
        except KeyboardInterrupt:
            results.append(StepResult(step, FAILED, "interrupted", time.time() - t0))
            say(f"\n!! interrupted during {step}")
            failed = True
            break
        results.append(StepResult(step, status, note, time.time() - t0))

    print_summary(cfg, base, results, steps, state, time.time() - t_all)
    return 1 if failed else 0
