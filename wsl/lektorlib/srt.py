"""Subtitles in, lektor-ready cues out.

Parsing, cleaning and the time-window arithmetic. This module is the one place that decides how
long a line is allowed to run, and every later stage budgets against what it says here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .units import convert_imperial
from pathlib import Path


@dataclass
class Cue:
    idx: int
    start_ms: int
    end_ms: int
    raw: str
    text: str = ""
    window_ms: int = 0
    budget_chars: int = 0
    condensed: bool = False
    # The condensed text came out of <base>.condensed.json rather than out of a paid draw.
    reused: bool = False
    # A human rewrote this line in the script file. Nothing regenerates it, and it never goes
    # into the reuse cache - the cache records what the model produced under stated conditions,
    # and this is not that.
    edited: bool = False
    over_budget: bool = False
    speed: float = 1.0
    has_digits: bool = False
    # How much later than its card this line is spoken, because the actor had not started yet.
    # Always >= 0: subtitles lead speech on purpose and a lektor is never moved earlier.
    delay_ms: int = 0
    # Ids of the cues folded into this one, empty when nothing was merged. Kept so the script file
    # can show what a line was assembled from.
    merged_from: list[int] = field(default_factory=list)
    # Set only for merged cues, built from the cleaned parts rather than by re-cleaning the
    # joined raw, which would run the whitespace rules across the join.
    text_original: str = ""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_srt(path: Path) -> list[Cue]:
    """Minimal SRT reader. Deliberately not pysrt: one less unmaintained dependency."""
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        m = _TIME_RE.search(lines[1] if _TIME_RE.search(lines[1] or "") else lines[0])
        body_from = 2
        if m is None:
            m = _TIME_RE.search(lines[0])
            body_from = 1
        if m is None:
            continue
        g = m.groups()
        cues.append(
            Cue(
                idx=len(cues) + 1,
                start_ms=_to_ms(*g[:4]),
                end_ms=_to_ms(*g[4:]),
                raw="\n".join(lines[body_from:]),
            )
        )
    return cues


def clean_text(raw: str, cfg) -> str:
    """Strip everything a lektor does not read out loud."""
    out = raw
    for pat in cfg["clean"]["strip_patterns"]:
        out = re.sub(pat, " ", out)

    # A leading dash marks a change of speaker in subtitles. Lektor reads the exchange as one
    # continuous line, so the dashes go and the lines are joined.
    lines = [re.sub(r"^\s*[-\u2013\u2014]\s*", "", ln).strip() for ln in out.split("\n")]
    out = " ".join(ln for ln in lines if ln)

    # The literal below is U+00A0; subtitle rippers emit it around punctuation.
    out = out.replace(" ", " ")
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    # Units before symbols and names: it rewrites numbers, and the later stages take the text
    # as it will be spoken.
    return respell(say_symbols(convert_imperial(out, cfg), cfg), cfg)


def say_symbols(text: str, cfg) -> str:
    """Turn marks into the words they are read as. Longest key first, so '##' cannot half-match."""
    table = cfg["clean"].get("spoken_symbols") or {}
    for src in sorted(table, key=len, reverse=True):
        text = text.replace(src, table[src])
    return re.sub(r"\s+", " ", text).strip()


def respell(text: str, cfg) -> str:
    """Rewrite foreign names the way they are actually said, in Polish spelling.

    A TTS voice reading Polish applies Polish letter rules, so a Spanish or English name comes out
    wrong: "Rojas" is said "Rohas", not "royas". The map lives in config so it can grow by ear,
    one show at a time.

    Matching is on the STEM, not the whole word, so Polish inflection follows for free:
    Rojas -> Rohas, Rojasa -> Rohasa, Rojasowi -> Rohasowi. Longer keys are applied first so a
    two-word name cannot be half-replaced by a shorter one.
    """
    table = cfg["clean"].get("pronunciation") or {}
    for src in sorted(table, key=len, reverse=True):
        # \b only on the left: the right side is where the case ending attaches.
        text = re.sub(rf"\b{re.escape(src)}", table[src], text)
    return text


def should_skip(raw: str, cfg) -> bool:
    return any(re.search(p, raw) for p in cfg["clean"]["skip_patterns"])


def predicted_ms(text: str, t: dict, cps: float | None = None) -> float:
    """How long this line is expected to take when read.

    The same fitted model the character budgets invert: a fixed cost per utterance plus a cost per
    character. It is a prediction, not a measurement -- the voice stage measures the audio that
    really came back -- but it is the only estimate available before anything has been spoken, and
    the delay planner needs one to know whether pushing a line later would push it out of its own
    window.
    """
    overhead = float(t.get("overhead_ms", 0.0))
    rate = float(t.get("ms_per_char", 0.0))
    if rate <= 0.0:
        rate = 1000.0 / float(cps or t.get("chars_per_second", 14.5) or 14.5)
    return (overhead + rate * len(text)) * numeral_drag(text, t)


# Polish number words, as the condensation stage is required to write them - digits never survive
# into a lektor line, so this is the only form they appear in.
_NUMERAL_RE = re.compile(
    r"\b(?:"
    # Explicit stems only. An earlier version used st\\w* for "sto" and matched "staje",
    # "starej" and "strumykiem" - 50 cues of 810 were being called numerals and quietly given a
    # tighter slot than they needed.
    r"zer[oa]|jeden|jedn[aeoy]\w*|dwa|dwie|dwóch|dwoma|dwu"
    r"|trzy|trzech|trzema|cztery|czterech|czterema"
    r"|pię[ćc]\w*|sze[śs][ćc]\w*|siedem|siedmi\w+|osiem|o[śs]mi\w+|dziewię[ćc]\w*"
    r"|dziesię[ćc]\w*|dziesiąt\w*"
    r"|jedena[śs]ci\w*|dwana[śs]ci\w*|trzyna[śs]ci\w*|czterna[śs]ci\w*|pi[ęe]tna[śs]ci\w*"
    r"|szesna[śs]ci\w*|siedemna[śs]ci\w*|osiemna[śs]ci\w*|dziewiętna[śs]ci\w*"
    r"|dwadzieścia|dwudziest\w+|trzydzie[śs]ci\w*|trzydziest\w+"
    r"|czterdzie[śs]ci\w*|czterdziest\w+|pię[ćc]dziesiąt\w*|sze[śs][ćc]dziesiąt\w*"
    r"|siedemdziesiąt\w*|osiemdziesiąt\w*|dziewię[ćc]dziesiąt\w*"
    r"|st[oau]|stem|dwie[śs]ci\w*|trzyst\w+|czteryst\w+|pię[ćc]set|sze[śs][ćc]set"
    r"|siedemset|osiemset|dziewię[ćc]set|setn\w+"
    r"|tysią\w+|tysię\w+|milion\w*|miliard\w*|bilion\w*"
    r"|pierwsz\w+|drug\w+|trzeci\w*|czwart\w+|piąt\w+|szóst\w+|siódm\w+|ósm\w+"
    r"|dziewiąt\w+|setn\w+|tysięczn\w+"
    r")\b", re.IGNORECASE)


def numeral_drag(text: str, t: dict) -> float:
    """How much slower than the fitted average a line reads because it is mostly numbers.

    Measured on the engine, not assumed. A written-out year - "Rok pański tysiąc dwieście
    czterdziesty siódmy" - came back at 87.6 ms per character against the model's 73.5, and three
    rewrites of the same line landed between 86 and 91: hyphenating the numeral saved 70 ms of
    4030, closing the spaces saved 120. So it is not the spelling, it is that the engine enunciates
    numbers deliberately and pauses between them, and no way of writing them down changes that.

    Left uncorrected this is not a cosmetic error. The planner sees a line that fits, asks for no
    speed-up, and the line then runs past the end of its slot - in the case that prompted this, 270
    ms past a shot cut and into a scene that had already gone silent.

    The factor is applied on the share of the line that is numerals, so an ordinary sentence with
    one number in it is barely touched and a line that is nothing but a number gets the whole of
    it.
    """
    peak = float(t.get("numeral_drag", 1.0))
    if peak <= 1.0 or not text:
        return 1.0
    tail = text.rstrip()
    # Only where it was actually measured: a line that ENDS on a number and gives the engine no
    # terminal punctuation. Everything else was tested and found not to need it - numerals in the
    # MIDDLE of a sentence read at 62.7 ms per character against 62.5 for the same sentences with
    # the numbers swapped out, i.e. no difference at all. Applied more widely it was inflating the
    # predicted duration of 50 cues of 810 and hurrying lines that had all the time they needed.
    if not tail or tail[-1] in ".!?…":
        return 1.0
    last = _NUMERAL_RE.search(tail.rstrip(",;:-\u2013\u2014"))
    if not last or last.end() < len(tail.rstrip(",;:-\u2013\u2014")) - 1:
        return 1.0
    hit = sum(len(m.group(0)) for m in _NUMERAL_RE.finditer(text))
    share = min(1.0, hit / max(1, len(text.replace(" ", ""))))
    return 1.0 + (peak - 1.0) * share


def compute_windows(cues: list[Cue], cfg) -> None:
    """How long each line may actually run, borrowing from the silence that follows it."""
    t = cfg["timing"]
    for i, c in enumerate(cues):
        gap = (cues[i + 1].start_ms - c.end_ms) if i + 1 < len(cues) else t["max_borrow_ms"]
        borrow = max(0, min(gap - t["gap_guard_ms"], t["max_borrow_ms"]))
        c.window_ms = c.duration_ms + borrow


SENTENCE_END = re.compile(r"[.!?][\"'\u201d\u00bb)\]]*\s*$")
ELLIPSIS_END = re.compile(r"(\.\.\.|\u2026)[\"'\u201d\u00bb)\]]*\s*$")
SPEAKER_DASH = re.compile(r"^\s*[-\u2013\u2014]\s*\S")


def is_screen_text(cue: "Cue") -> bool:
    """A title card, chyron or sign rather than something a person says.

    Subtitles set these in capitals. They are their own utterance and must never be glued to
    dialogue: merging produced lines like a commentator's sentence running straight into the show's
    title card, and in two cases a sentence ENDING on a screen graphic.
    """
    letters = [ch for ch in cue.text if ch.isalpha()]
    if len(letters) < 4:
        return False
    return sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.8


def continues_sentence(prev: "Cue", cur: "Cue", ev=None) -> bool:
    """Is `cur` the rest of a sentence that `prev` started?

    This is the whole predicate, and the earlier version got it wrong: it merged on the timestamp
    gap alone. Measured over six episodes, this release puts a fixed 82-85 ms between EVERY card,
    so that gap says nothing about continuation - 89% of the merges it authorised glued two
    FINISHED sentences together, including across shot changes, and only 10.7% were the
    mid-sentence continuation the feature exists for.

    A card that does not close its sentence is the real signal. An ellipsis counts as continuation;
    a leading dash marks a change of speaker and is never joined.

    The text rules above are everything the subtitles can tell us, and they cannot tell us what
    the actors or the editor did at the seam. `ev` is that evidence, from `speech.json`, and it can
    only ever REFUSE a merge the text rules already allowed - it never authorises one. Passing None
    (no artefact, or the gate switched off in config) leaves the predicate exactly as it shipped.
    """
    if SENTENCE_END.search(prev.text) and not ELLIPSIS_END.search(prev.text):
        return False
    if SPEAKER_DASH.match(cur.raw.lstrip()):
        return False
    if is_screen_text(prev) or is_screen_text(cur):
        return False
    if ev is not None:
        # A cut in the seam means the edit says these are two shots, and a lektor breath crossing
        # it asserts one continuous delivery over a discontinuous picture. A real silence in the
        # seam means the actors paused there themselves, and merging talks OVER that pause instead
        # of into it - which is the one thing the craft literature is unanimous about
        # (Wozniak 2012:216: deliver into the gaps of the original).
        if ev.cut_in(prev.end_ms, cur.start_ms):
            return False
        if ev.pause_at(prev.end_ms, cur.start_ms):
            return False
    return True


def merge_contiguous(cues: list[Cue], cfg, ev=None) -> list[Cue]:
    """Join the cards that carry ONE sentence, so the lektor reads it as one breath.

    A sentence split across two cards is read as two utterances, each with its own onset and tail,
    and the lektor leaves a silent hole in the middle of a thought that the picture does not have.

    Both conditions must hold: the cards are contiguous in time (gap at the release's floor) AND
    the first one does not finish its sentence. The timestamp alone is not evidence of anything.
    """
    m = cfg["clean"]
    gap_ms = float(m.get("merge_gap_ms", 0.0))
    if gap_ms <= 0 or not cues:
        return cues
    max_cues = int(m.get("merge_max_cues", 4))
    max_chars = int(m.get("merge_max_chars", 160))
    max_span_ms = int(m.get("merge_max_span_ms", 9000))

    out: list[Cue] = []
    group: list[Cue] = [cues[0]]

    def flush() -> None:
        if len(group) == 1:
            out.append(group[0])
            return
        first, last = group[0], group[-1]
        merged = Cue(idx=first.idx, start_ms=first.start_ms, end_ms=last.end_ms,
                     raw="\n".join(c.raw for c in group))
        # Built from the already-cleaned parts, never by re-cleaning the joined raw: the whitespace
        # rules would then run across the join and eat the space before a card opening on "...".
        merged.text = " ".join(c.text for c in group)
        merged.merged_from = [c.idx for c in group]
        merged.text_original = merged.text
        out.append(merged)

    for prev, cur in zip(cues, cues[1:]):
        joinable = (
            cur.start_ms - prev.end_ms <= gap_ms
            and continues_sentence(prev, cur, ev)
            and len(group) < max_cues
            and len(" ".join(c.text for c in group)) + 1 + len(cur.text) <= max_chars
            and cur.end_ms - group[0].start_ms <= max_span_ms
        )
        if joinable:
            group.append(cur)
        else:
            flush()
            group = [cur]
    flush()
    return out


def load_cues(path: Path, cfg, ev=None) -> list[Cue]:
    """parse -> skip -> clean -> drop empties -> merge -> windows. The order matters and is load-bearing.

    Merging comes after cleaning (so it joins what will actually be spoken, not markup) and before
    windows (so the merged cue gets one window spanning the whole group).

    `ev` is the optional speech evidence for the merge gate. It is threaded through rather than
    loaded here so this module keeps knowing nothing about docker, ffmpeg or the work dir.
    """
    cues = [c for c in parse_srt(path) if not should_skip(c.raw, cfg)]
    for c in cues:
        c.text = clean_text(c.raw, cfg)
    cues = [c for c in cues if c.text]
    cues = merge_contiguous(cues, cfg, ev)
    compute_windows(cues, cfg)
    return cues
