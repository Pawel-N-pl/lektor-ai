#!/usr/bin/env python3
"""Move the paid ElevenLabs cache into the package's cache, without buying a single line again.

Six episodes were read by `tools/render51.py`, which kept its cache under a scratch directory and
named every file after a key of its own: `sha256("{text}|{voice_id}|{stability}|{speed}|{prev}|
{next}|{model_id}")`. The package names the same audio differently, and its cache lives in the
work dir on E: rather than anywhere `/tmp` can reach. 8010 mp3 files, 300 MB, 85 475 characters of
speech - $4.27 at flash rates if it ever has to be read again.

The two key formats cannot be reconciled, but the mapping between them is reproducible: the same
inputs that produced the old name produce the new one. So this walks every cue of every shipped
script, recomputes both names, and copies the blob across.

Two things it refuses to guess, and searches for instead:

  * WHICH VOICE read the episode. E01-E05 were read by one voice and E06 by another, and the
    config only knows about the current one. The right voice is the one whose keys are actually
    on disk.
  * WHETHER the neighbour speed smoothing was in force. Three of the episodes were rendered
    before `speed_smooth_s` existed, and speed is part of the key.

Both are settled by counting hits, per episode, and the result is reported before anything is
copied.

    tools/migrate_tts_cache.py --from ~/.cache/lektor-ai/tts_cache_rescue        # report only
    tools/migrate_tts_cache.py --from ~/.cache/lektor-ai/tts_cache_rescue --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.backends.elevenlabs import cache_key  # noqa: E402
from lektorlib.config import Config  # noqa: E402
from lektorlib.stages import plan_speeds  # noqa: E402

# The voices this project has actually paid for. Bill read S02E01-E05, Mateusz read S02E06 and is
# the one in the config today; the search below decides which is which per episode rather than
# trusting either statement.
KNOWN_VOICES = {
    "Bill": "pqHfZKP75CvOlQylNhV4",
    "Mateusz": "MM9TwXLP67IpAXY0UOGg",
}

# What render51.py sent, hardcoded, on every call it ever made.
SHIPPED_STABILITY = 1.0
SHIPPED_CPS = 14.5


def old_key(text: str, voice_id: str, speed: float, prev: str, nxt: str, model_id: str) -> str:
    return hashlib.sha256(
        f"{text}|{voice_id}|{SHIPPED_STABILITY}|{speed}|{prev}|{nxt}|{model_id}".encode()
    ).hexdigest()


def variants(cfg, cues):
    """Every (label, speeds) the renderer could plausibly have used for this episode."""
    smoothed = plan_speeds(cfg, cues, SHIPPED_CPS)
    keep = cfg.raw["timing"].get("speed_smooth_s")
    try:
        cfg.raw["timing"]["speed_smooth_s"] = 0.0
        flat = plan_speeds(cfg, cues, SHIPPED_CPS)
    finally:
        cfg.raw["timing"]["speed_smooth_s"] = keep
    return [("smoothed", smoothed), ("per-line", flat)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", default="~/.cache/lektor-ai/tts_cache_rescue",
                    help="the flat directory of <oldkey>.mp3 files")
    ap.add_argument("--apply", action="store_true", help="copy; without it, only report")
    ap.add_argument("-c", "--config", default=str(ROOT / "lektor.config.json"))
    args = ap.parse_args()

    src = pathlib.Path(args.src).expanduser()
    if not src.is_dir():
        raise SystemExit(f"{src} is not a directory")
    cfg = Config(pathlib.Path(args.config))
    el = cfg.backend_cfg("elevenlabs")
    model_id = el["model_id"]
    settings = dict(el["settings"])
    if float(settings.get("stability", 0)) != SHIPPED_STABILITY:
        say = (f"!! voice.elevenlabs.settings.stability is {settings.get('stability')}, but every "
               f"line on disk was read at {SHIPPED_STABILITY}. Migrating under the config's value "
               f"would produce keys nothing will ever look up.")
        raise SystemExit(say)

    scripts = sorted(cfg.work_dir.glob("*.lektor.json"))
    if not scripts:
        raise SystemExit(f"no *.lektor.json in {cfg.work_dir}")
    total_copied = total_seen = 0

    for path in scripts:
        data = json.loads(path.read_text(encoding="utf-8"))
        cues = data["cues"]
        best = None
        for voice_name, voice_id in KNOWN_VOICES.items():
            for label, speeds in variants(cfg, cues):
                # The NEW key is computed from the same speeds as the old one, deliberately. An
                # entry is a promise that this is the audio you get for these parameters, and the
                # audio was read at the speed the old key names. Filing it under the speed a
                # render would ask for today would be filing audio under a request it never
                # answered - a render that computes a different speed is right to miss and buy.
                plan = []
                for i, c in enumerate(cues):
                    prev = cues[i - 1]["text"] if i > 0 else ""
                    nxt = cues[i + 1]["text"] if i + 1 < len(cues) else ""
                    old = old_key(c["text"], voice_id, speeds[i], prev, nxt, model_id)
                    blob = src / f"{old}.mp3"
                    if blob.exists():
                        plan.append((blob, cache_key(c["text"], voice_id, model_id, speeds[i],
                                                     settings, prev, nxt)))
                if best is None or len(plan) > len(best[2]):
                    best = (voice_name, label, plan)
        voice_name, label, plan = best
        rate = 100.0 * len(plan) / max(1, len(cues))
        print(f"{data['base'][:52]:<52} {len(plan):>4}/{len(cues):<4} ({rate:5.1f}%)  "
              f"{voice_name}, {label}")
        total_seen += len(cues)

        if not args.apply:
            continue
        for blob, new in plan:
            dst = cfg.cache_dir / new[:2] / f"{new}.mp3"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                continue
            tmp = dst.with_suffix(".mp3.tmp")
            shutil.copyfile(blob, tmp)
            tmp.replace(dst)
            total_copied += 1

    print(f"\n{total_seen} cues examined")
    if args.apply:
        print(f"{total_copied} entries copied into {cfg.cache_dir}")
    else:
        print("nothing copied (pass --apply). Every line NOT matched above is a line that would "
              "be bought again at $0.05 per 1000 characters.")


if __name__ == "__main__":
    main()
