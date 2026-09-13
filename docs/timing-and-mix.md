# Placement, speed and the mix

When the lektor speaks relative to the actor, how the reading rate is planned across an episode, and how the original audio is ducked underneath.

[< back to the README](../README.md)

---

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

## Trimming the ends of a clip

Every engine pads an utterance with dead air – measured over 180 real lines, 44 ms before the speech
and 323 ms after it. That tail is most of the fixed per-utterance cost, and here it is pure loss:
the gap between two lektor lines is set by the subtitle timings, not by the engine. Trimming takes
the overhead from 414 ms to about 47 ms and hands the difference back to every short cue, which is
where the timing actually fails. `keep_ms` (40 ms) of padding stays on both ends, and a cosine taper
as long as the edge is loud removes the click that trimming to an arbitrary sample would leave.

One rule inside it needs care. A trailing run of voiced frames is thrown away when it looks like an
artefact rather than the end of the line, and the artefact is real: on a measured clip the speech
decayed to −60 dB by 2660 ms, sat in true silence for 200 ms, and the engine then appended 60 ms at
−14 dB, which plays as a hiss cut off mid-way. But *short, and behind a stretch of silence* is also
the exact shape of a Polish final plosive – the closure of a /k/, /t/ or /p/ is 60–130 ms of
near-silence and the release that follows is 20–40 ms. With the gap threshold at 100 ms the rule
could not tell the two apart, and on a one-syllable card it ate the release: "tak" played as "ta".

Three keys in `[voice.trim]` now have to agree before anything is dropped, and they are shared by
every backend that speaks, because this is a property of TTS rather than of one vendor:

| key | value | what it decides |
|---|---|---|
| `blip_ms` | 150 ms | longest run that may be an artefact at all; the measured one was 60 ms |
| `blip_gap_ms` | 200 ms | silence that must separate it from the speech. Raised from 100, which sat inside the range of an ordinary plosive closure. The measured artefact hid behind 200 ms of TRUE silence, so it is still caught with room to spare |
| `blip_min_keep_ms` | 350 ms | how much speech must be left standing. Below this, what would remain is itself the length of a syllable – the rule is about to eat part of the word rather than something appended after it. The measured artefact had 2660 ms in front of it and is unaffected |

Set `blip_min_keep_ms` to 0 to go back to trimming on the first two conditions alone.

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
