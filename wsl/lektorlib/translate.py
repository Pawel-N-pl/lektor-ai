"""The translate stage: a foreign subtitle in, the Polish `<base>.srt` every later stage reads, out.

Everything from `speech` to `pack` reads `<base>.srt` and does not care where it came from. On a
film with no Polish track that file used to have no source at all and the episode stopped at
extract. This stage is that source and nothing more: it writes the same file an extracted Polish
track would have been, so condensation, alignment, synthesis and the mix run unchanged on it.

What it does, in the order it matters:

  * The whole episode goes to the model in ONE call, for the reason condensation does it: who
    speaks to whom, ty or pan, what a pronoun points back to, is decided by context a slice of the
    episode does not have. An episode longer than translate.max_prompt_chars is split into
    contiguous chunks as a fallback, and each chunk then sees only itself.
  * The translation is faithful and in subtitle style, NOT condensed. Shortening to the time
    window is `script`'s job and its prompt is not touched; it needs the full line to know what
    may go. The same file is therefore also usable as Polish subtitles.
  * Timings are the source cues', one for one. The authored grid is the one thing a translation
    must not move, and `speech` aligns against the source on exactly that grid.
  * Every reply is validated before anything is written - every id present, non-empty, of a sane
    length against its source - with one targeted retry and then the fallback model for whatever
    is still missing. An episode with any cue still untranslated writes NO `<base>.srt`: a partial
    one would ship a lektor who falls silent wherever the model did, and nothing downstream could
    tell that from a quiet scene.
  * Spend goes to the same `<base>.spend.json` condensation writes, tagged `"stage": "translate"`
    and appended, so the episode keeps one bill.
  * Lines are reused from `<base>.translated.json`, keyed on the source line and everything that
    decides its translation, so a rerun pays only for what changed.
  * `<base>.srt` is hashed when it is written. A file whose hash no longer matches was edited by
    hand and is never overwritten without --force, and even then it is copied aside first. A
    `<base>.srt` this stage never wrote - an extracted Polish track - is never touched either.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import say
from .media import read_source_json, translate_mode, translate_settings
from .script import CallStats, Validation, _ask, append_spend, spend_path
from .srt import SENTENCE_END, SPEAKER_DASH, Cue, parse_srt

__all__ = ["stage_translate", "origin", "source_info", "source_srt", "srt_state"]

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

TRANSLATE_SYSTEM = """Jesteś doświadczonym tłumaczem list dialogowych i napisów na język polski.

Dostajesz PEŁNĄ listę dialogową jednego odcinka serialu albo filmu, w kolejności
chronologicznej, w języku oryginału. Każda kwestia ma numer w nawiasach kwadratowych.
Najpierw czytasz całość, żeby rozumieć postacie, relacje i wątki, a dopiero potem tłumaczysz.

Tłumaczysz KAŻDĄ kwestię na polski i zwracasz każdą pod jej własnym numerem.

WIERNOŚĆ, NIE SKRÓT
Tłumaczenie oddaje całą treść kwestii. NIE skracasz, nie streszczasz, nie pomijasz wołaczy,
powtórzeń, wtrąceń ani wypełniaczy, które niosą charakter postaci. Skracaniem do wersji
lektorskiej zajmuje się osobny, późniejszy etap, który potrzebuje pełnego tekstu, żeby wiedzieć,
co wolno wyrzucić. Kwestia przetłumaczona krócej, niż trzeba, to błąd, nie oszczędność.
Nie dodajesz niczego od siebie: żadnych objaśnień, przypisów ani didaskaliów.

JEDNA KWESTIA TO JEDNA KWESTIA
Każdy numer ma swój czas na ekranie. Nie łączysz kwestii, nie dzielisz ich i nie przenosisz treści
między numerami, nawet gdy zdanie zaczyna się w jednej, a kończy w następnej – wtedy tłumaczysz
tak, żeby polskie zdanie też było rozłożone na te same dwie kwestie w tym samym miejscu.
Myślnik na początku kwestii albo w jej środku oznacza zmianę mówiącego. Zostawiasz go dokładnie
tam, gdzie stał.

STYL NAPISÓW
Zwykła, naturalna polszczyzna mówiona: poprawna, zwięzła w obrębie zdania, bez kalek
składniowych z oryginału. Tak, jak brzmiałyby dobre polskie napisy do tej sceny.
Angielskie "you" rozstrzygasz: "ty" albo "pan"/"pani", na podstawie relacji między tą konkretną
parą postaci, i trzymasz się tego konsekwentnie przez cały odcinek.
Wulgaryzmy zostawiasz, ale o stopień słabsze niż dosłowny odpowiednik.
Ironię, nacisk i emocję oddajesz doborem słów, nie wykrzyknikami.

RODZAJ GRAMATYCZNY: NIE DOPOWIADASZ TEGO, CZEGO NIE MOŻESZ WIEDZIEĆ
Masz sam tekst, bez obrazu. Jeśli z kontekstu nie wynika, jakiej płci jest mówiący albo
rozmówca, wybierasz formę, która płci nie rozstrzyga: czas teraźniejszy, przyszły, tryb
rozkazujący, bezokolicznik, formy bezosobowe. Kiedy kontekst ją rozstrzyga – imię, zaimek,
wcześniejsza kwestia – stosujesz ją konsekwentnie.

CZEGO NIE RUSZASZ
- Imion, nazwisk i nazw własnych nie spolszczasz i nie zapisujesz fonetycznie.
- Liczby zostawiasz cyframi, tak jak w oryginale. Jednostek i walut nie przeliczasz – robi to
  późniejszy etap, który zna kontekst wymowy.
- Opisy dźwięków i didaskalia w nawiasach kwadratowych albo okrągłych tłumaczysz i zostawiasz
  w tych samych nawiasach. Znak ♪ zostawiasz tam, gdzie stał.

Odpowiadasz wyłącznie obiektem JSON: {"cues":[{"id":<int>,"text":"<polska kwestia>"}]}"""

# The per-cue line format, as a constant for the same reason script.py keeps its markers as
# constants: it is part of what the model is told, so it is part of the cache key.
LINE_FORMAT = "[{id}] {text}"

# Same {"cues": [{"id", "text"}]} shape as condensation, so script.parse_reply and the whole call
# machinery behind it are reused rather than copied. The ceiling is looser because a translated
# line is a full line, not a shortened one; the real per-line check is client-side, in validate.
TRANSLATE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "lektor_translated_cues",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "cues": {
                    "type": "array",
                    "description": "One entry per requested cue id, in the same order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "The cue id in square brackets."},
                            "text": {
                                "type": "string",
                                "description": "The faithful Polish translation of this cue.",
                                "maxLength": 1000,
                            },
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["cues"],
            "additionalProperties": False,
        },
    },
}

_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")


def source_cues(path: Path) -> list[Cue]:
    """The foreign cues as the model is shown them: markup gone, one line each, ids kept.

    Line breaks inside a card are joined with a space, and a leading speaker dash survives, so
    "- Hi.\\n- Hello." is shown as "- Hi. - Hello." - the model sees the change of speaker and keeps
    the dash. `c.raw` is left as it was, so `format_srt` can still read the card's own layout off
    it rather than guess it from the translation. Cards with no text left once the markup is
    stripped are dropped here: there is nothing to translate in them and nothing for the lektor
    to read.
    """
    out = []
    for c in parse_srt(path):
        text = _TAG_RE.sub("", c.raw).replace(" ", " ")
        text = " ".join(ln.strip() for ln in text.split("\n") if ln.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            c.text = text
            out.append(c)
    return out


def translate_prompt(cues) -> str:
    return "\n".join(LINE_FORMAT.format(id=c.idx, text=c.text) for c in cues)


def request_note(ids, reasons: dict[int, str] | None = None, partial: bool = True) -> str:
    """Name the cues still wanted - and why, after a failed attempt - so a retry is not a reroll."""
    reasons = reasons or {}
    lines = [f"[{i}] " + (reasons[i] if i in reasons else "do przetłumaczenia") for i in sorted(ids)]
    head = ("Pozostałe kwestie są tylko kontekstem. " if partial else "")
    return (head + "Zwróć WYŁĄCZNIE kwestie o poniższych numerach, przetłumaczone w całości, w tym "
            "samym formacie JSON:\n" + "\n".join(lines))


def prompt_fingerprint() -> str:
    """Hash of everything the model is told. Edit any of it and every cached line misses.

    `request_note` is deliberately out, as `retry_note` is in script.py: it only names cues, and
    its wording cannot change a line that came back right the first time.
    """
    blob = "\n".join([TRANSLATE_SYSTEM, LINE_FORMAT,
                      json.dumps(TRANSLATE_SCHEMA, sort_keys=True, ensure_ascii=False)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Validation – pure, unit-tested in wsl/tests/test_translate.py
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W\d_]{2,}")


def check_line(src: str, text: str, t: dict) -> str:
    """Why a returned line is unusable, or '' when it is fine.

    Only the absurd is rejected, because nobody has measured where a real translation's length
    sits against its source: empty, many times longer than the source, a fraction of a long
    source (condensed, which is the next stage's job), or a sentence handed back untranslated.
    "Ted!" -> "Ted!" is a translation; "I don't know what you mean." -> itself is not.
    """
    if not isinstance(text, str) or not text.strip():
        return "empty"
    n_src, n = len(src), len(text)
    hi = float(t.get("max_len_ratio", 3.0))
    lo = float(t.get("min_len_ratio", 0.25))
    floor = int(t.get("min_check_chars", 24))
    if n > hi * n_src + 20:
        return f"too long: {n} chars for a {n_src}-char source"
    if n_src >= floor and n < lo * n_src:
        return f"too short: {n} chars for a {n_src}-char source - condensed, not translated"
    if text.strip().casefold() == src.strip().casefold() and len(_WORD_RE.findall(src)) >= 4:
        return "came back untranslated"
    return ""


def validate(returned: dict[int, str], wanted: dict[int, str], t: dict) -> Validation:
    """Every id we asked for, checked. script.Validation, so the bookkeeping reads the same."""
    v = Validation()
    for cue_id, text in returned.items():
        src = wanted.get(cue_id)
        if src is None:
            v.extra.append(cue_id)
            continue
        why = check_line(src, text, t)
        if why:
            v.rejected[cue_id] = why
        else:
            v.accepted[cue_id] = text.strip()
    v.missing = sorted(i for i in wanted if i not in returned)
    v.extra.sort()
    return v


def miss_reasons(v: Validation) -> dict[int, str]:
    """The retry note's wording for every miss, in Polish because the model reads it."""
    out = {i: "brak w odpowiedzi" for i in v.missing}
    out.update({i: f"odrzucona ({why})" for i, why in v.rejected.items()})
    return out


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def call_settings(cfg, model: str | None = None) -> dict:
    """What script.call_openrouter reads, from the [translate] table.

    The endpoint falls back to script's, because there is one OpenRouter and two places to spell
    it is one place to get it wrong.
    """
    t = translate_settings(cfg)
    s = dict(t)
    s["openrouter_model"] = model or str(t.get("openrouter_model") or "")
    s.setdefault("openrouter_base", (cfg.raw.get("script") or {}).get(
        "openrouter_base", "https://openrouter.ai/api/v1"))
    s.setdefault("max_tokens", 64000)
    return s


@dataclass
class TranslateOutcome:
    texts: dict[int, str] = field(default_factory=dict)
    usd: float = 0.0
    calls: list[CallStats] = field(default_factory=list)
    path: str = "whole_episode"
    response_format: str = "json_schema"
    asked: int = 0
    missing: list[int] = field(default_factory=list)
    rejected: dict[int, str] = field(default_factory=dict)
    # Which model produced which line, so the provenance can say how much the fallback wrote.
    by_model: dict[str, int] = field(default_factory=dict)
    error: str = ""


def plan_chunks(cues, max_chars: int) -> tuple[list[list], str]:
    """One chunk for the whole episode unless its prompt would not fit; then contiguous slices.

    Equal slices by cue count, the way script.plan_segments cuts. Each slice loses the context of
    the others, which is the price of an episode that would not fit at all otherwise; nothing has
    measured how much that costs in quality, because no real episode has come near the limit.
    """
    size = len(translate_prompt(cues))
    if size <= max_chars or len(cues) < 2:
        return [list(cues)], "whole_episode"
    parts = math.ceil(size / max(1, max_chars))
    step = math.ceil(len(cues) / parts)
    chunks = [list(cues[i:i + step]) for i in range(0, len(cues), step)]
    return chunks, f"chunked:{len(chunks)}"


def translate_episode(cues, todo: set[int], cfg, api_key: str) -> TranslateOutcome:
    """Translate the cues in `todo`, showing the model every cue of their chunk as context.

    Per chunk: one call; one targeted retry naming every miss and why; then the fallback model
    for whatever the primary still could not produce. A primary that fails outright - out of
    retries, refusing, gone - goes straight to the fallback, which is what a fallback is for.
    """
    t = translate_settings(cfg)
    primary = call_settings(cfg)
    fallback_model = str(t.get("fallback_model") or "")
    out = TranslateOutcome(asked=len(todo))
    chunks, out.path = plan_chunks(cues, int(t.get("max_prompt_chars", 200000)))
    if out.path != "whole_episode":
        say(f"  prompt too long for one call – {out.path.replace(':', ' into ')} chunks")
    use_schema = bool(t.get("structured_outputs", True))
    last_reasons: dict[int, str] = {}

    def attempt(s: dict, chunk, want: set[int], note: str, purpose: str) -> tuple[Validation, str]:
        nonlocal use_schema
        messages = [{"role": "system", "content": TRANSLATE_SYSTEM},
                    {"role": "user", "content": translate_prompt(chunk)}]
        if note:
            messages.append({"role": "user", "content": note})
        got, use_schema, err = _ask(s, api_key, messages, use_schema, out, purpose,
                                    schema=TRANSLATE_SCHEMA)
        wanted = {c.idx: c.text for c in chunk if c.idx in want}
        v = validate(got, wanted, t)
        for i, text in v.accepted.items():
            out.texts[i] = text
            out.by_model[s["openrouter_model"]] = out.by_model.get(s["openrouter_model"], 0) + 1
        return v, err

    for chunk in chunks:
        ids = {c.idx for c in chunk}
        want = ids & todo
        if not want:
            continue
        partial = want != ids
        v, err = attempt(primary, chunk, want, request_note(want) if partial else "", "translate")
        if err:
            say(f"  !! {primary['openrouter_model']}: {err}")
        still = {i for i in want if i not in out.texts}
        if still and not err and bool(t.get("retry_misses", True)):
            say(f"  {len(still)} cue(s) missed ({len(v.missing)} absent, {len(v.rejected)} "
                f"rejected) – one targeted retry")
            v, err2 = attempt(primary, chunk, still, request_note(still, miss_reasons(v)), "retry")
            if err2:
                say(f"  .. retry failed ({err2})")
            still = {i for i in want if i not in out.texts}
        last_reasons.update(miss_reasons(v))
        err_fb = ""
        if still and fallback_model and fallback_model != primary["openrouter_model"]:
            say(f"  {len(still)} cue(s) still untranslated – asking {fallback_model}")
            alt = call_settings(cfg, fallback_model)
            v, err_fb = attempt(alt, chunk, still, request_note(still, last_reasons), "fallback")
            if err_fb:
                say(f"  !! {fallback_model}: {err_fb}")
            last_reasons.update(miss_reasons(v))
            still = {i for i in want if i not in out.texts}
        # A call that failed outright and left lines behind ends the episode: the next chunk
        # would only meet the same dead key, empty account or missing model. Lines that merely
        # failed validation do not - they are reported, and the next chunk is a fresh chance.
        if still and (err or err_fb):
            out.error = err_fb or err
            break

    out.response_format = "json_schema" if use_schema else "json_object"
    out.missing = sorted(i for i in todo if i not in out.texts)
    out.rejected = {i: why for i, why in last_reasons.items() if i in out.missing}
    out.usd = sum(c.cost for c in out.calls)
    return out


# ---------------------------------------------------------------------------
# Reuse cache
# ---------------------------------------------------------------------------

CACHE_VERSION = 1


def cache_key(cue, language: str, s: dict) -> str:
    """Everything that decides what a translated line comes back as, and one thing more.

    The start time is in the key although it changes nothing about the words, because the same
    English card - "I'm ready.", "Thank you." - is said by different people in one episode, and
    Polish puts the speaker's gender into the translation. Keyed on the text alone, the first
    "Jestem gotowa." would be stamped on every later "I'm ready." for good.

    Out, as in script.cache_key: temperature (the draw, not the contract) and the fallback model
    (it only ever runs once the primary has failed, against the same validation).
    """
    blob = json.dumps(
        [CACHE_VERSION, language, int(cue.start_ms), cue.text, str(s.get("openrouter_model", "")),
         str(s.get("reasoning_effort", "medium")), prompt_fingerprint()],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def book_path(cfg, base: str) -> Path:
    return cfg.work_dir / f"{base}.translated.json"


def load_book(cfg, base: str) -> dict[str, dict]:
    """Previous translations, or an empty book. An unreadable file is kept aside, never lost."""
    p = book_path(cfg, base)
    if not p.exists():
        return {}
    try:
        entries = json.loads(p.read_text(encoding="utf-8")).get("entries")
        if not isinstance(entries, dict):
            raise ValueError("no 'entries' object")
    except (OSError, ValueError, AttributeError) as exc:
        keep = p.with_suffix(f".corrupt-{time.strftime('%Y%m%dT%H%M%S')}.json")
        try:
            p.replace(keep)
            say(f"  !! {p.name} would not parse ({exc}) – kept as {keep.name}, translating fresh")
        except OSError:
            say(f"  !! {p.name} would not parse ({exc}) – translating fresh")
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict) and v.get("text")}


def write_book(cfg, base: str, entries: dict[str, dict]) -> Path:
    p = book_path(cfg, base)
    entries = {**load_book(cfg, base), **entries}
    payload = {
        "base": base,
        "version": CACHE_VERSION,
        "prompt": prompt_fingerprint(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "_note": ("Translated lines, keyed on (source language, start time, source text, model, "
                  "effort, prompt hash). A hit is reused instead of paid for again. Delete this "
                  "file, or run `translate --redraw`, to pay for a clean translation."),
        "entries": entries,
    }
    _write_atomic(p, json.dumps(payload, ensure_ascii=False, indent=1))
    return p


def split_by_book(cues, language: str, book: dict[str, dict], s: dict, t: dict
                  ) -> tuple[dict[int, str], set[int]]:
    """(hits, misses). A stored line is checked by the same rule as a fresh one: off disk a month
    later it has earned no more trust than it had on the day."""
    hits: dict[int, str] = {}
    todo: set[int] = set()
    for c in cues:
        entry = book.get(cache_key(c, language, s))
        text = entry.get("text") if entry else None
        if isinstance(text, str) and not check_line(c.text, text, t):
            hits[c.idx] = text
        else:
            todo.add(c.idx)
    return hits, todo


# ---------------------------------------------------------------------------
# Provenance, and the hand-edit guard
# ---------------------------------------------------------------------------


def source_info(cfg, base: str) -> dict:
    """`<base>.source.json`, or {} when there is none or it will not parse."""
    p = cfg.work_dir / f"{base}.source.json"
    if not p.is_file():
        return {}
    try:
        data = read_source_json(p)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def origin(cfg, base: str) -> str:
    """Where this episode's Polish text comes from: "track", "subs", or "" when nothing says yet.

    A sidecar with no `subtitle_origin` is a Polish extract - this side's, which only writes the
    key for a translation, or the PowerShell's, which predates the idea.
    """
    info = source_info(cfg, base)
    if not info:
        return ""
    return str(info.get("subtitle_origin") or "track")


def source_srt(cfg, base: str, info: dict | None = None) -> Path | None:
    """The foreign subtitle extract wrote for this episode, if it is still there."""
    info = source_info(cfg, base) if info is None else info
    rel = info.get("source_srt")
    if rel:
        p = cfg.work_dir / str(rel)
        return p if p.is_file() else None
    hits = sorted((cfg.work_dir / "subs").glob(f"{base}.src.*.srt"))
    return hits[0] if hits else None


def provenance_path(cfg, base: str) -> Path:
    return cfg.work_dir / f"{base}.translate.json"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def srt_state(cfg, base: str) -> str:
    """What `<base>.srt` is, as far as this stage is concerned.

      absent   nothing there yet
      ours     written by this stage and untouched since - the hash still matches
      edited   written by this stage, then changed by somebody: never overwritten without --force
      foreign  there, but not ours - an extracted Polish track, or a file put there by hand
    """
    srt = cfg.work_dir / f"{base}.srt"
    if not srt.is_file():
        return "absent"
    p = provenance_path(cfg, base)
    try:
        recorded = json.loads(p.read_text(encoding="utf-8")).get("srt_sha256")
    except (OSError, ValueError, AttributeError):
        return "foreign"
    if not recorded:
        return "foreign"
    return "ours" if recorded == file_sha256(srt) else "edited"


# ---------------------------------------------------------------------------
# Writing the Polish file
# ---------------------------------------------------------------------------


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# Where a speaker's line may begin inside a joined card: whitespace, then a dash, with or without
# a space after it - "-Hi. -Hello." is a house style of its own on some rips.
_TURN_RE = re.compile(r"\s+(?=[-\u2013\u2014])")


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


def wrap_subtitle(text: str, width: int = 42, speakers: int = 0) -> str:
    """Lay one translated line out the way a subtitle card is laid out.

    A card the SOURCE laid out as `speakers` lines, one per speaker, gets them back, dash first,
    which is the convention every Polish track on this disk follows and the one clean_text undoes
    when it joins them for the lektor. Only the source can say so: guessed from the translation
    alone, "- Ja - to znaczy, ja." grows a third speaker. Of the dashes in the line, the ones after
    the end of a sentence are taken first, since a dash inside a speaker's line follows a word.
    Otherwise a line over `width` is broken once, at the space nearest the middle: two balanced
    lines read better than a full one and a stub, and nothing here ever needs three.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if speakers >= 2:
        cuts = [m for m in _TURN_RE.finditer(text) if m.start() > 0]
        cuts.sort(key=lambda m: (not SENTENCE_END.search(text[:m.start()]), m.start()))
        chosen = sorted(cuts[:speakers - 1], key=lambda m: m.start())
        if chosen:
            parts, at = [], 0
            for m in chosen:
                parts.append(text[at:m.start()])
                at = m.end()
            parts.append(text[at:])
            return "\n".join(p.strip() for p in parts if p.strip())
    if len(text) <= width or " " not in text:
        return text
    mid = len(text) // 2
    spaces = [m.start() for m in re.finditer(" ", text)]
    cut = min(spaces, key=lambda i: abs(i - mid))
    return text[:cut] + "\n" + text[cut + 1:]


def format_srt(cues, texts: dict[int, str]) -> str:
    """The Polish SubRip: the source's timings untouched, renumbered from 1, one card per cue."""
    blocks = []
    for n, c in enumerate(cues, 1):
        card = wrap_subtitle(texts[c.idx], speakers=speaker_turns(c.raw))
        blocks.append(f"{n}\n{_ts(c.start_ms)} --> {_ts(c.end_ms)}\n{card}\n")
    return "\n".join(blocks)


def set_aside(path: Path) -> Path:
    """Move a file out of the way under a dated name, for when it may not be overwritten but may
    not stay either. `<base>.srt.bak-*` does not end in .srt, so no stage takes it for an
    episode."""
    keep = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    path.replace(keep)
    return keep


def _write_atomic(path: Path, text: str) -> None:
    """Written aside and renamed: a kill part-way through must not leave half a file behind."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid():x}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_spend_run(cfg, base: str, out: TranslateOutcome, language: str, n_cues: int,
                    reused: int) -> Path:
    """This run's bill, appended to the episode's one spend record."""
    t = translate_settings(cfg)
    return append_spend(cfg, base, {
        "stage": "translate",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": t.get("openrouter_model", ""),
        "fallback_model": t.get("fallback_model", ""),
        "reasoning_effort": t.get("reasoning_effort", "medium"),
        "source_language": language,
        "context_mode": out.path,
        "response_format": out.response_format,
        "usd": round(out.usd, 6),
        "cues": n_cues,
        "cues_reused": reused,
        "cues_asked": out.asked,
        "cues_translated": len(out.texts),
        "cues_missing": len(out.missing),
        "cues_rejected": out.rejected,
        "by_model": out.by_model,
        "error": out.error,
        "calls": [c.as_dict() for c in out.calls],
    })


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def candidate_bases(cfg, only: str | None) -> list[str]:
    """Every episode extract has written a sidecar for, filtered the way the other stages filter."""
    tail = ".source.json"
    bases = sorted(p.name[: -len(tail)] for p in cfg.work_dir.glob(f"*{tail}"))
    return [b for b in bases if not only or only in f"{b}.srt"]


def stage_translate(cfg, only: str | None, force: bool = False,
                    redraw: bool = False) -> dict[str, str]:
    """Write `<base>.srt` for every episode extract marked as translated. Returns base -> what
    happened ("written", "kept", "edited", "foreign", "track", "never", "failed").

    Exits non-zero, after doing every episode it can, when one could not be translated - the same
    contract as `script`, and for the same reason: `run` and `all` must not carry an episode with
    no Polish text into the stages that bill for reading it.
    """
    mode = translate_mode(cfg)
    t = translate_settings(cfg)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    result: dict[str, str] = {}
    failed: list[str] = []
    spent = 0.0
    bases = candidate_bases(cfg, only)
    if not bases:
        say(f"no <base>.source.json in {cfg.work_dir} – run extract first")
        return result

    for base in bases:
        info = source_info(cfg, base)
        if (info.get("subtitle_origin") or "track") != "subs":
            say(f"{base}: Polish subtitle track in the film – nothing to translate")
            result[base] = "track"
            continue
        if mode == "never":
            say(f"{base}: translate.mode is 'never' – skipped")
            result[base] = "never"
            continue
        out_srt = cfg.work_dir / f"{base}.srt"
        state = srt_state(cfg, base)
        if state == "ours" and not force:
            say(f"{base}: {out_srt.name} exists, skipping (--force to redo)")
            result[base] = "kept"
            continue
        if state == "edited" and not force:
            say(f"{base}: {out_srt.name} was edited by hand after it was translated – kept as it "
                f"is (--force replaces it, after copying your version aside)")
            result[base] = "edited"
            continue
        if state == "foreign" and not force:
            say(f"{base}: {out_srt.name} exists and this stage did not write it – kept as it is "
                f"(--force replaces it, after copying it aside)")
            result[base] = "foreign"
            continue

        say(f"\n{base}")
        src = source_srt(cfg, base, info)
        if src is None:
            failed.append(f"{base}: the foreign subtitle extract wrote is gone – run extract "
                          f"--force")
            result[base] = "failed"
            continue
        language = str(info.get("source_language") or src.name.split(".")[-2])
        cues = source_cues(src)
        if not cues:
            failed.append(f"{base}: {src.name} holds no text to translate")
            result[base] = "failed"
            continue
        s = call_settings(cfg)
        say(f"  {len(cues)} cues, {sum(len(c.text) for c in cues)} chars of '{language}' from "
            f"{src.parent.name}/{src.name}")

        reuse = bool(t.get("reuse_previous", True)) and not redraw
        book = load_book(cfg, base) if reuse else {}
        hits, todo = split_by_book(cues, language, book, s, t) if reuse \
            else ({}, {c.idx for c in cues})
        if hits:
            say(f"  reusing {len(hits)} of {len(cues)} lines from {book_path(cfg, base).name}; "
                f"{len(todo)} left to translate")
        elif redraw:
            say(f"  --redraw: ignoring {book_path(cfg, base).name}")

        drawn = TranslateOutcome()
        if todo and not api_key:
            failed.append(f"{base}: OPENROUTER_API_KEY not set")
            say("  !! OPENROUTER_API_KEY not set – nothing written")
            result[base] = "failed"
            continue
        if todo:
            say(f"  translating {len(todo)} cues with {s['openrouter_model']} "
                f"(effort {s.get('reasoning_effort', 'medium')}), whole episode as context")
            drawn = translate_episode(cues, todo, cfg, api_key)
            spent += drawn.usd
            if drawn.usd:
                say(f"  openrouter billed ${drawn.usd:.4f} for this episode "
                    f"({len(drawn.calls)} call(s), usage.cost)")
            say(f"  -> {write_spend_run(cfg, base, drawn, language, len(cues), len(hits)).name}")
            # Filed before the verdict, so an episode that fails on three lines pays for three
            # lines next time and not for the whole episode again - as long as reuse_previous is
            # on. Off, the book is neither written nor read, the way script treats its own.
            by_id = {c.idx: c for c in cues}
            fresh = {cache_key(by_id[i], language, s): {
                         "text": text, "id": i, "start_ms": by_id[i].start_ms, "src": by_id[i].text,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                     for i, text in drawn.texts.items() if i in by_id}
            if fresh and (reuse or redraw):
                say(f"  -> {write_book(cfg, base, fresh).name} ({len(fresh)} new line(s))")

        texts = {**hits, **drawn.texts}
        missing = [c.idx for c in cues if c.idx not in texts]
        if missing:
            why = drawn.error or ", ".join(f"[{i}] {drawn.rejected.get(i, 'absent')}"
                                           for i in missing[:6])
            failed.append(f"{base}: {len(missing)} of {len(cues)} cues untranslated ({why}"
                          f"{' ...' if len(missing) > 6 else ''})")
            say(f"  !! {len(missing)} cue(s) still untranslated – {out_srt.name} NOT written; a "
                f"partial file would leave the lektor silent on those lines")
            result[base] = "failed"
            continue

        if state in ("edited", "foreign"):
            keep = set_aside(out_srt)
            say(f"  previous {out_srt.name} copied aside as {keep.name}")
        _write_atomic(out_srt, format_srt(cues, texts))
        _write_atomic(provenance_path(cfg, base), json.dumps({
            "base": base,
            "srt": out_srt.name,
            "srt_sha256": file_sha256(out_srt),
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_srt": src.relative_to(cfg.work_dir).as_posix()
            if cfg.work_dir in src.parents else str(src),
            "source_language": language,
            "model": s["openrouter_model"],
            "fallback_model": t.get("fallback_model", ""),
            "prompt": prompt_fingerprint(),
            "cues": len(cues),
            "reused": len(hits),
            "translated": len(drawn.texts),
            "by_model": drawn.by_model,
            "spend": spend_path(cfg, base).name,
            "_note": ("Written by the translate stage. srt_sha256 is how a hand edit of the .srt "
                      "is recognised: a file that no longer matches it is never overwritten "
                      "without --force."),
        }, ensure_ascii=False, indent=1))
        say(f"  -> {out_srt.name} ({len(cues)} cues, timings 1:1 from the source)")
        result[base] = "written"

    if spent:
        say(f"\ntranslation cost this run: ${spent:.4f}")
    if failed:
        # Said from the setting, not assumed: with reuse_previous off the next run reads no book,
        # so promising that nothing is paid for twice would be wrong about the next bill.
        kept = ("Lines that did come back are kept in <base>.translated.json and will not be paid "
                "for again." if t.get("reuse_previous", True) else
                "translate.reuse_previous is false, so the lines that did come back are not reused "
                "and a rerun pays for every line again.")
        raise SystemExit(
            "\ntranslation did not finish for:\n  " + "\n  ".join(failed)
            + "\nNo Polish subtitle file was written for these episodes, so nothing downstream will "
              "run on them. " + kept)
    return result
