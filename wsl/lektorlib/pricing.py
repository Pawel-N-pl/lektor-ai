"""What the run costs, priced against the backend that is actually selected.

Two independent bills, and conflating them is how the old table got it wrong:

  speech        only ElevenLabs charges for it. Piper and Chatterbox are free however many times
                you redo them, and printing a dollar figure for them would be a lie.
  condensation  OpenRouter, one call per episode, and it applies to every backend because every
                backend reads the same condensed script.
  translation   OpenRouter again, only for films with no Polish track, which have to be
                translated before there is anything to condense. Reported apart, because it is a
                per-film bill and not a per-season constant.
  transcription nothing: faster-whisper on the local GPU, for a film with no text subtitle at all.
                Said anyway, because it is the one step on that path that is not free in time.

ElevenLabs bills a FLAT rate per 1000 characters, identical on every paid plan. What the plan buys
is an included monthly allowance and a concurrency limit – never a cheaper character. The rate
ladder this module replaced (0.30/0.24/0.18/0.12 per plan) did not exist.
"""

from __future__ import annotations

import json

from . import media, say, transcribe, translate
from .config import Config
from .script import read_spend
from .srt import load_cues

FLASH_USD_PER_1K = 0.05   # eleven_flash_*, eleven_turbo_*
OTHER_USD_PER_1K = 0.10   # eleven_multilingual_v2, eleven_v3

# plan -> (included chars on flash/turbo, included chars on the other models)
PLAN_INCLUDED = {
    "free": (20_000, 10_000),
    "starter": (120_000, 60_000),
    "creator": (440_000, 220_000),
    "pro": (1_980_000, 990_000),
    "scale": (5_980_000, 2_990_000),
    "business": (19_800_000, 9_900_000),
}

# plan -> (parallel TTS calls on flash/turbo, on the other models)
PLAN_CONCURRENCY = {
    "free": (4, 2),
    "starter": (6, 3),
    "creator": (10, 5),
    "pro": (20, 10),
    "scale": (30, 15),
    "business": (30, 15),
}


def is_fast_model(model_id: str) -> bool:
    """Flash and Turbo bill at half the rate. Turbo is deprecated but still priced as Flash."""
    m = (model_id or "").lower()
    return "flash" in m or "turbo" in m


def usd_per_1k(model_id: str) -> float:
    return FLASH_USD_PER_1K if is_fast_model(model_id) else OTHER_USD_PER_1K


def tts_usd(chars: int, model_id: str) -> float:
    return chars / 1000.0 * usd_per_1k(model_id)


def plan_limits(plan: str, model_id: str) -> tuple[str, int, int]:
    """(normalised plan name, included chars, concurrent calls) for this plan and model."""
    key = (plan or "").lower()
    if key not in PLAN_INCLUDED:
        key = "creator"
    col = 0 if is_fast_model(model_id) else 1
    return key, PLAN_INCLUDED[key][col], PLAN_CONCURRENCY[key][col]


def _rows(cfg: Config) -> tuple[list[tuple[str, int, int]], bool]:
    """(base, lines, chars) per episode, from the scripts if they exist, else from the raw srt."""
    scripts = sorted(cfg.work_dir.glob("*.lektor.json"))
    if scripts:
        rows = []
        for p in scripts:
            d = json.loads(p.read_text(encoding="utf-8"))
            rows.append((d["base"], len(d["cues"]), sum(len(c["text"]) for c in d["cues"])))
        return rows, True
    rows = []
    for p in sorted(cfg.work_dir.glob("*.srt")):
        cues = load_cues(p, cfg)
        # c.text is what will actually be spoken, merges included; re-cleaning c.raw here would
        # count the pre-merge cards and overstate the line count by 62%.
        rows.append((p.stem, len(cues), sum(len(c.text) for c in cues)))
    return rows, False


def stage_cost(cfg: Config, plan: str) -> None:
    rows, from_scripts = _rows(cfg)
    total = sum(r[2] for r in rows)

    say(f"\n{'EPISODE':<52} {'LINES':>6} {'CHARS':>8}")
    if not from_scripts:
        say("  (no scripts yet – estimating from raw subtitles, before condensation)")
    for base, n, chars in rows:
        say(f"{base[:52]:<52} {n:>6} {chars:>8}")
    say(f"\n{'TOTAL':<52} {sum(r[1] for r in rows):>6} {total:>8} chars")

    name = cfg.backend_name
    if name == "elevenlabs":
        model = cfg.backend_cfg("elevenlabs").get("model_id", "")
        rate = usd_per_1k(model)
        key, included, conc = plan_limits(plan, model)
        say(f"speech: backend elevenlabs, model {model or '(unset)'} -> "
            f"${tts_usd(total, model):,.2f} at ${rate:.2f} per 1000 chars "
            f"(the same rate on every plan)")
        # The backend reads its own sub-table first and only then the shared default, so the
        # warning below has to look in the same order or it warns about a number nobody uses.
        asked = int(cfg.backend_cfg("elevenlabs").get("concurrency")
                    or cfg["voice"].get("concurrency", conc))
        say(f"plan {key}: {included:,} chars included per month, {conc} parallel calls "
            f"(config asks for {asked})")
        if asked > conc:
            say(f"  !! voice.concurrency {asked} is above the {key} cap of {conc} – the extra "
                f"calls come back as 429 concurrent_limit_exceeded")
        if total > included:
            over = total - included
            say(f"  {over:,} chars past the allowance -> ${tts_usd(over, model):,.2f} on top of the "
                f"subscription")
        else:
            say(f"  fits the allowance, with {included - total:,} chars to spare")
        say("cached lines cost nothing on a rerun.")
    else:
        say(f"speech: backend {name} runs locally – no per-character billing at all, "
            f"$0.00 however many times you redo it.")

    # The condensation bill applies to every backend: they all read the same condensed script.
    # Only condensation's runs: the spend record also holds translation, reported on its own.
    real = sum(read_spend(cfg, base, "script") for base, _, _ in rows)
    if real:
        say(f"condensation via {cfg['script']['openrouter_model']}: ${real:.4f} already billed "
            f"across {len(rows)} episode(s), read from OpenRouter usage.cost.")
    else:
        per_ep = float(cfg["script"].get("measured_usd_per_episode", 0.0))
        if per_ep:
            say(f"condensation via {cfg['script']['openrouter_model']}: about "
                f"${per_ep * max(1, len(rows)):.3f} for {max(1, len(rows))} episode(s), measured on "
                f"one real episode. The script stage records what was actually billed.")
    _translation_cost(cfg, {base for base, _, _ in rows})
    say("")


def translated_bases(cfg: Config) -> list[str]:
    """Episodes whose Polish text is (or will be) a translation, by their extract sidecar."""
    tail = ".source.json"
    bases = sorted(p.name[: -len(tail)] for p in cfg.work_dir.glob(f"*{tail}"))
    return [b for b in bases if translate.is_translated(cfg, b)]


def _translation_cost(cfg: Config, counted: set[str]) -> None:
    """The translation bill, for the films that have one. Silent when no film needs translating."""
    bases = translated_bases(cfg)
    if not bases:
        return
    t = media.translate_settings(cfg)
    model = t.get("openrouter_model", "?")
    n_cues = n_chars = 0
    for base in bases:
        src = translate.source_srt(cfg, base)
        if src is not None:
            cues = translate.source_cues(src)
            n_cues += len(cues)
            n_chars += sum(len(c.text) for c in cues)
    pending = [b for b in bases if not (cfg.work_dir / f"{b}.srt").exists()]
    heard = [b for b in bases if translate.origin(cfg, b) == "asr"]
    if heard:
        waiting = [b for b in heard if not transcribe.is_done(cfg, b)]
        say(f"transcription via faster-whisper "
            f"{transcribe.settings(cfg).get('model', 'large-v3')}: {len(heard)} episode(s) with "
            f"no text subtitle at all, $0.00 - it runs locally."
            + (f" {len(waiting)} not transcribed yet, so the source text below leaves them out."
               if waiting else ""))
    say(f"translation via {model}: {len(bases)} episode(s) with no Polish track, {n_cues} cues / "
        f"{n_chars} chars of foreign source text.")
    real = sum(read_spend(cfg, base, "translate") for base in bases)
    if real:
        say(f"  ${real:.4f} already billed, read from OpenRouter usage.cost.")
    per_ep = float(t.get("measured_usd_per_episode", 0.0) or 0.0)
    if pending and per_ep:
        say(f"  about ${per_ep * len(pending):.3f} for the {len(pending)} not translated yet.")
    elif pending:
        say(f"  {len(pending)} not translated yet, and there is no measured per-episode figure to "
            f"price them with: translate.measured_usd_per_episode is 0 (unmeasured) until a real "
            f"episode has run. The translate stage records what OpenRouter actually bills.")
    missing = [b for b in pending if b not in counted]
    if missing:
        say(f"  The speech character total above leaves out these {len(missing)}: they have no "
            f"Polish text yet to count.")
