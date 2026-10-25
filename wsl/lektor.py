#!/usr/bin/env python3
"""lektor-ai: Polish voice-over for one episode, from the mkv to the remuxed mkv.

Everything that thinks lives here. The 2.7 GB sources are opened only where there is no way
round it – the extract and the mux – and every stage in between works on the small artifacts in
the work dir: a .srt, a stereo FLAC downmix, a JSON script.

The whole thing, unattended
---------------------------
  python3 wsl/lektor.py run --only S23E01     one episode
  python3 wsl/lektor.py all                   every episode in the directory, one after another

`all` takes the same --from/--to/--skip/--redo as `run` and applies them to each episode in
turn, so a season is one command. Each episode is still resolved, run and summarised on its own,
and one that fails does not stop the rest.

`run` walks extract -> translate -> speech -> script -> retime -> voice -> pack for exactly ONE
episode (translate only where extract found no Polish track, mux only on `--to mux`),
skipping every step whose artefact is already on disk, and stops the chain where carrying on
would ship something wrong. It prints a plan before it starts and a summary of what it did,
what it skipped and why, what it cost and where the file is, when it finishes.

  --from STEP --to STEP   run part of the chain (default: the whole of it)
  --skip a,b              leave steps out of the middle
  --redo STEP[,STEP]|all  rebuild artefacts that already exist (--force means --redo all)
  --require-speech        treat a failed measurement as fatal instead of a degradation
  --dry-run               print the plan and touch nothing

Stages, one at a time
---------------------
  extract    Pull the Polish subtitle stream and a stereo FLAC downmix out of the mkv, and
             record which file they came from. Skips the language-tagged forced and SDH tracks.
             A film with no Polish text track gets its best foreign text track instead, to
             translate from (translate.mode; 'never' restores the old skip).
  translate  Only for those films: the foreign subtitle into the Polish <base>.srt every later
             stage reads, in one whole-episode LLM call. Faithful, not condensed, timings 1:1.
             A hand-edited <base>.srt is never overwritten without --force.
  calibrate  Measure how fast the chosen voice actually speaks. Only backends that have to
             predict a duration need it; the rest say so and do nothing.
  speech     Measure what the actors and the editor actually did: forced-aligned word times off
             the centre channel, the silences between them, and the shot cuts. Free, local, and
             the only evidence the subtitle grid does not carry. Optional - without it the script
             stage merges on the text rules alone, exactly as before.
  script     Turn subtitles into a lektor script: clean, then condense the cues that will not
             fit their time window, in one whole-episode call to an LLM that sees the entire
             dialogue. Cheap, and the output is a plain JSON you are meant to open and edit
             before spending TTS credits.
  retime     Push the handful of lines that speak before the actor does onto his first word,
             using the onsets `speech` measured. Patches an existing script in place, changes no
             text, and therefore costs nothing at all - no LLM draw, no TTS re-read.
  voice      Read the script with the configured backend, fit each line to its slot, duck the
             original audio underneath it, and encode the mix.
  pack       Rewrap the encoded track as <base>.mka beside the film, tagged Polish and marked
             default, which is what a player auto-loads. Stream copy, no video touched - and
             where the chain stops unless --to says otherwise.
  mux        Copy the film and append the lektor track as a new default audio stream, keeping
             every original stream, subtitle and chapter untouched. Writes a second copy of the
             video, so it is opt-in: `--to mux`.
  sample     Render the same ~90 s of one episode several ways, into separate files, so the
             choice is made by ear: --backends piper,elevenlabs compares ENGINES, --models
             compares ElevenLabs models on the current one.
  cost       Dry run: characters and dollars. Touches no API.
  status     What exists on disk for each episode.

Secrets come from the environment or from the project .env, never from the config file:
  ELEVENLABS_API_KEY, OPENROUTER_API_KEY
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import numpy  # noqa: F401  -- imported here only to fail early with a useful message
    import soundfile  # noqa: F401
    import requests  # noqa: F401
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency ({exc})\n  pip install numpy soundfile requests")

from lektorlib import media, pipeline, stages, translate
from lektorlib.backends import BACKEND_NAMES
from lektorlib.config import DEFAULT_CONFIG, Config

backend_list = " | ".join(BACKEND_NAMES)


def check_backends(names: list[str]) -> list[str]:
    """Reject a misspelled backend here, before a stage loads a model or spends a credit."""
    for name in names:
        if name not in BACKEND_NAMES:
            raise SystemExit(f"unknown backend {name!r} -- pick from {backend_list}")
    return names


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("stage", choices=["run", "extract", "translate", "calibrate", "speech",
                                      "script", "retime", "voice", "pack", "mux", "all", "sample",
                                      "cost", "status"])
    ap.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--only", help="substring filter on the episode filename")
    ap.add_argument("--force", action="store_true", help="redo work that already exists")
    ap.add_argument("--forget-edits", dest="forget_edits", action="store_true",
                    help="script: treat NOTHING in the existing script as a hand edit and let "
                         "every line be redrawn (use when the pipeline froze lines you never "
                         "touched)")
    ap.add_argument("--redraw", action="store_true",
                    help="script: ignore <base>.condensed.json and pay for a clean draw; "
                         "translate: ignore <base>.translated.json the same way")
    ap.add_argument("--plan", default="creator", help="cost: elevenlabs plan for the allowance table")
    ap.add_argument("--seconds", type=float, default=90.0, help="sample: how much to render")
    ap.add_argument("--models", default="", help="sample: comma-separated model ids to compare")
    ap.add_argument("--backend", help=f"override voice.backend for this run ({backend_list})")
    ap.add_argument("--backends", default="",
                    help=f"sample: comma-separated backends to compare ({backend_list})")
    # `run` only, and refused on the other stages below rather than ignored there.
    step_list = " | ".join(pipeline.STEPS)
    ap.add_argument("--from", dest="from_step", default=pipeline.STEPS[0], metavar="STEP",
                    help=f"run: first step of the chain ({step_list})")
    ap.add_argument("--to", dest="to_step", default=pipeline.DEFAULT_LAST_STEP, metavar="STEP",
                    help=f"run: last step of the chain ({step_list})")
    ap.add_argument("--skip", default="", metavar="STEP,STEP",
                    help="run: steps to leave out of the range")
    ap.add_argument("--redo", default="", metavar="STEP,STEP",
                    help="run: rebuild these steps even though their artefact exists ('all' for "
                         "every selected step, which is what --force means here)")
    ap.add_argument("--require-speech", action="store_true",
                    help="run: abort when the speech measurement fails instead of continuing "
                         "with the pause gate off")
    ap.add_argument("--dry-run", action="store_true",
                    help="run: print the plan and touch nothing")
    args = ap.parse_args(argv)

    # A flag that means nothing to the chosen stage is a usage error, not a no-op. `voice
    # --dry-run` is the one that matters: silently ignored, it reads the whole episode and bills
    # for it under a flag whose entire promise is that it touches nothing.
    chain_only = [flag for flag, given in (("--from", args.from_step != pipeline.STEPS[0]),
                                           ("--to", args.to_step != pipeline.DEFAULT_LAST_STEP),
                                           ("--skip", bool(args.skip)),
                                           ("--redo", bool(args.redo)),
                                           ("--require-speech", args.require_speech),
                                           ("--dry-run", args.dry_run)) if given]
    if args.stage not in ("run", "all") and chain_only:
        ap.error(f"only `run` and `all` take {', '.join(chain_only)}; the stage {args.stage!r} would "
                 f"ignore that and do the work anyway")

    cfg = Config(args.config)
    if args.backend:
        cfg.raw["voice"]["backend"] = check_backends([args.backend])[0]
    backends = check_backends([b.strip() for b in args.backends.split(",") if b.strip()])

    if args.stage == "run":
        raise SystemExit(pipeline.stage_run(cfg, args.only, args))
    elif args.stage == "extract":
        media.stage_extract(cfg, args.only, args.force)
    elif args.stage == "pack":
        media.stage_pack(cfg, args.only, args.force)
    elif args.stage == "mux":
        media.stage_mux(cfg, args.only, args.force)
    elif args.stage == "translate":
        translate.stage_translate(cfg, args.only, args.force, args.redraw)
    elif args.stage == "calibrate":
        stages.stage_calibrate(cfg)
    elif args.stage == "speech":
        stages.stage_speech(cfg, args.only, args.force)
    elif args.stage == "script":
        stages.stage_script(cfg, args.only, args.force, args.redraw, args.forget_edits)
    elif args.stage == "retime":
        stages.stage_retime(cfg, args.only)
    elif args.stage == "voice":
        stages.stage_voice(cfg, args.only, args.force)
    elif args.stage == "all":
        raise SystemExit(pipeline.stage_run_all(cfg, args.only, args))
    elif args.stage == "sample":
        models = [m.strip() for m in args.models.split(",") if m.strip()] or [
            "eleven_flash_v2_5",
            "eleven_multilingual_v2",
        ]
        stages.stage_sample(cfg, args.only, args.seconds, models, backends)
    elif args.stage == "cost":
        stages.stage_cost(cfg, args.plan)
    elif args.stage == "status":
        stages.stage_status(cfg)


if __name__ == "__main__":
    main()
