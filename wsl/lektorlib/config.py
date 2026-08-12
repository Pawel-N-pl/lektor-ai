"""Config loading, path resolution and the .env bootstrap.

Secrets are read from the environment, never from the config file. The .env loader below is the
only reason a plain `lektor.py voice` works without the caller exporting anything first.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "lektor.config.json"

# Keys the pipeline is allowed to pick up from a .env file. Anything else in there is ignored,
# so a shared .env cannot quietly inject PATH or PYTHONPATH into a subprocess.
ENV_KEYS = ("OPENROUTER_API_KEY", "ELEVENLABS_API_KEY")


def load_dotenv(path: Path) -> list[str]:
    """Populate os.environ from a KEY=VALUE file. Returns the names it set, never the values.

    An already-exported variable always wins, so `set -a && . .env` in the shell and this loader
    cannot disagree. Tolerates comments, blank lines, `export ` prefixes, quoted values and a
    missing trailing newline.
    """
    if not path.is_file():
        return []
    loaded: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in ENV_KEYS or os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


class Config:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.root = self.path.parent
        self.env_loaded = load_dotenv(self.root / ".env")
        if not self.env_loaded and self.root != DEFAULT_CONFIG.parent:
            self.env_loaded = load_dotenv(DEFAULT_CONFIG.parent / ".env")

    def __getitem__(self, key: str):
        return self.raw[key]

    def get(self, key: str, default=None):
        return self.raw.get(key, default)

    def backend_cfg(self, name: str | None = None) -> dict:
        """The voice.<backend> sub-table, or an empty one if the config predates that backend."""
        v = self.raw["voice"]
        return v.get(name or v.get("backend", "elevenlabs"), {})

    @property
    def backend_name(self) -> str:
        return self.raw["voice"].get("backend", "elevenlabs")

    @property
    def input_dir(self) -> Path:
        return Path(self.raw["paths"]["input_dir_wsl"])

    @property
    def work_dir(self) -> Path:
        d = self.input_dir / self.raw["paths"]["work_subdir"]
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def cache_dir(self) -> Path:
        d = self.work_dir / ".cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def ffmpeg(self) -> str:
        return self.raw["paths"]["ffmpeg_wsl"]


# ---------------------------------------------------------------------------
# Calibration state (per backend + voice + model, cached in the work dir)
# ---------------------------------------------------------------------------


def cps_path(cfg: Config) -> Path:
    b = cfg.backend_cfg()
    ident = f"{cfg.backend_name}|{b.get('voice_id', '')}|{b.get('model_id', '')}"
    return cfg.work_dir / f".cps.{hashlib.sha256(ident.encode()).hexdigest()[:12]}.json"


def load_cps(cfg: Config) -> float:
    """Measured speaking rate if calibration has run, otherwise the config default."""
    p = cps_path(cfg)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))["chars_per_second"]
    return cfg["timing"]["chars_per_second"]
