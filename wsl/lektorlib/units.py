"""Imperial measurements rewritten as metric ones a Polish lektor can actually say.

The point is not to catch every conversion. Mined over 26 950 Polish subtitle lines from 44 films,
a plain unit-name regex scored 22 hits of which exactly TWO were measurements: "stopa" was a body
part 12 times out of 12, "funt" was pounds sterling 4 out of 4, "kamień" was a stone 3 out of 3, and
"cali" was the adjective "cały" - a naive converter would have read "Wszyscy cali i zdrowi" as
"Wszyscy 2,54 cm i zdrowi". The English side of the same library carried 37 imperial mentions in
142 722 lines, so the source material barely has any.

That inverts the design. The risk is not a missed conversion; it is breaking a correct line. So the
matcher is deliberately narrow: a unit only converts when a NUMERAL sits immediately in front of it,
and never inside a known idiom. On that corpus this rule keeps both true measurements and rejects
all twenty false positives.

Values are rounded the way a person speaks them rather than the way a calculator prints them - six
feet is "metr osiemdziesiąt", not "1,8288 metra". A source that carries decimals is treated as
deliberate precision: its step (0.1 ft, say) is converted too, and the result is rounded to that,
so nothing is invented and nothing is thrown away.
"""

from __future__ import annotations

import math
import re

# --- what a number can look like in front of a unit -----------------------------------------------

WORD_NUMERALS: dict[str, float] = {
    "pół": 0.5, "półtora": 1.5, "półtorej": 1.5,
    "jeden": 1, "jedna": 1, "jedną": 1, "jednej": 1, "dwa": 2, "dwie": 2, "dwóch": 2, "dwu": 2,
    "trzy": 3, "trzech": 3, "cztery": 4, "czterech": 4, "pięć": 5, "pięciu": 5, "sześć": 6,
    "sześciu": 6, "siedem": 7, "siedmiu": 7, "osiem": 8, "ośmiu": 8, "dziewięć": 9,
    "dziewięciu": 9, "dziesięć": 10, "dziesięciu": 10, "jedenaście": 11, "dwanaście": 12,
    "piętnaście": 15, "dwadzieścia": 20, "dwudziestu": 20, "trzydzieści": 30, "czterdzieści": 40,
    "pięćdziesiąt": 50, "pięćdziesięciu": 50, "sto": 100, "stu": 100, "dwieście": 200,
    "trzysta": 300, "pięćset": 500, "tysiąc": 1000, "tysiąca": 1000,
}

# Unit stems, longest first so "mila morska" is seen before "mila".
UNITS: list[tuple[str, str]] = [
    (r"mil(?:a|e|i|ę|ą|o|ach|om|ami)?", "mile"),
    (r"stop(?:a|y|ę|ą|o|ie|om|ami|ach)|stóp", "foot"),
    (r"cal(?:a|e|i|owy|ów|em|ach)?", "inch"),
    (r"jard(?:a|y|ów|em|ach)?", "yard"),
    (r"funt(?:a|y|ów|em|ach)?", "pound"),
    (r"uncj(?:a|e|i|ę|ą|ach)", "ounce"),
    (r"galon(?:a|y|ów|em|ach)?", "gallon"),
    (r"akr(?:a|y|ów|em|ach)?", "acre"),
    # "32 stopnie Fahrenheita" puts a noun between the number and the unit name.
    (r"stop\w*\s+Fahrenheit\w*|Fahrenheit\w*", "fahrenheit"),
]

# metres, kilograms, litres, square metres; temperature is handled separately
FACTORS: dict[str, tuple[float, str]] = {
    "mile": (1609.344, "m"),
    "foot": (0.3048, "m"),
    "inch": (0.0254, "m"),
    "yard": (0.9144, "m"),
    "pound": (0.45359237, "kg"),
    "ounce": (0.0283495, "kg"),
    "gallon": (3.785411784, "l"),
    "acre": (4046.8564224, "m2"),
}

# Phrases where a unit word is not a unit. Checked against the whole line, case-insensitively.
IDIOMS: tuple[str, ...] = (
    "od stóp do głów", "u stóp", "palców u stóp", "na stopach", "stopy w górę",
    "cal po calu", "mila morska", "mile morskie", "zielona mila", "ostatnia mila",
    "sześć stóp pod ziemią", "kosa na kamień", "epoki kamienia",
    "stopa procentowa", "stopa bezrobocia", "stopa zwrotu", "stopa życiowa",
)

_NUM = r"(?:-?\d+(?:[.,]\d+)?|" + "|".join(sorted(WORD_NUMERALS, key=len, reverse=True)) + r")"
_UNIT_ALT = "|".join(f"(?P<u{i}>{pat})" for i, (pat, _) in enumerate(UNITS))
# (?<![\w-]) rather than \b: there is no word boundary before a minus sign, so \b would have made
# every negative temperature unmatchable and "-40 stopni Fahrenheita" came out as "-5 stopni".
MEASURE_RE = re.compile(rf"(?<![\w-])(?P<num>{_NUM})\s+(?:{_UNIT_ALT})\b", re.IGNORECASE)

# --- currency ------------------------------------------------------------------------------

# Never converted at an exchange rate: the film said dollars, the rate moves, and a lektor
# quoting a 2021 sum in today's zloty would be inventing a fact. Only the WORD ORDER is fixed,
# because English writes the symbol first and Polish says the number first - "$10" is
# "dziesiec dolarow", never "dolar dziesiec".
CURRENCIES: dict[str, tuple[tuple[str, str, str, str], tuple[str, str, str, str] | None]] = {
    "$": (("dolar", "dolary", "dolarów", "dolara"), ("cent", "centy", "centów", "centa")),
    "£": (("funt", "funty", "funtów", "funta"), ("pens", "pensy", "pensów", "pensa")),
    "€": (("euro", "euro", "euro", "euro"), ("cent", "centy", "centów", "centa")),
    "zł": (("złoty", "złote", "złotych", "złotego"), ("grosz", "grosze", "groszy", "grosza")),
}
CURRENCY_RE = re.compile(
    r"(?P<sym>US\$|\$|£|€|zł)\s?(?P<num>\d(?:[\d.,   ]*\d)?)"
    r"|(?P<num2>\d(?:[\d.,   ]*\d)?)\s?(?P<sym2>zł)",
    re.IGNORECASE)


def _agree_words(value: float, forms: tuple[str, str, str, str]) -> str:
    one, few, many, frac = forms
    if abs(value - round(value)) > 1e-9:
        return frac
    n = abs(int(round(value)))
    if n == 1:
        return one
    last_two, last = n % 100, n % 10
    return few if 2 <= last <= 4 and not 12 <= last_two <= 14 else many


def spoken_currency(text: str) -> str:
    """Amounts written the English way, said the Polish way. Values are never touched."""
    def repl(m: re.Match) -> str:
        sym = (m.group("sym") or m.group("sym2") or "").replace("US", "")
        raw = m.group("num") or m.group("num2")
        major, minor = CURRENCIES[sym if sym in CURRENCIES else sym.lower()]
        body = raw.replace(" ", "").replace("\u00a0", "").rstrip(".,")
        cents = 0
        # Every separator is a thousands group except a trailing one with fewer than three digits,
        # which is cents: 1,000,000 is a million, 1.10 is a dollar ten.
        head, sep, tail = body.replace(".", ",").rpartition(",")
        if sep and len(tail) < 3:
            body, cents = head.replace(",", ""), int(tail.ljust(2, "0"))
        else:
            body = body.replace(",", "").replace(".", "")
        whole = int(body or 0)
        out = f"{whole} {_agree_words(whole, major)}"
        if cents and minor:
            out += f" {cents} {_agree_words(cents, minor)}"
        return out

    return CURRENCY_RE.sub(repl, text)

# The mantissas people actually say. Scaled by powers of ten these give 5, 10, 15, 20, 25, 50, 100,
# 110, 120, 150, 200 and so on - the numbers that come out of a mouth without being read off a dial.
NICE_MANTISSAS: tuple[float, ...] = (
    1, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 2, 2.2, 2.5, 2.8, 3, 3.2, 3.5, 4, 4.5, 5, 5.5, 6, 6.5,
    7, 7.5, 8, 8.5, 9, 9.5,
)

# How round the SOURCE was. A value divisible by 100 was already an approximation and its conversion
# may be approximated just as freely; a value of 7 was meant precisely.
# No 2 and no 3: six is not a round number, and treating it as one let 'six feet' stretch to two
# metres. Only the steps a person reaches for when approximating count.
SOURCE_STEPS: tuple[int, ...] = (1000, 500, 250, 100, 50, 25, 10, 5, 1)


def _parse_number(raw: str) -> tuple[float, int]:
    """Value and how many decimal places the source showed. Word numerals have none."""
    key = raw.lower()
    if key in WORD_NUMERALS:
        return float(WORD_NUMERALS[key]), 0
    txt = raw.replace(",", ".")
    dec = len(txt.split(".")[1]) if "." in txt else 0
    return float(txt), dec


def source_slack(value: float, cap: float) -> float:
    """How far the conversion may be stretched, decided by how round the source already was.

    "50 miles" carries an implicit "give or take"; "7 miles" does not. The roundness of the source
    is the only honest evidence of how precise the speaker meant to be, so it sets the licence -
    capped, because "100" divides by 100 and would otherwise permit anything.
    """
    if value <= 0 or abs(value - round(value)) > 1e-9:
        return 0.0
    n = int(round(value))
    step = next((s for s in SOURCE_STEPS if n % s == 0), 1)
    return min(cap, step / n)


def nice_values(magnitude: float) -> list[float]:
    """Every sayable number in the decades around this magnitude."""
    if magnitude <= 0:
        return []
    top = math.floor(math.log10(magnitude))
    out: list[float] = []
    for k in (top - 1, top, top + 1):
        out.extend(m * (10.0 ** k) for m in NICE_MANTISSAS)
    # Above a hundred every multiple of ten is as easy to say as any other - "dwiescie dziesiec"
    # costs nothing - so do not force such values onto a coarse mantissa grid.
    if magnitude >= 100:
        step = 10.0 if magnitude < 1000 else 100.0
        lo, hi = magnitude * 0.8, magnitude * 1.25
        out.extend(v for v in (step * i for i in range(1, int(hi / step) + 2)) if lo <= v <= hi)
    return out


def _snap(value: float, tolerance: float) -> float:
    """Nearest sayable number, within the licence the source's own roundness granted.

    Nearest, not coarsest: 80 and 75 are equally easy to say, so accuracy breaks the tie - an
    earlier version preferred the coarser step and turned 50 miles into 75 km.
    """
    best, best_err = value, tolerance
    for cand in nice_values(value):
        err = abs(cand - value) / value
        if err <= best_err + 1e-12 and (err < best_err - 1e-12 or cand > best):
            best, best_err = cand, err
    return best


def _fmt(value: float, decimals: int) -> str:
    """Trailing zeros are noise here: 5,0 centymetra is not something anyone says."""
    if decimals and abs(value - round(value)) > 1e-9:
        return f"{value:.{decimals}f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{round(value):d}"


def _snap_temp(c: float) -> float:
    """Temperature rounds in DEGREES, not in percent.

    Everywhere else a few percent is harmless, but on a scale that crosses zero a relative tolerance
    is meaningless near freezing and reckless near body heat - 37,8 and 40 stopni are a fever and an
    emergency. So the rule is absolute: land on a ten or a five when that costs at most two degrees,
    otherwise on the nearest degree. Above a couple of hundred the scale is coarse enough that the
    allowance grows with it.
    """
    scale = max(1.0, abs(c) / 100.0)
    # A five or a ten has to be genuinely close to be worth taking: at a flat two degrees, 36,7 -
    # a normal body temperature - would have been read as 35, which is hypothermia.
    budget = {100.0: 3.0, 50.0: 3.0, 10.0: 1.5, 5.0: 1.5, 2.0: 1.0}
    steps = (100.0, 50.0, 10.0, 5.0, 2.0) if abs(c) >= 200 else (10.0, 5.0, 2.0)
    for step in steps:
        cand = round(c / step) * step
        if abs(cand - c) <= budget[step] * scale:
            return cand
    return round(c)


def _metric(value: float, dec: int, kind: str, tolerance: float) -> tuple[str, str]:
    """Convert one measurement. Returns (formatted number, Polish unit word)."""
    if kind == "fahrenheit":
        c = (value - 32.0) * 5.0 / 9.0
        if dec:
            return _fmt(c, dec), _agree(c, "C")
        return _fmt(_snap_temp(c), 0), _agree(_snap_temp(c), "C")

    factor, base = FACTORS[kind]
    metric = value * factor

    if dec:
        # The source chose a precision; carry it across rather than snapping. One tenth of a foot is
        # three centimetres, so that is the step the result is rounded to - no invented digits, none
        # thrown away.
        step = (10.0 ** -dec) * factor
        scale, unit = _scale_for(metric, base)
        step_scaled = step / scale
        places = max(0, min(3, math.ceil(-math.log10(step_scaled)) if step_scaled < 1 else 0))
        v = round(metric / scale / step_scaled) * step_scaled
        return _fmt(v, places), _agree(v, unit)

    # No decimal in the source means none in the result: the speaker gave a whole number, so the
    # lektor says a whole number. The unit is chosen to make that possible - six feet becomes 180
    # centymetrow rather than 1,8 metra - and the value is stretched onto something sayable within
    # the licence the source's roundness granted.
    slack = max(tolerance, source_slack(value, 0.12))
    for scale, unit in _ladder(metric, base):
        v = _snap(metric / scale, slack)
        if abs(v - round(v)) < 1e-9 and round(v) >= 1:
            return _fmt(v, 0), _agree(v, unit)
    scale, unit = _scale_for(metric, base)
    v = _snap(metric / scale, slack)
    return _fmt(v, 2), _agree(v, unit)


def _ladder(metric: float, base: str) -> list[tuple[float, str]]:
    """Unit scales to try, largest first, so the biggest unit that still gives a whole number wins."""
    if base == "m":
        return [(1000.0, "km"), (1.0, "m"), (0.01, "cm")]
    if base == "kg":
        return [(1.0, "kg"), (0.001, "g")]
    if base == "l":
        return [(1.0, "l"), (0.001, "ml")]
    if base == "m2":
        return [(10000.0, "ha"), (1.0, "m2")]
    return [(1.0, base)]


# nominative singular, nominative plural (2-4), genitive plural (5+), genitive singular (decimals)
FORMS: dict[str, tuple[str, str, str, str]] = {
    "km": ("kilometr", "kilometry", "kilometrów", "kilometra"),
    "m": ("metr", "metry", "metrów", "metra"),
    "cm": ("centymetr", "centymetry", "centymetrów", "centymetra"),
    "kg": ("kilogram", "kilogramy", "kilogramów", "kilograma"),
    "g": ("gram", "gramy", "gramów", "grama"),
    "l": ("litr", "litry", "litrów", "litra"),
    "ml": ("mililitr", "mililitry", "mililitrów", "mililitra"),
    "ha": ("hektar", "hektary", "hektarów", "hektara"),
    "m2": ("metr kwadratowy", "metry kwadratowe", "metrów kwadratowych", "metra kwadratowego"),
    # Bare degrees: in Polish "dwadziescia stopni" is already Celsius, and naming the scale only
    # adds syllables a lektor has to fit into someone else's pause. Fahrenheit is what needs
    # saying, and after conversion there is none left.
    "C": ("stopień", "stopnie", "stopni", "stopnia"),
}


def _agree(value: float, key: str) -> str:
    """The form of the unit that the number in front of it demands.

    Polish declines the noun after a numeral: one metr, two metry, five metrów, and a fractional
    value takes the genitive singular - 1,8 metra. Getting this wrong is instantly audible, and it
    is fully determined by the number, so it is decided here rather than left to the model.
    """
    one, few, many, frac = FORMS[key]
    if abs(value - round(value)) > 1e-9:
        return frac
    n = abs(int(round(value)))  # -18 % 10 is 2 in Python, which would give "stopnie" not "stopni"
    if n == 1:
        return one
    last_two, last = n % 100, n % 10
    if 2 <= last <= 4 and not 12 <= last_two <= 14:
        return few
    return many


def _scale_for(metric: float, base: str) -> tuple[float, str]:
    """Pick the unit a person would use for this magnitude."""
    if base == "m":
        if metric >= 1000:
            return 1000.0, "km"
        if metric < 1:
            return 0.01, "cm"
        return 1.0, "m"
    if base == "kg":
        return (0.001, "g") if metric < 1 else (1.0, "kg")
    if base == "l":
        return (0.001, "ml") if metric < 1 else (1.0, "l")
    if base == "m2":
        return (10000.0, "ha") if metric >= 10000 else (1.0, "m2")
    return 1.0, base


_FEET = r"stop(?:a|y|ę|ą|o|ie|om|ami|ach)|stóp"
_INCH = r"cal(?:a|e|i|ów|em|ach)?"
COMPOUND_RE = re.compile(
    rf"\b(?P<ft>{_NUM})\s+(?:{_FEET})\s+(?:i\s+)?(?P<in>{_NUM})\s+(?:{_INCH})\b", re.IGNORECASE)


def _compound(text: str, tolerance: float) -> str:
    """"Five feet ten" is one measurement, not two.

    Left to the general rule it came out as "150 centymetrow 25 centymetrow", which is both wrong
    and unsayable. Feet and inches together are almost always a person's height, so they collapse
    into a single metric value.
    """
    def repl(m: re.Match) -> str:
        ft, _ = _parse_number(m.group("ft"))
        inch, _ = _parse_number(m.group("in"))
        if ft <= 0 or inch < 0 or inch >= 12:
            return m.group(0)
        metric = ft * 0.3048 + inch * 0.0254
        v = _snap(metric / 0.01, max(tolerance, 0.02))
        return f"{_fmt(v, 0)} {_agree(v, 'cm')}"

    return COMPOUND_RE.sub(repl, text)


def convert_imperial(text: str, cfg) -> str:
    """Rewrite imperial measurements in a line. Conservative by design; see the module docstring."""
    u = cfg["clean"].get("units") or {}
    if not u.get("enabled"):
        return text
    low = text.lower()
    for phrase in u.get("idioms", IDIOMS):
        if phrase in low:
            return text
    allowed = set(u.get("convert", []))
    tolerance = float(u.get("tolerance", 0.035))
    if u.get("currency", True):
        text = spoken_currency(text)
    if "foot" in allowed and "inch" in allowed:
        text = _compound(text, tolerance)

    def repl(m: re.Match) -> str:
        kind = next(UNITS[i][1] for i in range(len(UNITS)) if m.group(f"u{i}"))
        if allowed and kind not in allowed:
            return m.group(0)
        value, dec = _parse_number(m.group("num"))
        # Zero and below are perfectly good temperatures; for a length they mean the match was junk.
        if kind != "fahrenheit" and value <= 0:
            return m.group(0)
        num, unit = _metric(value, dec, kind, tolerance)
        return f"{num} {unit}"

    return MEASURE_RE.sub(repl, text)
