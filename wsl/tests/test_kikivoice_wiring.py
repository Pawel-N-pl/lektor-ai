"""The wiring around the kikivoice backend, not the backend itself.

The engine is exercised by its own suite in the kikivoice checkout. What that suite cannot see is
whether this repository is plugged into it correctly, and every way it can be wrong is silent:

  * the registry does not know the name, so `--backend kikivoice` dies at argument parsing;
  * a config key drifts away from the name the backend reads, so a MEASURED number silently
    reverts to a default – and the defaults here are 850/40 characters per batch, which is the
    combination that came back AI_SERVER_ERROR after 1754 credits had already been taken;
  * importing the registry starts dragging in a GPU stack, which costs seconds and a card on a run
    that only wanted a network backend;
  * the vendored copies of the client and the batcher drift away from the checkout they were taken
    from, so the standalone CLI and this backend quietly run different code.

None of this touches the network, the session file or a single credit.
"""
import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

WSL = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WSL))

from lektorlib.config import Config

from lektorlib.backends import BACKEND_NAMES, Backend, get_backend

REPO = WSL.parent
CONFIG = REPO / "lektor.toml"
BACKEND_SRC = WSL / "lektorlib" / "backends" / "kikivoice.py"

# Read by the pipeline rather than by the backend: config.cps_path names the calibration file
# after voice_id and model_id of whatever backend is active, and stages.py puts model_id in the
# voice report. Neither shows up as a self.v.get() inside kikivoice.py, and neither is dead.
READ_ELSEWHERE = {"model_id"}


class FakeConfig:
    """The real config file, with only the two things a test cannot have: a work dir and a cache."""

    def __init__(self, tmp):
        self.raw = Config(CONFIG).raw
        self._cache = tmp

    def __getitem__(self, key):
        return self.raw[key]

    def backend_cfg(self, name=None):
        v = self.raw["voice"]
        return v.get(name or v.get("backend", "elevenlabs"), {})

    @property
    def backend_name(self):
        return self.raw["voice"].get("backend", "elevenlabs")

    @property
    def cache_dir(self):
        return self._cache


# -- the registry ------------------------------------------------------------

def test_the_name_is_in_the_registry():
    """check_backends in the CLI reads exactly this tuple, so an unlisted name is unreachable."""
    assert "kikivoice" in BACKEND_NAMES


def test_the_registry_builds_the_backend_from_the_real_config(tmp_path):
    b = get_backend("kikivoice", FakeConfig(tmp_path))
    assert isinstance(b, Backend)
    assert b.name == "kikivoice"
    # Construction must stay offline: the client is resolved in prepare(), not in __init__, so
    # that `lektor.py cost` and a plain --help never touch the session file.
    assert b.client is None


def test_the_shipped_config_survives_construction_with_its_measured_values(tmp_path):
    """The two numbers a silent default would cost real credits: the batch ceiling and the rate."""
    b = get_backend("kikivoice", FakeConfig(tmp_path))
    assert b.b_batch["char_budget"] == 700
    assert b.b_batch["max_cues"] == 25
    assert b.mode in ("library", "clone")


# -- config keys against the keys the backend actually reads -----------------

def _keys_read_from(source: str) -> dict[str, set[str]]:
    """Every literal key the backend pulls out of its own config, per sub-table.

    Parsed rather than grepped, because a grep for `.get("` also finds the ones read off a JSON
    response from the server, and those are not config at all.
    """
    holders = {"v": "", "b_net": "net", "b_budget": "budget", "b_timing": "timing",
               "b_clone": "clone", "b_batch": "batch"}
    found: dict[str, set[str]] = {section: set() for section in holders.values()}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "get" or not node.args:
            continue
        target = node.func.value                      # the thing .get() was called on
        if not isinstance(target, ast.Attribute) or target.attr not in holders:
            continue
        if not (isinstance(target.value, ast.Name) and target.value.id == "self"):
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            found[holders[target.attr]].add(key.value)
    return found


def _keys_in_config() -> dict[str, set[str]]:
    kiki = Config(CONFIG).raw["voice"]["kikivoice"]
    out = {"": set()}
    for key, value in kiki.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            out[key] = {k for k in value if not k.startswith("_")}
        else:
            out[""].add(key)
    return out


def test_the_backend_reads_every_key_the_config_ships():
    """A key nobody reads is a knob that lies: turning it does nothing and the note claims it does."""
    read = _keys_read_from(BACKEND_SRC.read_text(encoding="utf-8"))
    shipped = _keys_in_config()
    dead = {section: sorted(keys - read.get(section, set()) - (READ_ELSEWHERE if section == "" else set()))
            for section, keys in shipped.items()}
    dead = {section: keys for section, keys in dead.items() if keys}
    assert not dead, f"voice.kikivoice keys nothing reads: {dead}"


def test_the_config_ships_every_key_the_backend_reads():
    """The dangerous direction. A missing key does not fail – it falls back to a default that was
    never measured, and the note next to the value that IS measured stays there looking authoritative."""
    read = _keys_read_from(BACKEND_SRC.read_text(encoding="utf-8"))
    shipped = _keys_in_config()
    missing = {section: sorted(keys - shipped.get(section, set())) for section, keys in read.items()}
    missing = {section: keys for section, keys in missing.items() if keys}
    assert not missing, f"voice.kikivoice keys the backend reads but the config does not set: {missing}"


def test_every_configured_table_carries_a_note():
    """Repo rule, and the reason this backend is legible at all: a value without a note is a value
    nobody can second-guess a year from now. A note may cover several neighbouring keys, so this
    only asks that each table has as many notes as it has ideas, not one per key.

    Notes used to be `_key_note` STRINGS, because JSON has no comments. They are comments now, so
    this reads the source text rather than the parsed mapping - which is the point of the format
    change and would otherwise have gone unenforced the moment it landed.
    """
    import re

    tables = {}
    for f in (CONFIG, CONFIG.with_name(f"{CONFIG.stem}.tuning{CONFIG.suffix}")):
        if not f.is_file():
            continue
        current = None
        for line in f.read_text(encoding="utf-8").splitlines():
            header = re.match(r"\s*\[([^\]]+)\]", line)
            if header:
                current = header.group(1)
                tables.setdefault(current, [])
            elif current is not None:
                tables[current].append(line)
    kiki = {name: body for name, body in tables.items()
            if name == "voice.kikivoice" or name.startswith("voice.kikivoice.")}
    assert kiki, "no kikivoice table in either config file"
    for name, body in kiki.items():
        assert any(l.lstrip().startswith("#") for l in body), f"{name} carries no note at all"


# -- laziness ----------------------------------------------------------------

# Watched because they are the expensive ones, not because they are the only ones: a GPU stack
# costs seconds of import and a card that a network backend has no business needing.
GPU_STACK = ("torch", "torchaudio", "transformers")

_PROBE = """
import json, sys
WATCH = set({watch!r})
seen = []


class _Spy:
    \"\"\"Records an import ATTEMPT and then steps aside (returning None delegates).\"\"\"

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in WATCH and root not in seen:
            seen.append(root)
        return None


sys.meta_path.insert(0, _Spy())
error = ""
try:
    import {module}
except BaseException as exc:                 # a reach for a MISSING package lands here
    error = "{{}}: {{}}".format(type(exc).__name__, exc)
print(json.dumps({{"seen": sorted(seen), "error": error}}))
"""


def _imports_attempted(module: str, watch: tuple[str, ...]) -> list[str]:
    """Which of `watch` does importing `module` in a clean interpreter reach for?

    A meta_path spy, NOT `"torch" in sys.modules` after the import. The sys.modules version is how
    this file was written first and it asserted nothing whatsoever: torch is not installed in this
    venv at all, so the name is absent from sys.modules however hard a backend reaches for it, and
    the test was green by accident. A spy sees the ATTEMPT, which is the thing being forbidden, and
    it sees it whether or not the package is installed on the machine running the suite.
    """
    code = _PROBE.format(watch=tuple(watch), module=module)
    r = subprocess.run([sys.executable, "-c", code], cwd=str(WSL), capture_output=True, text=True)
    assert r.returncode == 0, f"probe of {module} did not run: {r.stdout}{r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # A reach for a package that is NOT installed raises after the spy has already seen it, and
    # that is the case worth getting right: it is the same defect as reaching for an installed one,
    # so the sighting wins over the traceback and the caller gets to name it.
    assert out["seen"] or not out["error"], f"probe of {module} failed: {out['error']}"
    return out["seen"]


def test_the_probe_would_notice():
    """The positive control, and the reason it exists is one directory away: without it a probe
    that silently stopped working reads exactly like a backend that behaves. numpy IS imported at
    module scope by kikivoice.py, so a probe that cannot see numpy cannot see torch either."""
    assert _imports_attempted("lektorlib.backends.kikivoice", ("numpy",)) == ["numpy"]


@pytest.mark.parametrize("module", ["lektorlib.backends", "lektorlib.backends.kikivoice"])
def test_importing_it_does_not_reach_for_a_gpu_stack(module):
    """kikivoice is a network backend. Importing it must not cost a 2 GB import, and `lektor.py
    --help` must not need a card present to list the backends it could use."""
    assert _imports_attempted(module, GPU_STACK) == [], f"{module} reached for a GPU stack"


def test_the_registry_does_not_import_the_backends_it_lists():
    """What `get_backend` promises, stated as a property instead of as a docstring: importing the
    registry loads the base class and nothing else. Every concrete engine waits for its branch."""
    code = ("import json, sys; import lektorlib.backends; "
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('lektorlib.'))))")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(WSL), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    loaded = json.loads(r.stdout.strip().splitlines()[-1])
    assert loaded == ["lektorlib.backends", "lektorlib.backends.base"], loaded


def test_importing_the_backend_does_not_import_its_client():
    """_load_client runs in prepare(), not at module scope, and 153 KB of client plus batcher is
    exactly the sort of thing that quietly moves to import time when someone tidies the imports."""
    code = ("import json, sys; import lektorlib.backends.kikivoice; "
            "print(json.dumps(sorted(m for m in sys.modules if 'kikivoice_' in m)))")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(WSL), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == []


def test_the_client_and_the_batcher_both_sit_next_to_the_backend():
    """They travel together or the failure is silent: with only the client copied in, the relative
    import wins, client_dir never reaches sys.path, `import kikivoice_batch` raises, the backend
    swallows it and falls back to one paid task per line – ~25 human GeeTest solves instead of 2."""
    here = BACKEND_SRC.parent
    assert (here / "kikivoice_client.py").is_file()
    assert (here / "kikivoice_batch.py").is_file()


# -- the copies against the checkout they came from --------------------------

# Where the client was taken from. A fact about one development machine, not a knob the pipeline
# turns, so it lives in the environment rather than in the config that ships to everybody else.
UPSTREAM = pathlib.Path(os.environ.get("KIKIVOICE_CHECKOUT", "~/projects/kikivoice")).expanduser()


@pytest.mark.parametrize("name", ["kikivoice_client.py", "kikivoice_batch.py"])
def test_the_copy_still_matches_the_checkout_it_came_from(name):
    """Two copies of a 153 KB client is a decision, not an accident, but nothing about it is
    self-enforcing: the relative import wins, so an edit made in the checkout reaches the standalone
    CLI and never reaches this repository, and the two read the same until the day they do not.
    Skipped where the checkout is absent, because that is a machine without the source, not a drift.
    """
    upstream = UPSTREAM / name
    if not upstream.is_file():
        pytest.skip(f"no kikivoice checkout at {UPSTREAM}")
    mine = BACKEND_SRC.parent / name
    assert mine.read_bytes() == upstream.read_bytes(), (
        f"{name} has drifted from {upstream}. Decide which copy is the source of truth and sync "
        f"the other; until then the standalone CLI and this backend are running different code.")
