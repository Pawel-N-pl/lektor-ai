# Known rough edges

What does not work well, stated plainly.

[< back to the README](../README.md)

---

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
  `mix.duck_to_db` is -3.0 while its note says -4.5 by ear.
- **The two piper fit figures disagree.** The comment above `voice.backend` says 597 of 598 lines
  landed inside their window on the real episode; the one above `voice.piper.search_min` says 4
  lines were clamped and still over on what reads as the same episode, worst +16 %. Both are
  recorded as measured, both date from the same commit, and the table above quotes the second.

  Re-measured on a full episode of dense fansub material: **557 of 755 inside their window, 198
  over, worst +197 %**. That does not convict either figure - this is a different release, with 53 %
  of its cards over budget at the source against 11-20 % on the material the originals were taken
  from - but it does show that both were written as properties of the ENGINE when they are
  properties of an episode. Read them as "piper on easy material", not as a specification.

- **Transcription has never run on a real film.** Everything about the `transcribe` stage – the
  hallucination thresholds, the blacklist, the card lengths, the 300 ms linger, `large-v3` itself
  – ships at the library's defaults or common Whisper practice where `lektor.tuning.toml` names
  one, and at this project's own unmeasured choice everywhere else (the loop and repeat counts,
  the speech-overlap and blacklist shares, the pause lengths), and is covered by unit tests on
  constructed segments only. Nobody has listened to a lektor read from a transcript, nor counted
  how many of Whisper's segments the filters drop on real audio, nor checked how often its
  language detection is wrong when the audio track has no tag. Whisper gives no speakers either,
  so a transcribed card never carries the speaker dash a subtitle would, nor a speaker hint.

- **The series glossary, the speaker hints and MMS_FA have never met a real episode.** All three
  are unit-tested against stand-ins only: no model has been shown a glossary or a `{mówi: ...}`
  hint, so whether it follows them, and how often, is unmeasured; the SDH label rule was not
  checked against how many real cards it misreads (`WARNING:` is taken for a name); MMS_FA has
  aligned no non-English episode, its scores are filtered by a `speech.min_score` measured on the
  English model, and the aligner script ran only against a stand-in torch.

- **piper is the one engine allowed to gabble.** `timing.max_speed` is 1.2 and every other backend
  obeys it - omnivoice has an explicit floor that lets a line overrun rather than be compressed past
  that ceiling. piper reaches its window by searching `length_scale` down to `voice.piper.search_min`
  = 0.6, which is 1.67x faster than natural, and the run above used it: **476 of 755 lines read
  faster than natural, up to 1.67x**. Nothing is wrong with the search; the ceiling simply is not
  wired into it, so the two engines answer "how fast may a line be read" differently.
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
- **Three numbers that guard short lines are judgement, not measurement.** Everything else in the
  tuning file carries the measurement behind it; these three do not. `voice.trim.blip_gap_ms` (200,
  raised from 100) and `voice.trim.blip_min_keep_ms` (350, new) were reasoned from Polish phonetics –
  a final plosive's closure runs 60–130 ms and its release 20–40 ms – and from the one measured
  artefact they still have to catch, which hid behind 200 ms of true silence. `voice.omnivoice.
  duration_floor_ms` (550, raised from 400) is simply the length a short word plus its release
  occupies. All three were changed on a report of one-syllable lines arriving cut short; **none has
  been put through a listening test.** They affect every line of every backend, so if the lektor
  starts drawling on short cards, `duration_floor_ms` is the first to put back.
- **A clone reference cut from a finished mix carries whatever sat under the speaker.** Not a
  hypothetical: searching a 28-minute mixed recording for the cleanest five seconds, the best
  window available still had the backing only **32 dB below the speaker**, and the worst candidates
  sat at 26 dB. The model clones what it hears, so that bed is reproduced in every line it then
  speaks. Three separate window-scoring passes were needed to get an honest number, because a naive
  score rewards windows whose "quiet background" is really a leading or trailing silence – a window
  with 250 ms of digital silence at one end scores as 45 dB cleaner than it is. A **dry** recording
  of the voice you want beats any careful cut from a mixed one, and no searching closes that gap.
- Cached TTS lives in `_lektor/.cache/`, keyed by everything that changes the waveform. Delete it to
  force regeneration; keep it and reruns are free.
