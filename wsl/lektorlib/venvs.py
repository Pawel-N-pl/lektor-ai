"""Where a virtual environment keeps its interpreter, on whichever platform this is running.

Three places in this project hand work to a model that cannot live in the pipeline's own
interpreter - two TTS backends and the forced aligner - and each one launches it from a venv of
its own. They all spelled the path the same POSIX-only way, `<venv>/bin/python`, which is simply
absent on Windows: there the interpreter is `<venv>/Scripts/python.exe`. One helper, so a Windows
box needs no special case and a future fourth caller cannot get it wrong again.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def venv_python(venv: str | os.PathLike) -> Path:
    """The interpreter inside `venv`, whether this is a POSIX layout or a Windows one.

    Both candidates are tried rather than branching on sys.platform, because the layout follows the
    venv that was CREATED, not the machine reading it: a repository shared over a mount, or a venv
    built under WSL and read from Windows, can present either shape. Falls back to the platform's
    own convention so the caller still gets a path to complain about by name.
    """
    root = Path(venv).expanduser()
    for candidate in (root / "bin" / "python", root / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def this_python() -> Path:
    """The interpreter currently running, for a caller that wants no separate venv at all."""
    return Path(sys.executable)
