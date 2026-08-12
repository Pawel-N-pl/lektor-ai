"""What the run costs, priced against the backend that is actually selected.

Two independent bills, and conflating them is how the old table got it wrong:

  speech        only ElevenLabs charges for it. Piper and Chatterbox are free however many times
                you redo them, and printing a dollar figure for them would be a lie.
  condensation  OpenRouter, one call per episode, and it applies to every backend because every
                backend reads the same condensed script.

ElevenLabs bills a FLAT rate per 1000 characters, identical on every paid plan. What the plan buys
is an included monthly allowance and a concurrency limit – never a cheaper character. The rate
ladder this module replaced (0.30/0.24/0.18/0.12 per plan) did not exist.
"""

from __future__ import annotations

import json

from . import say
from .config import Config
from .script import read_spend
from .srt import clean_text, parse_srt, should_skip

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
        cues = [c for c in parse_srt(p) if not should_skip(c.raw, cfg)]
        rows.append((p.stem, len(cues), sum(len(clean_text(c.raw, cfg)) for c in cues)))
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
    real = sum(read_spend(cfg, base) for base, _, _ in rows)
    if real:
        say(f"condensation via {cfg['script']['openrouter_model']}: ${real:.4f} already billed "
            f"across {len(rows)} episode(s), read from OpenRouter usage.cost.")
    else:
        per_ep = float(cfg["script"].get("measured_usd_per_episode", 0.0))
        if per_ep:
            say(f"condensation via {cfg['script']['openrouter_model']}: about "
                f"${per_ep * max(1, len(rows)):.3f} for {max(1, len(rows))} episode(s), measured on "
                f"one real episode. The script stage records what was actually billed.")
    say("")
