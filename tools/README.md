# tools

One-off and reference scripts. Nothing here is part of the pipeline: `wsl/lektor.py` is.

| script | what it does | status |
|---|---|---|
| `render51.py` | Thin wrapper: takes the mkv path the old renderer took and calls `lektor.py voice`. | **Folded in.** The 5.1 mix, the centre channel, the spectral duck, the neighbour-smoothed speeds and the streaming limiter live in `lektorlib.mix` / `lektorlib.stages` now. Kept only because this is the command that produced six episodes. Do not add features here. |
| `migrate_tts_cache.py` | Moves the paid ElevenLabs cache into the package's cache and key format, without buying a line again. | Done once, 3442 entries. Keep it: it is the only record of how the old key maps onto the new one. |
| `seed_condensed_cache.py` | Fills `<base>.condensed.json` from condensed text already sitting in `<base>.lektor.json`. | Done once, 1452 lines. Run it on any episode condensed before the reuse cache existed. |
| `ab_merge.py` | Renders the seams the merge gate refuses, both ways, over the real film audio, so the gate can be judged by ear. | Reference. This is what settles a question no measurement in this repo can. |
| `bench_condense.py` | Whole-episode condensation harness. Produced the model comparison quoted in `lektor.config.json`. | Reference; re-run it when changing model or prompt. It imports the package first and only falls back to the dead scratch module, so it still runs. |
| `voice_shootout.py` | Renders one hard 150 s window through N candidate voices, mixed exactly like an episode, so a voice is judged in context rather than on a dry sample. | Reference; imports the same dead scratch module. Re-point it at the package before using it. |

## The lesson these files are

`render51.py` and `voice_shootout.py` imported `lektor_original`, a module that lived in a scratch
directory under `/tmp`. That directory was wiped, and both had been dead code ever since – the renderer that produced every episode could not be run at all.

Its 300 MB ElevenLabs cache was in the same place, still holding every line of six episodes, worth
$4.27 to read again. It was still there when this was written, and it is now in the work dir on
`E:` where the pipeline looks for it.

**Anything that matters goes in the repo or in the work dir. Nothing that matters lives in `/tmp`.**

Run them with the project venv and the project `.env`:

    set -a && . ./.env && set +a
    .venv/bin/python tools/<script>.py
