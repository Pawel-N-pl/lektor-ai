# From subtitle file to lektor cards

Choosing the right track, cleaning it, and deciding which cards become one breath - including every rule that turned out to be a fact about one authoring tool rather than about the film.

[< back to the README](../README.md)

---

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

### No Polish track: a foreign one to translate from

Under `translate.mode = "auto"` a film with no Polish **text** track – none at all, or only a
bitmap one – no longer ends at extract. A Polish text track, when there is one, is extracted
exactly as before, into the same `<base>.srt` with the same sidecar; nothing below touches that
path. Otherwise extract picks the foreign text track the translation is made from, by
`pick_translation_source`, which is deliberately not `pick_subtitle`: that one answers "which track
IS the Polish dialogue" and must not drift.

* **Candidates** are text tracks with a real language tag (not empty, `und`, `zxx`, `mis`, `mul`),
  not Polish, and not forced – by disposition or by a title saying so. A forced track is signage;
  translating it gives a lektor who reads out street names.
* **Language** – the audio's own language first (read off the same `extract.audio_stream` the
  downmix uses), then English, then anything else. A translation of what the actors say beats a
  translation of a translation, and English is second because it is also what `speech` can align
  against. `translate.source_language` set to a tag takes only that language. Two- and
  three-letter codes and the bibliographic/terminology pairs (`ger`/`deu`, `fre`/`fra`) are one
  language, because mkv muxers write either.
* **Within a language** plain dialogue beats SDH (`translate.prefer_plain_dialogue`): SDH's
  `[door creaks]` would be translated and then stripped again by `clean.strip_patterns`. SDH is
  still a candidate, and still the preferred **alignment** reference – see below.

The track lands in `_lektor/subs/<base>.src.<lang>.srt`, not beside `<base>.srt`: every stage finds
its episodes with `*.srt` in the work dir, and a `<base>.src.eng.srt` there would come back as an
episode of its own, with `script` condensing the English. The sidecar gains `subtitle_origin =
"subs"`, `source_language`, `source_srt`, `source_sdh` and `audio_language`. A sidecar without
`subtitle_origin` – every Polish extract, and every one the PowerShell ever wrote – means the
film's own track. `translate.source = "asr"` (transcribing the audio) is phase 2 and is reported as
not implemented, never quietly treated as `subs`.

`translate` then writes `<base>.srt` from it: the whole episode in one OpenRouter call, faithful and
in subtitle style rather than condensed (condensation is still `script`'s job, on its untouched
prompt), timings copied card for card from the source. Every line is validated – present,
non-empty, not absurdly long, not a fraction of a long source, not handed back untranslated – with
one targeted retry and then `translate.fallback_model` for what is still missing. **An episode
with any line still untranslated gets no `<base>.srt` at all**: a partial one would leave the
lektor silent on exactly those lines, and nothing downstream can tell that from a quiet scene.
The written file is hashed into `<base>.translate.json`, so a hand edit is recognised and never
overwritten without `--force` – and with it, only after the edited file is copied aside. A
`<base>.srt` the stage did not write is treated the same way. Reruns reuse lines from
`<base>.translated.json`, keyed on the source line, its start time (two identical "I'm ready."
cards can be said by people of different gender, and Polish shows it), the source language, the
model, the effort and a hash of the prompt.

**The English it was translated from doubles as the alignment reference.** `find_reference_srt`
takes it after every configured pattern. Translation prefers the plain track and alignment
prefers SDH, so when the film carries an SDH stream other than the one translated from, `speech`
still extracts that one; without one it reuses the source rather than reading the same stream out
of the film twice. Any source language other than English is not a reference: the acoustic model
is English.

**Polish subtitles, only when asked and only for a translation.** `translate.subtitles` (or
`--subs` for one run): `sidecar` writes `<film>.pl.srt` beside the film and the `.mka`, re-synced
on every pack so a later fix to the translation reaches it; `mux` appends it to the remuxed `.mkv`
as a `pol` stream titled `Polski (AI)`, after every original subtitle and **not** default, which
only happens with `--to mux` – the plan says so when the chain stops at pack. A film that had a
Polish track never gets a second, "AI" copy of it.

## The mux

The mux keeps all 43 and appends the lektor after every original audio stream, tagging it
`language=pol` / `title=Lektor AI (PL)`, making it the default and demoting the originals – stream
copy only, verified on a cut clip. **Its audio index is counted off the source with ffprobe, not
assumed to be 1**, or a release with a commentary track would get the commentary labelled as the
lektor.

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

Three things about that stage are worth knowing, because each of them used to be a way for it to
fail quietly.

**The English reference comes out of the episode itself.** Alignment reads the verbatim English
text, and supplying it used to be a hand step – which meant it was the step that got skipped, and
the gate then ran on nothing. `speech` now pulls the English SDH stream out of the same mkv and
writes it to `_lektor/subs/`, so the evidence is there whether or not anybody remembered.

**A 5.1 tag does not promise the centre channel carries anything.** One release measured FC at
−114 dBFS against −31 for L and R: the aligner read 51 MB of digital silence and returned 0 of 468
cues **without erroring**, which looks exactly like an episode where alignment simply found nothing.
`speech.centre_min_mean_db` (−60 dBFS) is the gap between those two worlds – speech that is present
sits far above it, the same film's downmix measured −27 – and below it the downmix is taken instead.
`mix.centre_silent_lufs` does the same job one stage later for the dialogue loudness reference, so
an empty FC cannot quietly let title cards into the measurement either. Confirm from the log: the
stage says **"centre audio"** when it read the real thing.

**Shot cuts can decode on the GPU.** Measured on one minute of 2160p HEVC 10-bit, three planes of
`scdet` each time: 47.3 s on the CPU, 12.1 s through NVDEC, and 5.0 s with `speech.cuts_resize`
(960x540) as well – all three finding the same six cuts, to the millisecond. At 1080p H.264 the CPU
is the faster of the two (13.3 s against 20.7 s), so `cuts_decoder = 'auto'` sends only 4K to the
GPU, with `cuts_gpu_min_width` (2560) as the crossover. What the GPU buys below that is not speed
but **cores**: 93–134 % against the CPU path's 965 %, i.e. one core instead of ten, which is worth
having on a machine sharing the box with other work. Name a decoder (`h264_cuvid`) to force it.

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

The comment above `clean.merge_gate` records that conclusion. The value next to it is still `"both"`, i.e. the
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
