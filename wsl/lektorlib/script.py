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

import hashlib
import json
import math
import os
import random
import threading
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import say
from .config import Config, load_cps
from .speech import (describe_delays, describe_leads, lead_counts, load_events, load_onsets,
                     plan_delays)
from .srt import (clean_text, drop_repeated_cards, load_cues, parse_srt,
                  should_skip)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# --- OpenRouter throttle -----------------------------------------------------------------------
# One draw is one call, so the pipeline is already sequential; this exists because everything ELSE
# is not. Benchmarks fanned six models out at once and two runs of the same episode were briefly
# alive together, and from the provider's side that is one account hammering it. The gate makes the
# limit a property of the process rather than of whoever remembered to be careful.
_OR_GATE = threading.Semaphore(3)
_OR_LAST = [0.0]
_OR_LOCK = threading.Lock()


def _throttle(gap_ms: float) -> None:
    """Keep at least gap_ms between the starts of two OpenRouter calls, process-wide."""
    if gap_ms <= 0:
        return
    with _OR_LOCK:
        wait = (_OR_LAST[0] + gap_ms / 1000.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _OR_LAST[0] = time.monotonic()


CONDENSE_SYSTEM = """Jesteś doświadczonym autorem list dialogowych do polskiej wersji lektorskiej.

Dostajesz PEŁNĄ listę dialogową jednego odcinka serialu, w kolejności chronologicznej.
Większość kwestii służy wyłącznie jako kontekst – masz je przeczytać, żeby rozumieć scenę,
postacie i wątki, ale ICH NIE ZWRACASZ.

Zwracasz wyłącznie kwestie oznaczone znacznikiem >>> SKROC DO N ZNAKOW <<<.

CZYM JEST TEKST LEKTORSKI, A CZYM NIE JEST
Widz SŁYSZY ściszony oryginał. Głos lektora nie zastępuje aktorów – kładzie się na nich.
Wszystko, co widz i tak wyłapie z oryginalnej ścieżki albo zobaczy na ekranie, jest w tekście
lektorskim zbędne i tylko zabiera czas. Powtórne podawanie takich znaczeń to podstawowy błąd
tego rzemiosła, nie oszczędność.
Jeden płaski głos czyta wszystkie postacie i nie gra. Co w oryginale niosła intonacja,
w tekście lektorskim musi ponieść SŁOWO.
To nie są napisy. Nie ograniczasz się do tego, co widz zdąży przeczytać wzrokiem – ograniczasz
się do czasu, który postać naprawdę miała na ekranie.

SKALA SKRACANIA
Limit z >>> SKROC DO N ZNAKOW <<< jest twardy i jest JEDYNYM powodem, dla którego skracasz.
Tniesz dokładnie tyle, ile trzeba, żeby się w nim zmieścić – ani słowa więcej. Kwestia, która
mieści się w limicie po wyrzuceniu samego wołacza, ma stracić wołacz i nic poza tym.
NIE MASZ NORMY SKRÓTU. Nie ma progu procentowego, który trzeba osiągnąć. Wersja krótsza od
wejścia o dwa znaki jest poprawna, jeśli te dwa znaki wystarczyły.
Jeżeli kwestia mieści się w limicie bez żadnej zmiany, zwracasz ją DOSŁOWNIE tak, jak ją
dostałeś. Przepisanie jej "lepiej" jest błędem: kosztuje, psuje spójność z resztą odcinka
i niczego nie załatwia.

KAŻDA ZWRÓCONA KWESTIA STOI SAMA
Widz słyszy jedną kwestię naraz, bez dostępu do sąsiednich kart. Każda zwrócona kwestia musi
więc być skończoną, wymawialną całością – zdaniem albo równoważnikiem, który da się przeczytać
i zrozumieć bez tego, co stoi obok.
NIGDY nie kończysz kwestii na przymiotniku, liczebniku, przyimku ani zaimku, który czeka na
swój rzeczownik. "rodziny przestępczej Środkowego" to nie jest skrót – to urwany ogon, po
którym lektor milknie w połowie myśli. Jeśli określenie nie mieści się w całości, wyrzucasz je
RAZEM z tym, co określa, i zostawiasz krótszą, ale zamkniętą wersję.
Jeżeli kwestia na wejściu sama jest urwanym fragmentem zdania z sąsiedniej karty, twoim
zadaniem jest zwrócić z niej to, co się broni samodzielnie – nie przepisać urwania.

CO WYRZUCASZ, W TEJ KOLEJNOŚCI
1. Wołacze i zwroty adresatywne, gdy są tylko wołaczem: "Ted, chodź tu" -> "Chodź tu".
   Imię rzucone do kogoś, kto jest w kadrze, widz i tak słyszy w oryginale.
2. Formuły sytuacyjne: powitania, pożegnania, "proszę", "dziękuję", przedstawianie się
   przez telefon.
3. Wypełniacze i podtrzymywanie kontaktu: "no wiesz", "słuchaj", "prawda?", "serio?",
   "okej", "aha", "hm", "yyy".
4. Powtórzenia – wewnątrz jednej kwestii i między rozmówcami. Jeśli drugi rozmówca powtarza
   to, co dopiero padło, zostaje samo potwierdzenie: "Wiem".
5. Zdania urwane i falstarty. Urwane zdanie zwijasz do tego, co miało znaczyć, albo wywalasz.
6. Zwroty pieszczotliwe i poufałe: "kochanie", "skarbie", "stary".
7. Łączniki na początku kwestii: "bo", "no więc", "a poza tym", "tak w ogóle".
8. Przymiotniki ozdobne, dopowiedzenia, drugorzędne szczegóły.
9. Wszystko, co widz WIDZI na ekranie albo co wynika wprost z sytuacji.
Jeśli po tym zostaje samo jądro kwestii – to jest poprawny wynik.

CZEGO NIE WOLNO WYRZUCIĆ
- Informacji fabularnej: kto, co zrobił, komu, kiedy, z jakim skutkiem. I przeczenia.
- Liczb – zasady niżej.
- Imienia i nazwy własnej, kiedy są INFORMACJĄ, a nie wołaczem: gdy przedstawiają nową postać,
  gdy mowa o kimś spoza kadru, gdy bez nich nie wiadomo, o kogo albo o co chodzi.
- Rzeczy powiedzianej pierwszy raz, choćby wyglądała na drobiazg – widz nie ma jej skąd wziąć.

RODZAJ GRAMATYCZNY: NIE DOPOWIADASZ TEGO, CZEGO NIE MOŻESZ WIEDZIEĆ
Masz sam tekst, bez obrazu. Nie wiesz, jakiej płci jest rozmówca ani osoba, o której mowa.
Polszczyzna pozwala mówić bezrodzajowo i oryginał bardzo często właśnie tak mówi.
NIGDY nie zamieniasz formy bezrodzajowej na rodzajową. Jeśli wejście nie rozstrzyga płci,
wyjście też nie może jej rozstrzygać.
- Bezrodzajowe, więc bezpieczne: czas teraźniejszy ("robisz"), przyszły ("zrobisz",
  "będziesz robić"), tryb rozkazujący ("zrób"), bezokolicznik, formy bezosobowe ("trzeba",
  "można", "należy", "zrobiono").
- Rodzajowe, więc niebezpieczne: czas przeszły ("zrobił" / "zrobiła"), tryb przypuszczający
  ("żebyś zrobił" / "żebyś zrobiła"), imiesłowy ("zmęczony" / "zmęczona"), przymiotniki
  i orzeczniki odnoszące się do rozmówcy ("jesteś pewien" / "jesteś pewna").
- Błąd, dla którego ta reguła powstała: "żeby to była ostatnia rzecz, jaką zrobisz" nie
  wskazuje płci; skrót "byś tego nie przeżył" wskazuje mężczyznę. Kwestia leciała do kobiety,
  więc wersja lektorska skłamała - a z samego tekstu nikt tego nie wyłapie.
- Kiedy krótszy wariant wymagałby rodzaju, wybierasz dłuższy bezrodzajowy albo tniesz w innym
  miejscu kwestii. Limit znaków nie jest usprawiedliwieniem dla zgadywania płci.
Jeśli wejście JEST rodzajowe, zostawiasz dokładnie ten sam rodzaj. Nie odwracasz go, nie
"poprawiasz" i nie ujednolicasz. Tak samo nie zmieniasz osoby ani liczby: "wy" nie staje się
"ty", a "pan" nie staje się "pani".

KTO DO KOGO MÓWI
Największa wada wersji lektorskiej jest taka, że widz gubi, kto mówi. Nie pogarszaj tego.
Zaimka używasz w obrębie jednej wypowiedzi. Po zmianie mówiącego wolisz rzeczownik albo imię
niż "on", "ona", "tamten" – jeden płaski głos nie odróżni ich za ciebie.
Nie dopisujesz didaskaliów w rodzaju "powiedział", "zapytał", "odparł".

KWESTIE NAKŁADAJĄCE SIĘ
Jeden lektor nie przeczyta dwóch osób naraz. Gdy dwie kwestie się zazębiają, ratujesz tę, która
niesie fabułę, a drugą ścinasz do kilku słów albo zostawiasz oryginałowi. Nie produkujesz dwóch
pełnych zdań, których nie da się wypowiedzieć jednocześnie.

IRONIA, NACISK, EMOCJA
Lektor czyta bez emocji, jednym tonem. Sarkazm, groźba, niedowierzanie, nacisk położony na jedno
słowo – jeśli w oryginale niósł to ton głosu, w twojej kwestii musi to ponieść dobór słowa,
partykuła ("akurat", "niby", "przecież") albo szyk zdania. Inaczej scena wyjdzie płasko.
Nie załatwiasz tego wykrzyknikami ani wielkimi literami.

REJESTR I FORMY ZWRACANIA SIĘ
Angielskie "you" trzeba w polskim rozstrzygnąć: "ty" albo "pan"/"pani". Wybierasz na podstawie
relacji między tą konkretną parą postaci i trzymasz się tego konsekwentnie przez cały odcinek.
Nigdy nie mieszasz obu form w jednej kwestii.
Wulgaryzmy zostawiasz, ale o stopień słabsze niż dosłowny odpowiednik: angielskie "damn" jest
łagodniejsze niż polska "cholera", a "fuck" to nie automatycznie "kurwa". Płaski głos lektora
wzmacnia każde przekleństwo mocniej niż krzyk aktora. Nie mnożysz ich – jedno na kwestię
wystarczy, nawet gdy w oryginale padły trzy. Nie kasujesz ich całkiem tam, gdzie niosą emocję.

NAPISY NA EKRANIE, PIOSENKI
Tekst widoczny na ekranie oddajesz tylko wtedy, gdy fabuła bez niego nie działa i widz nie
odczyta go sam. Szyldy, plakaty, tablice rejestracyjne, napisy w tle – zostawiasz w spokoju.
Tekstów piosenek nie przekładasz, chyba że niosą fabułę.

JĘZYK
- To ma być PRZECZYTANE NA GŁOS. Zdanie musi być poprawne składniowo i wymawialne za pierwszym
  podejściem: pełna fleksja, pełne słowa, polskie znaki diakrytyczne. Nigdy "remisow" zamiast
  "remisów".
- ŻADNYCH SKRÓTÓW. "hisz." zamiast "hiszpański", "ok." zamiast "około", "itd.", "np.", "tzn." –
  każdy z nich jest błędem, bo lektor tego nie wymówi. Skracasz WYRZUCAJĄC SŁOWA, nigdy
  okrawając je do kropki.
- Bez didaskaliów, nawiasów i cudzysłowów.
- SYMBOLE CZYTASZ ZGODNIE Z ROLĄ, JAKĄ PEŁNIĄ, nie z nazwą znaku. "@" przed nazwą konta w
  serwisie społecznościowym po prostu ZNIKA – "@BrindaBarot" to "Brinda Barot", nigdy "małpa
  BrindaBarot". Tak samo "#hasztag" to samo hasło. Nazwę znaku wymawiasz wyłącznie tam, gdzie
  bohater naprawdę mówi o samym znaku.
- Nie sil się ani na literackość, ani na sztuczną potoczność. Zwykła, prosta polszczyzna.

LICZBY
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
  – jeśli padły trzy liczby, w wyniku muszą być trzy. Miejsce znajdujesz wyrzucając inne słowa.
  Tak samo zostaje to, co liczbę określa: dzień bez miesiąca albo wynik bez tego, czego dotyczy,
  jest bezużyteczny.

NAZWY WŁASNE
- NIE PRZEPISUJ NAZWISK ANI IMION. Zostawiaj je dokładnie w takiej formie, w jakiej dostałeś
  (poza odmianą, której wymaga zdanie). Angielskie nazwiska są czytane poprawnie i każda próba
  zapisania ich "fonetycznie" tylko je psuje. Jeśli w kwestii stoi już zapis fonetyczny, na
  przykład "Rohas", ZOSTAW GO – jest tam celowo i nie wolno go cofać do "Rojas".
- Nie spolszczasz imion. Widz ma je rozpoznać w oryginalnej ścieżce, którą słyszy pod spodem.
- JEDNOSTKI IMPERIALNE przelicz na metryczne, jeśli jakaś przejdzie przez filtr w kodzie: mile na
  kilometry, stopy i cale na metry lub centymetry, funty na kilogramy, Fahrenheita na zwykłe stopnie.
  Zaokrąglaj do wartości, którą człowiek wypowie - "sto osiemdziesiąt centymetrów", nie "sto
  osiemdziesiąt dwa i dziewięć dziesiątych". Przy temperaturze NIE dopisuj skali: po polsku
  "dwadzieścia stopni" i tak znaczy Celsjusza.
- WALUT NIE PRZELICZAJ. Dolary zostają dolarami, funty funtami - kurs się zmienia, a lektor podający
  starą kwotę w dzisiejszych złotówkach kłamie. Zmieniasz wyłącznie szyk: "$10" to "dziesięć
  dolarów", nigdy "dolar dziesięć".
- SKALA DUŻYCH LICZB: angielski "billion" to polski MILIARD, a "trillion" to BILION. Jeśli z kontekstu
  widać, że tłumacz przepisał angielskie słowo, popraw je - polski "bilion" jest tysiąc razy większy
  niż angielski "billion".
- Nazwy własne i skrótowce z liczbą zapisz tak, jak się je WYMAWIA, a nie litera po literze:
  "PS5" -> "PlayStation pięć", "F1" -> "Formuła jeden", "A4" -> "A cztery". Skrótowiec, który
  lektor przeczytałby jako zwykłe słowo, rozpisz na głoski albo na pełną nazwę.
- Realia obce, których nie da się wyjaśnić w limicie, oddajesz nazwą ogólną zamiast dosłownie:
  lepiej "odznaczenie" niż niezrozumiała kalka. Nie dopisujesz objaśnień – nie ma na nie czasu.

KOLEJNOŚĆ WAŻNOŚCI, GDY TRZEBA WYBIERAĆ
(1) zmieścić się w limicie, (2) pełne słowa bez skrótów, (3) kwestia zamknięta, nieurwana,
(4) informacja fabularna i liczby, (5) jak najwięcej reszty sensu, (6) jak najmniejsza zmiana
wobec wejścia. Krótka, uboższa, ale poprawna i zrozumiała kwestia jest lepsza
niż bogata, która się nie mieści.

Odpowiadasz wyłącznie obiektem JSON: {"cues":[{"id":<int>,"text":"<skrócona kwestia>"}]}"""


HAS_DIGIT = re.compile(r"\d")


def digit_share(text: str) -> float:
    """How much of a line is sentences carrying digits, 0.0 to 1.0.

    A merged utterance may be one card with a number and three without. The number budget bonus is
    for spelling a number out, so it should scale with how much of the line that actually is.
    """
    if not HAS_DIGIT.search(text):
        return 0.0
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if not parts:
        return 1.0
    with_digits = sum(len(p) for p in parts if HAS_DIGIT.search(p))
    return with_digits / max(1, sum(len(p) for p in parts))


def needs_number_words(text: str) -> bool:
    """A digit anywhere means the line cannot be read aloud as written."""
    return bool(HAS_DIGIT.search(text))


# The per-cue instructions, as constants rather than inline literals: they are part of the prompt
# the model is given, so they are part of what the reuse cache is keyed on, and a key cannot be
# computed from a string buried in a loop.
MARK_SHORTEN = ">>> SKROC DO {n} ZNAKOW <<<"
MARK_NUMBERS = ">>> LICZBY SLOWAMI <<<"


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
            marks.append(MARK_SHORTEN.format(n=c.budget_chars))
        if needs_number_words(c.text):
            marks.append(MARK_NUMBERS)
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
    # Which model was actually billed. Not decorative: the run record used to attribute every call
    # to the PRIMARY model, so an escalation was invisible in it. On one episode that hid the
    # entire story - the account was charged $0.5926, of which the escalation to gpt-5.6-terra was
    # $0.4668 across four calls (79%) while the five draws from the cheap model came to $0.1258.
    # The only place that split could be read was the provider's own dashboard.
    model: str = ""
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "model": self.model,
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
    # A re-roll has to be a different DRAW, and without this it is not one. Every retry sent a
    # byte-identical request - same model, same messages, same temperature, same effort - and got
    # a byte-identical reply back: measured on one episode, three consecutive re-rolls each scored
    # 240/413, to the cue. Four draws were paid for and one was received. Varying the seed keeps
    # the temperature that was tuned for counting characters (0.3, measured at 206/210 against
    # 92/210 at the default) and changes only the sampling path.
    if s.get("seed") is not None:
        body["seed"] = int(s["seed"])
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
            with _OR_GATE:
                _throttle(float(s.get("request_gap_ms", 400)))
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
            # What the provider says it served, falling back to what we asked for.
            model=str(payload.get("model") or s["openrouter_model"]),
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
    # Ids the model returned that no cue claims. Before merging every id in 1..N was a live cue, so
    # this could only be a hallucination; now that merging folds ids away it can also mean the model
    # answered the pre-merge numbering, which would otherwise read as a clean run with everything
    # missing.
    extra: list[int] = field(default_factory=list)
    error: str = ""


def budget_for(window_ms: int, cps: float, t: dict, spells_numbers: float = 0.0) -> int:
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
    if spells_numbers > 0.0:
        # Spelling out inflates the line badly - "14" is two characters, "czternascie" is eleven -
        # and against an ordinary budget the model buys the room back by dropping a number, which
        # is the one thing it must not do. Measured on S02E04: "4 wygrane, 4 przegrane, 14 remisow"
        # came back without the remisy at all.
        # Scaled by how much of the line actually carries digits. A merged utterance can be one
        # card with a number and three without; granting the full multiplier to all of it inflated
        # the budget by up to 30 characters and put the reading 873 ms behind the picture.
        slack *= 1.0 + (float(t.get("number_slack", 1.0)) - 1.0) * min(1.0, spells_numbers)
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
    by_id = {c.idx: c for c in cues}
    # A cue is worth re-rolling for if it has something to achieve: a length to get under, or
    # digits to spell out. Scoring only the over-long ones was fine while every send was the whole
    # episode, but with the reuse cache in front of this a send is routinely a handful of cues -
    # and a send of nothing but number cues would have had an empty scorecard, one draw, and no
    # re-roll at all, on exactly the lines the lektor cannot read if the draw is bad.
    budgets = {c.idx: c.budget_chars for c in cues
               if c.idx in target_ids
               and (len(c.text) > c.budget_chars or needs_number_words(c.text))}
    if not budgets:
        return _condense_once(cues, target_ids, cfg, api_key)

    def fits(o: CondenseOutcome) -> int:
        n = 0
        for i, t in o.texts.items():
            if i not in budgets:
                continue
            # Spelling a number out makes a line longer by design, so a digit cue is judged on
            # whether the digits are gone, not on its length.
            if needs_number_words(by_id[i].text):
                n += 0 if needs_number_words(t) else 1
            else:
                n += 1 if len(t) <= budgets[i] else 0
        return n

    best: CondenseOutcome | None = None
    spent: list[CallStats] = []
    # Rounded UP, or a send of one cue would need zero of them to be good and could never be
    # re-rolled. That floor was invisible while every send was 200 cues; the reuse cache makes
    # small sends the normal case.
    need = math.ceil(len(budgets) * ratio)
    # And the ladder is shortened for a small send, because the prompt is the whole episode
    # however few cues are marked: a draw for two cues costs nearly what a draw for two hundred
    # costs in input tokens, while ceil() has just made its bar effectively 100 %. Four re-rolls
    # plus an escalation over two lines is throwing money at a lottery it cannot win.
    rerolls = min(rerolls, max(1, len(budgets)))
    attempts = 0
    previous = None
    for attempt in range(rerolls + 1):
        # A distinct seed per attempt, so a re-roll is a re-roll. Derived from the attempt index
        # alone: the draw has to differ from the LAST one, not be unpredictable.
        draw = cfg if attempt == 0 else _ScriptOverride(
            cfg, dict(cfg.raw["script"], seed=1000 + attempt))
        out = _condense_once(cues, target_ids, cfg if attempt == 0 else draw, api_key)
        attempts += 1
        spent.extend(out.calls)
        this = fits(out)
        if best is None or this > fits(best):
            best = out
        got = fits(best)
        if got >= need or out.error or attempt == rerolls:
            break
        # If the endpoint ignores the seed, the ladder is buying nothing and the escalation below
        # is the only thing left that can help. Rolling three more times to find that out costs a
        # whole-episode prompt each time.
        if previous is not None and this == previous:
            say(f"  re-roll {attempt + 1}/{rerolls}: {this}/{len(budgets)} again, identical to the "
                f"previous draw - the seed is not changing the result, so escalating now")
            break
        previous = this
        say(f"  re-roll {attempt + 1}/{rerolls}: {got}/{len(budgets)} inside budget, "
            f"below the {need} needed")

    # Escalate rather than settle: a model that has drawn badly five times in a row will not draw
    # well on the sixth, and a steadier one earns its higher price exactly here, where the cheap
    # one has already had every chance. The best draw across both models wins, so this can only
    # improve the result.
    fallback = str(s.get("fallback_model") or "")
    if best is not None and fits(best) < need and fallback and fallback != s.get("openrouter_model"):
        say(f"  {fits(best)}/{len(budgets)} after {attempts} draw(s) - escalating to {fallback}")
        alt = _ScriptOverride(cfg, dict(cfg.raw["script"], openrouter_model=fallback))
        for _ in range(int(s.get("fallback_rerolls", 1)) + 1):
            out = _condense_once(cues, target_ids, alt, api_key)
            attempts += 1
            spent.extend(out.calls)
            if fits(out) > fits(best):
                best = out
            if fits(best) >= need or out.error:
                break

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


class _ScriptOverride:
    """A cfg wearing a different script.* table, so a fallback draw needs no plumbing through _ask."""

    def __init__(self, cfg, script_table: dict):
        self._cfg = cfg
        self.raw = dict(cfg.raw)
        self.raw["script"] = script_table

    def __getitem__(self, key):
        return self.raw[key]

    def __getattr__(self, name):
        return getattr(self._cfg, name)


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
    out.extra = final.extra
    out.usd = sum(c.cost for c in out.calls)
    return out


def _ask(s: dict, api_key: str, messages: list[dict], use_schema: bool, out: CondenseOutcome,
         purpose: str, schema: dict = CUES_SCHEMA) -> tuple[dict[int, str], bool, str]:
    """Call with json_schema, degrade to json_object once if the provider refuses it.

    `schema` is the one thing the translate stage changes: its reply has the same
    {"cues": [{"id", "text"}]} shape, so the parsing, the retries and the degradation are this
    function's and not a second copy of it.
    """
    if use_schema:
        try:
            got, stats = call_openrouter(s, api_key, messages, schema, purpose)
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
    return append_spend(cfg, base, {
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
    })


def append_spend(cfg: Config, base: str, run: dict) -> Path:
    """Add one run to `<base>.spend.json`, whichever stage paid for it.

    One file per episode for every OpenRouter bill, because the question it answers is "what has
    this episode cost", and a second file per stage is a second total somebody forgets to add.
    Runs from the translate stage carry `"stage": "translate"`; a run with no `stage` is a
    condensation run, which is every run this file held before translation existed. Merged, never
    rewritten: the history already on disk is read back and this run appended to it.
    """
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
    record["runs"].append(run)
    record["usd_total"] = round(sum(r.get("usd", 0.0) for r in record["runs"]), 6)
    # Written aside and renamed: a kill or a full disk part-way through a plain write leaves a
    # truncated file, and the truncated file is the whole billing history.
    tmp = p.with_suffix(f".{os.getpid():x}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def read_spend(cfg: Config, base: str, stage: str | None = None) -> float:
    """What this episode has cost on OpenRouter so far, or 0 if nothing has run.

    With no `stage` it is the whole bill - condensation and translation together, the number
    `usd_total` has always held. `stage="script"` or `"translate"` sums only that stage's runs, so
    a chain can say what each of the two cost this time without one swallowing the other.
    """
    p = spend_path(cfg, base)
    if not p.exists():
        return 0.0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if stage is None:
            return float(data.get("usd_total") or 0.0)
        return float(sum(float(r.get("usd") or 0.0) for r in data.get("runs") or []
                         if isinstance(r, dict) and (r.get("stage") or "script") == stage))
    except (ValueError, TypeError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Condensed-text reuse cache
# ---------------------------------------------------------------------------
#
# Why this exists at all: every change upstream of this stage - a merge-gate tweak, a new cleaning
# rule, a respelling - forces `script --force`, which today draws a NEW whole-episode condensation
# and rewrites every line, including the hundreds the change never touched. Rewritten text misses
# the TTS cache, and the TTS bill is 14-65x the LLM bill for the same change. Measured over the
# season: re-running it costs $0.71 without this file and $0.09 with it.
#
# The key is the whole contract under which a line was produced. Get it wrong in the loose
# direction and old text ships under a new prompt, silently, which is the one failure this cache
# must never have.

CACHE_VERSION = 1


def prompt_fingerprint() -> str:
    """Hash of everything the model is actually told, so an edit to any of it misses.

    Automatic rather than a hand-bumped version number: the one thing certain about a manual
    version field is that someone edits the prompt and forgets it, and the result is not a stale
    cache - it is an episode where half the lines were written by a prompt that no longer exists.

    "The prompt" is not just the system message. The per-cue instruction is carried by the markers
    `whole_prompt` writes inline, and the reply is shaped by the JSON schema; changing either one
    changes what comes back exactly as surely as rewriting the system message does. All three are
    hashed. `retry_note` is deliberately not: it only ever names cues that already missed, and its
    wording cannot change a line that came back right the first time.
    """
    blob = "\n".join([
        CONDENSE_SYSTEM,
        MARK_SHORTEN.format(n=0),
        MARK_NUMBERS,
        json.dumps(CUES_SCHEMA, sort_keys=True, ensure_ascii=False),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def cache_key(text: str, budget_chars: int, s: dict) -> str:
    """Everything that decides what a condensed line comes back as.

    In: the cleaned source text, the budget it had to hit, the model, how hard it was told to
    think, and the prompt. Out, deliberately: temperature and the re-roll settings (they change
    the draw, not the contract - `--redraw` is how you ask for a different draw), the fallback
    model (it only ever runs when the primary has already failed, and its output is judged against
    the same budget), and the cue id (ids move when merging changes, the text does not).
    """
    blob = json.dumps(
        [CACHE_VERSION, text, int(budget_chars), str(s.get("openrouter_model", "")),
         str(s.get("reasoning_effort", "medium")), prompt_fingerprint()],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def condensed_path(cfg: Config, base: str) -> Path:
    return cfg.work_dir / f"{base}.condensed.json"


def load_condensed(cfg: Config, base: str) -> dict[str, dict]:
    """Previous condensations for this episode, or an empty book if there are none.

    An unreadable file is kept aside rather than overwritten: it is worth real money, and the run
    can continue without it at the price of a fresh draw.
    """
    p = condensed_path(cfg, base)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries")
        if not isinstance(entries, dict):
            raise ValueError("no 'entries' object")
    except (OSError, ValueError) as exc:
        keep = p.with_suffix(f".corrupt-{time.strftime('%Y%m%dT%H%M%S')}.json")
        try:
            p.replace(keep)
            say(f"  !! {p.name} would not parse ({exc}) – kept as {keep.name}, drawing fresh")
        except OSError:
            say(f"  !! {p.name} would not parse ({exc}) – drawing fresh")
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict) and v.get("text")}


def write_condensed(cfg: Config, base: str, entries: dict[str, dict]) -> Path:
    """Save the book. Written aside and renamed, like the spend record.

    Whatever is on disk is merged back in first. Entries are content-addressed, so the union is
    unambiguous, and it means a second run of the same episode alongside this one loses nothing
    it paid for. There is deliberately no lock file: a duplicated draw costs about a cent, and a
    stale lock in a stage that gets interrupted costs a wedged pipeline.
    """
    p = condensed_path(cfg, base)
    entries = {**load_condensed(cfg, base), **entries}
    payload = {
        "base": base,
        "version": CACHE_VERSION,
        "prompt": prompt_fingerprint(),
        "model": cfg["script"]["openrouter_model"],
        "reasoning_effort": cfg["script"].get("reasoning_effort", "medium"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "_note": ("Condensed text, keyed on (cleaned source text, budget, model, effort, prompt "
                  "hash). A hit is reused verbatim instead of being redrawn. Delete this file, or "
                  "run `script --redraw`, to pay for a clean whole-episode draw."),
        "entries": entries,
    }
    tmp = p.with_suffix(f".{os.getpid():x}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def split_by_cache(cues, work: list[int], book: dict[str, dict], s: dict
                   ) -> tuple[dict[int, str], list[int], int]:
    """Which of the cues we would send are already answered, and which still have to be drawn.

    Only cues that were actually condensed are ever in the book, so a cue the model failed on last
    time gets another chance rather than a cached failure - a negative cache here would make one
    bad draw permanent.

    Returns (hits, misses, poisoned). A poisoned entry is one whose stored text still carries
    digits for a line that was sent to have them spelled out: the module's own rule is never to
    trust the reply, and a reply read back off disk a month later has earned no more trust than
    it had on the day.
    """
    by_id = {c.idx: c for c in cues}
    hits: dict[int, str] = {}
    misses: list[int] = []
    poisoned = 0
    for idx in work:
        c = by_id.get(idx)
        if c is None:
            continue
        entry = book.get(cache_key(c.text, c.budget_chars, s))
        text = entry.get("text") if entry else None
        if not isinstance(text, str) or not text.strip():
            misses.append(idx)
        elif needs_number_words(c.text) and needs_number_words(text):
            poisoned += 1
            misses.append(idx)
        else:
            hits[idx] = text
    return hits, misses, poisoned


def take_condensed(cue, new: str | None, reused: bool = False) -> bool:
    """Replace a cue's text with a condensation, if it really is one.

    The one acceptance rule in the pipeline, used for a line that just came back from the model
    and for a line read out of the book, so a cached line can never enter under laxer terms than
    the one that produced it. Shorter is the point - but a line whose digits were spelled out is
    supposed to grow, and is taken as long as the digits are actually gone.
    """
    if not new:
        return False
    spelled = needs_number_words(cue.text) and not needs_number_words(new)
    if len(new) < len(cue.text) or spelled:
        cue.text = new
        cue.condensed = True
        cue.reused = reused
        return True
    return False


def hand_edited(cues, prev: dict | None, book: dict[str, dict], s: dict) -> dict[int, str]:
    """Lines a human rewrote in `<base>.lektor.json`, which nothing here may overwrite.

    The script stage ends by telling you to open the file and fix anything that reads badly, and
    then every rerun quietly threw those fixes away. Two cases are unambiguous and both are
    honoured here:

      * the previous script says the line was never condensed, yet its text is not its source -
        only a human can have done that;
      * the line was condensed, and the book still holds what the model produced, and the file
        says something else.

    Everything else is left alone: a condensed line with no entry in the book cannot be told from
    a redraw, and freezing it on a guess would silently stop the model ever seeing that line
    again.

    A previous line is matched to a current one by cue id with the source text agreeing, or - when
    the ids have moved, which they do whenever the subtitles are re-extracted or the merge gate
    changes - by a source text that occurs exactly once on both sides. Never by text alone: an
    episode carries a dozen cards reading "Tak." or "Dzięki.", and one edit of one of them would
    otherwise be stamped onto all the rest.
    """
    if not prev:
        return {}
    keep: dict[int, str] = {}
    # Matched on the cue's own id AND on the source text having survived unchanged. Not on the
    # text alone: a real episode carries ten or a dozen cards reading "Tak." or "Dzięki.", and
    # matching by text would stamp one edit of one of them onto all the others - which is exactly
    # the sort of short line somebody edits for how it reads.
    by_id = {p.get("id"): p for p in prev.get("cues", []) if isinstance(p.get("id"), int)}
    prev_counts: dict[str, int] = {}
    for p in prev.get("cues", []):
        src = p.get("text_original")
        if isinstance(src, str) and src:
            prev_counts[src] = prev_counts.get(src, 0) + 1
    here_counts: dict[str, int] = {}
    for c in cues:
        here_counts[c.text] = here_counts.get(c.text, 0) + 1
    # Ids move when the subtitle file is re-extracted or when the merge gate changes, so a source
    # text that appears exactly once in both documents is also an identity - and a safe one.
    unique = {p.get("text_original"): p for p in prev.get("cues", [])
              if prev_counts.get(p.get("text_original")) == 1}

    for c in cues:
        p = by_id.get(c.idx)
        if p is None or p.get("text_original") != c.text:
            p = unique.get(c.text) if here_counts.get(c.text) == 1 else None
        if p is None or p.get("text_original") != c.text:
            continue
        was = p.get("text")
        if not isinstance(was, str) or not was or was == c.text:
            continue
        if not p.get("condensed"):
            keep[c.idx] = was
            continue
        entry = book.get(cache_key(c.text, c.budget_chars, s))
        if entry and isinstance(entry.get("text"), str) and entry["text"] != was:
            keep[c.idx] = was
    return keep


def load_previous_script(cfg: Config, base: str) -> dict | None:
    """The script from the last run, or None. Only ever read, and only for the lines it holds."""
    p = cfg.work_dir / f"{base}.lektor.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data.get("cues"), list) else None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def stage_script(cfg: Config, only: str | None, force: bool, redraw: bool = False,
                 forget_edits: bool = False) -> None:
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if redraw and not force:
        # "Pay for a clean draw, then skip the episode because its script already exists" cannot
        # mean anything.
        say("--redraw implies --force")
        force = True
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
        # The merge gate, when there is an artefact to gate with. Reported rather than assumed:
        # an episode whose speech.json was never built is a different document from one whose
        # gate ran, and the difference has to be visible in the log that produced the script.
        ev = load_events(cfg.work_dir, base, cfg)
        # Reported, never silent: dropping a card changes what gets spoken, and a subtitle file
        # that needs the guard is a fact about that release worth seeing in the log.
        raw = [c for c in parse_srt(srt) if not should_skip(c.raw, cfg)]
        for c in raw:
            c.text = clean_text(c.raw, cfg)
        _, repeated = drop_repeated_cards([c for c in raw if c.text], cfg)
        if repeated:
            say(f"  {len(repeated)} zdublowanych kart w napisach pominietych "
                f"(ten sam tekst na nachodzacych sie kartach): {repeated[:8]}"
                f"{' ...' if len(repeated) > 8 else ''}")
        cues = load_cues(srt, cfg, ev)
        if ev is not None:
            ungated = load_cues(srt, cfg)
            say(f"  merge gate on – {ev.describe()}; "
                f"{sum(1 for c in ungated if c.merged_from)} merges become "
                f"{sum(1 for c in cues if c.merged_from)}, {len(ungated)} cues become {len(cues)}")
        for c in cues:
            c.budget_chars = budget_for(c.window_ms, cps, t, digit_share(c.text))

        overflow = [c for c in cues if len(c.text) > c.budget_chars]
        # Digits cannot be read aloud, and a cue that already fits is never sent for shortening,
        # so number cues join the same call regardless of their length.
        numbers = [c for c in cues if needs_number_words(c.text)]
        work = sorted({c.idx for c in overflow} | {c.idx for c in numbers})
        by_id = {c.idx: c for c in cues}
        say(f"  {len(cues)} cues, {sum(len(c.text) for c in cues)} chars, "
            f"{len(overflow)} over budget ({100 * len(overflow) // max(1, len(cues))}%), "
            f"{len(numbers)} with digits to spell out")

        # The source text every cache key is computed from, snapshotted before anything rewrites
        # it. Both the lookup and the store use this, so a line saved under its own condensation
        # could never be found again.
        source = {c.idx: c.text for c in cues}
        s_tbl = cfg["script"]
        reuse = bool(s_tbl.get("reuse_previous", True)) and not redraw
        book = load_condensed(cfg, base) if reuse or redraw else {}

        # Lines a human rewrote in the script file are not the model's to redraw, whatever the
        # cache says, so they come out of the work list before anything else looks at it. This is
        # deliberately NOT conditioned on `reuse`: "draw the text again" and "throw away what I
        # typed" are two different requests, and --redraw only asks for the first.
        prev_script = load_previous_script(cfg, base)
        edits = {} if forget_edits else hand_edited(cues, prev_script, book, s_tbl)
        prev_by_id = {c.get("id"): c for c in (prev_script or {}).get("cues", [])}
        for idx, text in edits.items():
            by_id[idx].text = text
            by_id[idx].edited = True
            # Carry the previous provenance forward, and it is not bookkeeping: a kept line is
            # taken OUT of the work list, so nothing this run sets `condensed` and it would be
            # written back as False. The next run reads that False as "never condensed, yet its
            # text is not its source - only a human can have done that" and keeps it again. One
            # false positive then froze the line for good, and 172 of 755 cues on one episode had
            # ended up that way: 23 % of the episode invisible to every prompt fix, and no way out
            # short of deleting the script by hand. Preserving the flag leaves only the narrow
            # test - the book holds something else than the file - which is the one that means it.
            by_id[idx].condensed = bool(prev_by_id.get(idx, {}).get("condensed"))
        work = [i for i in work if i not in edits]

        hits, todo, poisoned = (split_by_cache(cues, work, book, s_tbl) if reuse
                                else ({}, list(work), 0))
        if edits:
            say(f"  {len(edits)} hand-edited line(s) kept as they are and never sent")
        if poisoned:
            say(f"  {poisoned} cached line(s) dropped for still carrying digits – redrawing them")
        if hits:
            say(f"  reusing {len(hits)} of {len(work)} condensed lines from "
                f"{condensed_path(cfg, base).name}; {len(todo)} left to draw")
        elif reuse and work and book:
            say(f"  nothing in {condensed_path(cfg, base).name} matches this input – drawing all "
                f"{len(todo)}")
        elif redraw and work:
            say(f"  --redraw: ignoring {condensed_path(cfg, base).name}, drawing all {len(todo)}")

        drawn: dict[int, str] = {}
        if todo and not api_key:
            say("  !! OPENROUTER_API_KEY not set – writing script without condensation")
            failed.append(f"{base}: OPENROUTER_API_KEY not set")
        elif todo:
            say(f"  rewriting {len(todo)} cues with {cfg['script']['openrouter_model']}"
                f" (effort {cfg['script'].get('reasoning_effort', 'medium')})")
            out = condense_episode(cues, set(todo), cfg, api_key)
            spent += out.usd
            drawn = out.texts
            if out.error:
                failed.append(f"{base}: {out.error}")
            say(f"  {out.response_format}, {out.returned}/{out.marked} cues back, "
                f"{out.accepted} accepted, {len(out.rejected)} rejected, {len(out.missing)} missing")
            if out.usd:
                say(f"  openrouter billed ${out.usd:.4f} for this episode "
                    f"({len(out.calls)} call(s), usage.cost)")
            say(f"  -> {write_spend(cfg, base, out).name}")

        # Applied only now, and never earlier: the prompt the model just saw carries every OTHER
        # cue as context, read from c.text. Putting a cached condensation into a cue before the
        # call would send the model a context it has never seen for a cold run of the same
        # episode, and make the prompt depend on how warm the cache happened to be.
        for idx, text in hits.items():
            take_condensed(by_id[idx], text, reused=True)
        for idx, text in drawn.items():
            if idx in by_id:
                take_condensed(by_id[idx], text)

        for c in cues:
            c.over_budget = len(c.text) > c.budget_chars
            c.has_digits = needs_number_words(c.text)

        # Delays are planned on the FINAL text: condensation shortens a line, a shorter line
        # leaves more room in its window, and the room is what decides how far a line may move.
        onsets = load_onsets(cfg.work_dir, base, cfg)
        if onsets is not None:
            moved = plan_delays(cues, onsets, t, cps)
            say(f"  onsets: {onsets.describe()}; {describe_delays(moved)}")
            say(f"    {describe_leads(lead_counts(cues, onsets))}")

        # Only lines that really were condensed go into the book, and only when the source they
        # were condensed FROM is the one this run cleaned. A cue the model failed on is left out
        # on purpose: caching a failure would make one bad draw permanent.
        if reuse or redraw:
            fresh = 0
            for c in cues:
                if not c.condensed or c.edited or c.idx not in source:
                    continue
                key = cache_key(source[c.idx], c.budget_chars, s_tbl)
                if key not in book:
                    fresh += 1
                book[key] = {
                    "text": c.text,
                    "id": c.idx,
                    "budget_chars": c.budget_chars,
                    "src_chars": len(source[c.idx]),
                    "src": source[c.idx],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
            if book:
                p = write_condensed(cfg, base, book)
                say(f"  -> {p.name} ({len(book)} lines, {fresh} new)")

        still_over = sum(1 for c in cues if c.over_budget)
        reused_n = sum(1 for c in cues if c.reused)
        edited_n = sum(1 for c in cues if c.edited)
        say(f"  {sum(1 for c in cues if c.condensed)} condensed"
            + (f" ({reused_n} reused, {sum(1 for c in cues if c.condensed and not c.reused)} "
               f"newly drawn)" if reused_n else "")
            + (f", {edited_n} kept from your own edits" if edited_n else "")
            + f", {still_over} still over budget (will be sped up)")

        payload = {
            "base": base,
            "source_srt": str(srt),
            # Which merge rules produced these cues. Two scripts of the same episode with
            # different values here are not comparable line for line, and the ElevenLabs cache
            # will miss on every line whose text the gate changed.
            "merge_gate": ev.mode if ev is not None else "off",
            "chars_per_second": cps,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cues": [
                {
                    "id": c.idx,
                    "start_ms": c.start_ms,
                    # How much later than the card this line is spoken, because the actor had not
                    # started yet. The voice stage adds it to start_ms; nothing else moves.
                    "delay_ms": c.delay_ms,
                    # Set by isolate_cards, not by the onset planner, so retime knows to leave it.
                    "card_delay": c.card_delay,
                    "end_ms": c.end_ms,
                    "window_ms": c.window_ms,
                    "budget_chars": c.budget_chars,
                    "condensed": c.condensed,
                    "reused": c.reused,
                    "edited": c.edited,
                    "over_budget": c.over_budget,
                    "text": c.text,
                    "text_original": c.text_original or clean_text(c.raw, cfg),
                    "merged_from": c.merged_from,
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
