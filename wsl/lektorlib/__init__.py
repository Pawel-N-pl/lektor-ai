"""lektor-ai: the WSL half of the pipeline, split into modules.

Everything that thinks lives here. Nothing in this package opens the multi-gigabyte sources;
it works on the small artifacts the Windows stage drops into the work dir (a .srt and a stereo
FLAC downmix) and produces one finished lektor track for Windows to remux.

  config    Config, path resolution, .env loading
  srt       Cue, parsing, cleaning, time windows
  audio     ffmpeg decode / stretch / resample helpers
  mix       assembly, ducking, normalisation, the streaming encode
  stages    the six things the CLI can ask for
  backends  pluggable TTS engines behind one interface
"""

from __future__ import annotations

import threading

__all__ = ["say"]

_print_lock = threading.Lock()


def say(msg: str, *, indent: int = 0) -> None:
    # Worker threads print progress, so the lock is what keeps lines from interleaving.
    with _print_lock:
        print(" " * indent + msg, flush=True)
