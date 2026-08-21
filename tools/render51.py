#!/usr/bin/env python3
"""The 5.1 renderer, now a wrapper around the package that absorbed it.

Everything this file used to do by hand -- reading the film's own 5.1 out of the mkv, putting the
lektor on the centre channel, ducking the speech band harder than the rest, planning each line's
speed against its neighbours, streaming the mix into E-AC-3 -- lives in
`lektorlib.stages.stage_voice` and `lektorlib.mix` now, driven by `mix.channel_layout`,
`mix.duck_mode` and the `encode.surround` profile.

It survives as a wrapper for one reason: it is the command that produced six episodes, and it took
a path to an mkv rather than an `--only` filter. Do not add features here. The pipeline entry
point is:

    set -a && . ./.env && set +a
    .venv/bin/python wsl/lektor.py voice --only S02E06 --force

What was folded in, and what changed with it:

  * `lektor_original` is gone, and with it this file's import of it -- the module lived in a
    scratch directory that was wiped, so this script had been dead code since.
  * The TTS cache moved from `tools/tts_cache` (also on the wipe list) to the work dir, and its
    key gained previous_text / next_text, which the requests always carried. `tools/migrate_tts_cache.py`
    moves the paid entries across without buying a single line again.
  * `stability` is 1.0, as it always was here. The config said 0.6 and was simply wrong.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.config import Config  # noqa: E402
from lektorlib.mix import is_surround  # noqa: E402
from lektorlib import stages  # noqa: E402


def main() -> None:
    cfg = Config(ROOT / "lektor.toml")
    only = None
    if len(sys.argv) > 1:
        # The episode used to be a path to an mkv; the package selects by name, and the stem of
        # that path is the name. Anything that is not a path is taken as the filter itself.
        arg = sys.argv[1]
        only = pathlib.Path(arg).stem if "/" in arg or arg.endswith(".mkv") else arg
    if not is_surround(cfg):
        print("mix.channel_layout is not a surround layout -- this wrapper exists for the 5.1 "
              "path. Set it back, or call `lektor.py voice` directly.", file=sys.stderr)
        raise SystemExit(2)
    print(f"render51 is now `lektor.py voice`; running it{f' for {only}' if only else ''}.\n")
    stages.stage_voice(cfg, only, force=True)


if __name__ == "__main__":
    main()
