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
