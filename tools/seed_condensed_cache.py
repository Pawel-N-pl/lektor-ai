#!/usr/bin/env python3
"""Fill the reuse cache from the scripts that already exist, so the first rerun is free.

`<base>.condensed.json` is written by the script stage as it condenses. Six episodes were
condensed before that file existed, and their text is sitting in `<base>.lektor.json` - about 1450
lines that were paid for once and would otherwise be redrawn the first time anything upstream
changes, at $0.71 an episode in ElevenLabs re-reads on top of the LLM draw.

So this reads what the model already produced and files it under the key it would have been filed
under. Nothing is called, nothing is billed, and a line is only entered when the script says it
really was condensed - a line the model failed on is left out, exactly as the stage would leave it
out, so it gets another chance rather than a cached failure.

The entries are keyed on the CURRENT model, effort and prompt. That is correct only while those
have not moved since the scripts were written; if they have, this seeds text under a contract it
was not produced under. Check `git log -p wsl/lektorlib/script.py` for CONDENSE_SYSTEM before
running it on old scripts.

    tools/seed_condensed_cache.py            # report only
    tools/seed_condensed_cache.py --apply
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.config import Config  # noqa: E402
from lektorlib.script import (cache_key, condensed_path, load_condensed,  # noqa: E402
                              prompt_fingerprint, write_condensed)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write; without it, only report")
    ap.add_argument("--only", help="substring filter on the episode filename")
    ap.add_argument("-c", "--config", default=str(ROOT / "lektor.toml"))
    args = ap.parse_args()

    cfg = Config(pathlib.Path(args.config))
    s = cfg["script"]
    scripts = sorted(cfg.work_dir.glob("*.lektor.json"))
    if args.only:
        scripts = [p for p in scripts if args.only in p.name]
    if not scripts:
        raise SystemExit(f"no *.lektor.json in {cfg.work_dir}")

    print(f"model {s['openrouter_model']}, effort {s.get('reasoning_effort', 'medium')}, "
          f"prompt {prompt_fingerprint()}\n")
    grand = 0
    for path in scripts:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = data["base"]
        book = load_condensed(cfg, base)
        before = len(book)
        skipped = 0
        for c in data["cues"]:
            if not c.get("condensed"):
                continue
            source = c.get("text_original") or ""
            if not source or not c.get("text"):
                skipped += 1
                continue
            book[cache_key(source, int(c["budget_chars"]), s)] = {
                "text": c["text"],
                "id": c["id"],
                "budget_chars": int(c["budget_chars"]),
                "src_chars": len(source),
                "src": source,
                "at": data.get("generated_at", ""),
                "from": "seeded from the shipped script",
            }
        added = len(book) - before
        grand += added
        total = sum(1 for c in data["cues"] if c.get("condensed"))
        print(f"{base[:52]:<52} {added:>4} new of {total:>4} condensed"
              + (f", {skipped} unusable" if skipped else ""))
        if args.apply and added:
            write_condensed(cfg, base, book)

    print(f"\n{grand} entries" + (" written" if args.apply
                                  else " would be written (pass --apply)"))
    if args.apply:
        print(f"-> {condensed_path(cfg, scripts[0].stem.replace('.lektor', '')).parent}")


if __name__ == "__main__":
    main()
