# Handoff

Written at the end of the session that built everything below, while its scratch directory still
existed. Everything worth keeping has been copied into the repo; nothing here depends on `/tmp`.

Read `docs/timing-and-translation.md` for the reasoning behind the ranking. This file is the state
of play and the next three jobs.

---

## What the pipeline does today

Polish subtitle track in, a classic Polish lektor track out: one flat voice reading a condensed
script over the original audio, which ducks underneath it. Six episodes of Ted Lasso S02 have been
produced end to end and listened to.

```
Windows   lektor.ps1 -Stage extract     .srt (picked by language TAG) + stereo FLAC downmix
WSL       lektor.py speech              <base>.speech.json - word onsets, pauses, shot cuts   FREE
WSL       lektor.py script              condensed lektor script, editable JSON                ~$0.05
you       open <base>.lektor.json       fix anything that reads badly
WSL       tools/render51.py <mkv>       5.1 mix, lektor on centre, spectral duck, E-AC-3      ~$0.45
WSL       ffmpeg -c copy -> .mka        placed beside the video; MPC-HC auto-loads it
```

The renderer still lives in `tools/render51.py` rather than in the package. See job 4.

### Current settings, all measured rather than guessed

| what | value | why, in one line |
|---|---|---|
| voice | Mateusz `MM9TwXLP67IpAXY0UOGg`, stability 1.0 | chosen by ear from ten candidates on the same hard 150 s |
| duration model | `ms = 80 + 73.4 * chars` | fitted per voice; a flat chars/second is wrong for short cues |
| condensation | `openai/gpt-5.6-luna`, effort medium, whole episode, re-roll on a weak draw, escalate to `gpt-5.6-terra` | the model is a lottery whose bad draws are free to detect |
| merging | sentence-continuation only, gated on real pauses and shot cuts | text rules cannot tell whether the actors paused |
| ducking | spectral, 300-4000 Hz down 11 dB, rest 1.5 dB, hold 1500 ms | the film keeps its weight and air; masking is a midrange problem |
| levels | relative to programme loudness, presence shelf +6 dB at 4 kHz | an absolute dBFS target buried a quiet film |
| time compression | `speed` up to 1.2 only, smoothed across neighbours; `atempo` DISABLED | owner decision: stretching sounds worse than reading faster |

### Real costs, measured over six episodes

- OpenRouter: **$0.0505 per episode on average across 18 draws.** "A cent an episode" is one clean
  draw, not the mean.
- ElevenLabs: **~$0.45 an episode**, and **$4.27 to re-read the whole season**.
- **The TTS bill is 14-65x the LLM bill for the same change.** Any change to the script text is a
  TTS proposal wearing an LLM costume. Batch text changes; evaluate them on piper, which is free.

---

## The ranking, and what is actually built

From `docs/timing-and-translation.md`.

| # | item | status |
|---|---|---|
| 1 | **Merge gate** on real pauses and shot cuts | **DONE** - `speech.py`, wired through `srt.py` and `script.py`, 23 tests, applied to S02E06 |
| 2 | **Per-line delay** for lines that speak before the actor | **NOT BUILT** - job 1 below |
| 3 | **Condensed-text reuse cache** in `stage_script` | **NOT BUILT** - job 2 below, and it gates the economics of everything else |
| 4 | Craft prompt rewrite from the lektor research | **NOT APPLIED** - job 3 below; the research is in `docs/research/nf-pl.md` |
| 5 | Local condensation model on the 3090 | unmeasured; the only thing that takes the recurring LLM cost to zero |

Killed by name, with the number that killed each: re-timing every line to ASR onsets (−50.8 s of
room, speed-ups 4 → 41), diarization (27.41% DER on drama; the free SDH labels touch 4 of 46 seams
where the free pause gate touches 20 of 43), repairing the Polish subtitles (24 of 3103 groups
flagged, zero real errors in the twenty worst), raising `max_borrow_ms` (+201 s of room and still
exactly 4 speed-ups), replacing the ASR container (torch 2.6 pin).

---

## Job 1 - per-line delay (free, no text change, no cache miss)

**What.** A few lines start before the actor does. `<base>.speech.json` already holds the true word
onsets, so the fix is to push those lines later. Nothing else moves, no text changes, so the TTS
cache stays warm and the whole job costs **$0**.

**Size of the prize, measured.** At forced-alignment score >= 0.8, **24 of 250 lines lead by more
than 300 ms, 4 by more than 500 ms, worst +634 ms.** The +2003 ms cases reported earlier are
low-confidence alignment noise and must be excluded by score, not by magnitude.

**Where.** `assemble_voice` in `wsl/lektorlib/mix.py` places each clip at `cue["start_ms"]`. Give it
the events object and, for a cue whose aligned onset is confidently later than its card start, place
at the onset instead. The push-later cursor logic already handles the knock-on.

**Watch out for.**
- Only trust an onset whose alignment score clears `speech.min_score`. Everything else keeps today's
  behaviour.
- Never move a line EARLIER than its card. Subtitles lead speech on purpose.
- `tools/render51.py` has its own copy of the placement loop. Both must change, or fold them
  together first (job 4).
- Verify against the same metric the design used: count lines leading by >300 ms before and after.

---

## Job 2 - condensed-text reuse cache (the enabler)

**What.** `stage_script --force` re-condenses the whole episode even when only a handful of cues
changed. Cache the condensed text per cue, keyed on the input that produced it, and only send the
cues whose input actually moved.

**Why it matters more than it looks.** Every upstream change - a merge-gate tweak, a new cleaning
rule, a prompt edit - currently costs a full re-condensation of every episode touched. Measured:
**$0.71 to re-run the season, versus $0.09 with the cache.** It is what makes iterating on jobs 1,
3 and 4 affordable at all.

**Where.** `wsl/lektorlib/script.py`, `stage_script`. Key the cache on a hash of
`(cue text after cleaning, budget_chars, model, reasoning effort, prompt version)`. Store it beside
the script as `<base>.condensed.json`. On a run, split the target set into hits and misses, send only
the misses, merge the results.

**Watch out for.**
- The prompt is part of the key. Change `CONDENSE_SYSTEM` and every entry must miss, or you will
  silently ship old text under a new prompt.
- Whole-episode condensation sees the entire dialogue as context, so a cached line was produced
  under a slightly different context than a fresh one. This is acceptable - the alternative is
  paying every time - but say so in the config note.
- The re-roll and escalation logic scores the whole reply. With a partial send, the accept ratio
  must be computed over the cues actually requested, not over all of them.

---

## Job 3 - apply the craft prompt

**What.** `docs/research/nf-pl.md` is a research brief on how Polish lektor translation is actually
done - compression norms, what gets dropped first and what never does, how overlapping dialogue and
on-screen text are handled. It contains a rewritten `CONDENSE_SYSTEM` that was never applied.

**Cost.** +$0.00036 an episode in tokens. But it changes the text, so it costs **$0.71 an episode in
ElevenLabs re-reads** - which is the whole point of job 2 coming first, and of judging it on piper.

**How to judge it.** Not by reading the diff. Render the same episode both ways on the piper backend
(free) and listen. The existing `sample` stage does A/B on the same window.

---

## Job 4 - fold `tools/render51.py` into the package

The renderer that produces every episode is a 420-line script in `tools/`, and it carries features
the package does not have at all: 5.1 mixing, the lektor on the centre channel, spectral ducking,
neighbour-smoothed speeds. `lektorlib.stages.stage_voice` is still stereo with broadband ducking.

Two copies of the placement loop, the speed planner and the mix loop is how a fix lands in one and
not the other - it already happened once, when the ducker hold was added to the package and the
renderer kept calling the pre-refactor module.

`stage_voice` needs a channel layout and a ducking mode; then `render51.py` becomes a thin wrapper
and then nothing.

---

## Things that will bite you

- **`.env` and empty strings.** `load_dotenv` treats an empty environment variable as unset and
  loads the file anyway. An agent that set `OPENROUTER_API_KEY=` to prove it would spend nothing
  spent $0.0114. To really disable it, unset the variable or point the config at an invalid base.
- **The scratch directory is wiped between sessions.** It took the test fixtures with it once,
  mid-run, while a workflow was reading them. Anything that matters goes in the repo.
- **`pkill -f "lektor.py"` kills its own shell**, because the pattern matches the wrapper's own
  command line. Match on something narrower.
- **The ASR container belongs to another project** (`~/projects/yt-translator`). It is pinned to
  torch 2.6 and has been up for months. Use it, do not rebuild it.
- **E: is 99% full** with about 18 GB free, on a slow 9p mount. Read freely; write only under
  `_lektor`; never write a 2.7 GB file from WSL - remux on Windows with `windows/lektor_mux.ps1`.
- **ElevenLabs `speed` is hard-capped at 1.2.** 1.25 returns `invalid_voice_settings`. Published
  claims of a 0.25-4.0 range are wrong for this endpoint.
- **`\b` does not match before a minus sign**, which silently made every negative temperature
  unconvertible. And `-18 % 10` is 2 in Python, which gives the wrong Polish plural.

---

## What has never been verified by ear

- Whether refusing a merge actually sounds better. 22 A/B pairs on piper would settle it and cost
  nothing. **This is the single most valuable free experiment left.**
- Whether the craft prompt improves anything.
- The 300 ms pause threshold and the 5000 ms cap on the merge gate.
- Whether a local model on the 3090 can hold the JSON contract well enough to replace OpenRouter.
  Two hours, $0, and the existing `validate()` scores it.

---

## Corpora and research kept in the repo

| file | what it is |
|---|---|
| `docs/research/units_corpus.json` | 22 imperial-unit matches mined from 26 950 Polish subtitle lines across 44 films, each adjudicated. Two are real measurements. The false positives are the test suite for `units.py`. |
| `docs/research/nf-pl.md` | Polish lektor translation craft, with the unapplied prompt rewrite |
| `docs/research/bbc.md`, `itc.md`, `shot.md` | subtitling standards: reading speed, segmentation, shot-change rules |
| `docs/research/recon-apis.md` | OpenRouter and ElevenLabs API facts read from the live docs |
| `docs/research/recon-piper.md`, `recon-chatterbox.md` | measured local TTS backends: piper runs an episode in 38 s on CPU; chatterbox is ~40x slower and its cloning crashes |

Six `<base>.speech.json` artefacts already exist in the media work dir - regenerating one takes
about ten seconds.
