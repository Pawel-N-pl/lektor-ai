# lektor-ai

Turns Polish subtitle tracks into a classic Polish **lektor** track: one flat voice reading a
condensed script over the original audio, which ducks underneath it and comes back up between
lines. Not a second silent-gapped TTS track – a mix.

The work is split across the Windows/WSL boundary on purpose. Source files stay on `E:` and are
never copied: Windows does the gigabyte-scale I/O natively, WSL does the thinking on small files,
and both sides meet in a shared `_lektor` work dir that is the same folder seen from both worlds.

```
  E:\Filmy\<show>\                 /mnt/e/Filmy/<show>/          <- same directory
    *.mkv            2.7 GB        (never opened from WSL)
    _lektor\
      *.srt          ~40 KB        Windows writes  ->  WSL reads
      *.orig.flac    ~300 MB       Windows writes  ->  WSL reads
      *.lektor.json  ~150 KB       WSL writes      ->  you edit
      *.lektor.ac3   ~80 MB        WSL writes      ->  Windows reads
    _lektor\output\
      *_PL_LEKTOR.mkv              Windows writes
```

## Pipeline

| # | Where | Command | Does |
|---|-------|---------|------|
| 1 | Windows | `.\windows\lektor.ps1 -Stage extract` | Polish `.srt` + stereo FLAC downmix of the original audio |
| 2 | WSL | `python3 wsl/lektor.py calibrate` | measures how fast your voice really speaks (6 calls, once per voice; backends that set duration directly say so and do nothing) |
| 3 | WSL | `python3 wsl/lektor.py script` | subtitles → lektor script, condensing what will not fit |
| – | you | open `_lektor/*.lektor.json` | fix anything that reads badly, before spending TTS credits |
| 4 | WSL | `python3 wsl/lektor.py voice` | TTS, time-fitting, ducking, mix, encode |
| 5 | Windows | `.\windows\lektor.ps1 -Stage mux` | remux into `*_PL_LEKTOR.mkv`, stream copy only |

Progress at any point: `lektor.ps1 -Stage status` or `lektor.py status`.

`wsl/lektor.py` is the only thing you invoke; it is argument parsing and dispatch over the
`wsl/lektorlib/` package (`config`, `srt`, `audio`, `mix`, `script`, `pricing`, `stages`, and one
module per TTS backend under `lektorlib/backends/`).

### Choosing and comparing engines

```bash
python3 wsl/lektor.py voice  --backend piper                    # override voice.backend for one run
python3 wsl/lektor.py sample --backends piper,elevenlabs --seconds 60
python3 wsl/lektor.py sample --models eleven_flash_v2_5,eleven_multilingual_v2
```

`--backend` overrides `voice.backend` for a single run of any stage and validates the name before
anything loads a model or spends a credit. `sample` renders the same seconds of the same episode
several ways into separate files, so the choice is made by ear rather than from a table:
`--backends` compares ENGINES (each with its own configured voice), `--models` compares ElevenLabs
models on the current engine. Files land next to the episode as
`<base>.lektor.sample.<backend>.ac3` and `<base>.lektor.sample.<model>.ac3`.

## Backends

Three engines behind one interface. Duration fitting is backend knowledge, not pipeline knowledge:
each one hits its window its own way, and the voice stage reports what actually happened.

| | ElevenLabs | Piper | Chatterbox |
|---|---|---|---|
| cost | $0.05 / 1000 chars (Flash, Turbo), $0.10 (Multilingual v2, v3) | free | free |
| hardware | network | CPU | needs the GPU, 2.6 GB VRAM |
| speed, one 371-utterance episode | minutes, bounded by plan concurrency | **19.7 s cold, 0.7 s warm** | ~42 min of GPU for the same 17.7 min of speech |
| fits the window by | predicting from a measured chars/second, then `voice_settings.speed`, then local `atempo` off the CACHED mp3 | binary-searching `length_scale` per line, exact | nothing – generate, then `atempo`, and hope |
| lines inside their window (measured, 371 utterances) | – | **597 / 598**, worst overrun +4.1% | 3 of 4 on a 20 s slice, worst overrun +56% |
| voice cloning | a paid feature | no | yes, from an 8–20 s reference clip |
| calibration needed | yes | no | no |

**Piper** is the default recommendation and what everything below was measured on: free, offline,
deterministic and the only engine with a direct duration knob. 16 worker threads over one ONNX
session pinned to one intra-op thread each – measured at 35 cues/s, against 9.9 cues/s for the
obvious 16-threads-one-worker arrangement. CUDA buys nothing here (136.5 s vs 138.4 s on an early
measurement) so there is no GPU switch; do not install `onnxruntime-gpu`.

**ElevenLabs** sounds like broadcast and costs money, so it never generates the same line twice:
every call is content-addressed on disk, and a line that still overruns is stretched from the
cached bytes rather than re-bought. The rate is flat on every plan – what a plan buys is a monthly
allowance and a concurrency cap, never a cheaper character.

**Chatterbox** is the only one that can clone a voice, and it is slow enough that this is the only
reason to pick it: measured RTF 2.37 against Piper's 0.0185, i.e. the 17.7 minutes of speech in one
episode take about 42 minutes of a 3090 instead of 20 seconds. It also has no length control at
all, so a line that comes out long can only be stretched by `timing.max_atempo` and then overruns.
`dtype` must stay `bfloat16` or `float32`: `float16` overflows in the CFG combine and the CUDA
assert that follows poisons the context for the rest of the process, so it is refused outright.

## Setup

```bash
# WSL
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python numpy soundfile requests
uv pip install --python .venv/bin/python piper-tts        # only for the piper backend

source .venv/bin/activate   # every `python3 wsl/lektor.py ...` below assumes this

cp .env.example .env      # then put the two keys in it
```

`uv venv` does not activate anything and the dependencies go into `.venv` only, so a bare `python3`
in a fresh shell is the system interpreter and exits with `missing dependency (No module named
'soundfile')`. Either activate as above, or spell every command `.venv/bin/python wsl/lektor.py …`.

`.env.example` lists the two names: `OPENROUTER_API_KEY` (the script stage) and
`ELEVENLABS_API_KEY` (the elevenlabs backend). They are read from `<project root>/.env`
automatically, so nothing has to be exported first, and an already-exported variable still wins
over the file. Only those two names are ever taken from `.env`; anything else in it is ignored, so
a shared file cannot inject `PATH` into a subprocess.

**Piper voice**: downloaded on first use into `~/.cache/lektor-ai/piper-voices`, 63 MB, once. The
Polish shelf is `pl_PL-darkman-medium` (male, flat – the lektor profile), `pl_PL-bass-high`,
`pl_PL-mc_speech-medium`, `pl_PL-gosia-medium` (female) and `pl_PL-mls_6892-low`. If the download
fails the backend prints the two exact `curl` commands and deletes the half-written file.

**Chatterbox** needs its own python 3.11 venv with torch and chatterbox installed, pointed at by
`voice.chatterbox.venv`. The model is held by a worker subprocess launched from that venv and
spoken to over a JSON pipe, so the pipeline itself never imports torch.

Windows needs `ffmpeg.exe` and `ffprobe.exe`; point `paths.ffmpeg_windows` /
`paths.ffprobe_windows` at them. Everything else lives in `lektor.config.json`, which both halves
read, and nearly every key carries a `_<key>_note` explaining what it is and what was measured to
land on that value. No secrets in it.

## The three things this gets right that the naive version does not

**Subtitle tracks are chosen by language tag.** These releases carry 43 subtitle streams. Polish
sits at a different index in different files, and the index next to it is Portuguese. Nothing here
hardcodes a track number. The mux keeps all 43 and appends the lektor after every original audio
stream, tagging it `language=pol` / `title=Lektor AI (PL)`, making it the default and demoting the
originals – stream copy only, verified on a cut clip. Its audio index is counted off the source
with ffprobe, not assumed to be 1, or a release with a commentary track would get the commentary
labelled as the lektor.

**Lines are shortened, not just read faster.** A lektor script is an adaptation, not the subtitles
read aloud. A third to two thirds of cues cannot physically be spoken inside their slot. The whole
episode goes to an LLM in ONE call, in order, with the cues that need work marked inline as
`[id] >>> SKROC DO N ZNAKOW <<< text`; the model returns only those, having read the entire
dialogue as context. Measured against the same model batched: $0.196 vs $0.433 per episode, i.e.
2.2x cheaper AND every cue is condensed against the full episode rather than three neighbours.

Model comparison, whole-episode, effort medium, 210 overflowing cues, scored on how many landed
inside their character budget:

| model | $/episode | time | in budget |
|---|---|---|---|
| `openai/gpt-5.6-luna` | $0.0077 | 67 s | 206/210 – but see below |
| `openai/gpt-5.6-luna-pro` | $0.0168–0.0233 | 95 s | 207–208/210 |
| `google/gemini-3.6-flash` | $0.1963 | 93 s | 196/210 |
| `openai/gpt-5.6-terra` | $0.0373 | 31 s | 158/210 |
| `google/gemini-3.5-flash-lite` | $0.0140 | 11 s | 120/210 |

`luna` is a **lottery**: seven identical calls landed 207, 206, 205, 111, 93, 69 and 67 cues inside
budget, and the bad draws are visible in the bill before anyone reads the text, because the model
simply declines to think (513 reasoning tokens → 93/210; 5520 → 207/210). So the stage scores every
reply itself and re-rolls a bad draw (`accept_fit_ratio`, `max_rerolls`) instead of buying a
steadier model. Whatever OpenRouter reports in `usage.cost` is what gets written to
`<episode>.spend.json` – the price is never recomputed from token counts, which would miss
reasoning tokens.

**The original audio ducks.** A gain envelope is built from where the lektor actually speaks (not
from the subtitle timings, which are only an approximation of it), smoothed with a real
attack/release, and applied under the voice. Measured on the 60 s smoke slice: the original sits
**11.0 dB** under its between-lines level while the lektor talks (+4.000 dB of makeup in the gaps,
−6.999 dB under speech). The gaps are a level, not a bypass: at the shipped `program_gain_db` of +4
they are 4 dB above the source, and the one-pole release needs 7.4 s to come all the way back,
longer than most gaps between cues. With `program_gain_db` at 0 the stretches where it has settled
do come back bit-identical – 0.00e+00 across every one of them, 2.7% of that slice.

## Cost

`python3 wsl/lektor.py cost --plan creator` counts characters and prices them against the backend
that is actually selected, before you spend anything. Local backends print $0.00 rather than a
fake number. Rough shape for one Ted Lasso episode: ~18 500 characters of subtitles, ~370 utterances,
condensed to ~14 700.

ElevenLabs bills a flat rate per 1000 characters, the same on every plan:

| plan | included, Flash/Turbo | included, other models | concurrency |
|---|---|---|---|
| Free | 20 000 | 10 000 | 4 / 2 |
| Starter | 120 000 | 60 000 | 6 / 3 |
| Creator | 440 000 | 220 000 | 10 / 5 |
| Pro | 1 980 000 | 990 000 | 20 / 10 |
| Scale | 5 980 000 | 2 990 000 | 30 / 15 |
| Business | 19 800 000 | 9 900 000 | 30 / 15 |

Flash and Turbo are $0.05 per 1000 characters, everything else $0.10. Exceeding the concurrency cap
returns 429; `voice.concurrency` defaults to 8, which fits Creator with Flash. Cached lines cost
nothing on a rerun, and `cost` says so.

Condensation is the other bill and it applies to every backend, since they all read the same
script: around $0.014 an episode with the re-roll policy, i.e. well under a dollar for a season.

## Known rough edges

- **The character budgets are tighter than the flat model they replaced.** `timing.overhead_ms` /
  `ms_per_char` model duration as `47 + 73.5 * chars` ms, fitted on ElevenLabs output. Measured on
  S02E01 with the config exactly as it ships: **230** of 371 utterances over budget, against 195 for the
  flat 14.5 chars/s model over the same windows. (An earlier note here claimed 210 → 381; that pair
  was fitted before trimming was accounted for and before `max_borrow_ms` went from 700 to 1500,
  and neither number is reachable from this config.) How the re-roll policy behaves at 230 marked
  cues has not been measured – the one recorded real run marked 210 and landed 207 inside budget on
  its first draw. Nothing breaks either way: what stays over budget is simply read faster.
- **The duration model is one voice's.** `ms_per_char` was fitted on the ElevenLabs voice, and the
  script stage applies it whatever backend will speak. Piper measures every line for real and does
  not care, but the number of cues sent for condensation is set by somebody else's speaking rate.
- **Sentences spanning two cues.** Subtitles split one sentence across cues; the condenser sees the
  whole episode but still shortens per cue, so a line ending mid-clause stays mid-clause.
- **Chatterbox cannot aim at a window.** Nothing about generation targets a duration, so a long
  reading is stretched by at most `timing.max_atempo` and then simply overruns; on the 20 s smoke
  slice one line of four ran 56% past its slot.
- **The FLAC downmix is 5.1 folded to stereo** with ffmpeg's default coefficients. Fine for lektor,
  not a mastering decision.
- **`chars_per_second` starts as a guess** (14.5) until `calibrate` measures it, and only backends
  that predict duration can measure it at all.
- **The limiter has no lookahead.** It is sample-accurate and its state crosses the 10 s encode
  blocks, so a peak pulls down its own neighbourhood and releases over `mix.limiter_release_ms`
  rather than scaling a whole block. But the attack is instantaneous, so an isolated transient has
  its own peak flattened rather than being ramped into. It did not engage anywhere in the smoke run
  (peak 0.23 against a ceiling of 0.89).
- Cached TTS lives in `_lektor/.cache/`, keyed by everything that changes the waveform. Delete it
  to force regeneration; keep it and reruns are free.
