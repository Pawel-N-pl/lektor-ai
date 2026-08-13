# Timing and translation: what to build, in what order, what it costs

## Decision

Build one free local artefact per episode – ASR word times, CTC forced-alignment onsets and ffmpeg
shot cuts, written to `<base>.speech.json` – and spend it on exactly two things: **gating the merge**
(refuse to join two cards when the actors themselves paused there or when the edit cuts between them)
and **delaying the handful of lines that speak before the actor does**. Do not re-time the lektor to
speech onsets, do not diarize, do not repair the Polish subtitles, do not generate your own – every
one of those is killed by a measured number below, not by taste. The condensation prompt rewritten
from the craft brief is correct and costs $0.0004 an episode in tokens, but it invalidates every
cached ElevenLabs line, so it ships **batched with the merge gate in one text change**, evaluated on
piper, never as its own paid re-read. Recurring dollar cost of everything recommended: **$0** on top
of today's condensation, and about **7.5 minutes of 3090 and CPU per episode**.

---

## 1. Ranking by value per cost

Costs are per 35-minute episode. "Free" means it runs on the 3090 or the CPU and bills nothing.
Every MEASURED figure below was run on this machine against this release; sources for the ones taken
from the four briefs are named inline.

| # | Candidate | $/ep | min/ep | installs | verdict |
|---|---|---|---|---|---|
| 1 | Merge gate on shot cuts + acoustic pauses | **$0** | 7.5 (GPU+CPU) | none | **BUILD FIRST** |
| 2 | Per-line delay for lines that lead the actor | **$0** | 0 (reuses #1) | none | **BUILD SECOND** |
| 3 | Condensed-text reuse cache in `stage_script` | **$0** | 0 | none | **BUILD, it is the enabler** |
| 4 | Craft-rewritten condensation prompt | +$0.0004 LLM / $0.71 TTS | 0 | none | ship batched with #1 |
| 5 | Local condensation model on the 3090 | −$0.05 (saves) | unknown | vLLM or llama.cpp | **unmeasured, one experiment settles it** |
| 6 | FA per-cue score as a QA column | $0 | 0 | none | keep as debug output only |
| – | Re-timing placement from ASR onsets | $0 | 7.5 | none | **KILLED** |
| – | Speaker turns / diarization | $0 | 3–7 | ~7 GB sidecar + HF token | **KILLED** |
| – | Repairing the Polish subtitles | $0.01–0.05 | 1 | none | **KILLED** |
| – | Generating Polish subtitles ourselves | ~$0.03 | 2 | none | **KILLED for this release** |
| – | Raising `max_borrow_ms` to reclaim hidden silence | $0 | 0 | none | **KILLED** |
| – | Swapping Whisper for Parakeet / installing whisperx | $0 | – | multi-GB | **KILLED** |

### 1. Merge gate on shot cuts and acoustic pauses – BUILD FIRST

**What it fixes.** `merge_contiguous` joins two cards into one lektor breath on text evidence alone
(first card does not close its sentence, gap at the release's 83 ms floor). It has no idea whether
anything happened in the picture or in the delivery at that seam. Measured on S02E06, over the
43 merges the shipped code produces:

| Signal at the seam | Merges affected | MEASURED |
|---|---|---|
| A ≥ 300 ms acoustic pause in the aligned speech, within ±250 ms of the seam | **20 of 43 (47 %)** | forced alignment, `WAV2VEC2_ASR_LARGE_LV60K_960H`, score ≥ 0.5, 499 such pauses in the episode |
| A shot cut inside the 83 ms seam | **2 of 43** | ffmpeg `select='gt(scene,0.35)'`, 433 cuts, median shot 2.59 s |
| Either | **22 of 43 (51 %)** | both of the above |
| Merged utterances spanning ≥ 1 cut | **41.9 %** vs **19.1 %** of unmerged cues | same run |

The cut result replicates the standards brief's S02E01 figure (4 of 45 seams, 31 % vs 16 % spanning),
so N=2 in the same direction. The pause result is new here and is much the larger effect: **half of
all merges glue together two things the actors said with a real pause between them.** Merging there
makes the lektor assert one breath where the delivery has two, and with atempo disabled there is no
way to buy that time back later.

**What it costs.** Simulated by running the shipped `load_cues` with the gate wired into
`continues_sentence`, S02E06:

```
today   546 cues, 43 merges, 233 over budget, 2087 chars of deficit
gated   571 cues, 21 merges, 247 over budget, 2145 chars of deficit
```

**+14 cues for the LLM to shorten, +58 characters to remove, +2.8 % of deficit.** The condensation is
one whole-episode call, so that is roughly 25 extra output tokens – about $0.00001. The real cost is
that 49 lines change text and must be re-read: **1827 characters, $0.09 at ElevenLabs flash**, or $0
on piper. See §3 for why that number is the one that governs the whole plan.

**Craft backing, not just arithmetic.** Woźniak 2012:216 – the lektor should deliver into the pauses
of the original. Refusing a merge at a real pause is exactly that: the lektor's own silence lands on
the actors' silence instead of over it. The standards brief's framing holds – reading-speed rules do
not bind us, rules about the relationship between an utterance and the edit bind us harder.

### 2. Per-line delay where the lektor speaks before the actor – BUILD SECOND

Free, no text change, no cache invalidation, no LLM. Measured on S02E06 against the 250 lektor lines
that match a forced-aligned English cue at score ≥ 0.8:

| lead over the first spoken word | lines |
|---|---|
| > 300 ms | 24 (9.6 %) |
| > 500 ms | 4 (1.6 %) |
| > 800 ms | 0 |

**Correction to the ASR brief.** It reported 10 lines over 500 ms with worst cases +2003 ms and
+1866 ms. Those come from lower-confidence anchors; at FA score ≥ 0.8 the worst lead in the whole
episode is **+634 ms**, which is also the maximum of the aligner's own `card_start_to_first_word`
distribution. The multi-second leads are alignment noise, not cueing error. So the prize is real but
small: a sub-600 ms nudge on about four lines an episode, twenty-four if the threshold is 300 ms.
Build it because it is free and bounded, not because it is large.

### 3. Condensed-text reuse cache in `stage_script` – BUILD, it is the enabler

Not a feature; the thing that makes every other feature affordable. Today any change upstream of
the script stage forces `script --force`, which draws a **new** whole-episode condensation at
temperature 0.3, which rewrites every condensed line, which misses every ElevenLabs cache entry,
which costs a full re-read. Keyed reuse – `(merged_from ids, cleaned source text, budget_chars)` →
previous condensed text – turns "re-run the pipeline after a merge change" from **$0.71** into
**$0.09**. Measured basis: §3 below.

### 4. Craft-rewritten condensation prompt – ship, but batched

The prompt is already written and 63/63 tests pass. Token cost of the larger prompt, from the craft
brief's own least-squares fit over 64 real calls: **+$0.00036 an episode**, about 3 %. That is
nothing. What is not nothing: it changes every line's text by design, so it costs a full ElevenLabs
re-read. Ship it **in the same text change as the merge gate**, and judge it on piper first.

Its strongest single claim is worth restating because it inverts a shipped rule: the old prompt said
"BEZWZGLĘDNIE zachowaj imiona", the craft literature says the vocative is the *first* thing a Polish
lektor translator deletes (Hendrykowski 1984:247; Adamowicz-Grzyb 2013:110; Belczyk 2007:18; TVP's
own translator instruction per Garcarz). Expect it to *free* characters, which reduces the number of
cues sent for shortening, which partly pays for itself.

### 5. Local condensation model on the 3090 – UNMEASURED, and the highest-value unknown

The only recurring dollar cost in the entire pipeline is condensation, and the measured total across
six episodes is **$0.303** (`*.spend.json`, `usage.cost`): mean **$0.0505 an episode across all
draws**, best single clean draw $0.0089, worst episode $0.0839 over four runs. With ~13 GB of 3090
free after the resident Whisper model, a Polish-capable 8–14B instruct model would make that $0.
Nothing here measures whether it can hold the JSON contract and the character budgets, and the
existing stage already has the validator that would prove it for free (`accept_fit_ratio`, the
fit-ratio re-roll). See §6 for the exact experiment.

### 6. FA per-cue score as a QA column – keep, but do not oversell it

Cues scoring < 0.35 have mean ASR token recall 0.314; cues ≥ 0.7 have 0.901 (S02E06). Two
independent methods agreeing on which cues are hard is a genuine signal, but what it flags is
"Hey!", "Come on!", "Mum!" – **acoustic difficulty, not translation divergence.** It is not the
"the Polish diverges from the spoken English" detector that was asked for, and must not be shipped
as one. Write it into `speech.json`, print it in a report, gate nothing on it.

---

## Killed, by name, with the number that kills them

**Re-timing lektor placement from ASR onsets.** The card goes up a median **+90 ms before** the
first word and comes down **−190 ms after** the last; that lead and trail *are* the lektor's working
room. Re-timing to the acoustic span, simulated with the pipeline's own arithmetic over 415 shipped
lines of S02E06: total room **1298.6 s → 1247.8 s (−50.8 s)**, median Δ room **−71 ms**, lines
needing any speed-up **4 → 41**, lines pinned at `max_speed` **1 → 16**, total overrun **0.5 s →
7.4 s**. It is not a small win, it is a negative one. Dead.

**Speaker turns / diarization.** Best open 2026 models score 17–27 % DER on the benchmarks nearest
ensemble comedy, and an independent 2026 drama-audio test could not get below **27.41 % DER**;
Sortformer is architecturally capped at four speakers. It cannot live in the `asr` container
(pyannote 4.0.7 needs torch ≥ 2.8, the container is pinned at 2.6 by a byte-match reference), so it
costs a second ~7 GB sidecar plus a gated HuggingFace token that does not exist on this machine.
And the only thing it would plausibly gate is the merge – where the free, 0 % DER SDH labels reach
**4 of 46 merge seams** on S02E06 (and only **176 of 3473** Polish cues across six episodes), while
the free acoustic-pause gate reaches **20 of 43 merges**. Buying 27 % DER to duplicate a signal we
already get for nothing is the worst trade on this list. Dead.

**Repairing the Polish subtitles against the English source.** A divergence measure calibrated on
the corpus itself flags **24 of 3103 aligned groups (0.8 %)**, worth **570 characters, 0.4 % of the
English total** – and all twenty worst are textbook subtitle compression, not error. The independent
entity test leaves **one** genuinely lost number in six episodes (E01 16:48, "1999"). Zero lines of
dialogue are missing; the 34 English orphans are terrace chants and *Last Christmas*. Any LLM pass
here spends money to rewrite a paid, credited ATVP translation that is already better than the
rewrite. Dead.

**Raising `max_borrow_ms` to reach the hidden silence.** Real gaps between utterances total 1146 s
against the cards' 1013 s, so the cards hide 134 s of silence – but only 87 s is reachable under the
1500 ms cap, and raising the cap to 4000 ms adds 201 s of room and still leaves **exactly 4** lines
needing a speed-up. The cap is not the binding constraint; the next cue is. Dead as a lever.

**Swapping the ASR stack.** whisperx 3.8.6 needs torch ~2.8 and faster-whisper ≥ 1.2; pyannote 4.0.7
needs torch ≥ 2.8 and torchcodec ≥ 0.7. Installing either into the running `asr` container breaks a
service another project depends on, and editing anything under `~/projects/yt-translator/asr/`
restarts a `--reload` uvicorn and drops the resident model. Parakeet TDT is 6.32 % WER against
large-v3's 7.44 % and emits native word timestamps, but they are transducer timestamps, not forced
alignment, and it needs a multi-GB NeMo tree for a transcription stage that is already validated.
torchaudio 2.6.0 inside the container already ships the CTC aligner. **Zero installs is the correct
number.** Dead.

---

## 2. Where the money actually is

Three separate budgets, and the small one is the one everybody watches.

**OpenRouter, measured from `*.spend.json` (`usage.cost`, not estimates):**

| episode | billed | draws |
|---|---|---|
| S02E01 | $0.0727 | 3 |
| S02E02 | $0.0772 | 4 |
| S02E03 | $0.0331 | 3 |
| S02E04 | $0.0260 | 3 |
| S02E05 | $0.0100 | 1 |
| S02E06 | $0.0839 | 4 |
| **total** | **$0.3029** | 18 |

The "about a cent an episode" figure is the cost of *one clean draw* ($0.0089–0.0111). The real
recorded mean is **$0.0505**, because re-rolls and re-runs happen. **Every `script --force` is
another draw.** That is the budget line any proposal has to be measured against, and it is why #3
(text reuse) matters more than its size suggests.

**ElevenLabs, measured from the shipped scripts.** Six episodes hold **85 475** characters of
lektor text (14 203–16 066 per episode). At flash `$0.05/1000`:

- re-reading one episode: **$0.71**
- re-reading the season: **$4.27**
- re-reading only the 49 lines the merge gate changes on S02E06: **$0.09**

So **the TTS bill is 14x to 65x the LLM bill for the same change.** Any proposal that rewrites text
is a TTS proposal wearing an LLM costume. The constraint says do not call ElevenLabs in this
workflow at all – which is satisfiable exactly as long as the changes are batched and evaluated on
piper, and paid for once, later, deliberately.

**The 3090, measured.** Whisper large-v3 on the 5.1 centre channel: **212.7 s for a 2108.6 s
episode (9.9x realtime)**, +1345 MiB over an 8686 MiB baseline. Forced alignment of the whole
episode in 30 s windows: **9.0 s (234x realtime)**, +1461 MiB resident / +3617 MiB peak. ffmpeg
centre-channel extract: 18.4 s. Shot detection: **61 s wall, CPU only** (measured here on S02E06,
433 cuts). Total **≈ 7.5 minutes per episode, $0, zero installs.** Everything ranked 1, 2, 3 and 6
runs entirely inside that envelope.

**Free because it runs locally:** ASR, forced alignment, shot detection, the merge gate, the per-line
delay, the QA score, piper TTS. **Not free:** condensation (OpenRouter), any ElevenLabs re-read,
anything that would need a strong model per line.

---

## 3. Implementation

### 3.1 New: `wsl/lektorlib/speech.py` and `lektor.py speech`

One stage, one artefact, no dependency on anything paid.

```
inputs   <input_dir>/<base>.mkv                  (read only, never re-encoded)
         <work>/subs/<base>.eng3.srt             (verbatim English, the FA reference text)
outputs  <work>/<base>.speech.json
```

Pipeline, all of it already proven by the scratchpad scripts named at the end:

1. `ffmpeg -map 0:1 -af "pan=mono|c0=FC,highpass=120,lowpass=7500,dynaudnorm" -ar 16000` – the 5.1
   centre channel is the dialogue stem, free, no source separation.
2. POST to the running container at `http://172.22.0.3:9000/transcribe`, field `audio`, poll
   `GET /transcribe/{job_id}`. ~3.5 min. Do **not** add a port mapping, do **not** write into
   `~/projects/yt-translator/asr/`.
3. `docker exec asr python3` running `torchaudio.pipelines.WAV2VEC2_ASR_LARGE_LV60K_960H` +
   `torchaudio.functional.forced_align` over the English SRT text, **30 s windows** (better scores
   than 120 s – 475 vs 393 of 616 cues – 2.6x less VRAM, 1.9x faster, medians agree to 10 ms).
   Use the English bundle, not `MMS_FA`: better on English TV dialogue (88 vs 57 of 97) and MIT
   rather than CC-BY-NC.
4. `ffmpeg -vf "select='gt(scene,0.35)',metadata=print:file=..."` for the cuts.

```json
{ "base": "...", "audio_dur_s": 2108.58,
  "words":  [[9229, 9829], ...],                         // ms, forced-aligned, score >= 0.5 only
  "gaps":   [[10930, 11637], ...],                        // >= min_pause_ms between consecutive words
  "cuts":   [1234, 5678, ...],                            // ms
  "cues":   [{"cue_start_ms":9259,"fa_first_ms":9229,"score":0.872,"n_words":5}, ...] }
```

Rule that must not be broken, and cost the ASR brief a −41.9 s anchor before it was found:
**never run a global text alignment over a VAD-filtered ASR stream.** Restrict every cue to ASR
words within ±3 s of its own card.

### 3.2 Merge gate – `wsl/lektorlib/srt.py`

`continues_sentence(prev, cur)` gains an optional third argument and `merge_contiguous` passes it:

```python
def continues_sentence(prev, cur, ev=None) -> bool:
    ...existing text rules, unchanged...
    if ev is not None:
        # A cut between the cards means the edit says these are two shots; a lektor crossing it
        # asserts one breath over two. A >= 300 ms silence in the delivery means the actors
        # themselves paused here, and merging talks over the pause instead of into it.
        if ev.cut_in(prev.end_ms, cur.start_ms):   return False
        if ev.pause_at(prev.end_ms, cur.start_ms): return False
    return True
```

`load_cues(path, cfg, speech=None)` loads `speech.json` when present and builds `ev`; with no file
the behaviour is byte-identical to today, which is what keeps the 63 shipped tests green.

New config keys under `clean`:

| key | default | meaning |
|---|---|---|
| `merge_gate` | `"off"` | `off` / `cut` / `pause` / `both` |
| `merge_min_pause_ms` | `300` | shortest acoustic silence that counts as a real pause |
| `merge_pause_tol_ms` | `250` | how far from the 83 ms seam that silence may sit |
| `merge_cut_pad_ms` | `0` | widen the seam before looking for a cut |

Measured on S02E06: `cut` → 41 merges, `pause` → 23, `both` → 21, against 43 today.

### 3.3 Per-line delay – `wsl/lektorlib/mix.py`

`assemble_voice` is the only place that turns a cue into a sample offset:

```python
start = int((clip.cue["start_ms"] + clip.cue.get("delay_ms", 0)) / 1000.0 * sr)
```

`delay_ms` is written into `<base>.lektor.json` by the script stage (or by a small `retime` command
that patches an existing script in place), computed as

```
delay = min(fa_first_ms - cue.start_ms - timing.delay_keep_lead_ms,
            timing.max_delay_ms,
            window_ms * safety - predicted_ms)      # never create an overrun
```

applied only when `score >= timing.delay_min_score` (0.8) and the lead exceeds
`timing.delay_min_lead_ms` (300). New keys: `timing.max_delay_ms` = 600,
`timing.delay_min_lead_ms` = 300, `timing.delay_keep_lead_ms` = 100,
`timing.delay_min_score` = 0.8.

### 3.4 Condensed-text reuse – `wsl/lektorlib/script.py`

In `stage_script`, before choosing `work`: load the previous `<base>.lektor.json` if it exists, index
it by `(tuple(merged_from) or idx, text_original, budget_chars)`, and for every cue whose key is
unchanged copy `text`, `condensed`, `over_budget` straight across. Only the residue goes into
`condense_episode`. Add `script.reuse_previous` (default `true`) and a `--redraw` flag to force a
clean draw. Report the split, so a run that reused 500 of 546 lines says so.

### 3.5 What could break, concretely

- **Cue identity.** Refusing merges changes cue ids and text, so `<base>.lektor.json` is a different
  document. Without 3.4 that means a fresh condensation draw ($0.01–0.05) **and** a full ElevenLabs
  re-read ($0.71). Build 3.4 first.
- **Budgets.** Un-merging splits one window into two, each with its own 80 ms fixed cost, so budgets
  tighten: measured +14 over-budget cues and +58 characters of deficit on S02E06. Both fall inside
  the existing `accept_fit_ratio` re-roll policy; neither approaches `max_rerolls`.
- **Merge guards.** `merge_max_cues` / `merge_max_chars` / `merge_max_span_ms` only ever constrain
  groups from above, and the gate only shrinks groups, so nothing there needs touching.
- **`assemble_voice` cursor.** A delayed line can push the following one (`start < cursor`); the
  function already handles this and prints the count. The delay formula's third term keeps a delayed
  line inside its own window, so the push count must not rise – assert that in the run report.
- **Duck envelope.** Built from `spans`, which are computed after the delay, so ducking follows for
  free. Nothing in `build_duck_envelope` needs to know about the delay.
- **`tools/render51.py`.** The shipping 5.1 path is standalone. `load_script` (line ~78) drops every
  key it does not know, and `synthesise` (line ~247) rebuilds a minimal cue dict – `delay_ms` must be
  carried through both or the delay silently does nothing in the only renderer that ships. Its
  `plan_speeds` uses `c.start_ms` for the smoothing window and should keep using the undelayed value:
  speed is a question about window fit, which the delay does not change.
- **TTS cache.** ElevenLabs key is `text|voice_id|stability|speed|prev|next|model_id`; piper's is
  `text|voice|length_scale|synthesis settings`. **Neither includes timing**, so the per-line delay is
  free of TTS cost, and the merge gate costs exactly the lines whose text changed – 49 on S02E06.
  `prev`/`next` are in the ElevenLabs key, so a merge change also invalidates the two neighbours of
  every changed line; the $0.09 figure already counts only changed texts, so budget ~$0.15 to be safe.
- **Tests.** `merge_gate` defaults to `off` and `speech.json` is optional, so `test_srt_merge.py`
  keeps passing unchanged. Add: a gate unit test with a synthetic pause, and one with a synthetic cut.

### 3.6 Verification

1. **Reproduce the simulation.** With `merge_gate: "both"` and S02E06's `speech.json`, the script
   stage must report **571 cues, 21 merges, 247 over budget**. Any other number means the gate is
   wired differently from the simulation.
2. **Regression.** With `merge_gate: "off"` the pipeline must produce a byte-identical
   `<base>.lektor.json` to today's, given the same condensation draw.
3. **By ear, free.** Render only the 22 refused seams ±5 s, twice, on **piper** – gate off and gate
   on – and listen. This is the only test that can decide whether refusing a merge sounds better, and
   the whole argument for #1 is a judgement about prosody that no number settles.
4. **Delay.** Assert the "lines pushed later" count in `assemble_voice` does not rise, and that no
   line's `achieved_ms` exceeds `window_ms * safety` after delaying.

---

## 4. Should we generate the Polish subtitles ourselves?

**No, not for this release – and the reason is not cost.**

The cost is affordable, which is the surprising part. A whole-episode translation is roughly 28 000
characters of English in and 19 000 of Polish out; at the prices fitted from 64 real calls
($0.133/M in, $0.515/M out) that is about $0.001 of input and $0.004 of output, and reasoning
dominates – call it **$0.02–0.03 an episode at effort medium**, two to three times condensation.
That is inside the budget scale. The problem is what you would be replacing and what you could not
check.

- The existing track is a paid, credited ATVP translation (Daria Okoniewska) at a median 0.80
  compression ratio, and the strongest divergence test the corpus can calibrate finds **24 suspect
  groups in 3103 (0.8 %), 0.4 % of the English characters, with zero real errors among the twenty
  worst.** There is no defect to fix.
- Condensation is cheap to trust because it has a **free local validator**: did the line fit the
  budget. Translation has none. The fit-ratio re-roll that makes gpt-5.6-luna's lottery affordable
  (7 identical calls landing between 67 and 207 of 210) simply does not exist for a translation, so
  a bad draw is undetectable without reading the whole episode. Budget $0.03 an episode and you are
  really budgeting a human proofread.
- A generated track would also have **no timing**. The Polish cue starts are the English cue starts –
  588 of 598 bit-identical on S02E01, 570 of 593 on S02E06 – so today's grid is inherited, not
  authored. Generating text means authoring a grid, which means the ASR + FA stack plus every rule in
  the standards brief. That is a much larger project than the translation itself.

**It becomes worth it under exactly one condition:** there is no professional Polish subtitle track
for the material. Fan-subbed, machine-subbed or English-only releases. The trigger is already built
and free – the length-ratio divergence test over aligned PL↔EN groups. On this release it fires on
0.8 % of groups. **If it ever fires on more than about 5 %, or if there is no Polish track at all,
generate; otherwise translate nothing.** Note that the craft brief already assembled everything a
generation prompt would need – Tomaszkiewicz/Belczyk's ordered deletion list, the 30–40 % reduction
norm, the T/V rule, the vulgarity calibration – so the prompt is close to free when the day comes.
And the professional rules are not the hard part: the hard part is that nobody publishes a lektor
timing specification. There is none. Netflix's Polish TTSG §22 acknowledges "lector dubbing" only as
an input to SDH authoring.

---

## 5. What could not be established, and what would settle it

**A local model instead of OpenRouter.** Unmeasured, and it is the only item that would take the
recurring cost to zero. *Experiment:* serve an 8–14B Polish-capable instruct model on the 3090
(~13 GB free after Whisper), feed it the exact `whole_prompt` from `script.py` for S02E01, and score
the reply with the existing `validate()` – the accept metric is already "how many of the 210
overflowing cues came back inside budget". Two hours of work, $0, and a hard yes/no against
gpt-5.6-luna's 207/210 best draw. Watch VRAM: Whisper is resident at ~3960 MiB and the aligner peaks
at +3617 MiB, so a model over ~13 GB will contend.

**Whether refusing a merge sounds better.** The gate is justified by two measured signals and by the
craft literature, but "the lektor should pause where the actors paused" is a claim about prosody that
no measurement in this document tests. *Settled by:* §3.6 test 3, twenty-two A/B pairs on piper, one
listening session, zero dollars.

**N on the gate.** The cut evidence replicates (E01 4/45 seams, E06 2/43). The pause evidence is
**N=1** – S02E06 only. *Settled by:* running §3.1 over the other five episodes, 7.5 minutes each,
$0. Do this before shipping the gate on the season.

**Whether the craft prompt actually improves the script.** +$0.00036 an episode is not the question;
whether the output is better is, and nothing measures it. *Settled by:* re-running the script stage
on one episode with both prompts, diffing on three free metrics – characters saved, over-budget
count, and how many vocatives survive – then reading fifty lines side by side.

**The `merge_min_pause_ms` threshold.** 300 ms is inherited from the ASR brief's breath-point
analysis, not derived. At 300 ms the gate refuses 20 merges; nothing here shows whether 250 or 500 is
better. *Settled by:* a sweep against the same 22 A/B pairs.

**Whether the 9.9x ASR figure holds on dialogue-dense episodes.** One episode, one show. A 300 s
probe extrapolated to 6.1x and the full episode ran at 9.9x, so the direction of the error is
benign, but the ASR container has no way to report progress and `T_MAX = 50 s` from yt-translator
would fire on any episode – do not reuse that deadline.

**What is not knowable from here:** how much OpenRouter credit remains. Everything recommended above
is $0 recurring, so the plan survives the balance reaching zero – except candidate 4's re-read, which
is ElevenLabs money, and candidate 5, which is the way out of both.

---

Scratchpad sources for every MEASURED figure: `.../scratchpad/e06/{asr_job,fa_episode,anchor2,
measure3,prize,prize2,freebies}.py`, data in `fa_w30.json`, `asr_words.json`, `anchors.json`,
`measure3.json`, `prize.json`, `cuts_e06.txt`; subtitle-quality scripts in `.../scratchpad/plen/`.
Nothing was written to `/mnt/e`.

---

## Corrections, measured while implementing §2 and §3.4

Everything below was re-measured against the artefacts on disk after all six `speech.json` were
regenerated. The decisions above survive; three of the numbers do not.

**§2's prize was sized off the mildest episode.** S02E06 has 22 lines leading by more than 300 ms;
S02E02 and S02E05 have 38 and 39. Across all six episodes at score ≥ 0.8: **182 of 3443 lines lead
by more than 300 ms, 35 by more than 500 ms, 8 by more than 800 ms** – about 30 lines an episode
rather than 24 in the only episode that was measured.

**`score >= 0.8` does not bound the lead at 634 ms.** That figure was the maximum of one episode's
distribution before the artefacts were rebuilt; the worst lead now passing the score gate is
**+2526 ms** (S02E05), and S02E06's own worst is +991 ms. So the `min(..., max_delay_ms)` term is
load-bearing rather than cosmetic – it is the only thing containing a bad anchor. The bad anchors
are short rows, not low-scoring ones: `n_words >= 3` would drop the >800 ms cases from 8 to 3 while
costing 8 % of the matched set, where `score >= 0.9` costs 41 % of it and changes the worst case not
at all.

**Matching is bimodal, so the tolerance barely matters.** A Polish card and its English counterpart
start on the same millisecond 90.9 % of the time; loosening the match from 0 to 200 ms buys 10 cues
in 3443, and to 400 ms buys 28. Everything past that is a card with no counterpart at all. The
shipped `delay_match_tol_ms` of 120 ms is a safety margin, not a search.

**The delay does not cost a push, measured on real audio rather than on predicted durations.**
§3.6's test 4 asked that the "lines pushed later" count must not rise. On S02E06's shipped script,
rendered through the real backend: **73 pushed with the delay off, 73 with it on.**

**§3.4's proposed key was wrong in one respect.** It suggested keying reuse on
`(tuple(merged_from) or idx, text_original, budget_chars)`. The id tuple must not be in the key:
`parse_srt` numbers cues by their position in the file, so a re-extracted `.srt` with one extra
block renumbers everything after it and would miss on every line whose text never changed. The
merge is already carried by the text, since a merged cue's text is its parts joined. What the key
gained instead is the **prompt**, as a hash of `CONDENSE_SYSTEM`, without which a prompt edit ships
old text under a new prompt with nothing saying so.
