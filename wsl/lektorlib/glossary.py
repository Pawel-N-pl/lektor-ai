"""The series glossary: what a human decided once, for every episode of one show.

A whole-episode prompt lets the model work out who is who, but only from one episode, and it has
to work it out again in the next one - where it may decide differently. Polish makes that visible
on every other line: a character who was "gotowa" last week is "gotowy" this week, the boss who
was "pani" becomes "ty", "the Greyhounds" are "Charty" in one episode and stay English in the next.
None of it is a wrong translation of a single episode; it is an inconsistent series. A person who
has watched the show settles those once, here, and the translate stage hands it to the model as
binding.

    translate.glossary = 'auto'   <input_dir>/lektor.glossary.toml when it exists, otherwise none
                         'off'    none, even when the file is there
                         a path   that file; relative to input_dir; missing is an error

The file, all three tables optional:

    [characters.Ted]
    gender = 'm'                      # 'm' or 'f'; leave out when the show does not say
    aliases = ['Coach', 'Lasso']      # other names the character goes by in the dialogue
    note = 'amerykański trener, do wszystkich mówi po imieniu'

    [address]
    default = 'ty'                    # how characters address each other unless listed
    'Nate -> Ted' = 'pan'             # what the first one calls the second
    'Ted -> Rebecca' = 'pani'

    [terms]
    'Greyhounds' = 'Charty'           # rendered this way every time, inflected as Polish needs

Only what an episode can use goes into its prompt: a character whose name (or alias, or SDH
speaker label) never occurs in it, a pair one of whose members is absent, a term the episode never
says - all left out. That keeps the prompt short, and it is also what decides what a change to the
file costs (see `key_part`).

A file that will not parse, or says something this module cannot read - a gender of 'male', a
form of address of 'sir', a table it does not know - stops the stage before anything is paid for:
translating a season without the glossary somebody wrote, and filing every line under keys that
say so, is the worse of the two outcomes.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .media import translate_settings

__all__ = ["Glossary", "Character", "GlossaryError", "load_glossary", "glossary_path",
           "parse_glossary", "dump_glossary", "ADDRESS_FORMS", "GENDERS"]

DEFAULT_NAME = "lektor.glossary.toml"

# What Polish distinguishes when one person speaks to another. 'wy' and 'państwo' are for a
# character addressing a group, which a pair key can still name ('Ted -> team').
ADDRESS_FORMS = ("ty", "pan", "pani", "wy", "państwo")
# The model is told the word, not the letter.
GENDERS = {"m": "mężczyzna", "f": "kobieta"}

_PAIR_RE = re.compile(r"^\s*(.+?)\s*-?>\s*(.+?)\s*$")


class GlossaryError(ValueError):
    """The glossary file is there and cannot be used as it is."""


@dataclass
class Character:
    name: str
    gender: str = ""
    aliases: tuple[str, ...] = ()
    note: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass
class Glossary:
    path: Path | None = None
    characters: dict[str, Character] = field(default_factory=dict)
    address_default: str = ""
    # (who speaks, to whom) -> form, both as written in the file.
    address: dict[tuple[str, str], str] = field(default_factory=dict)
    terms: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.characters or self.address_default or self.address or self.terms)

    def lookup(self, name: str) -> Character | None:
        """The character a name or alias belongs to, ignoring case: an SDH label says 'TED'."""
        key = name.strip().casefold()
        for c in self.characters.values():
            if any(n.casefold() == key for n in c.names):
                return c
        return None

    def canonical(self, name: str) -> str:
        c = self.lookup(name)
        return c.name if c else name.strip()

    # --- what one episode can use --------------------------------------------------------------

    def for_episode(self, texts, speakers=()) -> Glossary:
        """The part of the glossary this episode can use, and nothing else.

        A character counts when any of its names occurs in the source text - case kept, since
        'Will' the name and 'will' the verb are only told apart that way - or labels one of its
        lines. A pair counts when both of its people do; the default always counts. A term counts
        when the text says it, ignoring case.
        """
        blob = "\n".join(texts)
        spoken = {self.canonical(s).casefold() for s in speakers}
        present = {}
        for c in self.characters.values():
            if c.name.casefold() in spoken or any(mentions(blob, n) for n in c.names):
                present[c.name] = c

        def here(name: str) -> bool:
            c = self.lookup(name)
            if c is not None:
                return c.name in present
            return name.casefold() in spoken or mentions(blob, name)

        return Glossary(
            path=self.path,
            characters=present,
            address_default=self.address_default,
            address={pair: form for pair, form in self.address.items()
                     if here(pair[0]) and here(pair[1])},
            terms={src: pl for src, pl in self.terms.items() if mentions(blob, src, fold=True)},
        )

    def terms_in(self, text: str) -> dict[str, str]:
        return {src: pl for src, pl in self.terms.items() if mentions(text, src, fold=True)}

    # --- the cache key -------------------------------------------------------------------------

    def cast_fingerprint(self) -> str:
        """The characters and the forms of address, hashed. '' when there are none."""
        if not (self.characters or self.address_default or self.address):
            return ""
        blob = json.dumps({
            "characters": sorted([c.name, c.gender, sorted(c.aliases), c.note]
                                 for c in self.characters.values()),
            "default": self.address_default,
            "address": sorted([a, b, f] for (a, b), f in self.address.items()),
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def key_part(self, text: str) -> str:
        """What of this (episode-restricted) glossary one source line's translation depends on.

        Two scopes, because the two kinds of entry reach a line differently:

          * the cast - characters, genders, notes, forms of address - reaches EVERY line. Who is
            speaking and to whom is decided from context, not from the words of the line: "I'm
            ready." carries no name and still turns on Rebecca's gender. So a change to any
            character in this episode retranslates the whole episode. Wider than it might be;
            narrower would be a guess about which line a character says, which is the very thing
            the model is asked to decide.
          * a term reaches only a line that says it. Changing the rendering of 'Greyhounds'
            retranslates the lines with 'Greyhounds' in them and reuses the rest.

        A glossary entry this episode cannot use is not in its glossary at all (`for_episode`),
        so editing it retranslates nothing here. '' with no glossary, which keeps the cache key
        exactly what it was before glossaries existed.
        """
        cast = self.cast_fingerprint()
        terms = self.terms_in(text)
        if not cast and not terms:
            return ""
        return json.dumps([cast, sorted(terms.items())], ensure_ascii=False)

    def fingerprint(self) -> str:
        """All of it, hashed: what the provenance records, so a change is seen as one. '' if empty."""
        if not self:
            return ""
        blob = json.dumps([self.cast_fingerprint(), sorted(self.terms.items())],
                          ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    # --- the prompt ----------------------------------------------------------------------------

    def render(self) -> str:
        """The glossary as the model reads it, in Polish, under the rules in translate.py."""
        out = []
        if self.characters:
            out.append(HEADINGS["characters"])
            for c in sorted(self.characters.values(), key=lambda c: c.name.casefold()):
                line = f"- {c.name}"
                if c.aliases:
                    line += f" (też: {', '.join(c.aliases)})"
                facts = [GENDERS[c.gender]] if c.gender else []
                if c.note:
                    facts.append(c.note)
                out.append(line + (": " + ". ".join(facts) if facts else ""))
        if self.address or self.address_default:
            out.append(HEADINGS["address"])
            for (a, b), form in sorted(self.address.items()):
                out.append(f'- {a} do: {b} – "{form}"')
            if self.address_default:
                out.append(f'- {HEADINGS["default"]} – "{self.address_default}"')
        if self.terms:
            out.append(HEADINGS["terms"])
            for src, pl in sorted(self.terms.items(), key=lambda kv: kv[0].casefold()):
                out.append(f"- {src} → {pl}")
        return "\n".join(out)


# The headings the rendered glossary uses. A constant, and part of the prompt fingerprint whenever
# a glossary is sent, for the reason translate.LINE_FORMAT is: it is part of what the model reads.
HEADINGS = {
    "characters": "Postacie:",
    "address": "Zwracanie się (kto do kogo – jaką formą):",
    "default": "wszyscy pozostali do siebie",
    "terms": "Terminy (zawsze tak, odmieniane jak trzeba):",
}


def mentions(text: str, needle: str, fold: bool = False) -> bool:
    """Does `text` say `needle`, starting at a word boundary?

    Only the left edge is a boundary, as in srt.respell: 'Greyhound' must still find
    'Greyhounds', and a German name its genitive. The price is an occasional false hit - 'Art'
    in 'Arthur' - which costs one extra entry in the prompt and nothing else.
    """
    needle = needle.strip()
    if not needle:
        return False
    flags = re.IGNORECASE if fold else 0
    return re.search(rf"(?<!\w){re.escape(needle)}", text, flags) is not None


# ---------------------------------------------------------------------------
# Reading and writing the file
# ---------------------------------------------------------------------------


def glossary_path(cfg) -> Path | None:
    """The glossary file this config names, or None for none. A named file that is missing is an
    error; the 'auto' default that finds nothing is not."""
    raw = str(translate_settings(cfg).get("glossary", "auto") or "auto").strip()
    if raw.lower() == "off":
        return None
    input_dir = getattr(cfg, "input_dir", None)
    if raw.lower() == "auto":
        if input_dir is None:
            return None
        p = Path(input_dir) / DEFAULT_NAME
        return p if p.is_file() else None
    p = Path(raw).expanduser()
    if not p.is_absolute() and input_dir is not None:
        p = Path(input_dir) / p
    if not p.is_file():
        raise GlossaryError(f"translate.glossary names {p}, and there is no such file")
    return p


def load_glossary(cfg) -> Glossary | None:
    """The configured glossary, or None. GlossaryError when the file cannot be used."""
    p = glossary_path(cfg)
    if p is None:
        return None
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GlossaryError(f"{p}: {exc}") from None
    return parse_glossary(data, p)


def _str(value, where: str) -> str:
    if not isinstance(value, str):
        raise GlossaryError(f"{where} must be a string, not {type(value).__name__}")
    return value.strip()


def _form(value, where: str) -> str:
    form = _str(value, where).casefold()
    if form not in ADDRESS_FORMS:
        raise GlossaryError(f"{where} = {value!r} – pick from {', '.join(ADDRESS_FORMS)}")
    return form


def parse_glossary(data: dict, path: Path | None = None) -> Glossary:
    where = path.name if path is not None else "glossary"
    unknown = sorted(set(data) - {"characters", "address", "terms"})
    if unknown:
        raise GlossaryError(f"{where}: unknown table(s) {', '.join(unknown)} – the glossary has "
                            f"[characters.<name>], [address] and [terms]")
    g = Glossary(path=path)
    for name, entry in (data.get("characters") or {}).items():
        at = f"{where}: characters.{name}"
        if not isinstance(entry, dict):
            raise GlossaryError(f"{at} must be a table")
        extra = sorted(set(entry) - {"gender", "aliases", "note"})
        if extra:
            raise GlossaryError(f"{at}: unknown key(s) {', '.join(extra)} – a character has "
                                f"gender, aliases and note")
        gender = _str(entry.get("gender", ""), f"{at}.gender").casefold()
        if gender not in ("", *GENDERS):
            raise GlossaryError(f"{at}.gender = {entry['gender']!r} – 'm', 'f', or leave it out")
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            raise GlossaryError(f"{at}.aliases must be a list of names")
        g.characters[name.strip()] = Character(
            name=name.strip(), gender=gender,
            aliases=tuple(a for a in (_str(x, f"{at}.aliases") for x in aliases) if a),
            note=_str(entry.get("note", ""), f"{at}.note"))
    for key, value in (data.get("address") or {}).items():
        at = f"{where}: address.{key!r}"
        if key.strip().casefold() == "default":
            g.address_default = _form(value, at)
            continue
        m = _PAIR_RE.match(key)
        if not m:
            raise GlossaryError(f"{at}: a pair is written 'Who -> To whom'")
        g.address[(g.canonical(m.group(1)), g.canonical(m.group(2)))] = _form(value, at)
    for src, pl in (data.get("terms") or {}).items():
        text = _str(pl, f"{where}: terms.{src!r}")
        if src.strip() and text:
            g.terms[src.strip()] = text
    return g


def _toml_str(value: str) -> str:
    # A JSON string is a valid TOML basic string: the same quotes and the same escapes.
    return json.dumps(value, ensure_ascii=False)


def dump_glossary(g: Glossary, header: str = "") -> str:
    """The glossary as a file `load_glossary` reads back to the same thing."""
    out = [header.rstrip(), ""] if header else []
    for c in g.characters.values():
        out.append(f"[characters.{_toml_str(c.name)}]")
        out.append(f"gender = {_toml_str(c.gender)}")
        if c.aliases:
            out.append("aliases = [" + ", ".join(_toml_str(a) for a in c.aliases) + "]")
        out.append(f"note = {_toml_str(c.note)}")
        out.append("")
    if g.address or g.address_default:
        out.append("[address]")
        if g.address_default:
            out.append(f"default = {_toml_str(g.address_default)}")
        for (a, b), form in g.address.items():
            out.append(f"{_toml_str(f'{a} -> {b}')} = {_toml_str(form)}")
        out.append("")
    if g.terms:
        out.append("[terms]")
        for src, pl in g.terms.items():
            out.append(f"{_toml_str(src)} = {_toml_str(pl)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
