# The five TTS engines

What each one costs, what hardware it wants, how it hits a time window, and how to compare them by ear rather than from a table.

[< back to the README](../README.md)

---

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
**4 lines on the real 598-cue episode, worst overrun +16 %** (comment above `voice.piper.search_min`).

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

A take that fails that gate three times is re-rolled on a fresh seed, then asked once more with **no
duration at all** – the unconstrained regime is the one this model is most reliable in, and the line
may then overrun its window, which the mix tolerates and the report counts. If even that comes back
short the take is **kept anyway** rather than discarded. The floor is proportional to the TEXT, so
on a one-syllable card a take failing the ratio is still most of the word, while the alternative is
the whole card gone silently with nothing in the report to point at. Only a genuinely EMPTY take –
the zero-length array this retry loop was written for – still ends as silence. The run summary
counts all three outcomes separately: re-rolled, read unconstrained, kept short.

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
