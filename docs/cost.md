# What a run costs

Characters, dollars, credits and GPU minutes.

[< back to the README](../README.md)

---

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
