"""ElevenLabs backend: the one that costs money, so it never generates the same line twice.

Strategy, in order:
  1. Predict the duration from the measured chars/second and ask the API to read that much faster.
  2. Make exactly one call and put the bytes in a content-addressed cache on disk.
  3. If the result still overruns its window, stretch it with atempo off the CACHED bytes.
Step 3 is free. Step 1 exists so step 3 rarely has to do anything audible.

The API key in use is SCOPED to text_to_speech + voices_read: /v1/models and /v1/user return 401,
so nothing here may call them and the plan cannot be auto-detected.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import numpy as np
import requests

from .. import say
from ..audio import decode_mono, shorten_pauses, trim_silence
from ..config import load_cps
from .base import Backend, Rendered, Utterance

TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Only these are worth retrying. 401 (bad key), 402 (out of credits) and 422 (bad voice_settings)
# will fail identically on every attempt, so they must surface at once instead of sleeping 5 times.
# 409 is the concurrency limiter refusing a call it will happily accept a second later.
RETRY_STATUS = (409, 429, 500, 502, 503)

# Container extension per output_format family. Headerless PCM cannot be fed to `ffmpeg -i pipe:0`.
_EXT = {"mp3": "mp3", "opus": "opus"}


def cache_key(text: str, voice_id: str, model_id: str, speed: float, settings: dict,
              prev: str = "", nxt: str = "") -> str:
    """Everything the API was asked for, and therefore everything that decides what came back.

    prev/next are in here because they are in the request: the same sentence read with different
    neighbours is different audio, and a key that ignored them would serve one for the other.
    """
    blob = json.dumps(
        [text, voice_id, model_id, round(speed, 3), settings, prev, nxt],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ElevenLabsBackend(Backend):
    name = "elevenlabs"
    billable = True
    needs_calibration = True
    wants_speed_plan = True    # one call per line, and no way to revise it for free

    def __init__(self, cfg):
        super().__init__(cfg)
        self.v = cfg.backend_cfg("elevenlabs")
        self.timing = cfg["timing"]
        self.sample_rate = cfg["mix"]["sample_rate"]
        self.cache = cfg.cache_dir
        self.api_key = ""
        self.chars_spent = 0
        self.calls = 0
        self._cps = float(cfg["timing"]["chars_per_second"])
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        self.api_key = os.environ.get("ELEVENLABS_API_KEY", "")
        if not self.api_key:
            raise SystemExit(
                "ELEVENLABS_API_KEY is not set (put it in the project .env or export it)"
            )
        if not self.v.get("voice_id"):
            raise SystemExit("voice.elevenlabs.voice_id is empty in the config")
        fmt = self.v["output_format"]
        if fmt.split("_")[0] not in _EXT:
            raise SystemExit(
                f"voice.elevenlabs.output_format {fmt!r} is not a container ffmpeg can read from a "
                f"pipe -- use an mp3_* or opus_* format"
            )
        self._cps = load_cps(self.cfg)

    def set_chars_per_second(self, cps: float) -> None:
        if cps and cps > 0:
            self._cps = float(cps)

    @property
    def max_concurrency(self) -> int:
        return int(self.v.get("concurrency") or self.cfg["voice"]["concurrency"])

    # -- rendering ---------------------------------------------------------

    def _speed_for(self, text: str, target_ms: int | None) -> float:
        """How much faster to ask for, so the line lands inside its window on the first try."""
        if not target_ms or target_ms <= 0:
            return 1.0
        # The API documents 0.7-1.2 and rejects anything outside it, so the config's max_speed is
        # a ceiling on top of the API's, never a way past it. Never below 1.0: a lektor is only
        # ever asked to hurry, never to dawdle.
        hi = min(float(self.timing["max_speed"]), float(self.v.get("speed_max", 1.2)))
        predicted_s = len(text) / self._cps
        return round(min(hi, max(1.0, predicted_s / (target_ms / 1000.0))), 2)

    def _neighbours(self, u: Utterance) -> tuple[str, str]:
        """What to send as previous_text / next_text, or two empty strings if it is switched off."""
        if not bool(self.v.get("send_neighbour_text", True)):
            return "", ""
        return u.prev_text or "", u.next_text or ""

    def _drop_cache(self, text: str, speed: float, model_id: str | None = None,
                    prev: str = "", nxt: str = "") -> None:
        key = cache_key(text, self.v["voice_id"], model_id or self.v["model_id"], speed,
                        self.v["settings"], prev, nxt)
        self._cache_path(key).unlink(missing_ok=True)

    def _cache_path(self, key: str) -> Path:
        d = self.cache / key[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.{_EXT[self.v['output_format'].split('_')[0]]}"

    def fetch(self, text: str, speed: float, model_id: str | None = None,
              prev: str = "", nxt: str = "") -> tuple[bytes, bool]:
        """Cached bytes for one line. Returns (audio, came_from_cache)."""
        model_id = model_id or self.v["model_id"]
        key = cache_key(text, self.v["voice_id"], model_id, speed, self.v["settings"], prev, nxt)
        path = self._cache_path(key)
        if path.exists():
            return path.read_bytes(), True

        settings = dict(self.v["settings"])
        settings["speed"] = round(speed, 3)
        url = TTS_URL.format(voice_id=self.v["voice_id"])
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        body = {"text": text, "model_id": model_id, "voice_settings": settings,
                "previous_text": prev, "next_text": nxt}
        params = {"output_format": self.v["output_format"]}

        retries = int(self.v["max_retries"])
        last = ""
        for attempt in range(retries):
            try:
                r = requests.post(url, headers=headers, params=params, json=body, timeout=180)
            except requests.RequestException as exc:
                last = str(exc)
                _sleep(attempt, retries, None)
                continue
            if r.status_code in RETRY_STATUS:
                last = f"HTTP {r.status_code} {r.text[:200]}"
                # 429 also covers concurrent_limit_exceeded, where the right move is to wait for
                # the in-flight calls rather than to hammer; Retry-After says how long.
                _sleep(attempt, retries, _retry_after(r))
                continue
            if not r.ok:
                raise RuntimeError(f"TTS refused: HTTP {r.status_code} {r.text[:300]}")
            # Written aside and renamed, as piper does: a plain write_bytes leaves a truncated file
            # behind if the disk fills, and two workers rendering duplicate cue texts hash to the
            # same key and would otherwise interleave two different mp3s into one path.
            tmp = path.with_suffix(f"{path.suffix}.{os.getpid():x}.{threading.get_ident():x}.tmp")
            tmp.write_bytes(r.content)
            tmp.replace(path)
            with self._lock:
                self.chars_spent += len(text)
                self.calls += 1
            return r.content, False
        raise RuntimeError(f"TTS failed after {retries} tries: {last}")

    def _finish(self, blob: bytes, atempo: float | None = None) -> np.ndarray:
        """Decode, compress the pauses the engine put inside the line, then trim its ends.

        The order is load-bearing: compressing a pause changes the length of the clip, and the
        trim is what decides where the clip starts and stops. Doing it the other way round trims
        an utterance that is then rewritten underneath the decision.
        """
        audio = decode_mono(self.cfg, blob, self.sample_rate, atempo=atempo)
        return trim_silence(shorten_pauses(self.cfg, audio, self.sample_rate),
                            self.sample_rate, cfg=self.cfg)

    def render(self, u: Utterance) -> Rendered:
        # A speed planned across the whole episode wins over one computed from this line alone:
        # a line at 1.2 between lines at 1.0 is heard as the lektor hurrying and then stopping.
        speed = u.speed_hint if u.speed_hint is not None else self._speed_for(u.text, u.target_ms)
        speed = round(float(speed), 2)
        prev, nxt = self._neighbours(u)
        blob, cached = self.fetch(u.text, speed, prev=prev, nxt=nxt)
        try:
            audio = self._finish(blob)
        except RuntimeError:
            if not cached:
                raise
            # A cache entry that will not decode is a bad write from some earlier run, not a
            # verdict on the text. Left in place it fails every run from here on, so it is dropped
            # and the line is bought once more rather than making the user delete the file by hand.
            say(f"    .. unreadable cache entry for line {u.index} – refetching")
            self._drop_cache(u.text, speed, prev=prev, nxt=nxt)
            blob, cached = self.fetch(u.text, speed, prev=prev, nxt=nxt)
            audio = self._finish(blob)

        # Whatever still does not fit gets stretched locally -- from the cached bytes, so a miss
        # here costs no credits. Trim again afterwards: atempo works on the padded original.
        atempo = 1.0
        if u.target_ms and u.target_ms > 0:
            window_s = u.target_ms / 1000.0
            actual_s = len(audio) / self.sample_rate
            if actual_s > window_s:
                atempo = min(float(self.timing["max_atempo"]), actual_s / window_s)
                if atempo > 1.01:
                    audio = self._finish(blob, atempo=atempo)
                else:
                    atempo = 1.0

        return Rendered(
            audio=audio,
            sample_rate=self.sample_rate,
            speed=speed,
            atempo=atempo,
            cached=cached,
            billable_chars=0 if cached else len(u.text),
        )

    def close(self) -> None:
        if self.calls:
            say(f"    elevenlabs: {self.calls} API calls, {self.chars_spent} billable chars")


def _sleep(attempt: int, retries: int, retry_after: float | None) -> None:
    """Back off before the next attempt. After the last one there is none, so it does not wait:
    a persistent 429 with Retry-After 120 would otherwise burn two minutes on the way to failing."""
    if attempt >= retries - 1:
        return
    time.sleep(retry_after if retry_after is not None else min(45, 2**attempt) + random.random() * 2)


def _retry_after(resp) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(120.0, float(raw)))
    except ValueError:
        return None
