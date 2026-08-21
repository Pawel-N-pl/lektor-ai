# Handoff

State of play after the session that built jobs 1 to 4 of the previous handoff. The four jobs are
done; what is left is two decisions that only a listener can make, and one experiment.

Read `timing-and-translation.md` for the reasoning behind the ranking, and its Corrections
section for the figures that have since moved.

---

## What the pipeline does today

Polish subtitle track in, a classic Polish lektor track out: one flat voice reading a condensed
script over the original audio, which ducks underneath it. Six episodes of Ted Lasso S02 have been
produced end to end and listened to.

```
Windows   lektor.ps1 -Stage extract     .srt (picked by language TAG) + stereo FLAC downmix
WSL       lektor.py speech              <base>.speech.json - word onsets, pauses, shot cuts   FREE
WSL       lektor.py script              condensed script, reusing <base>.condensed.json       ~$0.00-0.05
you       open <base>.lektor.json       fix anything that reads badly - your edits survive
WSL       lektor.py retime              push the lines that speak before the actor            FREE
WSL       lektor.py voice               5.1 mix, lektor on centre, spectral duck, E-AC-3      $0 warm
WSL       ffmpeg -c copy -> .mka        placed beside the video; MPC-HC auto-loads it
```

The renderer is no longer a script in `tools/`. `tools/render51.py` is a wrapper around
`lektor.py voice`, which grew the channel layout, the spectral ducking, the neighbour-smoothed
speed planner and the ElevenLabs neighbour context that used to live only there.

### Current settings, all measured rather than guessed

| what | value | why, in one line |
|---|---|---|
| voice | Mateusz `MM9TwXLP67IpAXY0UOGg`, **stability 1.0** | chosen by ear from ten candidates on the same hard 150 s; the config said 0.6 and was simply wrong |
| duration model | `ms = 80 + 73.5 * chars` | fitted per voice; a flat chars/second is wrong for short cues |
| condensation | `openai/gpt-5.6-luna`, effort medium, whole episode, re-roll on a weak draw, escalate to `gpt-5.6-terra` | the model is a lottery whose bad draws are free to detect |
| merging | sentence-continuation only, gated on real pauses and shot cuts | text rules cannot tell whether the actors paused |
| ducking | spectral, 300-4000 Hz down 14 dB, rest 3 dB, hold 1500 ms | the film keeps its weight and air; masking is a midrange problem |
| levels | relative to programme loudness measured on a **stereo fold**, presence shelf +6 dB at 4 kHz, +3 dB centre compensation | an absolute dBFS target buried a quiet film; one channel does not sum the way two correlated ones do |
| time compression | `speed` up to 1.2 only, smoothed across neighbours; `atempo` DISABLED | owner decision: stretching sounds worse than reading faster |
| placement | card start, plus `delay_ms` for lines that speak before the actor | free, bounded, and it moves 180 lines a season |

### Real costs, measured

- OpenRouter: **$0.0505 per episode on average across 18 draws** - before the reuse cache. With it,
  a rerun of an unchanged episode is **one call or none**: measured on S02E06, 245 of 246 lines
  reused and the text byte-identical.
- ElevenLabs: **~$0.45-0.71 an episode**, and **$4.27 to re-read the whole season**.
- **The TTS bill is 14-65x the LLM bill for the same change.** Any change to the script text is a
  TTS proposal wearing an LLM costume. Batch text changes; evaluate them on piper, which is free.

---

## The four jobs, and what they turned out to be

| # | item | status |
|---|---|---|
| 1 | **Merge gate** on real pauses and shot cuts | **DONE** earlier - `speech.py`, wired through `srt.py` and `script.py`, applied to S02E06 |
| 2 | **Per-line delay** for lines that speak before the actor | **DONE** - `speech.py` onsets, `mix.assemble_voice`, `lektor.py retime`, applied to all six scripts |
| 3 | **Condensed-text reuse cache** | **DONE** - `<base>.condensed.json`, keyed on a hash of the prompt itself; seeded from the six shipped scripts |
| 4 | Craft prompt rewrite from `docs/research/nf-pl.md` | **ALREADY SHIPPED, and the brief does not exist** - see below |
| 5 | Fold `tools/render51.py` into the package | **DONE** - and the renderer had been dead code for a day |
| 6 | Local condensation model on the 3090 | unmeasured; still the only thing that takes the recurring LLM cost to zero |

### Job 3 was not a job. Two corrections to the previous handoff

**`docs/research/nf-pl.md` is not a lektor craft brief.** It is a verbatim scrape of the *Netflix
Polish Timed Text Style Guide*, Zendesk furniture included, 23 sections about **subtitles**. It
contains no prompt and no Polish prompt text; `grep -c lektor` on it returns 0. The craft brief that
the ranking cites - Hendrykowski, Adamowicz-Grzyb, Belczyk, Garcarz, the ordered deletion list, the
30-40 % norm - lived in the scratch directory and was wiped with it.

**The craft prompt had already been applied**, in commit `a5f20a0`, fifteen minutes before the
handoff that called it "NOT APPLIED" was written. Every section the ranking asks for is in the
shipped `CONDENSE_SYSTEM`: the vocative is deletion rule 1, `BEZWZGLĘDNIE zachowaj imiona` is gone,
T/V is there, the vulgarity calibration is there.

**Do not apply `nf-pl.md` to the prompt.** It is written for a medium nobody reads aloud and it
contradicts shipped rules that are deliberate: numbers above ten in digits (the lektor cannot
pronounce a digit), expletives at equal severity (a flat voice amplifies a curse more than a
shouting actor does), transliterating foreign names (the voice reads English orthography correctly,
and the phonetic respellings in the config are there on purpose). It is a useful reference for
subtitle conventions and nothing else.

---

## What got rescued along the way

**The paid ElevenLabs cache was in a `/tmp` scratch directory of a previous session.** 8010 files,
300 MB, every line of six episodes, $4.27 to buy again.

Its path was `SC / "tts_cache"`, relative to the script's own location, so running the scratch copy
of the renderer put the cache in the scratch directory with it.

It is now in `<work>/.cache`, in the package's key format: `tools/migrate_tts_cache.py` recomputes
both key formats per cue and copies the blob across. It reproduced **100 % of all six episodes**,
which also proved the folded speed planner is bit-identical to the one it replaced. The migration
had to search for two things the config could not tell it: E01-E05 were read by Bill, E06 by
Mateusz, and three of the episodes were rendered before neighbour smoothing existed.

Under today's voice, **S02E06 renders for $0.00 and the other five would cost $3.50** - they are
Bill's, and were before this work started.

---

## Two decisions waiting for a listener

Both have their material already rendered. Neither is a code question.

**The merge gate: on or off.** The gate refuses a merge where the actors themselves paused or the
edit cut. Measured over all six episodes: of the 231 seams the text rules join, 117 (51 %) sit on a
real pause or a cut. The A/B pairs are in `<work>/ab/` (22 pairs, both ways, over the real film
audio) and the verdict recorded on them was **OFF wins**. `<work>/E06_MACIERZ.txt` is the follow-up:
the same episode rendered six ways, gate on/off against three pause settings, to check that verdict
in context rather than on isolated seams.

Note the inconsistency until it is settled: `lektor.config.json` says `merge_gate: "off"`, while
`S02E06.lektor.json` was produced with `"both"` (28 merges instead of 43). Whichever way it goes,
the other five episodes were built gate-off and re-running them with the gate on costs the lines
whose text changes.

**Comma pauses: full, gentle or strong.** `mix.pause_ratio` is 1.0 today, i.e. untouched. The
matrix in `E06_MACIERZ.txt` has the measurements: the engine's pauses run 180 ms median, 700 ms
worst, and only the excess above 120 ms is compressed, so a sentence end stays longer than a comma.
Fewer lines have to be pushed later as the pauses shorten - 70, 17 and 7 with the gate off.

The pause compression now runs in the package for **both** backends, so whatever is decided applies
to a piper A/B as well as to a paid render.

---

## What is still worth doing

- **A local condensation model on the 3090.** The only recurring dollar cost left. Two hours, $0,
  and the existing `validate()` scores it against luna's 207/210 best draw. Watch VRAM: Whisper is
  resident at ~3960 MiB and the aligner peaks at +3617 MiB, so a model over ~13 GB will contend.
- **Re-render the five Bill episodes as Mateusz**, once, deliberately, when the two decisions above
  are made. $3.50, and it is the only way the season is one voice.
- **`voice_shootout.py` still imports the dead scratch module** and does not run. `bench_condense.py`
  tries the package first and falls back, so it does.

---

## Things that will bite you

- **`.env` and empty strings.** `load_dotenv` treats an empty environment variable as unset and
  loads the file anyway. An agent that set `OPENROUTER_API_KEY=` to prove it would spend nothing
  spent $0.0114. To really disable it, unset the variable, or - better - point it at an invalid
  value: a 401 is not retryable, so the run fails loudly instead of quietly buying something. That
  is how the $0.00 claims in this file were verified.
- **The scratch directory is wiped between sessions.** It has now taken the test fixtures once and
  came within a wipe of taking $4.27 of audio. Anything that matters goes in the repo or the work
  dir.
- **`pkill -f "lektor.py"` kills its own shell**, because the pattern matches the wrapper's own
  command line. Match on something narrower.
- **The ASR container belongs to another project** (`~/projects/yt-translator`). It is pinned to
  torch 2.6 and has been up for months. Use it, do not rebuild it.
- **E: is 99% full** with about 14 GB free, on a slow 9p mount. Read freely; write only under
  `_lektor`; never write a 2.7 GB file from WSL - remux on Windows with `windows/lektor_mux.ps1`.
- **ElevenLabs `speed` is hard-capped at 1.2.** 1.25 returns `invalid_voice_settings`. Published
  claims of a 0.25-4.0 range are wrong for this endpoint.
- **The TTS cache key contains `previous_text` and `next_text`.** They are in the request, so they
  are in the key. Turning `send_neighbour_text` off re-reads the season.
- **`\b` does not match before a minus sign**, which silently made every negative temperature
  unconvertible. And `-18 % 10` is 2 in Python, which gives the wrong Polish plural.

---

## Corpora and research kept in the repo

| file | what it is |
|---|---|
| `docs/research/units_corpus.json` | 22 imperial-unit matches mined from 26 950 Polish subtitle lines across 44 films, each adjudicated. Two are real measurements. The false positives are the test suite for `units.py`. |
| `docs/research/nf-pl.md` | The Netflix Polish subtitle style guide, verbatim. **Not** a lektor brief, and not applicable to the prompt - see job 3 above. |
| `docs/research/bbc.md`, `itc.md`, `shot.md` | subtitling standards: reading speed, segmentation, shot-change rules |
| `docs/research/recon-apis.md` | OpenRouter and ElevenLabs API facts read from the live docs |
| `docs/research/recon-piper.md`, `recon-chatterbox.md` | measured local TTS backends: piper runs an episode in 38 s on CPU; chatterbox is ~40x slower and its cloning crashes |

All six `<base>.speech.json` exist in the work dir; regenerating one takes about ten seconds of
alignment plus a minute of scene detection.
