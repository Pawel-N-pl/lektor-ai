# tools

One-off and reference scripts. Nothing here is part of the pipeline: `wsl/lektor.py` is.

| script | what it does | status |
|---|---|---|
| `render51.py` | Thin wrapper: takes the mkv path the old renderer took and calls `lektor.py voice`. | **Folded in.** The 5.1 mix, the centre channel, the spectral duck, the neighbour-smoothed speeds and the streaming limiter live in `lektorlib.mix` / `lektorlib.stages` now. Kept only because this is the command that produced six episodes. Do not add features here. |
| `migrate_tts_cache.py` | Moves the paid ElevenLabs cache into the package's cache and key format, without buying a line again. | Done once, 3442 entries. Keep it: it is the only record of how the old key maps onto the new one. |
| `seed_condensed_cache.py` | Fills `<base>.condensed.json` from condensed text already sitting in `<base>.lektor.json`. | Done once, 1452 lines. Run it on any episode condensed before the reuse cache existed. |
| `ab_merge.py` | Renders the seams the merge gate refuses, both ways, over the real film audio, so the gate can be judged by ear. | Reference. This is what settles a question no measurement in this repo can. |
| `ab_mix.py` | Auditions what the mix does to the FILM rather than how the lektor sounds: one passage, many settings, voice silent by default, variants differing only in the numbers that touch the programme. Decodes the passage once into a cache next to the film, so re-running the same `--at/--len` starts encoding immediately. | Reference, and the one to reach for before changing a duck or a shelf. `--verify` nulls every variant against the source per channel, searching for the best integer offset: that is the check that catches an encoder delay, a swapped channel or a gain that is not what the config says. All three have shipped here at least once. |
| `fit_duration.py` | Refits `timing.overhead_ms` and `timing.ms_per_char` on a length-stratified corpus, measuring the clip AFTER `trim_silence` at speed 1.0, from a single word to a long sentence. | Reference. The two are one straight line fitted over a 43-63 character window, which cannot separate a fixed per-utterance cost from a per-character one - the split is decided by noise and every extrapolation inherits it. `stage_calibrate` does not fit them at all; it reports one characters-per-second ratio, a different model. |
| `bench_condense.py` | Whole-episode condensation harness. Produced the model comparison quoted in `lektor.tuning.toml`. | **Needs a corpus before it runs.** The config read is fixed (see below), but it still wants `tools/testwork/Ted.Lasso.S02E01.Test.srt`, which has no history in this repo: it lived in the wiped scratch directory. Point `load_cues(srt_path=...)` at any episode's `.srt` before re-running. It imports the package first and only falls back to the dead scratch module, so that part is fine. |
| `voice_shootout.py` | Renders one hard 150 s window through N candidate voices, mixed exactly like an episode, so a voice is judged in context rather than on a dry sample. | **Dead.** `import lektor_original` at line 27, at module level: `ModuleNotFoundError` on every invocation. Re-point it at the package before using it. Note `lektor.py sample --backends` now covers the same ground for whole engines. |

## Two things the config migration left behind

The JSON config became a TOML pair (`lektor.toml` for the 33 knobs you edit, `lektor.tuning.toml`
for the 200 measured constants beside it). `Config._read` still accepts a flat legacy `.json`, so
most things kept working - but `bench_condense.py` read the config itself, with `json.loads`, and
the rename left that call pointing at a TOML file. It raised on every invocation, `--help`
included. It now goes through `Config`, which also merges the tuning file underneath; the old
monolithic JSON carried those constants inline, so this is what keeps the benchmark measuring the
same thing it used to.

The temp config it writes stays JSON on purpose: a temp `.toml` would need a matching
`.tuning.toml` beside it to mean the same thing.

## The lesson these files are

`render51.py` and `voice_shootout.py` imported `lektor_original`, a module that lived in a scratch
directory under `/tmp`. That directory was wiped, and both had been dead code ever since - the
renderer that produced every episode could not be run at all. `bench_condense.py`'s benchmark SRT
went with it, which is why that script now has nothing to read.

Its 300 MB ElevenLabs cache was in the same place, still holding every line of six episodes, worth
$4.27 to read again. It was still there when this was written, and it is now in the work dir on
`E:` where the pipeline looks for it.

**Anything that matters goes in the repo or in the work dir. Nothing that matters lives in `/tmp`.**

Run them with the project venv and the project `.env`:

    set -a && . ./.env && set +a
    .venv/bin/python tools/<script>.py
