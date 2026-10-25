"""Who says a line, where the source subtitle already says so.

Polish puts the speaker's gender into the verb - "byłem" or "byłam" - and the listener's into
every second sentence, and the model translating from a text with no picture has to guess both.
Some sources do not leave it to a guess. SDH tracks label speakers - `JOHN: Where were you?`,
`[Mary] At the pub.` - and a card laid out with dialogue dashes says how many people speak in it.
This module reads that off the source card, turns it into a short hint the model is shown in front
of the line, and takes the label out of the line itself:

  * a label is not dialogue. Left in, `JOHN:` is translated, survives into <base>.srt because
    clean.strip_patterns only knows brackets, and is read out by the lektor;
  * the hint is in braces, which source_cues has already stripped from every source line as ASS
    markup, so the model can never mistake one for text.

What counts as a label, and why it is this narrow:

  * `NAME:` at the start of a line, NAME in capitals (`JOHN:`, `MR. SMITH:`, `TED (V.O.):`) - the
    SDH convention. A capitalised word with a colon that is not a name (`WARNING:`) is taken for
    one; nothing on this disk was checked for how often that happens.
  * `Name:` in any case, when the glossary knows the name.
  * `[Name]` or `(Name)` at the start of a line, ONLY when the glossary knows the name or it is a
    `NAME:` label elsewhere in the episode. A bracket at the start of a line is far more often a
    sound - `[Laughs]`, `(Sighs)`, `[MUSIC]` - and calling a sound a speaker would tell the model a
    character named Laughs said the line.

A file with no labels and no dropped first dash gets no hints at all, and its cues - text, prompt
and cache keys - are exactly what they were without this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .glossary import GENDERS, Glossary
from .srt import SPEAKER_DASH

__all__ = ["harvest", "SpeakerHints", "speaker_turns", "HINT_FORMATS"]

# Markup source_cues strips before the model sees a line, stripped here for the same reason.
_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
_DASH = r"[-–—]"
_CAPS_LABEL = re.compile(
    rf"^(?P<lead>\s*(?:{_DASH}\s*)?)"
    r"(?P<name>[^\W\d_][^\s:()\[\]]*(?:\s+[^\W\d_][^\s:()\[\]]*){0,2})"
    r"\s*(?:\([^)]*\)\s*)?:(?=\s|$)\s*")
_BRACKET_LABEL = re.compile(
    rf"^(?P<lead>\s*(?:{_DASH}\s*)?)[\[(](?P<name>[^\])\n]{{1,40}})[\])]\s*:?\s*")

# What the model is shown in front of a line. Part of the prompt fingerprint whenever an episode
# carries a hint, as translate.LINE_FORMAT is: change the wording and the hinted lines miss.
HINT_FORMATS = {
    "one": "{{mówi: {who}}}",
    "many": "{{mówią po kolei: {who}}}",
    "turns": "{{mówiących: {n}}}",
}


def speaker_turns(raw: str) -> int:
    """How many speakers a source card holds, read off its own line breaks; 0 for a plain card.

    `source_cues` joins the lines for the model, so this is the one place the card's layout is
    still known. A card whose later lines start with a dash is a dialogue card even when the first
    line carries no dash of its own, which some rips leave off.
    """
    lines = [ln for ln in _TAG_RE.sub("", raw).split("\n") if ln.strip()]
    dashed = sum(1 for ln in lines if SPEAKER_DASH.match(ln))
    if not any(SPEAKER_DASH.match(ln) for ln in lines[1:]):
        return 0
    return dashed + (0 if SPEAKER_DASH.match(lines[0]) else 1)


@dataclass
class SpeakerHints:
    text: dict[int, str] = field(default_factory=dict)    # cue id -> hint the model is shown
    # Cue id -> how many speakers a card holds whose own layout does not show it: labels on
    # separate lines with no dashes. format_srt lays the Polish card out by it.
    turns: dict[int, int] = field(default_factory=dict)
    names: set[str] = field(default_factory=set)          # every label seen, as written

    def __bool__(self) -> bool:
        return bool(self.text)


def _lines(raw: str) -> list[str]:
    text = _TAG_RE.sub("", raw).replace(" ", " ")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _caps_name(line: str, glossary: Glossary | None) -> re.Match | None:
    m = _CAPS_LABEL.match(line)
    if not m:
        return None
    name = m.group("name")
    letters = [ch for ch in name if ch.isalpha()]
    if len(letters) >= 2 and name.isupper():
        return m
    if glossary is not None and glossary.lookup(name):
        return m
    return None


def _join(lines: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _who(name: str, glossary: Glossary | None, many: bool) -> str:
    c = glossary.lookup(name) if glossary is not None else None
    shown = c.name if c else name
    if c and c.gender:
        return f"{shown} ({GENDERS[c.gender]})" if many else f"{shown}, {GENDERS[c.gender]}"
    return shown


def harvest(cues, glossary: Glossary | None = None) -> SpeakerHints:
    """Take the speaker labels out of the cues' text and say what they said as hints.

    `cues` are source_cues' output; the text of a labelled cue is rewritten without its labels,
    and a cue left with nothing - a card that was only `JOHN:` - ends with empty text for the
    caller to drop. Every other cue is not touched.
    """
    out = SpeakerHints()
    # A bracket label is believed only for a name the episode labels the SDH way somewhere, so
    # that pass comes first.
    known = set()
    for c in cues:
        for ln in _lines(c.raw):
            m = _caps_name(ln, glossary)
            if m:
                known.add(m.group("name").casefold())

    for c in cues:
        lines = _lines(c.raw)
        speakers: list[str] = []
        # (line, opens a speaker's turn) - a label alone on its line opens the next one.
        kept: list[tuple[str, bool]] = []
        opens = False
        for ln in lines:
            m = _caps_name(ln, glossary)
            if m is None:
                b = _BRACKET_LABEL.match(ln)
                name = b.group("name").strip() if b else ""
                if b and (name.casefold() in known
                          or (glossary is not None and glossary.lookup(name))):
                    m = b
            if m is None:
                kept.append((ln, opens))
                opens = False
                continue
            name = m.group("name").strip()
            out.names.add(name)
            if not speakers or speakers[-1].casefold() != name.casefold():
                speakers.append(name)
            lead = m.group("lead").strip()
            rest = ln[m.end():].strip()
            if rest:
                kept.append((f"{lead} {rest}" if lead else rest, True))
                opens = False
            else:
                opens = True
        if speakers:
            texts = [ln for ln, _ in kept]
            if len(speakers) > 1 and not any(SPEAKER_DASH.match(ln) for ln in texts[1:]):
                # Two labelled speakers on lines with no dashes: the dashes are what tells the
                # model, and later the card's layout, that the speaker changes here.
                texts = [f"- {ln}" if turn and not SPEAKER_DASH.match(ln) else ln
                         for ln, turn in kept]
                out.turns[c.idx] = sum(1 for _, turn in kept if turn)
            c.text = _join(texts)
            if len(speakers) == 1:
                out.text[c.idx] = HINT_FORMATS["one"].format(who=_who(speakers[0], glossary, False))
            else:
                out.text[c.idx] = HINT_FORMATS["many"].format(
                    who=", ".join(_who(s, glossary, True) for s in speakers))
            continue
        turns = speaker_turns(c.raw)
        if turns >= 2 and lines and not SPEAKER_DASH.match(lines[0]):
            # The rip dropped the first speaker's dash, so the joined line shows one dash fewer
            # than there are people.
            out.text[c.idx] = HINT_FORMATS["turns"].format(n=turns)
    return out
