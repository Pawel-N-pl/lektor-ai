"""The script stage: subtitles in, a lektor-ready script out, condensed by an LLM.

The whole episode goes to the model in ONE call. Measured against the same model batched, that is
$0.196 vs $0.433 per episode and every cue is shortened with the full dialogue as context instead
of three neighbours. Cues that overflow their time window are marked inline as

    [id] >>> SKROC DO N ZNAKOW <<< tekst

and the model returns only those. The prompt and the system message below are the ones that
produced the model comparison in the config notes; do not reword them without re-running it.

Two things the pipeline must never do: trust the reply, and guess the price. Every returned cue is
checked (present, non-empty, not longer than the original) before it can replace anything, and the
spend written to disk is the number OpenRouter reports in usage.cost, never one recomputed from
token prices, which would miss mandatory reasoning tokens.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import say
from .config import Config, load_cps
from .srt import clean_text, load_cues

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

CONDENSE_SYSTEM = """Jesteś doświadczonym autorem tekstów lektorskich do polskiej telewizji.

Dostajesz PEŁNĄ listę dialogową jednego odcinka serialu, w kolejności chronologicznej.
Większość kwestii służy wyłącznie jako kontekst - masz je przeczytać, żeby rozumieć scenę,
postacie i wątki, ale ICH NIE ZWRACASZ.

Zwracasz wyłącznie kwestie oznaczone znacznikiem >>> SKROC DO N ZNAKOW <<<.

Zasady skracania:
- Zachowaj sens, intencję i ton. Nie wolno zgubić informacji potrzebnej do zrozumienia fabuły.
- Tekst lektorski to nie napisy. Skracaj tak, jak robi to lektor: wywalaj wypełniacze,
  powtórzenia i redundancję, którą widz i tak widzi na ekranie. Upraszczaj składnię.
- BEZWZGLĘDNIE zachowaj imiona, nazwiska, nazwy własne i liczby. Wywalenie imienia to błąd.
- SKRACAJ ODWAŻNIE. Twoim zadaniem jest ZMIEŚCIĆ SIĘ W LIMICIE, a nie ocalić całe zdanie.
  Nie musisz zachować wszystkiego, co zostało powiedziane. Masz zachować SENS i NAJWAŻNIEJSZE
  SŁOWA - to, bez czego widz zgubi się w scenie.
- Wywalaj bez wahania: grzecznościówki, wołacze, powtórzenia, dopowiedzenia, przymiotniki
  ozdobne, wahania, i wszystko, co widz i tak widzi na ekranie. Drugorzędny szczegół ma wylecieć
  pierwszy. Jeśli po skróceniu zostaje samo jądro kwestii - to jest poprawny wynik.
- ŻADNYCH SKRÓTÓW. To ma być PRZECZYTANE NA GŁOS. "hisz." zamiast "hiszpański", "ok." zamiast
  "około", "itd.", "np.", "tzn." - każdy z nich jest błędem, bo lektor tego nie wymówi.
  Skracasz WYRZUCAJĄC SŁOWA, nigdy okrawając je do kropki.
- BEZWZGLĘDNIE zostaw imiona, nazwiska, nazwy własne i liczby, jeśli w ogóle zostają w zdaniu.
- To, co zostaje, musi być pełnym, wymawialnym zdaniem po polsku: poprawna fleksja, pełne słowa,
  polskie znaki diakrytyczne. Nigdy "remisow" zamiast "remisów".
- Bez didaskaliów, nawiasów i cudzysłowów.
- KAŻDA LICZBA SŁOWAMI. Kwestie oznaczone >>> LICZBY SLOWAMI <<< zwracasz zawsze, nawet jeśli poza
  tym nic w nich nie zmieniasz. Żadnej cyfry nie wolno zostawić: "7" -> "siedem", "1963" ->
  "tysiąc dziewięćset sześćdziesiąt trzy", "2:2" -> "dwa do dwóch", "50%" -> "pięćdziesiąt
  procent", "3:30" -> "trzecia trzydzieści", "1." -> "pierwszy", "23 kwietnia" -> "dwudziestego
  trzeciego kwietnia".
- Liczba ma być ODMIENIONA tak, jak wymaga zdanie: przypadek, rodzaj i liczba muszą się zgadzać
  z rzeczownikiem i czasownikiem. "o 7 remisach" to "o siedmiu remisach", nie "o siedem remisach".
  "2 zawodników" to "dwóch zawodników". "w 5 minucie" to "w piątej minucie". Przeczytaj zdanie
  w głowie i zapisz to, co naprawdę by padło.
- LICZBA TO INFORMACJA. Skracając kwestię z liczbami nie wolno żadnej z nich wyrzucić ani scalić
  - jeśli padły trzy liczby, w wyniku muszą być trzy. Miejsce znajdujesz wyrzucając inne słowa.
  Tak samo zostaje to, co liczbę określa: dzień bez miesiąca albo wynik bez tego, czego dotyczy,
  jest bezużyteczny.
- NIE PRZEPISUJ NAZWISK ANI IMION. Zostawiaj je dokładnie w takiej formie, w jakiej dostałeś
  (poza odmianą, której wymaga zdanie). Angielskie nazwiska są czytane poprawnie i każda próba
  zapisania ich "fonetycznie" tylko je psuje. Jeśli w kwestii stoi już zapis fonetyczny, na
  przykład "Rohas", ZOSTAW GO - jest tam celowo i nie wolno go cofać do "Rojas".
- Nazwy własne i skrótowce z liczbą zapisz tak, jak się je WYMAWIA, a nie litera po literze:
  "PS5" -> "PlayStation pięć", "F1" -> "Formuła jeden", "A4" -> "A cztery". Skrótowiec, który
  lektor przeczytałby jako zwykłe słowo, rozpisz na głoski albo na pełną nazwę.
- Kolejność ważności, gdy trzeba wybierać: (1) zmieścić się w limicie, (2) pełne słowa bez
  skrótów, (3) jak najwięcej sensu w tym, co zostało. Krótka, uboższa, ale poprawna i zrozumiała
  kwestia jest lepsza niż bogata, która się nie mieści.
- Wykorzystuj kontekst sąsiednich kwestii: jeśli imię padło zdanie wcześniej, można użyć zaimka.

Odpowiadasz wyłącznie obiektem JSON: {"cues":[{"id":<int>,"text":"<skrócona kwestia>"}]}"""


HAS_DIGIT = re.compile(r"\d")


def needs_number_words(text: str) -> bool:
    """A digit anywhere means the line cannot be read aloud as written."""
    return bool(HAS_DIGIT.search(text))


def whole_prompt(cues, target_ids: set[int]) -> str:
    """Every cue in order; the ones needing work carry markers saying which work.

    Two jobs share one call because both need the same thing the prompt already carries: the whole
    episode as context. Shortening needs it to know what may be dropped, and spelling a number out
    needs it to pick the right Polish case - "7 remisow" is "siedem remisow" in one sentence and
    "siedmiu remisach" in the next.
    """
    out = []
    for c in cues:
        marks = []
        if c.idx in target_ids:
            marks.append(f">>> SKROC DO {c.budget_chars} ZNAKOW <<<")
        if needs_number_words(c.text):
            marks.append(">>> LICZBY SLOWAMI <<<")
        prefix = (" ".join(marks) + " ") if marks else ""
        out.append(f"[{c.idx}] {prefix}{c.text}")
    return "\n".join(out)


# The per-cue limit differs for every cue, so JSON Schema cannot express it – maxLength is only a
# coarse ceiling and strict mode is not guaranteed to enforce even that. The real limit lives in the
# prompt and is enforced client-side in validate().
CUES_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "lektor_condensed_cues",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "cues": {
                    "type": "array",
                    "description": "One entry per marked cue, in the same order. Never return unmarked cues.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "description": "The cue id in square brackets."},
                            "text": {
                                "type": "string",
                                "description": "Condensed Polish lektor line, within the character limit given for this id.",
                                "maxLength": 400,
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

JSON_OBJECT_FORMAT = {"type": "json_object"}


# ---------------------------------------------------------------------------
# Validation – pure, no network, unit-tested in wsl/tests/test_script.py
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """One cue the model was asked to shorten."""

    idx: int
    original: str
    budget: int
    # Spelling a number out makes the line LONGER ("7" -> "siedem"), so the usual "a condensation
    # that grew is a failure" rule cannot apply to it.
    allow_growth: bool = False


@dataclass
class Validation:
    accepted: dict[int, str] = field(default_factory=dict)   # id -> usable shortened text
    rejected: dict[int, str] = field(default_factory=dict)   # id -> why it was refused
    missing: list[int] = field(default_factory=list)         # asked for, never came back
    over_budget: list[int] = field(default_factory=list)     # accepted but still too long
    extra: list[int] = field(default_factory=list)           # returned without being asked

    @property
    def misses(self) -> list[int]:
        """Everything worth a second attempt, in cue order."""
        return sorted(set(self.missing) | set(self.rejected) | set(self.over_budget))


class ReplyError(ValueError):
    """The model's reply was not the object we asked for."""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_reply(content: str) -> dict[int, str]:
    """Turn the message content into id -> text, or raise ReplyError.

    json_schema mode makes this trivial; the json_object fallback occasionally arrives wrapped in a
    markdown fence or with prose around it, so the outermost object is dug out before parsing.
    """
    if not isinstance(content, str) or not content.strip():
        raise ReplyError("empty message content")
    text = _FENCE_RE.sub("", content.strip())
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ReplyError("content is not JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError as exc:
            raise ReplyError(f"content is not JSON ({exc})") from None
    if not isinstance(data, dict):
        raise ReplyError("top level is not an object")
    cues = data.get("cues")
    if not isinstance(cues, list):
        raise ReplyError("no 'cues' array in the reply")
    out: dict[int, str] = {}
    for item in cues:
        if not isinstance(item, dict) or "id" not in item or "text" not in item:
            continue
        try:
            cue_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        if not isinstance(item["text"], str):
            continue
        # A duplicated id means the model answered twice; the last word wins, as it would in a dict.
        out[cue_id] = item["text"].strip()
    if not out:
        raise ReplyError("reply carried no usable cue")
    return out


def validate(returned: dict[int, str], targets: list[Target]) -> Validation:
    """Check every cue we asked for. A condensation that grew is a failure, not a result."""
    v = Validation()
    wanted = {t.idx: t for t in targets}
    for cue_id, text in returned.items():
        t = wanted.get(cue_id)
        if t is None:
            v.extra.append(cue_id)
            continue
        if not text:
            v.rejected[cue_id] = "empty"
        elif len(text) > len(t.original) and not t.allow_growth:
            v.rejected[cue_id] = f"grew {len(t.original)} -> {len(text)} chars"
        elif t.allow_growth and needs_number_words(text):
            v.rejected[cue_id] = "still has digits"
        else:
            v.accepted[cue_id] = text
            if len(text) > t.budget:
                v.over_budget.append(cue_id)
    v.missing = [t.idx for t in targets if t.idx not in returned]
    v.extra.sort()
    v.over_budget.sort()
    return v


def retry_note(v: Validation, targets: list[Target]) -> str:
    """Name the cues that missed, and why, so the second attempt is not a blind reroll."""
    wanted = {t.idx: t for t in targets}
    lines = []
    for cue_id in v.misses:
        t = wanted.get(cue_id)
        if t is None:
            continue
        if cue_id in v.missing:
            why = "brak w odpowiedzi"
        elif cue_id in v.rejected:
            why = f"odrzucona ({v.rejected[cue_id]})"
        else:
            why = f"za długa: {len(v.accepted[cue_id])} znaków przy limicie {t.budget}"
        lines.append(f"[{cue_id}] limit {t.budget} znaków - {why}")
    return (
        "Poprzednia odpowiedź nie załatwiła poniższych kwestii. Popraw WYŁĄCZNIE je i zwróć "
        "TYLKO je, w tym samym formacie JSON:\n" + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

# OpenRouter: 408 timed out, 429 rate limited, 502 model down, 503 no provider. Everything else in
# the 4xx range means the same thing on every attempt, so retrying it only burns minutes.
RETRYABLE = (408, 429, 502, 503)

_SCHEMA_COMPLAINT_RE = re.compile(
    r"response_format|json[_ ]schema|structured[_ ]output", re.IGNORECASE
)


class CondenseError(RuntimeError):
    """The call failed in a way no retry will fix."""


class SchemaRejected(CondenseError):
    """The provider refused response_format json_schema. Fall back to json_object."""


@dataclass
class CallStats:
    purpose: str
    response_format: str
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "response_format": self.response_format,
            "usd": round(self.cost, 6),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "seconds": round(self.seconds, 1),
        }


def _retry_after(resp) -> float | None:
    raw = getattr(resp, "headers", {}).get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(120.0, float(raw)))
    except (TypeError, ValueError):
        return None


def _error_text(resp) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 – an error body that is not JSON is still worth printing
        return (getattr(resp, "text", "") or "")[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        meta = err.get("metadata") or {}
        extra = f" [{meta.get('provider_name') or meta.get('error_type')}]" if meta else ""
        return f"{err.get('message', '')}{extra}"[:300]
    return json.dumps(body)[:300]


def call_openrouter(s: dict, api_key: str, messages: list[dict], response_format: dict,
                    purpose: str) -> tuple[dict[int, str], CallStats]:
    """One request. Retries only what is worth retrying, and honours Retry-After when told to."""
    body = {
        "model": s["openrouter_model"],
        # max_tokens is not optional here: reasoning effort is spent as a fraction of it, so a
        # request without one gets a token budget too small to think with. Measured on this
        # episode – no max_tokens: 62 reasoning tokens and 139/210 cues inside budget; with
        # max_tokens 48000: the 206/210 the model comparison recorded.
        "max_tokens": int(s.get("max_tokens", 48000)),
        "reasoning": {"effort": s.get("reasoning_effort", "medium")},
        "response_format": response_format,
        "messages": messages,
    }
    # Counting characters is a constraint-following job, and the default sampling temperature is
    # measurably bad at it: same model, same prompt, one call – 92/210 cues inside budget without
    # this field, 206/210 with it at 0.3. Some endpoints drop temperature; those are exactly the
    # endpoints where the client-side validation below earns its keep.
    if s.get("temperature") is not None:
        body["temperature"] = float(s["temperature"])
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # HTTP-Referer is the header that actually creates the app entry; X-OpenRouter-Title alone
        # does nothing, and a localhost referer needs the title as well to be tracked at all.
        "HTTP-Referer": s.get("referer", "https://github.com/local/lektor-ai"),
        "X-OpenRouter-Title": "lektor-ai",
        "X-Title": "lektor-ai",
        "X-OpenRouter-Categories": "cli-agent",
    }
    fmt = response_format.get("type", "?")
    retries = max(1, int(s.get("max_retries", 4)))
    last = "no attempt made"

    for attempt in range(retries):
        started = time.time()
        try:
            r = requests.post(
                f"{s['openrouter_base']}/chat/completions",
                headers=headers,
                json=body,
                timeout=int(s.get("timeout_s", 600)),
            )
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            _sleep(attempt, retries, None)
            continue

        if r.status_code in RETRYABLE:
            last = f"HTTP {r.status_code}: {_error_text(r)}"
            _sleep(attempt, retries, _retry_after(r))
            continue
        if r.status_code >= 400:
            detail = _error_text(r)
            if fmt == "json_schema" and _SCHEMA_COMPLAINT_RE.search(detail):
                raise SchemaRejected(detail)
            raise CondenseError(f"HTTP {r.status_code}: {detail}")

        try:
            payload = r.json()
        except ValueError as exc:
            last = f"reply was not JSON: {exc}"
            _sleep(attempt, retries, None)
            continue

        # A 200 can still be a failure: OpenRouter emits mid-generation errors in the body.
        err = payload.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", ""))
            code = err.get("code")
            if fmt == "json_schema" and _SCHEMA_COMPLAINT_RE.search(msg):
                raise SchemaRejected(msg)
            if code in RETRYABLE:
                last = f"error {code}: {msg}"
                _sleep(attempt, retries, None)
                continue
            raise CondenseError(f"error {code}: {msg}")

        usage = payload.get("usage") or {}
        stats = CallStats(
            purpose=purpose,
            response_format=fmt,
            # usage.cost is what the account was actually charged. Recomputing it from token prices
            # would miss reasoning tokens, which bill as output and are mandatory on some models.
            cost=float(usage.get("cost") or 0.0),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            seconds=time.time() - started,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CondenseError(f"reply had no message content ({exc})") from None
        # A malformed body is worth one more roll of the dice; the spend is already booked either
        # way, so the stats of a discarded attempt must still reach the caller.
        try:
            return parse_reply(content), stats
        except ReplyError as exc:
            last = f"{exc}"
            if attempt == retries - 1:
                raise CondenseError(f"unusable reply after {retries} tries: {last}") from None
            say(f"    .. {last} – retrying")
            _sleep(attempt, retries, None)
            continue

    raise CondenseError(f"gave up after {retries} tries: {last}")


def _sleep(attempt: int, retries: int, retry_after: float | None) -> None:
    """Back off before the next attempt. After the last one there is none, so it does not wait:
    Retry-After is honoured up to 120 s, and spending that on the way to giving up is dead time."""
    if attempt >= retries - 1:
        return
    time.sleep(retry_after if retry_after is not None else min(30.0, 2**attempt) + random.random())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class CondenseOutcome:
    texts: dict[int, str] = field(default_factory=dict)
    usd: float = 0.0
    calls: list[CallStats] = field(default_factory=list)
    path: str = "whole_episode"
    response_format: str = "json_schema"
    marked: int = 0
    returned: int = 0
    accepted: int = 0
    over_budget: list[int] = field(default_factory=list)
    rejected: dict[int, str] = field(default_factory=dict)
    missing: list[int] = field(default_factory=list)
    error: str = ""


def budget_for(window_ms: int, cps: float, t: dict, spells_numbers: bool = False) -> int:
    """How many characters actually fit in a window.

    A single chars-per-second figure is wrong for subtitle cues, and wrong in the direction that
    hurts: measured over 598 real lines the duration is `414 + 73.5 * chars` ms, so every utterance
    carries a fixed cost on top of its text. Half the cues here are under 25 characters, where that
    cost dominates -- a ten-character line runs 1218 ms, not the 672 ms a flat 14.9 chars/s predicts.
    Ignoring it sent 43% of lines over their window even after condensation.

    The linear model wins whenever timing.ms_per_char is set, and the measured chars/second is then
    not consulted at all – it only reaches the budgets through the 1000/cps fallback, i.e. with
    timing.ms_per_char at 0. Setting overhead_ms to 0 does NOT restore the flat model; it only drops
    the fixed cost from the linear one.
    """
    overhead = float(t.get("overhead_ms", 0.0))
    ms_per_char = float(t.get("ms_per_char", 0.0)) or (1000.0 / cps)
    usable = window_ms * t["safety"] - overhead
    # Slack, because overshoot is cheap and mutilation is not. A line a few percent long is pulled
    # in by atempo, which is free and inaudible at these factors; a line squeezed to fit by
    # abbreviating ("hiszpanski" -> "hisz.") or by dropping a clause is a defect the viewer hears.
    slack = float(t.get("budget_slack", 1.0))
    if spells_numbers:
        # Spelling out inflates the line badly - "14" is two characters, "czternascie" is eleven -
        # and against an ordinary budget the model buys the room back by dropping a number, which
        # is the one thing it must not do. Measured on S02E04: "4 wygrane, 4 przegrane, 14 remisow"
        # came back without the remisy at all.
        slack *= float(t.get("number_slack", 1.0))
    return max(6, int(usable * slack / ms_per_char))


def plan_segments(cues, target_ids: set[int], max_chars: int):
    """One call for the whole episode, unless the prompt would not fit. Then contiguous slices.

    Batching is strictly worse – 2.2x the price and each cue sees only its neighbours – so it is a
    fallback for an episode long enough to blow the context window, not a mode anyone should pick.
    """
    if len(whole_prompt(cues, target_ids)) <= max_chars:
        return [(list(cues), set(target_ids))], "whole_episode"
    parts = math.ceil(len(whole_prompt(cues, target_ids)) / max_chars)
    size = math.ceil(len(cues) / parts)
    segments = []
    for start in range(0, len(cues), size):
        chunk = list(cues[start : start + size])
        marked = {c.idx for c in chunk if c.idx in target_ids}
        if marked:
            segments.append((chunk, marked))
    return segments, f"batched:{len(segments)}"


def condense_episode(cues, target_ids: set[int], cfg: Config, api_key: str) -> CondenseOutcome:
    """Shorten every marked cue, re-rolling a bad draw rather than living with it.

    This model decides for itself how hard to think, and the decision swings the result far more
    than the choice of model does: across seven identical calls, 513 reasoning tokens landed 93 of
    210 cues inside budget and 5520 landed 207. A bad draw is visible in the reply, so the cheap
    call is worth re-rolling -- about $0.014 an episode on average, against $0.02 for a model that
    is steady on the first try but no better when it lands.
    """
    s = cfg["script"]
    ratio = float(s.get("accept_fit_ratio", 0.9))
    rerolls = max(0, int(s.get("max_rerolls", 4)))
    budgets = {c.idx: c.budget_chars for c in cues
               if c.idx in target_ids and len(c.text) > c.budget_chars}
    if not budgets:
        return _condense_once(cues, target_ids, cfg, api_key)

    def fits(o: CondenseOutcome) -> int:
        return sum(1 for i, t in o.texts.items() if i in budgets and len(t) <= budgets[i])

    best: CondenseOutcome | None = None
    spent: list[CallStats] = []
    need = int(len(budgets) * ratio)
    attempts = 0
    for attempt in range(rerolls + 1):
        out = _condense_once(cues, target_ids, cfg, api_key)
        attempts += 1
        spent.extend(out.calls)
        if best is None or fits(out) > fits(best):
            best = out
        got = fits(best)
        if got >= need or out.error or attempt == rerolls:
            break
        say(f"  re-roll {attempt + 1}/{rerolls}: {got}/{len(budgets)} inside budget, "
            f"below the {need} needed")

    assert best is not None
    # Report the whole bill, not just the winning attempt's share of it.
    best.calls = spent
    best.usd = sum(c.cost for c in spent)
    # An attempt that died still belongs in the spend record even when an earlier draw is the one
    # being kept, or a run cut short by a 402 would be filed as a clean run that simply stopped.
    if out.error and not best.error:
        best.error = out.error
    if fits(best) < need:
        say(f"  !! settled for {fits(best)}/{len(budgets)} inside budget after {attempts} "
            f"attempt(s); the rest will be sped up")
    return best


def _condense_once(cues, target_ids: set[int], cfg: Config, api_key: str) -> CondenseOutcome:
    """One whole-episode pass plus its targeted retry of the misses."""
    s = cfg["script"]
    targets = [Target(c.idx, c.text, c.budget_chars, needs_number_words(c.text))
               for c in cues if c.idx in target_ids]
    out = CondenseOutcome(marked=len(targets))
    if not targets:
        return out

    segments, path = plan_segments(cues, target_ids, int(s.get("max_prompt_chars", 400_000)))
    out.path = path
    if path != "whole_episode":
        say(f"  prompt too long for one call – {path.replace(':', ' into ')} segments")
    else:
        say("  whole-episode prompt, one call")

    use_schema = bool(s.get("structured_outputs", True))
    returned: dict[int, str] = {}

    for chunk, marked in segments:
        chunk_targets = [t for t in targets if t.idx in marked]
        messages = [
            {"role": "system", "content": CONDENSE_SYSTEM},
            {"role": "user", "content": whole_prompt(chunk, marked)},
        ]
        got, use_schema, err = _ask(s, api_key, messages, use_schema, out, "condense")
        if err:
            out.error = err
            say(f"  !! {err}")
            break
        returned.update(got)

        # One targeted second attempt: the misses are named, with the reason, over the same context.
        v = validate(got, chunk_targets)
        if v.misses and bool(s.get("retry_misses", True)):
            say(f"  {len(v.misses)} cues missed ({len(v.missing)} absent, {len(v.rejected)} rejected, "
                f"{len(v.over_budget)} over budget) – one targeted retry")
            retry_ids = set(v.misses)
            messages = [
                {"role": "system", "content": CONDENSE_SYSTEM},
                {"role": "user", "content": whole_prompt(chunk, retry_ids)},
                {"role": "user", "content": retry_note(v, chunk_targets)},
            ]
            got2, use_schema, err2 = _ask(s, api_key, messages, use_schema, out, "retry")
            if err2:
                say(f"  .. retry failed ({err2}) – keeping the best available")
            else:
                fixed = validate(got2, [t for t in chunk_targets if t.idx in retry_ids])
                # Only take a retry line if it beats what the first pass produced.
                for cue_id, text in fixed.accepted.items():
                    prev = returned.get(cue_id)
                    if prev is None or len(text) < len(prev):
                        returned[cue_id] = text

    out.response_format = "json_schema" if use_schema else "json_object"
    final = validate(returned, targets)
    out.texts = final.accepted
    out.returned = len(returned.keys() & {t.idx for t in targets})
    out.accepted = len(final.accepted)
    out.over_budget = final.over_budget
    out.rejected = final.rejected
    out.missing = final.missing
    out.usd = sum(c.cost for c in out.calls)
    return out


def _ask(s: dict, api_key: str, messages: list[dict], use_schema: bool, out: CondenseOutcome,
         purpose: str) -> tuple[dict[int, str], bool, str]:
    """Call with json_schema, degrade to json_object once if the provider refuses it."""
    if use_schema:
        try:
            got, stats = call_openrouter(s, api_key, messages, CUES_SCHEMA, purpose)
            out.calls.append(stats)
            return got, True, ""
        except SchemaRejected as exc:
            say(f"    .. provider refused json_schema ({exc}) – falling back to json_object")
        except CondenseError as exc:
            return {}, use_schema, str(exc)
    try:
        got, stats = call_openrouter(s, api_key, messages, JSON_OBJECT_FORMAT, purpose)
        out.calls.append(stats)
        return got, False, ""
    except CondenseError as exc:
        return {}, False, str(exc)


# ---------------------------------------------------------------------------
# Spend record
# ---------------------------------------------------------------------------


def spend_path(cfg: Config, base: str) -> Path:
    return cfg.work_dir / f"{base}.spend.json"


def write_spend(cfg: Config, base: str, out: CondenseOutcome) -> Path:
    """Append this run to the episode's spend record. Reruns really do cost more; say so."""
    p = spend_path(cfg, base)
    record = {"base": base, "cost_source": "openrouter usage.cost", "runs": []}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old.get("runs"), list):
                record["runs"] = old["runs"]
        except ValueError:
            # This file is the only record of what the account was billed. An unreadable one is
            # kept aside instead of being overwritten with a fresh empty history, which would
            # silently understate the total for the rest of the project's life.
            keep = p.with_suffix(f".corrupt-{time.strftime('%Y%m%dT%H%M%S')}.json")
            p.replace(keep)
            say(f"  !! {p.name} would not parse – kept as {keep.name}, starting a new record")
    record["runs"].append(
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": cfg["script"]["openrouter_model"],
            "reasoning_effort": cfg["script"].get("reasoning_effort", "medium"),
            "context_mode": out.path,
            "response_format": out.response_format,
            "usd": round(out.usd, 6),
            "cues_marked": out.marked,
            "cues_returned": out.returned,
            "cues_accepted": out.accepted,
            "cues_in_budget": out.accepted - len(out.over_budget),
            "cues_over_budget": len(out.over_budget),
            "cues_missing": len(out.missing),
            "cues_rejected": out.rejected,
            "error": out.error,
            "calls": [c.as_dict() for c in out.calls],
        }
    )
    record["usd_total"] = round(sum(r.get("usd", 0.0) for r in record["runs"]), 6)
    # Written aside and renamed: a kill or a full disk part-way through a plain write leaves a
    # truncated file, and the truncated file is the whole billing history.
    tmp = p.with_suffix(f".{os.getpid():x}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def read_spend(cfg: Config, base: str) -> float:
    """Everything this episode has cost in condensation so far, or 0 if it has never run."""
    p = spend_path(cfg, base)
    if not p.exists():
        return 0.0
    try:
        return float(json.loads(p.read_text(encoding="utf-8")).get("usd_total") or 0.0)
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def stage_script(cfg: Config, only: str | None, force: bool) -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    srts = sorted(p for p in cfg.work_dir.glob("*.srt"))
    if only:
        srts = [p for p in srts if only in p.name]
    if not srts:
        raise SystemExit(f"no .srt in {cfg.work_dir} – run the Windows extract stage first")

    cps = load_cps(cfg)
    t = cfg["timing"]
    spent = 0.0
    failed: list[str] = []

    for srt in srts:
        base = srt.stem
        out_path = cfg.work_dir / f"{base}.lektor.json"
        if out_path.exists() and not force:
            say(f"{base}: script exists, skipping (--force to redo)")
            continue

        say(f"\n{base}")
        cues = load_cues(srt, cfg)
        for c in cues:
            c.budget_chars = budget_for(c.window_ms, cps, t, needs_number_words(c.text))

        overflow = [c for c in cues if len(c.text) > c.budget_chars]
        # Digits cannot be read aloud, and a cue that already fits is never sent for shortening,
        # so number cues join the same call regardless of their length.
        numbers = [c for c in cues if needs_number_words(c.text)]
        work = sorted({c.idx for c in overflow} | {c.idx for c in numbers})
        by_id = {c.idx: c for c in cues}
        say(f"  {len(cues)} cues, {sum(len(c.text) for c in cues)} chars, "
            f"{len(overflow)} over budget ({100 * len(overflow) // max(1, len(cues))}%), "
            f"{len(numbers)} with digits to spell out")

        if work and not api_key:
            say("  !! OPENROUTER_API_KEY not set – writing script without condensation")
            failed.append(f"{base}: OPENROUTER_API_KEY not set")
            for c in overflow:
                c.over_budget = True
        elif work:
            say(f"  rewriting {len(work)} cues with {cfg['script']['openrouter_model']}"
                f" (effort {cfg['script'].get('reasoning_effort', 'medium')})")
            out = condense_episode(cues, set(work), cfg, api_key)
            spent += out.usd
            if out.error:
                failed.append(f"{base}: {out.error}")
            for c in (by_id[i] for i in work):
                new = out.texts.get(c.idx)
                # Shorter is the point for a condensation, but a line whose digits were spelled out
                # is supposed to grow -- take it as long as the digits are actually gone.
                spelled = needs_number_words(c.text) and new and not needs_number_words(new)
                if new and (len(new) < len(c.text) or spelled):
                    c.text = new
                    c.condensed = True
                c.over_budget = len(c.text) > c.budget_chars
                c.has_digits = needs_number_words(c.text)
            say(f"  {out.response_format}, {out.returned}/{out.marked} cues back, "
                f"{out.accepted} accepted, {len(out.rejected)} rejected, {len(out.missing)} missing")
            if out.usd:
                say(f"  openrouter billed ${out.usd:.4f} for this episode "
                    f"({len(out.calls)} call(s), usage.cost)")
            say(f"  -> {write_spend(cfg, base, out).name}")

        still_over = sum(1 for c in cues if c.over_budget)
        say(f"  {sum(1 for c in cues if c.condensed)} condensed, "
            f"{still_over} still over budget (will be sped up)")

        payload = {
            "base": base,
            "source_srt": str(srt),
            "chars_per_second": cps,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cues": [
                {
                    "id": c.idx,
                    "start_ms": c.start_ms,
                    "end_ms": c.end_ms,
                    "window_ms": c.window_ms,
                    "budget_chars": c.budget_chars,
                    "condensed": c.condensed,
                    "over_budget": c.over_budget,
                    "text": c.text,
                    "text_original": clean_text(c.raw, cfg),
                }
                for c in cues
            ],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        say(f"  -> {out_path.name}   (open it, fix anything that reads badly, then run `voice`)")

    if spent:
        say(f"\ncondensation cost this run: ${spent:.4f}")

    # A key that is expired, wrong or missing leaves the script uncondensed but perfectly valid, so
    # nothing downstream can tell that the lines were never shortened. Exiting non-zero is what
    # stops `all` from carrying the raw subtitles into a stage that bills per character.
    if failed:
        raise SystemExit(
            "\ncondensation did not run for:\n  " + "\n  ".join(failed)
            + "\nThe scripts were written with the original subtitle text. Fix the key or the "
              "model and rerun `script --force`; running `voice` on them now would pay a TTS "
              "backend to read the full uncondensed episode."
        )
