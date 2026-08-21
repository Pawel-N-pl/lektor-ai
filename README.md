# lektor-ai

Turns Polish subtitle tracks into a classic Polish **lektor** track: one flat voice reading a
condensed script over the original audio, which ducks underneath it and comes back up between
lines. Not a second silent-gapped TTS track – a mix.

Everything runs on one machine. The pipeline opens the source video where it has to (shot
detection decodes the whole episode, and the 5.1 path reads the film's own surround track), and
every stage exists in POSIX Python that shells out to ffmpeg. The PowerShell in `windows/` is
still there and still works, because on a Windows+WSL box the 2.7 GB read and the 2.7 GB write
are faster native than across the 9p bridge – but nothing depends on it any more.

```
  <input_dir>/
    *.mkv             2.7 GB    the source, opened by extract, speech and the 5.1 mix
    _lektor/
      *.srt              ~40 KB   extract: the Polish subtitle stream, by language tag
      *.orig.flac       ~300 MB   extract: stereo downmix of a:0 (only a stereo mix needs it)
      *.source.json       <1 KB   extract: which file and which stream this base came from
      error_log.txt               extract: one tab-separated line per episode it had to skip
      *.speech.json     ~170 KB   speech: word onsets, the silences between them, shot cuts
      *.lektor.json     ~160 KB   script: the script -> you edit this
      *.condensed.json   ~70 KB   script: condensed text under its full contract, so a rerun is free
      *.spend.json        <1 KB   script: what OpenRouter really billed, per episode, cumulative
      *.lektor.eac3     ~200 MB   voice: the finished track
      clip_timing.csv    ~60 KB   voice: id, chars, speed, achieved vs target ms, per line
      .cps.<hash>.json    <1 KB   calibrate: measured chars/s, keyed by backend+voice+model
      .cache/                     content-addressed TTS bytes; delete to regenerate, keep and reruns are free
    _lektor/output/
      *_PL_LEKTOR.mkv             mux: stream copy, the lektor appended and made default
```

In the shipped 5.1 layout the mix is laid over the film's own surround track, read straight out of
the mkv and never rewritten, so `*.orig.flac` is only needed for a stereo mix.

## One command per episode

```bash
python3 wsl/lektor.py run --only S23E01
```

`run` is the whole chain: **extract → speech → script → retime → voice → mux**. Each step is a
stage you can also call on its own, and calling them by hand is exactly equivalent – `run` only
frames them, resolves the episode once and adds up what they spent.

| flag | what it does |
|---|---|
| `--only S23E01` | resolved ONCE, before any step runs, against the union of `input_dir/*.mkv` and `work_dir/*.srt`. Exactly one substring match or exit 2 with the candidate list; that exact base is then passed to every step, so no step can widen the selection on its own |
| `--from STEP` / `--to STEP` | trim the chain at either end. Defaults `extract` and `mux` |
| `--skip a,b` | subtracted from the selected range |
| `--redo STEP[,STEP]` / `--redo all` | per-step force. `run` **refuses a bare `--force`** and says so: in a chain a blanket force re-cuts the FLAC, re-pays ~2 min of GPU for shot cuts, redraws the condensation and re-reads the whole episode on a per-character backend |
| `--redraw` | script only: ignore `*.condensed.json` and pay for a clean draw. It implies `script` in the redo set, and is a usage error when `script` is outside the selected range |
| `--require speech` | turn the speech warning into an abort |
| `--dry-run` | print the plan header and stop |
| `--backend NAME` | overrides `voice.backend` for the whole chain |

Before touching anything it prints a plan header: the resolved base, the config path, the backend,
model and mix layout, and one line per step marked *run* / *satisfied by `<artefact>`* / *skipped
by `--skip`*. Three silent degradations are warned about in the same header, because each one
otherwise shows up only as a worse episode:

* no `.cps.<hash>.json` for this backend and voice – with `timing.ms_per_char` set to 67.0 that
  affects only the engine's own speed prediction, not a single character budget;
* no English reference `.srt` under `speech.reference_srt_patterns` – the pause gate will be off;
* a `--redo voice` about to overwrite a track made by an unknown backend, because
  `<base>.lektor.<ext>` carries no backend tag.

Failure policy is per step. A `SystemExit` out of the script stage propagates – it exists
precisely to stop the chain before a per-character backend reads uncondensed text – and so do the
no-srt and no-script guards; a backend exception, a mux ffmpeg failure and an extract that found
no subtitle stream are hard aborts. Only `speech` is wrapped in warn-and-continue, per its own
contract, unless `--require speech` was given. After `voice` returns, the existence of
`<base>.lektor.<ext>` is asserted, because the missing-source path inside that stage is a
`continue` and not an exit.

The footer is the point of the command: the status row for this episode before and after, one line
per step with ran/skipped/warned and wall time, every artefact written with its size, the final
`*_PL_LEKTOR.mkv` and its size, a cost block (condensation from this run plus the lifetime total in
`*.spend.json`; TTS in dollars on elevenlabs, in credits on kikivoice, an explicit $0.00 on piper,
chatterbox and omnivoice), the fit numbers, the measured lektor-over-film median LU on the
dialogue-reference path, an explicit note when the episode was made with the pause gate off or
with no onsets, and one next-action line. Exit codes: **0** every selected step ran or was already
satisfied, **1** a step failed, **2** a usage error (ambiguous or empty `--only`, `--redraw`
outside the range, a bare `--force`).

### The steps, if you would rather drive them yourself

| # | Command | Does |
|---|---------|------|
| 1 | `python3 wsl/lektor.py extract` | picks the Polish subtitle stream **by language tag**, writes `<base>.srt`, a stereo FLAC downmix of `extract.audio_stream`, and `<base>.source.json`. Recurses into subdirectories; skips a bitmap track by name instead of dying inside `-c:s srt` |
| – | `python3 wsl/lektor.py calibrate` | measures how fast the chosen voice really speaks (6 calls, once per voice). Backends that are given a duration rather than a rate say so and do nothing |
| 2 | `python3 wsl/lektor.py speech` | optional, free, local: forced-aligned word times, the silences between them and the shot cuts → `*.speech.json`. ~90 s per episode, no API of any kind. Run it before `script` and the merge gate and `retime` use it |
| 3 | `python3 wsl/lektor.py script` | subtitles → lektor script, condensing what will not fit. Reuses `*.condensed.json`, so only lines whose input really changed are redrawn |
| – | open `_lektor/*.lektor.json` | fix anything that reads badly, before spending TTS credits. Your edits survive the next `script --force` |
| 4 | `python3 wsl/lektor.py retime` | free, changes no text: pushes the handful of lines that speak before the actor onto his first word |
| 5 | `python3 wsl/lektor.py voice` | TTS, time-fitting, ducking, 5.1 mix with the lektor on the centre channel, encode |
| 6 | `python3 wsl/lektor.py mux` | remux into `*_PL_LEKTOR.mkv`, stream copy only |
| – | `python3 wsl/lektor.py status` | what exists on disk for each episode, one row per base |
| – | `python3 wsl/lektor.py all` | `script` + `voice` in one call, the old two-stage shorthand |
| – | `python3 wsl/lektor.py cost --plan creator` | characters and dollars, before anything is spent |
| – | `python3 wsl/lektor.py sample --seconds 90` | render the same 90 s several ways and pick by ear |

`--force` redoes work that already exists, on any single stage. `--only` is a plain substring
filter there, not the strict one-match resolution `run` does.

`wsl/lektor.py` is the only thing you invoke; it is argument parsing and dispatch over the
`wsl/lektorlib/` package: `config`, `srt`, `audio`, `media` (the two stages that open the .mkv
container), `speech`, `loudness`, `units`, `mix`, `script`, `pricing`, `venvs`, `stages`, and one
module per TTS backend under `lektorlib/backends/`.

## Setup

Two layouts. Pick one; the difference is only who opens the .mkv.

### (a) Linux or macOS, on its own

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python numpy soundfile requests
uv pip install --python .venv/bin/python piper-tts        # only for the piper backend
source .venv/bin/activate                                  # every command below assumes this

sudo apt install ffmpeg          # Debian/Ubuntu: gives both ffmpeg and ffprobe
brew install ffmpeg              # macOS

cp .env.example .env             # then put the two keys in it
```

Then two keys in `lektor.config.json`: `paths.input_dir_wsl` is the directory holding the `.mkv`
files (the name is historical, it is simply "the input directory as this OS sees it"), and
`paths.ffmpeg_wsl` / `paths.ffprobe_wsl` stay as `"ffmpeg"` / `"ffprobe"` when both are on PATH.
The `paths.*_windows` twins are read only by `windows/lektor.ps1`; nothing in Python ever reaches
for them, because a Windows binary cannot open the POSIX paths every stage hands it.

`uv venv` does not activate anything and the dependencies go into `.venv` only, so a bare `python3`
in a fresh shell is the system interpreter and exits with `missing dependency (No module named
'soundfile')`. Either activate as above, or spell every command `.venv/bin/python wsl/lektor.py …`.

`.env.example` lists the two names: `OPENROUTER_API_KEY` (the script stage) and
`ELEVENLABS_API_KEY` (the elevenlabs backend). They are read from `<project root>/.env`
automatically, so nothing has to be exported first, and an already-exported variable still wins
over the file. Only those two names are ever taken from `.env`; anything else in it is ignored, so
a shared file cannot inject `PATH` into a subprocess.

**ffmpeg and ffprobe are not optional and not only for extract.** `config`, `media`, `mix`,
`loudness`, `speech` and `stages` all shell out to them: decoding the source, measuring BS.1770
loudness, detecting shot cuts, stretching a clip, encoding the track.

### (b) Windows + WSL, sources on `E:`

The current layout here, and the reason the PowerShell exists: the sources sit on an NTFS drive,
Windows does the gigabyte-scale I/O natively, WSL does everything else, and both sides meet in the
same `_lektor` directory seen from both worlds.

```powershell
winget install Gyan.FFmpeg          # then point paths.ffmpeg_windows / ffprobe_windows at the exe
.\windows\lektor.ps1 -Stage extract
.\windows\lektor.ps1 -Stage mux
.\windows\lektor.ps1 -Stage status
```

WSL still needs its own ffmpeg (`sudo apt install ffmpeg`) and its own venv, exactly as in (a):
`paths.input_dir_windows` and `paths.input_dir_wsl` are two hand-maintained strings that must name
the same directory, and nothing verifies that they do. `<base>.source.json` carries both spellings
of the source path, so a work dir produced by either extract can be finished by either mux.

### Optional pieces, and what stops working without them

**piper voice** – downloaded on first use into `~/.cache/lektor-ai/piper-voices`, 63 MB, once. The
Polish shelf is `pl_PL-darkman-medium` (male, flat – the lektor profile), `pl_PL-bass-high`,
`pl_PL-mc_speech-medium`, `pl_PL-gosia-medium` (female) and `pl_PL-mls_6892-low`. If the download
fails the backend prints the two exact `curl` commands and deletes the half-written file.

**chatterbox** – its own python 3.11 venv, pointed at by `voice.chatterbox.venv`. **GPU only**,
2607 MiB resident in bfloat16 (3716 MiB in float32).

```bash
uv venv --python 3.11 ~/.cache/lektor-ai-chatterbox/venv-chatterbox
uv pip install --python ~/.cache/lektor-ai-chatterbox/venv-chatterbox/bin/python \
    torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python ~/.cache/lektor-ai-chatterbox/venv-chatterbox/bin/python chatterbox-tts
```

The model is held by a worker subprocess launched from that venv and spoken to over a JSON pipe, so
the pipeline itself never imports torch. `voice.chatterbox.dtype` must stay `bfloat16` or
`float32`; `float16` is refused by name.

**omnivoice** – same shape, its own python 3.11 venv at `voice.omnivoice.venv`, and the worker
imports **torch, omnivoice, numpy and soundfile**, failing with a named message if any of the four
is missing.

```bash
uv venv --python 3.11 ~/.cache/lektor-ai-omnivoice/venv-omnivoice
uv pip install --python ~/.cache/lektor-ai-omnivoice/venv-omnivoice/bin/python \
    torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python ~/.cache/lektor-ai-omnivoice/venv-omnivoice/bin/python \
    omnivoice numpy soundfile
```

**GPU only, and it means it**: the worker exits with a sentence when `torch.cuda.is_available()`
is False, naming the torch build it found. `prepare()` sets `PYTORCH_CUDA_ALLOC_CONF=
expandable_segments:True` for it, and the worker falls back from `device_map="cuda:0"` to loading
on CPU and moving, because on a shared GPU the single contiguous 2.28 GiB warmup allocation is
refused outright while a 1 GiB one succeeds. `voice.omnivoice.reference_wav` is the voice to clone,
3–10 s, kept outside the repo on purpose: it is a recording of a real person, not source code.

**kikivoice** – no venv, but the client lives in another repository. `voice.kikivoice.client_dir`
must point at the directory holding `kikivoice_client.py` and `kikivoice_batch.py` (here:
`/home/pawel/projects/kikivoice`); it is deliberately not vendored, so `--backend kikivoice`
without it exits with a named error rather than an ImportError. `voice.kikivoice.clone.sample_path`
is the voice sample to clone from, 5–120 s. The budget is a **weekly bucket of 80 000 credits**,
not money, and a render can **stop and exit 75** waiting for a human to solve a GeeTest challenge
in a browser – roughly every 30 accepted tasks. Re-running the same command resumes from cache.

**forced alignment for `speech`** – optional, and the only step that needs more than ffmpeg. Two
delivery routes, chosen by `speech.aligner` (`auto` by default):

* `local` – a venv named by `speech.aligner_venv` with torch and torchaudio in it, and nothing
  else. No container, no daemon, nothing copied: the wav and the cues are already on this
  filesystem. This is the only route a Windows or macOS box without a container runtime has.
* `docker` – hand the same script to a running container named by `speech.asr_container`
  (`asr` here). That container is **borrowed, not owned**: it belongs to `~/projects/yt-translator`
  and is used only for the torchaudio 2.6 already installed there, so the torch pin must not be
  disturbed. This repo installs nothing into it and writes nothing into its source tree.
* `auto` – local when a usable venv is configured, docker otherwise.

Without either, the stage says so and writes an artefact carrying **shot cuts only**: the merge
gate degrades to cut-only, `retime` has no onsets to move anything onto, and the pipeline is
otherwise byte-identical to a run without the artefact. Shot detection itself is plain ffmpeg
(`scdet`) and always works. Alignment also needs an English reference subtitle under
`speech.reference_srt_patterns` – the acoustic model is English – and without one the stage says
`no English reference subtitle` and skips straight to the cuts.

Everything else lives in `lektor.config.json`, which every side reads, and nearly every key carries
a `_<key>_note` explaining what it is and what was measured to land on that value. No secrets in it.

## Backends

Five engines behind one interface. Duration fitting is backend knowledge, not pipeline knowledge:
each one hits its window its own way, and the voice stage reports what actually happened, measured
off the audio that came back rather than off what the backend meant to do.

**`voice.backend` ships as `elevenlabs`**, so a bare `python3 wsl/lektor.py voice` on a fresh clone
spends money. `--backend piper` is the free equivalent and validates the name before anything loads
a model or buys a character.

| | ElevenLabs | piper | chatterbox | omnivoice | kikivoice |
|---|---|---|---|---|---|
| cost | $0.05 / 1000 chars (Flash, Turbo), $0.10 (Multilingual v2, v3) | free | free | free, GPU time only | no money: a weekly bucket of 80 000 credits, ~46 % of it per episode cloning, ~23 % on a library voice |
| hardware | network | CPU, 14 threads | GPU, 2607 MiB in bfloat16 | GPU, 3.03 GiB of weights, 3.96 GiB peak at batch 6, CUDA torch in a 3.11 venv | network, plus a HUMAN for the GeeTest roughly every 30 accepted tasks |
| speed, one 598-cue episode | minutes, bounded by plan concurrency | **19.7 s cold, 0.7 s warm** | RTF 2.37, i.e. ~42 min of GPU for 17.7 min of speech | RTF 1.38 one line at a time (~21 min); **RTF 0.317 batched** (~4.5 min) | 24 tasks at concurrency 3 in 27 s; ~47 requests an episode, i.e. 2 manual GeeTest solves |
| hits its window by | predicting from a measured chars/second, then `voice_settings.speed` ≤ 1.2, one call | binary-searching `length_scale`, 2.24 syntheses per cue, exact | nothing: it generates, and a long reading simply runs long | `generate(duration=…)` in seconds, one generation, median error 55 ms after `trim_silence` (65 ms batched) | asking at speed 1.0 always, then local `atempo` ≤ 1.2 off the CACHED bytes. A second reading is never bought |
| voice cloning | a paid feature | no | yes, from an 8–20 s clip | yes, from a 3–10 s clip | yes, from the configured sample |
| calibration | yes | no | no | no | no |

**piper** is the free default recommendation and what most of the numbers below were measured on:
offline, deterministic and the only CPU engine with a direct duration knob. 14 worker threads over
one shared ONNX session, one intra-op thread each – measured at ~35 cues/s, against 9.9 cues/s for
the obvious 16-threads-one-worker arrangement. The count is `voice.piper.concurrency` and it is
**14 of this machine's 16 by owner rule** (other containers share this WSL instance and must keep a
core), which costs about 1 % against the 35.2 cues/s the curve peaks at. Parallel output was
verified bit-for-bit identical to serial. CUDA buys nothing here (136.5 s against 138.4 s) so there
is no GPU switch; do not install `onnxruntime-gpu`. `noise_w_scale` stays at 0, or the same text at
the same `length_scale` varies 4783–5050 ms across eight runs and the search stops being exact.
A line that will not fit even at `search_min` 0.6 is handed back clamped and reported as clamped:
**4 lines on the real 598-cue episode, worst overrun +16 %** (`voice.piper._search_min_note`).

**ElevenLabs** sounds like broadcast and costs money, so it never generates the same line twice:
every call is content-addressed on disk, and the rate is flat on every plan – what a plan buys is a
monthly allowance and a concurrency cap, never a cheaper character. The current voice measured
16.92 chars/s against the previous voice's 14.888, and line-to-line loudness spreads 3.5 LU between
the 10th and 90th percentile, inside what per-cue levelling can hide.

**chatterbox** is slow enough that cloning is the only reason to pick it: RTF 2.37 against piper's
0.0185, i.e. one episode's 17.7 minutes of speech take about 42 minutes of a 3090 instead of 20
seconds. A cloned clip sits at cosine 0.82–0.85 to its reference against 0.63–0.67 for the builtin
voice. It has no length control at all, and with `timing.max_atempo` at 1.0 nothing stretches it
afterwards either, so a long reading simply runs into the silence after it. `dtype` must stay
`bfloat16` or `float32`: `float16` overflows in the CFG combine and the CUDA assert that follows
poisons the context for the rest of the process, so it is refused outright.

**omnivoice** is the first engine here asked for a LENGTH rather than a rate, which is the whole
reason it is interesting. `generate()` takes `duration=<seconds>`, so one generation lands in the
window: nothing is searched, predicted or re-bought. Two things about it are counter-intuitive and
both are measured. The raw clip comes back at **exactly target + 200 ms, six times out of six with
zero variance** – that tail is SILENCE, `trim_silence` takes it off downstream, and subtracting it
from the ask as well (as the first version did) spends it twice and leaves every line a fifth of a
second short. And **compression is the only regime that works**: asked for more time than a line
naturally takes, the model sometimes stretches and sometimes just reads it and stops, which is
harmless here because a lektor is only ever asked to hurry. `voice.omnivoice.batch_size` is the
number that decides whether this engine is usable at all: batching runs 4.4x faster with no loss of
fit (65 ms median against 55 ms), taking the episode from ~21 minutes of GPU to ~4.5. Takes shorter
than `min_fill_ratio` (0.95) of what the words need at `timing.max_speed` are re-rolled, because
this model returns STUBS rather than failing: 100 of 759 takes on one episode came back under half
the plausible length, and a stub fits any window, so they were being counted as lines that landed.

**kikivoice** is billed in a weekly credit bucket instead of money, which changes every decision.
`billable = False` on purpose, so `pricing.py` does not value its characters at the ElevenLabs rate
and print a dollar figure that is simply untrue; the backend reports credits itself. It never buys
a line twice: the request always goes at speed 1.0 and the window is closed locally with `atempo`
up to `local_atempo_max` 1.2 off the cached bytes – an explicit exception to the global
`timing.max_atempo` of 1.0, listened to separately on this engine's 24 kHz output, where the
narrower band masks part of the resampling artefacts. Requests are batched to 700 billable
characters or 25 cues, whichever comes first (a 40-cue batch came back `AI_SERVER_ERROR` **after**
taking 1754 credits). Calibration is off because six calls would be a fifth of the ~30-task budget
between two human CAPTCHA solves.

### Choosing and comparing engines

```bash
python3 wsl/lektor.py voice  --backend piper                    # override voice.backend for one run
python3 wsl/lektor.py sample --backends piper,elevenlabs --seconds 60
python3 wsl/lektor.py sample --models eleven_flash_v2_5,eleven_multilingual_v2
```

`sample` renders the same seconds of the same episode several ways into separate files, so the
choice is made by ear rather than from a table: `--backends` compares ENGINES (each with its own
configured voice), `--models` compares ElevenLabs models on the current engine. `--seconds`
defaults to 90. Files land next to the episode as `<base>.lektor.sample.<backend>.<ext>` and
`<base>.lektor.sample.<model>.<ext>`, in the same layout and codec as a real render.

A sample costs nothing on an episode that has already been rendered: the speed plan and the
neighbour text that go into the TTS cache key are decided over the whole episode before the slice
is cut, so a line in the sample has the same key it had in the full render.

## Subtitle tracks are chosen by language tag

These releases carry 43 subtitle streams. Polish sits at a different index in different files of the
same season (S03E01–E05 at 30, S03E06 at 31 because a forced track was added ahead of it), and the
index next to it is Portuguese. Nothing here hardcodes a track number: a stream is a candidate only
when its `tags.language` matches `extract.subtitle_language`, and it is rejected when its title
matches any of `extract.reject_title_patterns` or its disposition is `forced` – a forced track
carries signage, not dialogue. Both comparisons are case-insensitive, which is why `"forced"` and
`"Forced"` in that list are the same rule written twice. If every candidate is rejected the first
one is used anyway and named as a rejected fallback; only an episode with no candidate at all is
skipped, with a line appended to `_lektor/error_log.txt`.

The mux keeps all 43 and appends the lektor after every original audio stream, tagging it
`language=pol` / `title=Lektor AI (PL)`, making it the default and demoting the originals – stream
copy only, verified on a cut clip. **Its audio index is counted off the source with ffprobe, not
assumed to be 1**, or a release with a commentary track would get the commentary labelled as the
lektor.

## Lines are shortened, not just read faster

A lektor script is an adaptation, not the subtitles read aloud. A third to two thirds of cues cannot
physically be spoken inside their slot. The whole episode goes to an LLM in ONE call, in order, with
the cues that need work marked inline as `[id] >>> SKROC DO N ZNAKOW <<< text`; the model returns
only those, having read the entire dialogue as context. Measured against the same model batched:
$0.196 against $0.433 per episode, i.e. 2.2x cheaper AND every cue is condensed against the full
episode rather than three neighbours.

Model comparison, whole-episode, effort medium, 210 overflowing cues, scored on how many landed
inside their character budget:

| model | $/episode | time | in budget |
|---|---|---|---|
| `openai/gpt-5.6-luna` | $0.0077 | 67 s | 206/210 – but see below |
| `openai/gpt-5.6-luna-pro` | $0.0168–0.0233 | 95 s | 207–208/210 |
| `google/gemini-3.6-flash` | $0.1963 | 93 s | 196/210 |
| `openai/gpt-5.6-terra` | $0.0373 | 31 s | 158/210 |
| `google/gemini-3.5-flash-lite` | $0.021 | 11 s | 108–132/210 |

`luna` is a **lottery**: seven identical calls landed 207, 206, 205, 111, 93, 69 and 67 cues inside
budget, and the bad draws are visible in the bill before anyone reads the text, because the model
simply declines to think (513 reasoning tokens → 93/210; 5520 → 207/210). Whatever OpenRouter
reports in `usage.cost` is what gets written to `<episode>.spend.json` – the price is never
recomputed from token counts, which would miss reasoning tokens.

### The ladder: two draws, then a model that thinks

The stage scores every reply itself against `script.accept_fit_ratio` (0.9) and, below it, draws
again. What changed: `script.max_rerolls` is **1**, not 4, and after that the stage escalates to
`script.fallback_model` = `openai/gpt-5.6-luna-pro` with `fallback_rerolls` = 1. So the ladder is
two luna draws, then two luna-pro draws, and the best reply across all four wins – escalating can
never make the result worse.

Four rolls were sized for a lottery, and for a ladder that until the seed fix **was not rolling at
all**: every re-roll sent the same seed, so "four re-rolls" was one draw billed five times, and
every cost comparison built on that number was fiction. Each attempt now derives a distinct seed
from the attempt index, and a draw that comes back byte-identical to the previous one stops the
ladder immediately and escalates, because it proves the endpoint is ignoring the seed.

Measured on one hard episode, the same episode all three ways:

| ladder | cost |
|---|---|
| five luna draws then terra (the old shape) | **$0.5926**, 79 % of it terra, which bills ten times the rate and still missed |
| two luna draws then luna-pro (what ships) | **$0.3034** |
| luna-pro alone, four calls | **$0.1796** |

terra was the wrong fallback on the numbers already in the config: the bar it is escalated to clear
is 0.9, and terra scores 158/210, which is 75 %. luna and luna-pro bill at the same rate ($0.20 in,
$1.20 out per Mtok), so escalation buys effort, not a price tier – luna-pro spends 3681 reasoning
tokens a call against luna's 222. On easy material luna clears the bar on the first draw for cents;
only dense fansub material ever reaches the escalation.

OpenRouter calls are spaced by `script.request_gap_ms` (400 ms) with a hard cap of 3 concurrent
calls, process-wide. One episode is one call per draw, so this is not for the pipeline: it is for
benchmarks fanning several models out at once, which from the provider's side is one account
hammering it.

### The lektor does not invent a gender the source never stated

The condensation prompt has a rule that costs characters and is not negotiable: **a genderless form
is never turned into a gendered one.** Polish lets you speak without marking gender and dialogue
constantly does; the model has the text and no picture, so it cannot know. Present and future tense,
the imperative, infinitives and impersonal forms are safe; past tense, conditionals, participles and
predicative adjectives are not. When the shorter variant would require a gender, the model keeps the
longer genderless one or cuts elsewhere – the character budget is not an excuse to guess.

One named case, which is why the rule exists: *"żeby to była ostatnia rzecz, jaką zrobisz"* states
no gender; the shorter *"byś tego nie przeżył"* states a man. The line was spoken to a woman. From
the text alone nobody catches that afterwards.

It is a prompt rule, so it costs one full redraw to introduce: `CONDENSE_SYSTEM` is hashed into the
reuse key, and changing it misses every cached line by design – $0.71 an episode, $4.27 a season.

## Cleaning, before anything is measured or bought

`clean.*` runs on every cue, before budgets and before the LLM.

* **`skip_patterns`** – a cue matching any of them never reaches the lektor. Music notes (matched
  **anywhere** in the cue now, not as an opening-and-closing pair: `♪TRENERNATE - #AFCRICHMOND` opens
  with one, never closes it, and was read aloud), blank cues, and three release-signature patterns:
  `napisy24`, `.::…::.`, and a line consisting only of *Tłumaczenie / Synchronizacja / Synchro /
  Korekta*.
* **`strip_patterns`** – markup, HI tags, square brackets and parentheticals.
* **`pronunciation`** – foreign names respelled the way a Polish voice must say them, matched on the
  stem so the case ending follows (`Rojas` → `Rohas`, `Rojasowi` → `Rohasowi`). Keep it short: the
  multilingual voice already reads English names correctly and respelling those only breaks them.
* **`spoken_symbols`** – `#`, `&`, `@` rewritten into the words a lektor says. Short cues never go to
  the model, so a glyph left in one is read as whatever the voice makes of it.
* **`units`** – imperial measurements and currency said the Polish way. Deliberately narrow: mined
  over 26 950 Polish subtitle lines from 44 films, a plain unit-name regex found 22 matches of which
  TWO were measurements ("stopa" was a body part 12 times out of 12, and *"Wszyscy cali i zdrowi"*
  would have become *"Wszyscy 2,54 cm i zdrowi"*). A unit converts only when a numeral stands
  immediately in front of it and the line matches no idiom. Values are never converted for currency –
  exchange rates move, and a lektor quoting an old sum in today's złoty would be stating a falsehood.

## Merging cards into one breath

`merge_contiguous` joins two cards into one lektor breath when the first does not close its sentence
and the cards are close enough. "Close enough" is two bands, because a gap in milliseconds is a fact
about the authoring tool rather than about the actors.

* **`clean.merge_gap_ms` = 140** – the tight band. Merging on the gap alone glued two finished
  sentences 89 % of the time, including across shot changes and onto title cards, which is why the
  sentence test is an AND and not an OR.
* **`clean.merge_gap_wide_ms` = 250** – a second band, added once the gaps were measured against the
  **frame grid** instead of in milliseconds. A fansub authored at 23.976 fps spaces its cards at
  exact frame multiples: 83–84 ms twice (338 of them), 125–126 ms three times (225), 167 ms four
  times, 208–209 ms five times (44). 140 falls arbitrarily between three frames and four, so every
  seam its author happened to space at four or five frames was refused because of where a constant
  landed. The wide band covers six frames and demands stronger evidence instead: **the next card must
  open in lower case**, which a continuation does and a new sentence does not.

Raising the tight threshold outright was tried first and glued the recap card **"Poprzednio w NCIS"**
onto the sentence four frames after it, because that card carries no full stop and `is_screen_text`
only knows the ALL-CAPS convention. Inside the tight band nothing changes, so no existing render
moves. The case that prompted all this: *"Carla Marino, matriarcha największej"* + *"zorganizowanej
rodziny przestępczej na Środkowym Zachodzie."* – one sentence over two cards, condensed separately
into a complete line and a stranded fragment the lektor read as *"rodziny przestępczej
Środkowego"* and stopped.

## The merge gate: what the subtitle grid cannot tell you

Every subtitle stream in this release is one authored display grid – 3191 of 3396 Polish cues start
and end on byte-identical timestamps to the English track, the minimum gap between any two cards is
82 ms, and 55 % of all gaps are 83–84 ms. So contiguity proves nothing about the delivery.

`lektor.py speech` goes and gets the missing evidence, free and locally: the 5.1 centre channel is
band-passed to 16 kHz mono, CTC forced alignment (`WAV2VEC2_ASR_LARGE_LV60K_960H`, 30 s windows)
puts word times on the verbatim English subtitle text, and ffmpeg `scdet` finds the shot cuts.
Whisper is deliberately **not** called: its raw word timestamps measured 316 ms early at utterance
starts, and skipping it takes the episode from ~3.5 min of GPU to ~10 s. One episode is ~18 s of
audio extraction, ~9 s of alignment at 234x realtime, and ~58 s of scene detection.

What that buys, measured over six episodes: of the 231 seams the text rules join into 211 merged
utterances, **105 sit on a ≥ 300 ms silence in the delivery and 33 on a shot cut, 117 (51 %) on
either.** Half of every merge glues together two things the actors said with a real pause between
them, and `clean.merge_gate` can refuse those.

| | gate off | gate `both` |
|---|---|---|
| cues | 3241 | 3356 |
| merged utterances | 211 | 111 |
| over budget | 1333 | 1407 |
| characters of deficit | 11 833 | 12 249 |

**And then the A/B went the other way.** Judged on 22 pairs rendered both ways over the real audio
with everything else held identical, merging won decisively: splitting a sentence at its comma,
which is exactly where actors pause, made the lektor sound like it was reading word by word. A
lektor is a narrator reading a text, not a performer imitating a delivery – he reads the sentence,
and the pause in the picture is not his to copy.

`clean._merge_gate_note` records that conclusion. The value next to it is still `"both"`, i.e. the
gate is ON as this config ships, and one of the two is stale. Until that is settled by whoever ran
the A/B, set `clean.merge_gate` to `"off"` to get what the listening test preferred, or leave it and
know which choice you are making. `merge_min_pause_ms` (300) is the knob that matters if it stays
on: across six episodes it takes the 211 text-rule merges to 95 at 200 ms, 124 at 300, 162 at 500
and 190 at 800. Recurring cost of the gate: **$0**. One-off cost of switching it: 229 lines change
text, 9610 characters, **$0.48** to re-read the season on ElevenLabs flash, $0.00 on piper.

## The card a subtitle file shows twice

`S03E04` of one release carries **16 pairs of cards with byte-identical timestamps AND
byte-identical text**, plus 9 more whose spans overlap. The lektor read every one of those sentences
a second time. The same test over all 10 017 Polish cards on this disk finds **zero** in the other
seventeen episodes, so this is one bad authoring run rather than a property of the format – and the
guard has to be narrow enough that those seventeen stay byte-identical.

The test is the **overlap**, not the similarity, and that is the whole design. Dialogue repeats
itself constantly and a lektor has to read it: "Roy Kent!" shouted by two different fans, a name
asked and then confirmed. A first pass scoring text similarity flagged 23 pairs in the episode and
half of them were real exchanges. Measured on the three clean episodes, **every genuine repetition
sits 0.5 to 4.5 s apart and none overlaps at all, while every defective pair overlaps by 1.8 to
4.9 s.** Two people cannot say the same sentence in the same instant; a grid showing the same words
over one span is showing one line twice.

It runs before merging, so a duplicate can never be folded INTO an utterance and read twice inside
one breath, and before windows, so the survivor takes the union of both spans. Verified on the
episodes already rendered: 696 → 696, 666 → 666, 658 → 658, and **682 → 660**.
`clean.drop_repeated_cards` switches it off; `clean.repeat_overlap_ms` widens the test past pure
overlap and should stay at 0, because at 500 it starts eating real exchanges.

## The lektor waits for the actor

A card goes up a median 73 ms before the first word is spoken, and that lead is working room, not
error. But a handful of lines an episode go up much earlier, and the lektor then talks into a silent
picture. `speech.json` already knows when each actor really started, so `retime` pushes those lines
onto their onset – and nothing else moves.

Measured across all six episodes at alignment score ≥ 0.8: **182 of 3443 lines lead by more than
300 ms, 35 by more than 500 ms**, and after retiming that first figure falls to 26. The delay is
capped three ways: it never closes the lead entirely (`delay_keep_lead_ms` = 100), never exceeds
`max_delay_ms` = 600, and never exceeds the room left in the line's own window – so a nudge can
never become an overrun the speed planner has to buy back. Verified on the real audio of S02E06: the
number of lines pushed later by the previous line **does not rise**, 73 either way. Re-timing *every*
line to its acoustic onset was measured as a net loss (-50.8 s of room, lines needing a speed-up
4 → 41), which is why this is a nudge on a handful of lines and never a re-timing.

Without onsets, `timing.lead_in_ms` (550) is the fallback: the lektor simply starts that long after
the card, so the original actor is heard first.

It costs nothing at all. No text changes, so every ElevenLabs line stays in cache.

## Planning the speed, not just the characters

The script stage sizes budgets from a fitted duration model; the voice stage then plans the actual
speeds over the whole episode rather than line by line.

* **`timing.overhead_ms` = 0.0, `ms_per_char` = 67.0** – fitted by `tools/fit_duration.py` on 36
  lines spanning 6 to 170 characters, measured after `trim_silence` at speed 1.0, which is what the
  pipeline actually consumes. The free fit wanted -131 + 68.17 and a negative fixed cost is not
  something an engine can do; forcing the intercept to zero costs R² 0.9919 → 0.9914.
* **`timing.duration_sd_ms` = 333** – the residual spread, which stays 333 ms whatever shape the
  model is given, including one with curvature. It is the engine's own line-to-line variation, and it
  is used as a margin wherever a decision would otherwise treat the prediction as exact.
* **`timing.speed_smooth_s` = 6.0, `speed_relax` = 0.88** – a single line jumping to 1.2 while its
  neighbours sit at 1.0 is heard as the lektor suddenly hurrying, so the speed-up is spread over six
  seconds of neighbours and only 88 % of the computed excess is applied. It pays for itself:
  neighbours that finish earlier leave the crowded line more room.
* **`timing.cut_guard_ms` = 500** – a line must finish this long before a shot change, because a
  lektor who runs out exactly as the picture cuts sounds as though the cut chopped him off. Measured
  on 810 cues against 541 scdet cuts: it shortens 189 slots and puts 136 of them over their natural
  pace, against 33 with no guard, and a card left with under 400 ms keeps its own end instead.
* **`timing.numeral_drag` = 1.2** – a run of numerals with no terminal punctuation drifts: the engine
  treats it as a list going nowhere, slows on every item and falls in pitch. A written-out year came
  back at 87.6 ms per character against the model's 73.5; the same line with a full stop came back at
  73.8. Adding a full stop to every unterminated line globally was measured and rejected – on
  ordinary sentences the effect is noise, and it puts a false ending inside a thought.
* **`timing.max_borrow_ms` = 1500** – how far a line may run into the silence after its own cue,
  capped by the real gap minus `gap_guard_ms`. A real lektor routinely lags the picture.
* **`timing.max_atempo` = 1.0, i.e. off.** Post-hoc time stretching resamples finished speech and the
  artefacts were judged worse than an honestly faster reading. Anything that will not fit at
  `max_speed` 1.2 is simply allowed to run long into the silence that follows, which is what a real
  lektor does. kikivoice is the one documented exception, on its own 24 kHz output.

## The mix

The lektor lands on the **centre channel**, where dialogue lives and where a stereo downmix folds it
evenly into both speakers. `mix.voice_channel_comp_db` (+3 dB) is a correction, not taste: every
level here was tuned against a stereo fold where L and R sum 3 dB, which one channel does not do.
`mix.level_reference: stereo_fold` for the same reason – an RMS across six raw channels is a
different number and every level hangs off it.

**Levelled against the dialogue, not against the film.** `mix.dialogue_reference` is on: the lektor
sits `voice_over_dialogue_lu` = **6.0 LU** above the film's own dialogue, measured BS.1770 over
windows taken from the Polish cue timings and gated by the centre channel (`dialogue_speech_gate_lu`
= 12 LU below the typical cue means nobody is speaking under it – screen text, a sign, a lyric). The
old reference averaged silence, music and action together, so on a sparsely-talking film "+9.5 dB
over programme" measured as roughly **18 dB over the dialogue itself**. The unit is LU because it is
the difference between two loudness measurements; anything that merely multiplies a signal
(`duck_to_db`, `voice_trim_db`, `limiter_ceiling_db`) stays in dB. The voice stage measures what it
actually did afterwards, over the same spans, and prints the median and the 10–90 percentile.

**The fader is ridden.** `ride_tracking` = 0.5 means a scene 6 LU louder lifts the voice 3 dB, capped
at `ride_max_db` = 3 either way, eased with a zero-phase 2 s constant so the ride neither anticipates
the picture nor lags it. `ride_within_cue` is false: one level across a cue, easing between cues,
which is what a reader and a mixer actually do. `dialogue_window_s` = 20 is short enough to follow a
scene and long enough not to follow a sentence.

**The original ducks, broadband.** A gain envelope is built from where the lektor actually speaks
(not from the subtitle timings, which are only an approximation of it), and applied under the voice:
`duck_to_db` = -3.0, `attack_ms` = 90, `pre_roll_ms` = 450, `release_ms` = 600, `hold_ms` = 1500.
`duck_mode` is **broadband** – spectral ducking was built, measured and then switched back off after
the owner reported a phaser-like colouration that survived the crossover fix. It was not a bug: by
construction it sweeps an 11 dB dip through 300–4000 Hz on every line, attack 90 ms and release
600 ms, and a moving midrange notch is what a phaser is. The spectral path is still there behind
`duck_mode: spectral`, with S02E10 kept as its A/B reference.

`hold_ms` matters more than it sounds: without it, one episode measured **324 full recoveries, 198 of
them under a second, the programme swinging 11 dB at up to 52 dB/s** between neighbouring lines. That
pumping is what a listener hears as the film being chopped.

`mix.program_gain_db` is **0.0**: where the lektor is not speaking the film is left exactly as it
was, and those stretches come back bit-identical, 0.00e+00 across every one of them. It is not this
tool's business to re-master the film – it was 4.0 for a show whose mix ran quiet, and on a film
already at a sensible level the boosted stereo downmix peaked at +1.20 dBFS against the original's
-2.74, i.e. it clipped where the source never did.

**The voice itself is treated in four places, each measured:**

* `voice_presence_db` = 7.0 at `voice_presence_hz` = 2000 – matched by measurement, not taste. Two
  voices at equal RMS are not equally present. The current voice is not rolled off, it is **dipped**:
  6.2 dB under the reference voice at 2–4 kHz and 5.6 dB at 4–8 kHz, while 8–16 kHz already matches
  to 0.6 dB. A shelf can only fill a mid dip by paying in air above it, so the corner sits at 2000 Hz
  rather than 4000: same top-end cost, 2.2 dB more of the 2–4 kHz band bought back. At 7 dB the
  4–8 kHz band lands on the reference exactly, 2–4 kHz closes from -6.2 to -2.3, and 8–16 kHz
  overshoots by +5.7 dB – that overshoot is the price of the filter shape and the thing to listen for
  if the lektor ever reads as sibilant. **Re-measure after any voice change.**
* `per_cue_level` (bounded at `per_cue_level_max_db` = 4) levels every line to the episode median
  before placing it, so a voice that wanders does not take the listener with it, while a whisper
  stays a whisper.
* **Internal pauses are compressed.** Measured over 476 of the pauses the engine inserts, mostly at
  commas: median 180 ms, p75 320, p90 510, max 700, and **a line with two commas spends 23 % of its
  length not speaking**. `pause_ratio` = 0.35 keeps only that share of each pause ABOVE
  `pause_floor_ms` = 120; the floor is where a gap stops being heard as a pause and reads as
  articulation, so it is never taken. Compressing only the excess keeps a sentence boundary longer
  than a comma, which a flat scaling does not.
* `voice_trim_db` = 1.5 – the per-voice nudge for when the tone is right and the balance still is
  not. Cheap to retune: the speech is cached, so only the mix is rebuilt.

The encode profile follows the layout: `eac3` at 768k for surround, `ac3` at 256k for stereo, with
`delay_samples` = 256 compensated by advancing the source read, because any player rendering both
tracks at once would otherwise sum the film with a delayed copy of itself – a comb filter notching
every 188 Hz at 12.9 dB, audible as flanging on loud music.

## Paying once: the reuse cache

The LLM bill is the small one. **The TTS bill is 14–65x larger for the same change**, because any
line whose text changes has to be read again. The condensation bill it saves is $0.71 an episode and
$4.27 a season without the cache, against $0.09 and $0.54 with it; the TTS bill avoided alongside it
is the multiple above. And a whole-episode condensation draw rewrites every line, including the
hundreds the change never touched.

So condensed text is filed in `<base>.condensed.json` under a key that is the whole contract it was
produced under: the cleaned source text, the character budget, the model, the reasoning effort, and a
**hash of `CONDENSE_SYSTEM` itself** – automatic, because a hand-bumped version number is the thing
that gets forgotten, and the result would not be a stale cache but an episode half-written by a
prompt that no longer exists. Change the prompt and every line misses, by design. Change a cleaning
rule and only the lines it touched miss.

Measured on S02E06, `script --force` on an unchanged episode: **245 of 246 lines reused, one call,
byte-identical text**, against a full redraw before. The one miss is a cue the model had failed on
before – failures are deliberately not cached, so they get another chance instead of becoming
permanent.

Two things it will not do. It never overwrites a line you edited by hand in `<base>.lektor.json` (the
report says how many it kept). And it never reuses a line that still carries digits when the source
was sent to have them spelled out – a reply read back off disk earns no more trust than it had on the
day.

`script --redraw` ignores the file and pays for a clean draw; `script.reuse_previous: false` is the
permanent version.

## Cost

`python3 wsl/lektor.py cost --plan creator` counts characters and prices them against the backend
that is actually selected, before you spend anything. Local backends print $0.00 rather than a fake
number, and kikivoice prints credits.

Measured shape, seven episodes rendered end to end. Six of Ted Lasso S03: **666–787 utterances and
18 139–22 285 characters** of finished lektor script per episode. One of Platonic S01, a sitcom that
talks without pause: 596 utterances, 16 006 characters in 31 minutes.

**The ElevenLabs bill is a third to a half above that character count, and the `cost` estimate does
not say so.** A line that overruns its window after synthesis is re-read at a corrected speed, and
that second call is billed like any other: 244 to 348 lines an episode came back long and were bought
twice. Against scripts of 16 006 to 22 285 characters the real bills were 23 703 to 28 511, i.e.
**+28 % to +48 %, or $1.19 to $1.43 an episode** on flash rather than the $0.80–$1.11 the script alone
prices at. On piper, omnivoice and kikivoice it is $0.00 either way, because the duration knob is
exact (piper, omnivoice) or the window is closed off cached bytes (kikivoice).

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

Condensation is the other bill and it applies to every backend, since they all read the same script.
`script.measured_usd_per_episode` is 0.014, and that figure belongs to the OLD policy (luna at ~2.3
draws); it is only what `cost` reports until `<base>.spend.json` exists, after which the real billed
figure is used. On easy material a single clean draw of a 598-cue episode billed **$0.0089**; on hard
fansub material the shipped ladder measured **$0.3034** an episode. Both are still small next to the
TTS bill on a paid backend, but "well under a dollar for a season" is only true of the first.

## Known rough edges

- **The over-budget count has not been re-measured since the duration model was re-fitted.** The last
  recorded run of S02E01 marked 210 of 598 cues for condensation, under the previous `47 + 73.5`
  model. That pair was fitted on the six calibration lines, which span 43–63 characters, and two
  parameters cannot be separated over a twenty-character window: on the proper 6–170 character
  benchmark it scores R² 0.9625, over-predicts by +436 to +892 ms across most length bands and has a
  worst case of 1668 ms, which is why lines were being hurried that had all the time they needed. The
  shipped `0 + 67.0` fixes the model; nobody has counted the cues since.
- **The duration model is one voice's.** It was fitted on the ElevenLabs voice, and the script stage
  applies it whatever backend will speak. piper and omnivoice measure or are told the real length and
  do not care, but the number of cues sent for condensation is set by somebody else's speaking rate.
- **Two config notes disagree with the value beside them, and this README cannot settle either.**
  `clean.merge_gate` is `"both"` while its note says the gate was switched off after listening;
  `mix.duck_to_db` is -3.0 while its note says -4.5 by ear. A third is cosmetic:
  `voice.omnivoice.reference_wav` points at `szolajski.ref.wav` while its note describes the Knapik
  sample.
- **The two piper fit figures disagree.** `voice._backend_note` says 597 of 598 lines inside their
  window on the real episode; `voice.piper._search_min_note` says 4 lines clamped and still over on
  what reads as the same episode, worst +16 %. Both are recorded as measured, both date from the same
  commit, and the table above quotes the second. One of them is wrong and the episode would have to be
  re-rendered to find out which.
- **`voice._backend_note` and `backends/base.py` still describe three engines.** They predate
  omnivoice and kikivoice, and they are the two places text gets copied from into documentation, so
  the error propagates.
- **chatterbox cannot aim at a window and nothing rescues it.** Nothing about generation targets a
  duration, and with `timing.max_atempo` at 1.0 a long reading is not stretched either – it simply
  overruns into the silence after it. On the 20 s smoke slice one line of four ran 56 % past its slot.
- **kikivoice can stop the render and wait for a person.** Exit 75 on a GeeTest challenge or an
  exhausted bucket, roughly every 30 accepted tasks. The cache is kept and re-running the same command
  resumes, but an unattended overnight run is not a thing this backend does.
- **Extract recurses, everything after it does not.** `find_sources` walks the whole input tree,
  while `speech.find_video` looks only at `input_dir/<base>.{mkv,mp4,m4v}`. A season laid out one
  subdirectory per episode therefore extracts fine and then has no video to detect shot cuts in and
  no 5.1 source to lay the mix over: `speech` says so, and `voice` skips the episode.
- **The FLAC downmix is 5.1 folded to stereo** with ffmpeg's default coefficients, and extract writes
  it whether or not the mix will use it. Fine for lektor, not a mastering decision.
- **`chars_per_second` starts as a guess** (14.5) until `calibrate` measures it, and only backends
  that predict a duration can measure it at all. With `ms_per_char` set it changes no character
  budget – it only tells the ElevenLabs backend how much speed to ask for.
- **Sentences spanning two cues.** Subtitles split one sentence across cues; the condenser sees the
  whole episode but still shortens per cue, so a line ending mid-clause stays mid-clause. The wide
  merge band above closes the common case and not the general one.
- **The limiter has no lookahead.** It is sample-accurate and its state crosses the 10 s encode
  blocks, so a peak pulls down its own neighbourhood and releases over `mix.limiter_release_ms` rather
  than scaling a whole block. But the attack is instantaneous, so an isolated transient has its own
  peak flattened rather than being ramped into. It did not engage anywhere in the smoke run (peak 0.23
  against a ceiling of 0.89).
- **Two hardcoded thread counts are properties of this machine**, not of yours: `voice.piper.
  concurrency` = 14 and `speech.ffmpeg_threads` = 14, both capped at 14 of 16 by owner rule so other
  containers keep a core.
- Cached TTS lives in `_lektor/.cache/`, keyed by everything that changes the waveform. Delete it to
  force regeneration; keep it and reruns are free.
