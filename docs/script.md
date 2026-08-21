# Condensation: shortening a line instead of reading it faster

How the whole episode is condensed in one call, which model draws it, the rules the prompt enforces, and why the result is filed so nothing is drawn twice.

[< back to the README](../README.md)

---

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
