"""Subtitles in, lektor-ready cues out.

Parsing, cleaning and the time-window arithmetic. This module is the one place that decides how
long a line is allowed to run, and every later stage budgets against what it says here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    over_budget: bool = False
    speed: float = 1.0

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
    return respell(say_symbols(out, cfg), cfg)


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


def compute_windows(cues: list[Cue], cfg) -> None:
    """How long each line may actually run, borrowing from the silence that follows it."""
    t = cfg["timing"]
    for i, c in enumerate(cues):
        gap = (cues[i + 1].start_ms - c.end_ms) if i + 1 < len(cues) else t["max_borrow_ms"]
        borrow = max(0, min(gap - t["gap_guard_ms"], t["max_borrow_ms"]))
        c.window_ms = c.duration_ms + borrow


def load_cues(path: Path, cfg) -> list[Cue]:
    """parse -> skip -> clean -> drop empties -> windows. The order matters and is load-bearing."""
    cues = [c for c in parse_srt(path) if not should_skip(c.raw, cfg)]
    for c in cues:
        c.text = clean_text(c.raw, cfg)
    cues = [c for c in cues if c.text]
    compute_windows(cues, cfg)
    return cues
