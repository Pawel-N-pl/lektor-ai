#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone HTTP client for the kikivoice.ai TTS / voice-cloning API.

Design constraints (deliberate, do not "improve" without reading first):

* Standard library + ``requests`` only. No playwright, no browser automation,
  no TLS/JS fingerprint spoofing. Cloudflare lets a plain client through as
  long as it sends a realistic, honest browser header set; if that ever stops
  working the correct reaction is to stop, not to escalate.
* Identity is a single ``uuid`` cookie (HttpOnly, Max-Age 10 years). The
  server binds both the credit bucket and the GeeTest human-verification
  state to that cookie, so the cookie is persisted to disk and reused.
* GeeTest is NEVER solved programmatically. When the API answers with
  ``error_code 777`` the client prints the verification URL plus a short
  instruction and raises :class:`HumanVerificationRequired`. A human solves
  the captcha once in a real browser; this client simply inherits the result.

Measured facts this code relies on:

* verification budget is ~30 successful ``create-*-task`` calls, independent
  of wall-clock time and concurrency; polling and sample upload do not count;
* credits are charged as ``len(text) * credit_rate``, no fixed fee;
* audio comes back as WAV PCM s16le mono 24000 Hz regardless of what was
  requested, but the container is sniffed anyway because the server has been
  observed ignoring ``audio_format``;
* the clone upload multipart field is ``voice-file`` (not ``file``);
* everything on the cloning path except ``create-new-clone-task`` is ungated.

User-facing CLI messages are in Polish; code, comments and docstrings are in
English on purpose.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

try:
    import fcntl  # POSIX only; used to serialise session-file updates.
except ImportError:  # pragma: no cover - Windows fallback, best effort.
    fcntl = None  # type: ignore[assignment]

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "kikivoice: brak biblioteki 'requests'. Zainstaluj ja: pip install requests\n"
    )
    raise

__all__ = [
    "KikiClient",
    "KikiSession",
    "TaskResult",
    "CloneVoice",
    "Cue",
    "Batch",
    "BatchLimits",
    "Gap",
    "SplitError",
    "plan_batches",
    "find_marker_gaps",
    "split_batch",
    "SEP",
    "PAUSE_MS",
    "BatchPlanError",
    "MARKER_BAND_LOW",
    "MARKER_BAND_HIGH",
    "CUT_MARGIN_MS",
    "KikiError",
    "HumanVerificationRequired",
    "UuidRequired",
    "QuotaExhausted",
    "TransientKikiError",
    "TaskFailed",
    "CloudflareBlocked",
    "sniff_audio",
]

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
#  Endpoints
# ---------------------------------------------------------------------------

BASE_URL = "https://kikivoice.ai"

EP_VOICES = "/jsapi/tts/get-all-tts-voice-data"
EP_CAPS = "/jsapi/tts/tts-model-capabilities"
EP_TTS_CREATE = "/jsapi/tts/create-new-tts-task"
EP_TTS_STATUS = "/jsapi/tts/get-tts-task-status"
EP_CLONE_SIG = "/jsapi/get-cloning-file-sig"
EP_CLONE_CREATE = "/jsapi/create-new-clone-task"
EP_CLONE_STATUS = "/jsapi/get-job-task-status"
# The GeeTest page path is returned by the server as ``validation_url_path``;
# this is only the fallback shape when the server does not send one.
EP_GEETEST_PAGE_FMT = "/jsapi/auth/geetest-validation-page/{public_ip}"

DEFAULT_SESSION_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "kikivoice",
    "session.json",
)

# ---------------------------------------------------------------------------
#  Browser headers
# ---------------------------------------------------------------------------
# One honest, static, realistic Chrome header set. Kept in a single dict so it
# can be refreshed when Chrome's major version moves on, without touching the
# request code. This is NOT fingerprint spoofing: plain requests/urllib3 does
# the TLS handshake, we only avoid presenting ourselves as python-requests.

CHROME_MAJOR = "139"

BASE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": (
        f'"Chromium";v="{CHROME_MAJOR}", "Not;A=Brand";v="99", '
        f'"Google Chrome";v="{CHROME_MAJOR}"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

XHR_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
}

DOC_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
}

MEDIA_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "sec-fetch-dest": "audio",
    "sec-fetch-mode": "no-cors",
}

DEFAULT_REFERER = f"{BASE_URL}/text-to-speech"

# ---------------------------------------------------------------------------
#  Server error map (mirrors the map from the reconnaissance script)
# ---------------------------------------------------------------------------

ERROR_MEANINGS: dict[Any, str] = {
    0: "OK - brak bledu.",
    -1: "Nieokreslony blad wewnetrzny serwera.",
    -2: "INVALID_PARAMS - zle parametry zadania (np. zla nazwa pola multipart).",
    400: "BAD_REQUEST - bledne parametry zadania.",
    401: "UNAUTHORIZED - brak/niepoprawna autoryzacja (wygasl podpis 'sig').",
    403: "FORBIDDEN - dostep zablokowany (Cloudflare albo blokada IP).",
    404: "NOT_FOUND - endpoint nie istnieje (zmieniona sciezka API?).",
    429: "TOO_MANY_REQUESTS - rate limit.",
    500: "INTERNAL_SERVER_ERROR - blad po stronie serwera.",
    503: "SERVICE_TEMPORARILY_UNAVAILABLE - serwer zajety, kolejka pelna.",
    777: "HUMAN_VERIFICATION_REQUIRED - wymagana weryfikacja GeeTest.",
    1001: "INSUFFICIENT_CREDITS - brak wystarczajacej liczby kredytow.",
    1002: "QUOTA_EXCEEDED - przekroczony limit tygodniowy.",
    1003: "TEXT_TOO_LONG - tekst przekracza limit modelu.",
    1004: "VOICE_LIMIT_REACHED - przekroczony limit probek glosu (max 10).",
    2001: "UPLOAD_FAILED - nie udalo sie wgrac pliku audio.",
    2002: "INVALID_AUDIO_FORMAT - nieobslugiwany format audio.",
    2003: "AUDIO_TOO_SHORT - probka za krotka do klonowania.",
    2004: "AUDIO_TOO_LONG - probka za dluga.",
    2005: "ASR_FAILED - nie rozpoznano mowy w probce.",
    3001: "GENERATION_FAILED - model nie wygenerowal audio.",
    3002: "MODEL_UNAVAILABLE - wybrany model niedostepny.",
    3003: "LANGUAGE_UNSUPPORTED - jezyk nieobslugiwany przez wybrany model.",
    "HUMAN_VERIFICATION_REQUIRED": "Wymagana weryfikacja GeeTest.",
    "UUID_REQUIRED": "Brak ciasteczka uuid - klient nie ma tozsamosci.",
    "SERVICE_TEMPORARILY_UNAVAILABLE": "Serwer chwilowo niedostepny.",
    "INSUFFICIENT_CREDITS": "Brak wystarczajacej liczby kredytow.",
    "QUOTA_EXCEEDED": "Przekroczony limit tygodniowy kredytow.",
    "MODEL_VERSION_DEPRECATED": "Ta wersja modelu zostala wylaczona.",
}

JOB_STATE_MEANINGS: dict[int, str] = {
    0: "w kolejce / przetwarzanie",
    1: "gotowe",
    2: "blad zadania po stronie klastra",
    3: "zadanie anulowane",
}

# Error codes that are worth retrying. Everything else is a hard stop:
# repeating a request that the server already rejected on the merits only
# looks like hammering and earns a block.
TRANSIENT_CODES = {500, 502, 503, 504, 429}
TRANSIENT_SUMMARIES = {
    "SERVICE_TEMPORARILY_UNAVAILABLE",
    "SERVER_BUSY",
    "TOO_MANY_REQUESTS",
    "INTERNAL_SERVER_ERROR",
}
QUOTA_CODES = {1001, 1002}
QUOTA_SUMMARIES = {"INSUFFICIENT_CREDITS", "QUOTA_EXCEEDED", "NO_CREDITS"}

# ---------------------------------------------------------------------------
#  Tunables
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
DOWNLOAD_READ_TIMEOUT_S = 120.0
MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 60.0
RETRY_AFTER_CAP_S = 120.0

FIRST_POLL_DELAY_S = 2.5
POLL_INTERVAL_MIN_S = 1.0
POLL_INTERVAL_MAX_S = 5.0
POLL_TIMEOUT_MIN_S = 60.0
POLL_TIMEOUT_PER_CHAR_S = 0.06
POLL_TIMEOUT_OVERHEAD_S = 30.0

VERIFY_BUDGET = 30          # measured threshold of successful create-* calls
VERIFY_WARN_AT = 24

CLONE_TTL_S = 86400         # server-declared ttl of an uploaded sample
CLONE_TTL_MARGIN_S = 3600   # re-upload this long before the ttl actually ends
CLONE_SAMPLE_RATE = 24000   # matches the model output rate; mono s16le
CLONE_MIN_S = 5.0
CLONE_MAX_S = 120.0
ASR_POLL_INTERVAL_S = 2.0
ASR_TIMEOUT_S = 60.0

VOICE_CACHE_TTL_S = 7 * 86400


# ---------------------------------------------------------------------------
#  Exceptions
# ---------------------------------------------------------------------------


class KikiError(Exception):
    """Base class. Carries the raw response body when there is one."""

    def __init__(self, message: str, body: Optional[dict] = None) -> None:
        super().__init__(message)
        self.body = body or {}


class TransientKikiError(KikiError):
    """Something that may plausibly succeed on a retry."""


class HumanVerificationRequired(KikiError):
    """error_code 777. Never retried, never automated."""

    def __init__(
        self,
        message: str,
        body: Optional[dict] = None,
        validation_url: str = "",
        public_ip: str = "",
        uuid_prefix: str = "",
        tasks_since_verification: int = 0,
    ) -> None:
        super().__init__(message, body)
        self.validation_url = validation_url
        self.public_ip = public_ip
        self.uuid_prefix = uuid_prefix
        self.tasks_since_verification = tasks_since_verification

    def instructions_pl(self) -> str:
        """Human-readable Polish instruction block for the terminal."""
        who = f" (cookie uuid ...{self.uuid_prefix})" if self.uuid_prefix else ""
        return (
            "\n"
            "kikivoice: HUMAN_VERIFICATION_REQUIRED (error_code 777).\n"
            f"  Zadan od ostatniej weryfikacji: {self.tasks_since_verification}"
            f" (zmierzony prog to okolo {VERIFY_BUDGET}).\n"
            "\n"
            f"  1. Otworz ponizszy adres w TEJ SAMEJ przegladarce{who},\n"
            "     z ktorej pochodzi zapisane ciasteczko uuid:\n"
            f"     {self.validation_url}\n"
            "  2. Rozwiaz GeeTest recznie. Nic tego nie zrobi za Ciebie.\n"
            "  3. Wroc tutaj i uruchom te sama komende jeszcze raz.\n"
            "\n"
            "  Weryfikacja jest wiazana z ciasteczkiem uuid, nie z adresem IP,\n"
            "  wiec musi to byc ta sama przegladarka i ten sam profil.\n"
        )


class UuidRequired(KikiError):
    """UUID_REQUIRED - configuration error, never retried."""


class QuotaExhausted(KikiError):
    """Out of credits. Hard stop; the weekly bucket resets on its own."""


class CloudflareBlocked(KikiError):
    """Cloudflare stopped letting the plain client through."""


class TaskFailed(KikiError):
    """The cluster accepted the job and then failed it (job_state 2/3)."""


class SampleError(KikiError):
    """The reference sample cannot be used (bad file, too short, too long)."""


# ---------------------------------------------------------------------------
#  Small helpers
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def describe_error(body: Any) -> str:
    """Render a server response into one readable diagnostic line."""
    if not isinstance(body, dict):
        return f"nieznana odpowiedz (nie-slownik): {body!r}"
    code = body.get("error_code", body.get("errcode"))
    summary = body.get("error_summary") or body.get("errmsg") or ""
    msg = body.get("msg") or body.get("message") or ""
    meaning = ERROR_MEANINGS.get(code) or ERROR_MEANINGS.get(summary)
    parts: list[str] = []
    if code is not None:
        parts.append(f"kod={code}")
    if summary:
        parts.append(f"summary={summary}")
    if meaning:
        parts.append(f"znaczenie: {meaning}")
    if msg and msg != summary:
        parts.append(f"msg={msg}")
    if not parts:
        return f"brak rozpoznanego kodu bledu, pelna odpowiedz: {body!r}"
    return " | ".join(parts)


# Container sniffing. The server has been observed returning a container other
# than the one requested, and the URL extension lies, so the bytes decide.
_MP3_FRAME_STARTS = (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa", b"\xff\xe3")


def sniff_audio(data: bytes) -> tuple[str, str]:
    """Return ``(extension, human description)`` based on magic bytes."""
    if len(data) < 12:
        return ".bin", "za malo danych, zeby rozpoznac format"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav", "WAV (RIFF/WAVE)"
    if data[:3] == b"ID3" or data[:2] in (s[:2] for s in _MP3_FRAME_STARTS):
        return ".mp3", "MP3 (ID3 albo ramka MPEG)"
    if data[:4] == b"OggS":
        return ".ogg", "Ogg (Vorbis albo Opus)"
    if data[4:8] == b"ftyp":
        brand = data[8:12].decode("ascii", "replace")
        return ".m4a", f"MP4/M4A (ftyp brand {brand})"
    if data[:4] == b"fLaC":
        return ".flac", "FLAC"
    if data[:4] == b"\x1aE\xdf\xa3":
        return ".webm", "Matroska/WebM"
    if data[:5] == b"<?xml" or data[:1] == b"{" or data[:9].lower() == b"<!doctype":
        return ".txt", "to nie jest audio (XML/JSON/HTML - prawdopodobnie strona bledu)"
    return ".bin", f"nierozpoznany format, pierwsze bajty: {data[:12]!r}"


def _wav_duration_s(data: bytes) -> Optional[float]:
    """Best-effort duration of a canonical PCM WAV, without decoding it."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        import struct

        pos, byte_rate, data_len = 12, 0, 0
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            (csz,) = struct.unpack("<I", data[pos + 4:pos + 8])
            body = data[pos + 8:pos + 8 + csz]
            if cid == b"fmt " and len(body) >= 16:
                byte_rate = struct.unpack("<I", body[8:12])[0]
            elif cid == b"data":
                data_len = min(csz, len(body))
            pos += 8 + csz + (csz & 1)
        if byte_rate:
            return data_len / float(byte_rate)
    except Exception:
        return None
    return None


def _atomic_write(path: str, data: bytes, mode: int = 0o644) -> None:
    """Write a file atomically. A full disk must not leave a truncated file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".kiki-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _which_ffmpeg(name: str = "ffmpeg") -> str:
    exe = shutil.which(name)
    if not exe:
        raise SampleError(
            f"Nie znaleziono '{name}' w PATH. Sciezka klonowania go wymaga "
            "(konwersja probki do WAV mono)."
        )
    return exe


# ---------------------------------------------------------------------------
#  Persistent session state
# ---------------------------------------------------------------------------


class KikiSession:
    """Persistent identity and counters, stored as one JSON file.

    Holds the ``uuid`` cookie (the whole identity), the count of successful
    task creations since the last human verification, the most recent quota
    snapshot, and the cache of uploaded cloning samples.

    Every read-modify-write goes through :meth:`mutate`, which takes an
    ``flock`` on a sidecar lock file, so a pipeline run and a CLI invocation
    can safely touch the file at the same time. A drifting counter means an
    unexpected 777 in the middle of a long render, which is exactly the
    failure this class exists to prevent.
    """

    VERSION = 1

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(
            path
            or os.environ.get("KIKIVOICE_SESSION")
            or DEFAULT_SESSION_PATH
        )
        self.lock_path = self.path + ".lock"
        self._local_lock = threading.RLock()
        self.data: dict[str, Any] = self._blank()
        self.load()

    # -- structure ---------------------------------------------------------

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            "version": KikiSession.VERSION,
            "uuid": "",
            "uuid_source": "",          # "browser" | "minted"
            "verification": {
                "verified_at": None,
                "tasks_since_verification": 0,
                "last_777_at": None,
                # Set when a CALLER stopped itself one task short of the budget and told
                # the human to go and solve GeeTest. See note_verification_prompt().
                "prompted_at": None,
                "public_ip": "",
            },
            "quota": {},
            "clone": {},
            "updated_at": None,
        }

    def _normalise(self, raw: Any) -> dict[str, Any]:
        base = self._blank()
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in ("verification", "quota", "clone"):
                    if isinstance(value, dict):
                        merged = dict(base.get(key) or {})
                        merged.update(value)
                        base[key] = merged
                else:
                    base[key] = value
        return base

    # -- io ----------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        except (ValueError, OSError) as exc:
            sys.stderr.write(
                f"kikivoice: nie moge odczytac {self.path} ({exc}); "
                "zaczynam od pustej sesji.\n"
            )
            raw = {}
        self.data = self._normalise(raw)
        return self.data

    def _save_unlocked(self) -> None:
        self.data["version"] = self.VERSION
        self.data["updated_at"] = _now()
        payload = json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write(self.path, payload, mode=0o600)

    def save(self) -> None:
        """Persist the file under the cross-process lock.

        This deliberately goes through :meth:`mutate`, which re-reads the file
        first, so a caller holding a stale in-memory snapshot cannot roll the
        verification counter back over a value another process wrote. A plain
        "write what I have" save is exactly how a counter drifts, and a drifted
        counter means an unexpected 777 in the middle of a long render.
        """
        self.mutate(lambda _data: None)

    def mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Run ``fn(data)`` under a cross-process lock, then persist."""
        with self._local_lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            lock_fh = None
            if fcntl is not None:
                lock_fh = open(self.lock_path, "a+")
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                self.load()                 # re-read: another process may have written
                result = fn(self.data)
                self._save_unlocked()
                return result
            finally:
                if lock_fh is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                    lock_fh.close()

    # -- convenience accessors --------------------------------------------

    @property
    def uuid(self) -> str:
        return str(self.data.get("uuid") or "")

    @property
    def uuid_source(self) -> str:
        return str(self.data.get("uuid_source") or "")

    @property
    def uuid_prefix(self) -> str:
        u = self.uuid
        return u[-4:] if len(u) >= 4 else u

    @property
    def quota(self) -> dict:
        """Last quota snapshot the server sent, as a plain dict.

        Exposed as an attribute because the lektor-ai adapter reads it that way
        to run its credit gates; without it both gates degrade into silent
        no-ops and the run it was supposed to stop goes ahead. An empty dict
        means "never seen", and every consumer must treat it as unknown rather
        than as zero credits.
        """
        q = self.data.get("quota")
        return dict(q) if isinstance(q, dict) else {}

    @property
    def tasks_since_verification(self) -> int:
        try:
            return int(self.data["verification"].get("tasks_since_verification") or 0)
        except (KeyError, TypeError, ValueError):
            return 0

    @property
    def verification_prompted_at(self):
        """When a caller last stopped ITSELF and sent the human to solve GeeTest.

        Distinct from ``last_777_at``, which records a refusal that really came
        back from the server. The preventive stop never reaches the server, so
        nothing else in the system knows it happened - and without this field
        the counter that triggered it stays where it is forever. See
        :meth:`note_verification_prompt`.
        """
        try:
            return self.data["verification"].get("prompted_at")
        except (KeyError, TypeError):
            return None

    def note_verification_prompt(self) -> None:
        """Record that the caller stopped short of the budget and asked for a solve.

        Written under the same flock as the counter, so a second process that
        starts afterwards sees the hold rather than walking into the same wall.
        """

        def _apply(data: dict[str, Any]) -> None:
            data.setdefault("verification", {})["prompted_at"] = _now()

        self.mutate(_apply)

    def clear_verification_prompt(self) -> int:
        """Release a preventive hold: zero the counter, drop the flag, report the count.

        This is the other half of the fix the previous repair only did halfway.
        ``note_verification_required`` zeroes the counter when a 777 really
        arrives - but with ``verify_reserve = 1`` the PREVENTIVE gate always
        fires first, so the server never gets the chance to answer 777 and
        nothing ever zeroes anything. The user solved the challenge, re-ran the
        command exactly as told, and was stopped again without a single request
        leaving the machine. Measured on the previous build: runs 2 and 3 exited
        75 with zero create-task calls.

        Releasing costs nothing when the human did NOT actually solve it: the
        next create-task answers 777, which takes neither a credit nor a slot,
        and lands in note_verification_required, which zeroes the counter again.
        The counter is a local estimate of a server-side budget, never an
        authority - so when the two disagree, the server has to be the one that
        says no.

        Returns the counter value that was released, or 0 when no hold was set.
        """

        def _apply(data: dict[str, Any]) -> int:
            ver = data.setdefault("verification", {})
            if not ver.get("prompted_at"):
                return 0
            was = int(ver.get("tasks_since_verification") or 0)
            ver["prompted_at"] = None
            ver["tasks_since_verification"] = 0
            ver["verified_at"] = _now()
            return was

        return int(self.mutate(_apply))

    def set_uuid(self, value: str, source: str = "browser") -> None:
        value = (value or "").strip().strip('"')
        if not value:
            raise KikiError("Pusta wartosc ciasteczka uuid.")

        def _apply(data: dict[str, Any]) -> None:
            if data.get("uuid") != value:
                # A different identity means a different verification state
                # and a different credit bucket. Nothing carries over.
                data["verification"] = KikiSession._blank()["verification"]
                data["quota"] = {}
                data["clone"] = {}
            data["uuid"] = value
            data["uuid_source"] = source

        self.mutate(_apply)

    def note_task_created(self, quota: Optional[dict] = None) -> int:
        """Count one successful create-* call. Persisted immediately."""

        def _apply(data: dict[str, Any]) -> int:
            ver = data.setdefault("verification", {})
            count = int(ver.get("tasks_since_verification") or 0)
            if ver.get("last_777_at") or ver.get("prompted_at"):
                # First success after a refusal (or after a preventive stop)
                # proves the human really did solve the challenge. The counter
                # was already zeroed when the 777 arrived (see
                # note_verification_required) or when the hold was released, so
                # this only clears the pending flags and timestamps the proof.
                ver["last_777_at"] = None
                ver["prompted_at"] = None
                ver["verified_at"] = _now()
            count += 1
            ver["tasks_since_verification"] = count
            if quota:
                snap = dict(quota)
                snap["observed_at"] = _now()
                data["quota"] = snap
            return count

        return int(self.mutate(_apply))

    def note_verification_required(self, public_ip: str = "") -> None:
        """Record a 777 and reset the budget counter to zero.

        The reset is the point. ``tasks_since_verification`` means "accepted
        tasks since the last verification event", and a 777 IS a verification
        event: the old budget is spent and the only way forward is a human in a
        browser. Leaving the counter at 30 deadlocks every caller that refuses
        to send once the budget looks exhausted - the user solves the captcha,
        re-runs the same command exactly as told, and is stopped again without
        a single request leaving the machine.

        Zeroing here costs nothing if the human did not actually solve it: the
        next create-task answers 777 again, which is free (no credit, no slot),
        and lands right back in this method. The counter is a local estimate of
        a server-side budget, never an authority.
        """

        def _apply(data: dict[str, Any]) -> None:
            ver = data.setdefault("verification", {})
            ver["last_777_at"] = _now()
            ver["prompted_at"] = None
            ver["tasks_since_verification"] = 0
            if public_ip:
                ver["public_ip"] = public_ip

        self.mutate(_apply)

    def reset_verification(self) -> None:
        def _apply(data: dict[str, Any]) -> None:
            data["verification"] = {
                "verified_at": _now(),
                "tasks_since_verification": 0,
                "last_777_at": None,
                "prompted_at": None,
                "public_ip": data.get("verification", {}).get("public_ip", ""),
            }

        self.mutate(_apply)

    # -- clone sample cache ------------------------------------------------

    def get_clone(self, key: str, margin_s: int = CLONE_TTL_MARGIN_S) -> Optional[dict]:
        entry = (self.data.get("clone") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        uploaded_at = int(entry.get("uploaded_at") or 0)
        ttl = int(entry.get("ttl_s") or CLONE_TTL_S)
        if _now() >= uploaded_at + ttl - margin_s:
            return None
        return entry

    def put_clone(self, key: str, entry: dict) -> None:
        def _apply(data: dict[str, Any]) -> None:
            store = data.setdefault("clone", {})
            store[key] = entry
            # Drop entries that expired long ago so the file stays small.
            stale = [
                k
                for k, v in store.items()
                if isinstance(v, dict)
                and _now() > int(v.get("uploaded_at") or 0) + int(v.get("ttl_s") or CLONE_TTL_S)
            ]
            for k in stale:
                store.pop(k, None)

        self.mutate(_apply)


# ---------------------------------------------------------------------------
#  Result types
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """One finished synthesis job."""

    job_id: str
    audio: bytes
    audio_url: str = ""
    audio_ext: str = ""
    audio_format: str = ""
    deducted_credits: Optional[int] = None
    quota: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    char_count: int = 0
    raw_create: dict = field(default_factory=dict)
    raw_status: dict = field(default_factory=dict)


@dataclass
class CloneVoice:
    """An uploaded reference sample, ready to be used as a cloning source."""

    voice_id: str
    gender: str = "0"
    prompt_text: str = ""
    prompt_text_language: str = "Polish"
    uploaded_at: int = 0
    ttl_s: int = CLONE_TTL_S
    voice_name: str = ""

    @property
    def expires_at(self) -> int:
        return self.uploaded_at + self.ttl_s

    def as_dict(self) -> dict:
        return {
            "voice_id": self.voice_id,
            "gender": self.gender,
            "prompt_text": self.prompt_text,
            "prompt_text_language": self.prompt_text_language,
            "uploaded_at": self.uploaded_at,
            "ttl_s": self.ttl_s,
            "voice_name": self.voice_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CloneVoice":
        return cls(
            voice_id=str(d.get("voice_id") or ""),
            gender=str(d.get("gender") if d.get("gender") is not None else "0"),
            prompt_text=str(d.get("prompt_text") or ""),
            prompt_text_language=str(d.get("prompt_text_language") or "Polish"),
            uploaded_at=int(d.get("uploaded_at") or 0),
            ttl_s=int(d.get("ttl_s") or CLONE_TTL_S),
            voice_name=str(d.get("voice_name") or ""),
        )


# ---------------------------------------------------------------------------
#  The client
# ---------------------------------------------------------------------------


class KikiClient:
    """Thread-safe HTTP client for kikivoice.ai.

    Usage as a library::

        cli = KikiClient()
        voices = cli.list_voices(language="pl")
        audio = cli.tts("Dzien dobry.", voice="S089")
        open("out.wav", "wb").write(audio)

    The ``uuid`` cookie is resolved in this order: explicit ``uuid`` argument,
    ``KIKIVOICE_UUID`` environment variable, session file. It is always sent
    explicitly per request rather than through a shared cookie jar, so a
    stray ``Set-Cookie`` from any endpoint can never silently swap identity.
    """

    def __init__(
        self,
        uuid: Optional[str] = None,
        session_path: Optional[str] = None,
        session: Optional[KikiSession] = None,
        verbose: bool = True,
        max_retries: int = MAX_RETRIES,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        read_timeout: float = READ_TIMEOUT_S,
        referer: str = DEFAULT_REFERER,
        first_poll_delay_s: float = FIRST_POLL_DELAY_S,
        poll_interval_s: Optional[float] = None,
    ) -> None:
        self.session = session or KikiSession(session_path)
        self._uuid_override = (uuid or os.environ.get("KIKIVOICE_UUID") or "").strip()
        self.verbose = verbose
        self.max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.referer = referer
        # How long to wait before the first status poll, and how often after
        # that. poll_interval_s=None means "believe the server's
        # heartbeat_interval_seconds"; a number overrides it. Both are clamped
        # to [POLL_INTERVAL_MIN_S, POLL_INTERVAL_MAX_S] at use time, because
        # 3 s of granularity on a 4-second job wastes half the wall clock and
        # anything under 1 s is just noise on their side.
        self.first_poll_delay_s = max(0.0, float(first_poll_delay_s))
        self.poll_interval_s = None if poll_interval_s is None else float(poll_interval_s)

        self._tls = threading.local()
        self._sessions: list["requests.Session"] = []
        self._state_lock = threading.Lock()
        self._voice_cache: Optional[list[dict]] = None
        self._caps_cache: Optional[dict] = None
        self.last_quota: dict = dict(self.session.data.get("quota") or {})
        self.last_result: Optional[TaskResult] = None
        self.credits_spent: int = 0
        self.tasks_created: int = 0

    # -- plumbing ----------------------------------------------------------

    def say(self, message: str) -> None:
        if self.verbose:
            sys.stderr.write(f"kikivoice: {message}\n")
            sys.stderr.flush()

    @property
    def uuid(self) -> str:
        return self._uuid_override or self.session.uuid

    @property
    def uuid_source(self) -> str:
        if self._uuid_override:
            return "env" if not self.session.uuid else "override"
        return self.session.uuid_source

    def require_uuid(self) -> str:
        u = self.uuid
        if not u:
            raise UuidRequired(
                "Brak ciasteczka uuid. Zdobadz je z przegladarki "
                "(DevTools > Application > Cookies > kikivoice.ai > uuid) i zapisz: "
                "python3 kikivoice_client.py session set-uuid <wartosc>"
            )
        return u

    def _requests_session(self) -> "requests.Session":
        """One ``requests.Session`` per thread. ``Session`` is not thread-safe.

        Every session created here is also registered on the client so that
        :meth:`close` can shut the sockets down; a pool that is built and torn
        down twice per run otherwise leaks a connection per worker until the
        garbage collector gets around to it.
        """
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(BASE_HEADERS)
            s.cookies.clear()
            self._tls.session = s
            with self._state_lock:
                self._sessions.append(s)
        return s

    def close(self) -> None:
        """Close every per-thread HTTP session. Idempotent, safe after use."""
        with self._state_lock:
            sessions, self._sessions = list(self._sessions), []
        for s in sessions:
            try:
                s.close()
            except Exception:  # noqa: BLE001 - tearing down must not raise
                pass
        self._tls = threading.local()

    def _headers(self, url: str, kind: str = "xhr") -> dict[str, str]:
        headers = dict(BASE_HEADERS)
        if kind == "doc":
            headers.update(DOC_HEADERS)
        elif kind == "media":
            headers.update(MEDIA_HEADERS)
        else:
            headers.update(XHR_HEADERS)
        headers["Referer"] = self.referer
        same_site = url.startswith(BASE_URL)
        if kind != "media":
            headers["Origin"] = BASE_URL
        headers["sec-fetch-site"] = "same-origin" if same_site else "cross-site"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        kind: str = "xhr",
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        json_body: Optional[dict] = None,
        send_uuid: bool = True,
        read_timeout: Optional[float] = None,
        allow_retry: bool = True,
        stream: bool = False,
    ) -> "requests.Response":
        """One HTTP call with exponential backoff and jitter.

        Retries cover network failures and 429/5xx only. A 403 gets exactly
        one retry, because a persistent 403 means Cloudflare stopped letting
        the plain client through, and the honest answer to that is to stop.
        """
        cookies = {}
        if send_uuid and self.uuid:
            cookies["uuid"] = self.uuid
        timeout = (self.connect_timeout, read_timeout or self.read_timeout)
        attempts = (self.max_retries if allow_retry else 0) + 1
        last_exc: Optional[Exception] = None

        # When a response tells us how long to wait (Retry-After, or the fixed
        # 5 s pause after a 403) that number REPLACES the backoff instead of
        # being added to it. Sleeping twice for one refusal used to turn a
        # Retry-After of 120 s into 180.
        forced_delay: Optional[float] = None

        for attempt in range(attempts):
            if attempt:
                if forced_delay is not None:
                    delay = forced_delay
                else:
                    delay = min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_CAP_S)
                    delay += random.uniform(0, delay * 0.3)
                self.say(f"ponawiam za {delay:.1f}s (proba {attempt + 1}/{attempts})")
                time.sleep(delay)
            forced_delay = None
            try:
                # multipart bodies must be rebuilt per attempt: file handles
                # are consumed by the previous send.
                http = self._requests_session()
                # The jar must be empty on the way out: a Set-Cookie picked up
                # from any endpoint would otherwise ride along on requests made
                # with send_uuid=False, which is precisely the identity leak
                # the explicit-cookies rule exists to prevent.
                http.cookies.clear()
                resp = http.request(
                    method,
                    url,
                    headers=self._headers(url, kind),
                    cookies=cookies or None,
                    params=params,
                    data=data,
                    files=files() if callable(files) else files,
                    json=json_body,
                    timeout=timeout,
                    stream=stream,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise TransientKikiError(
                        f"Blad sieci przy {method} {url}: {exc}"
                    ) from exc
                continue
            finally:
                try:
                    self._requests_session().cookies.clear()
                except Exception:  # noqa: BLE001 - jar hygiene is best effort
                    pass

            status = resp.status_code
            if status == 403:
                snippet = (resp.text or "")[:200].replace("\n", " ")
                if attempt == 0 and allow_retry:
                    self.say("HTTP 403 (Cloudflare?) - jedna ponowna proba za 5 s")
                    forced_delay = 5.0
                    continue
                raise CloudflareBlocked(
                    "HTTP 403 - Cloudflare przestal przepuszczac zwykly "
                    "klient HTTP. Nie obchodzimy tego. Sprawdz recznie w "
                    f"przegladarce, czy strona dziala. Fragment odpowiedzi: {snippet}"
                )
            if status in TRANSIENT_CODES and allow_retry and attempt + 1 < attempts:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        forced_delay = max(0.0, min(float(retry_after), RETRY_AFTER_CAP_S))
                        self.say(f"HTTP {status}, Retry-After={forced_delay:.0f}s")
                    except ValueError:
                        forced_delay = None
                continue
            if status in TRANSIENT_CODES:
                raise TransientKikiError(
                    f"HTTP {status} przy {method} {url} po {attempts} probach."
                )
            if status >= 400:
                snippet = (resp.text or "")[:300].replace("\n", " ")
                raise KikiError(f"HTTP {status} przy {method} {url}: {snippet}")
            return resp

        raise TransientKikiError(f"Nie udalo sie wykonac {method} {url}: {last_exc}")

    # -- response handling -------------------------------------------------

    def _parse_json(self, resp: "requests.Response") -> dict:
        try:
            body = resp.json()
        except ValueError as exc:
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise KikiError(
                f"Serwer nie zwrocil JSON-a ({resp.url}): {snippet}"
            ) from exc
        if not isinstance(body, dict):
            raise KikiError(f"Nieoczekiwany ksztalt odpowiedzi: {body!r}")
        return body

    def _check(self, body: dict, context: str) -> dict:
        """Raise the right exception for a ``success:false`` envelope.

        kikivoice answers HTTP 200 with ``success:false``, so the retry
        decision is made on ``error_code``, never on the HTTP status.
        """
        code = body.get("error_code", body.get("errcode"))
        summary = str(body.get("error_summary") or body.get("errmsg") or "")
        success = body.get("success")

        ok = (success is True) or (success is None and code in (0, None, "0"))
        if ok and code in (0, None, "0"):
            return body

        if code == 777 or summary == "HUMAN_VERIFICATION_REQUIRED":
            public_ip = str(body.get("public_ip") or "")
            path = str(body.get("validation_url_path") or "")
            if not path and public_ip:
                path = EP_GEETEST_PAGE_FMT.format(public_ip=public_ip)
            url = (BASE_URL + path) if path.startswith("/") else (path or BASE_URL)
            # Read the counter BEFORE recording the 777: note_verification_required
            # zeroes it, and the human wants to be told how many tasks the budget
            # actually lasted, not the zero it was just reset to.
            reached = self.session.tasks_since_verification
            self.session.note_verification_required(public_ip)
            exc = HumanVerificationRequired(
                f"{context}: wymagana weryfikacja GeeTest (777).",
                body=body,
                validation_url=url,
                public_ip=public_ip,
                uuid_prefix=self.session.uuid_prefix or (self.uuid[-4:] if self.uuid else ""),
                tasks_since_verification=reached,
            )
            if self.verbose:
                sys.stderr.write(exc.instructions_pl())
                sys.stderr.flush()
            raise exc

        if summary == "UUID_REQUIRED":
            raise UuidRequired(
                f"{context}: serwer nie widzi ciasteczka uuid. "
                "Ustaw je: python3 kikivoice_client.py session set-uuid <wartosc>",
                body=body,
            )

        if code in QUOTA_CODES or summary in QUOTA_SUMMARIES:
            raise QuotaExhausted(
                f"{context}: {describe_error(body)}. Limit tygodniowy resetuje sie sam.",
                body=body,
            )

        if code in TRANSIENT_CODES or summary in TRANSIENT_SUMMARIES:
            raise TransientKikiError(f"{context}: {describe_error(body)}", body=body)

        raise KikiError(f"{context}: {describe_error(body)}", body=body)

    def _get_json(self, path_or_url: str, **kw) -> dict:
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        resp = self._request("GET", url, **kw)
        return self._parse_json(resp)

    @staticmethod
    def _payload(body: dict) -> dict:
        """Unwrap the ``data`` envelope when there is one."""
        data = body.get("data")
        return data if isinstance(data, dict) else body

    # -- identity ----------------------------------------------------------

    def mint_uuid(self) -> str:
        """Ask the site for a fresh ``uuid`` cookie by loading a page.

        A minted uuid is a brand new identity: it has a full credit bucket but
        NO human verification, so ``create-*-task`` will answer 777 on the
        very first call. Useful for metadata calls and for bootstrapping, not
        for production rendering; verification is inherited only by reusing
        the uuid of a browser where a human actually solved GeeTest.
        """
        resp = self._request("GET", BASE_URL + "/", kind="doc", send_uuid=False)
        value = resp.cookies.get("uuid") or ""
        if not value:
            for cookie in resp.raw.headers.getlist("Set-Cookie") if hasattr(resp.raw, "headers") else []:
                m = re.search(r"\buuid=([^;]+)", cookie)
                if m:
                    value = m.group(1)
                    break
        if not value:
            raise KikiError(
                "Serwer nie ustawil ciasteczka uuid przy renderze strony. "
                "Wez wartosc recznie z DevTools."
            )
        self.session.set_uuid(value, source="minted")
        self._uuid_override = ""
        self.say(f"zmintowano nowe uuid (...{value[-4:]}), zrodlo=minted")
        return value

    def whoami(self) -> dict:
        """Cheap, ungated identity check. Zero credits, zero task budget.

        ``get-cloning-file-sig`` is the only endpoint that both requires the
        uuid cookie and costs nothing, so it is the cheapest probe available.

        Two honest caveats, both measured:

        * the endpoint echoes whatever uuid it was given back as ``user_id``
          and hands out a ``sig`` for it, so a success proves only that the
          cookie reaches the server intact. It does NOT prove the uuid is a
          real, previously seen identity;
        * it says nothing about human verification either, because the whole
          cloning path except task creation is ungated.

        The only conclusive test is :meth:`probe`, which costs one slot of
        the ~30-task budget and about 5 credits.
        """
        self.require_uuid()
        body = self._get_json(EP_CLONE_SIG)
        self._check(body, "whoami")
        return self._payload(body)

    def verification_state(self) -> dict:
        """Local view of the verification budget, plus one cheap live check."""
        state = {
            "uuid_present": bool(self.uuid),
            "uuid_suffix": self.uuid[-4:] if self.uuid else "",
            "uuid_source": self.uuid_source,
            "session_path": self.session.path,
            "tasks_since_verification": self.session.tasks_since_verification,
            "verify_budget": VERIFY_BUDGET,
            "tasks_left": max(0, VERIFY_BUDGET - self.session.tasks_since_verification),
            "last_777_at": self.session.data.get("verification", {}).get("last_777_at"),
            "verified_at": self.session.data.get("verification", {}).get("verified_at"),
            "quota": dict(self.session.data.get("quota") or {}),
            "uuid_accepted": None,
            "user_id": "",
        }
        if state["uuid_present"]:
            try:
                info = self.whoami()
                state["uuid_accepted"] = True
                state["user_id"] = str(info.get("user_id") or "")
            except UuidRequired:
                state["uuid_accepted"] = False
            except KikiError as exc:
                state["uuid_accepted"] = False
                state["error"] = str(exc)
        return state

    def probe(self, text: str = "Test.", voice: str = "S089") -> bool:
        """Spend exactly one task (and ~5 credits) to prove verification.

        Returns ``True`` when the task was accepted. Raises
        :class:`HumanVerificationRequired` when it was not. This is the only
        way to know for sure, and it is deliberately not called by anything
        automatic.
        """
        self.tts(text, voice=voice, download=False)
        return True

    # -- catalogue ---------------------------------------------------------

    def _voice_cache_path(self) -> str:
        return os.path.join(os.path.dirname(self.session.path), "voices.json")

    def list_voices(
        self,
        library: Optional[str] = None,
        language: Optional[str] = None,
        category: Optional[str] = None,
        gender: Optional[str] = None,
        query: Optional[str] = None,
        refresh: bool = False,
    ) -> list[dict]:
        """Flat list of voice records, optionally filtered.

        Filters: ``library`` in {llm, standard}; ``language`` matched as a
        case-insensitive prefix of ``language_hint`` (so ``pl`` matches
        ``pl-PL``); ``category`` matched against ``category_id`` or
        ``category_name``; ``gender`` accepts 0/1 or male/female/m/k;
        ``query`` is a substring of code, id, display name or description.
        """
        voices = self._all_voices(refresh=refresh)
        out = voices

        if library:
            lib = library.strip().lower()
            out = [v for v in out if str(v.get("library_type", "")).lower() == lib]
        if language:
            lang = language.strip().lower()
            out = [
                v for v in out
                if str(v.get("language_hint", "")).lower().startswith(lang)
            ]
        if category:
            cat = category.strip().lower()
            out = [
                v for v in out
                if cat in str(v.get("category_id", "")).lower()
                or cat in str(v.get("category_name", "")).lower()
            ]
        if gender is not None and str(gender) != "":
            g = str(gender).strip().lower()
            wanted = {"0": "0", "m": "0", "male": "0", "mezczyzna": "0",
                      "1": "1", "f": "1", "female": "1", "k": "1", "kobieta": "1"}.get(g)
            if wanted is not None:
                out = [v for v in out if str(v.get("gender", "")) == wanted]
        if query:
            q = query.strip().lower()
            out = [
                v for v in out
                if q in str(v.get("voice_code", "")).lower()
                or q in str(v.get("voice_id", "")).lower()
                or q in str(v.get("display_name", "")).lower()
                or q in str(v.get("description", "")).lower()
                or any(q in str(t).lower() for t in v.get("style_tags") or [])
            ]
        return out

    def _all_voices(self, refresh: bool = False) -> list[dict]:
        with self._state_lock:
            if self._voice_cache is not None and not refresh:
                return self._voice_cache

        cache_path = self._voice_cache_path()
        raw: Optional[dict] = None
        if not refresh:
            try:
                st = os.stat(cache_path)
                if _now() - int(st.st_mtime) < VOICE_CACHE_TTL_S:
                    with open(cache_path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
            except (OSError, ValueError):
                raw = None

        if raw is None:
            # Metadata endpoint: plain GET, no credits, no task budget.
            body = self._get_json(EP_VOICES, send_uuid=bool(self.uuid))
            self._check(body, "katalog glosow")
            raw = body
            try:
                _atomic_write(
                    cache_path,
                    json.dumps(raw, ensure_ascii=False).encode("utf-8"),
                )
            except OSError:
                pass  # cache is an optimisation, never a requirement

        voices = self._flatten_voices(raw)
        with self._state_lock:
            self._voice_cache = voices
        return voices

    @staticmethod
    def _flatten_voices(body: dict) -> list[dict]:
        """Pull every voice record out of the catalogue envelope.

        Written defensively: the shape is ``data.libraries.<name>.voices[]``
        today, but any dict carrying both ``voice_id`` and ``voice_code`` is
        accepted so a server-side reshuffle degrades instead of exploding.
        """
        out: list[dict] = []
        seen: set[str] = set()

        def walk(node: Any, lib_hint: str = "") -> None:
            if isinstance(node, dict):
                if "voice_id" in node and "voice_code" in node:
                    rec = dict(node)
                    rec.setdefault("library_type", lib_hint)
                    key = f"{rec.get('library_type')}::{rec.get('voice_id')}"
                    if key not in seen:
                        seen.add(key)
                        out.append(rec)
                    return
                for key, value in node.items():
                    hint = key if key in ("llm", "standard") else lib_hint
                    hint = str(node.get("library_type") or hint)
                    walk(value, hint)
            elif isinstance(node, list):
                for item in node:
                    walk(item, lib_hint)

        walk(body.get("data", body))
        return out

    def resolve_voice(self, spec: str) -> dict:
        """Find one voice record by code, id, or display-name substring."""
        if isinstance(spec, dict):
            return spec
        needle = str(spec).strip()
        if not needle:
            raise KikiError("Nie podano glosu.")
        voices = self._all_voices()
        low = needle.lower()

        exact = [v for v in voices if str(v.get("voice_code", "")).lower() == low]
        if len(exact) == 1:
            return exact[0]
        exact = [v for v in voices if str(v.get("voice_id", "")).lower() == low]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return exact[0]

        partial = [
            v for v in voices
            if low in str(v.get("display_name", "")).lower()
            or low in str(v.get("voice_id", "")).lower()
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise KikiError(
                f"Nie znam glosu '{needle}'. Lista: "
                "python3 kikivoice_client.py voices --lang pl"
            )
        preview = ", ".join(
            f"{v.get('voice_code')} ({v.get('display_name')})" for v in partial[:8]
        )
        raise KikiError(
            f"Glos '{needle}' jest niejednoznaczny, pasuje {len(partial)}: {preview}"
        )

    def model_capabilities(self, refresh: bool = False) -> dict:
        """``tts-model-capabilities`` payload. Plain GET, free."""
        with self._state_lock:
            if self._caps_cache is not None and not refresh:
                return self._caps_cache
        body = self._get_json(EP_CAPS, send_uuid=bool(self.uuid))
        self._check(body, "model capabilities")
        caps = self._payload(body)
        with self._state_lock:
            self._caps_cache = caps
        return caps

    def default_model_version(self, model_type: str) -> str:
        """The ``model_version_text`` the frontend would send for a model."""
        if not model_type:
            return ""
        try:
            models = (self.model_capabilities().get("models") or {})
            info = models.get(model_type) or {}
            version = str(info.get("default_version") or "")
            return version or "default"
        except KikiError:
            return "default"

    def max_text_length(self, model_type: str = "", library_type: str = "standard") -> int:
        """Server-side text limit for the given model / library.

        The MODEL decides when there is one, whatever library string came with
        it. The limit is a property of the model (measured: kiki_core 1000,
        kiki_pro 500) and the cloning path in particular runs a model while the
        caller's config may still say ``standard``. Letting the library win
        there returns 3000 for a model that refuses anything over 1000, and the
        refusal arrives after the rest of the episode has already paid.
        """
        try:
            caps = self.model_capabilities()
        except KikiError:
            return 1000 if model_type else 3000
        if not model_type:
            return int(caps.get("standard_max_text_length") or 3000)
        limits = caps.get("model_text_limits") or {}
        if model_type in limits:
            return int(limits[model_type])
        info = (caps.get("models") or {}).get(model_type) or {}
        return int(info.get("max_text_length") or 1000)

    def credit_rate(self, model_type: str = "", library_type: str = "standard") -> float:
        """Credits per character. Same rule as above: the model wins when set."""
        try:
            caps = self.model_capabilities()
        except KikiError:
            return 1.0
        if not model_type:
            return float(caps.get("standard_credit_rate") or 1.0)
        info = (caps.get("models") or {}).get(model_type) or {}
        return float(info.get("credit_rate") or 1.0)

    # -- quota -------------------------------------------------------------

    @property
    def quota(self) -> dict:
        """Most recent ``quota_info``.

        The server only reports quota inside a ``create-*-task`` response, so
        this is by definition a snapshot from the last task, not a live read.
        Between runs it is restored from the session file.
        """
        return dict(self.last_quota)

    def _remember_result(self, result: TaskResult) -> None:
        """Publish the last finished task under the state lock.

        ``last_result`` is a convenience for single-threaded callers only, and
        the lock does not change that: with a pool of workers it holds whichever
        task finished last, which is nobody's task in particular. Anything that
        needs ITS OWN result must call tts_task()/clone_task() and read the
        returned object.
        """
        with self._state_lock:
            self.last_result = result

    def _record_quota(self, quota: Optional[dict]) -> None:
        if not isinstance(quota, dict):
            return
        with self._state_lock:
            self.last_quota = dict(quota)
            try:
                self.credits_spent += int(quota.get("deducted_credits") or 0)
            except (TypeError, ValueError):
                pass

    # -- task plumbing -----------------------------------------------------

    def _create_task(self, path: str, form: dict[str, str], context: str) -> dict:
        """POST a multipart create-task request and count it if it succeeded.

        The verification counter is incremented only on a genuine success,
        because a 777 consumes neither a credit nor a slot of the budget.
        """
        self.require_uuid()
        if self.uuid_source == "minted":
            self.say(
                "UWAGA: uuid zostal zmintowany przez skrypt, wiec nigdy nie "
                "przeszedl weryfikacji GeeTest. Spodziewaj sie 777."
            )
        left = VERIFY_BUDGET - self.session.tasks_since_verification
        if left <= VERIFY_BUDGET - VERIFY_WARN_AT:
            if left <= 0:
                self.say(
                    "licznik zadan od ostatniej weryfikacji przekroczyl zmierzony prog "
                    f"({VERIFY_BUDGET}); nastepne 777 jest bardzo prawdopodobne."
                )
            elif left <= VERIFY_BUDGET - VERIFY_WARN_AT:
                self.say(f"zostalo okolo {left} zadan do progu weryfikacji.")

        # requests wants tuples for multipart-without-file bodies
        multipart = {k: (None, "" if v is None else str(v)) for k, v in form.items()}
        # allow_retry=False, and this is the single most expensive line in the
        # file to get wrong. create-*-task is NOT idempotent: the server charges
        # the credits and burns a slot of the ~30-task verification budget the
        # moment it accepts the POST. A read timeout means "the answer got lost",
        # never "nothing happened", so a retry buys the same text a second time.
        # One lost 2000-character batch at credit_rate 2.0 would cost 4000
        # credits extra, i.e. 5% of the weekly bucket, for no audio at all.
        # Retries stay where they are harmless: GETs, polling, downloads.
        # ...with ONE exception, added after a production run stalled on it. A 429 is a REFUSAL:
        # the rate limiter answered, which proves the task was never created, no credits moved and
        # no slot of the verification budget was burned. Retrying that is safe in a way retrying a
        # timeout never is, and without it a rate limit ends the episode instead of delaying it.
        # 5xx is deliberately NOT retried here even though _request calls it transient: a 500 can
        # land after the task was accepted, and then a retry buys the same text twice.
        resp = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                resp = self._request("POST", BASE_URL + path, files=multipart, allow_retry=False)
                break
            except TransientKikiError as exc:
                if "HTTP 429" not in str(exc) or attempt == RATE_LIMIT_RETRIES:
                    raise
                wait = min(RATE_LIMIT_MAX_WAIT_S, 15.0 * (2 ** attempt))
                self.say(f"rate limit (429) - czekam {wait:.0f}s i ponawiam "
                         f"({attempt + 1}/{RATE_LIMIT_RETRIES}). Serwer odmowil, wiec zadanie "
                         f"NIE powstalo i ponowienie niczego nie kupuje drugi raz.")
                time.sleep(wait)
        body = self._parse_json(resp)
        self._check(body, context)
        payload = self._payload(body)
        if not payload.get("job_id"):
            raise KikiError(f"{context}: brak job_id w odpowiedzi: {body!r}", body=body)

        quota = payload.get("quota_info") if isinstance(payload.get("quota_info"), dict) else None
        count = self.session.note_task_created(quota)
        with self._state_lock:
            self.tasks_created += 1
        self._record_quota(quota)
        if quota:
            self.say(
                f"zadanie {payload['job_id'][:8]} przyjete; "
                f"pobrano {quota.get('deducted_credits')} kredytow, "
                f"zostalo {quota.get('available_count')}/{quota.get('max_count')}, "
                f"reset za {quota.get('next_reset_days')} dni; "
                f"zadan od weryfikacji: {count}/{VERIFY_BUDGET}"
            )
        return payload

    def _wait_for_job(
        self,
        job_id: str,
        status_path: str,
        *,
        params: Optional[dict] = None,
        heartbeat_s: float = 3.0,
        timeout_s: float = 120.0,
    ) -> dict:
        """Poll a job until it reports done, fails, or the deadline passes.

        The poll interval comes from ``heartbeat_interval_seconds`` in the
        create response unless the caller pinned ``poll_interval_s`` on the
        client, and either way it is clamped: 3 s of granularity on a
        4-second job wastes half the wall clock, and anything under 1 s is
        just noise on the server.

        The loop is deliberately stubborn about everything except a real
        failure verdict. By the time it runs, the credits are already spent
        and the slot of the ~30-task budget is already gone, so a single odd
        answer must never be allowed to throw the job away: a transient error
        in the body (``SERVICE_TEMPORARILY_UNAVAILABLE`` arrives as HTTP 200)
        and a response with no ``job_state`` at all are both retried until the
        deadline. ``job_state`` 2 and 3 stay a hard failure - that is the
        cluster telling us the job is genuinely dead, and waiting for it to
        change its mind would only waste the deadline.
        """
        query = {"job_id": job_id}
        if params:
            query.update(params)
        raw_interval = self.poll_interval_s if self.poll_interval_s else float(heartbeat_s or 3.0)
        interval = min(max(raw_interval, POLL_INTERVAL_MIN_S), POLL_INTERVAL_MAX_S)
        deadline = time.monotonic() + timeout_s
        last_soft: Optional[str] = None
        soft_failures = 0

        def _nap() -> None:
            time.sleep(min(interval, max(0.05, deadline - time.monotonic())))

        time.sleep(min(self.first_poll_delay_s, max(0.0, deadline - time.monotonic())))
        while True:
            payload: Optional[dict] = None
            state_i = -1
            try:
                body = self._get_json(status_path, params=query)
                self._check(body, f"status zadania {job_id[:8]}")
                payload = self._payload(body)
                state = payload.get("job_state", payload.get("state"))
                try:
                    state_i = int(state)
                except (TypeError, ValueError):
                    state_i = -1
            except TransientKikiError as exc:
                # The job is paid for. A busy status endpoint is not a verdict.
                last_soft = str(exc)
                soft_failures += 1
                if time.monotonic() >= deadline:
                    raise TransientKikiError(
                        f"Zadanie {job_id}: status nie odpowiadal do konca terminu "
                        f"({soft_failures} przejsciowych bledow, ostatni: {last_soft}). "
                        "Kredyty juz poszly; ponowienie zaplacisz drugi raz."
                    ) from exc
                _nap()
                continue

            if state_i == 1:
                return payload
            if state_i in (2, 3):
                raise TaskFailed(
                    f"Zadanie {job_id} zakonczylo sie stanem {state_i} "
                    f"({JOB_STATE_MEANINGS.get(state_i, 'nieznany stan')}). "
                    f"{describe_error(payload)}",
                    body=payload,
                )
            if time.monotonic() >= deadline:
                if state_i == -1:
                    raise TaskFailed(
                        f"Zadanie {job_id}: serwer ani razu nie podal poprawnego "
                        f"job_state do konca terminu ({timeout_s:.0f}s). "
                        f"Ostatnia odpowiedz: {payload!r}",
                        body=payload,
                    )
                raise TransientKikiError(
                    f"Zadanie {job_id} nie skonczylo sie w {timeout_s:.0f}s. "
                    "Kredyty juz poszly; ponowienie zaplacisz drugi raz."
                )
            if state_i == -1:
                # Missing or unparsable job_state: one weird poll is not a
                # failed job. Tolerated exactly like a transient error.
                soft_failures += 1
                last_soft = f"brak job_state w odpowiedzi: {payload!r}"
            _nap()

    def _download_audio(self, url: str) -> tuple[bytes, str, str]:
        """Fetch the produced file and identify the real container."""
        resp = self._request(
            "GET", url, kind="media", send_uuid=False,
            read_timeout=DOWNLOAD_READ_TIMEOUT_S,
        )
        data = resp.content
        ext, desc = sniff_audio(data)
        if ext in (".bin", ".txt"):
            raise KikiError(
                f"Pobrany plik nie wyglada na audio ({desc}); url={url}"
            )
        return data, ext, desc

    @staticmethod
    def _audio_url_of(payload: dict) -> str:
        for key in ("audiourl", "audio_url", "audioUrl", "url", "file_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        raise KikiError(f"Brak adresu audio w odpowiedzi statusu: {payload!r}")

    def _poll_timeout_for(self, text: str) -> float:
        return max(
            POLL_TIMEOUT_MIN_S,
            POLL_TIMEOUT_PER_CHAR_S * len(text) + POLL_TIMEOUT_OVERHEAD_S,
        )

    # -- public: TTS -------------------------------------------------------

    def _voice_record(
        self,
        voice: Any = None,
        voice_code: str = "",
        voice_id: str = "",
        library_type: Optional[str] = None,
    ) -> dict:
        """Resolve whatever the caller gave us into one catalogue-shaped record.

        Three accepted shapes, in order of how much work they cost:

        * ``voice_code`` and/or ``voice_id`` given explicitly - used as-is, no
          catalogue lookup at all. This is what a pipeline that already has the
          identifiers in its config wants: a render must not depend on a
          metadata endpoint being reachable;
        * ``voice`` as a dict - a record someone already resolved;
        * ``voice`` as a string - a code, an id, or a display-name fragment,
          resolved against the catalogue (one cached GET, free).
        """
        if isinstance(voice, dict):
            rec = dict(voice)
            if voice_code:
                rec["voice_code"] = voice_code
            if voice_id:
                rec["voice_id"] = voice_id
        elif voice_code or voice_id:
            rec = {"voice_code": str(voice_code or ""), "voice_id": str(voice_id or "")}
        elif isinstance(voice, str) and voice.strip():
            rec = dict(self.resolve_voice(voice))
        else:
            raise KikiError(
                "Nie podano glosu: uzyj voice='S089' albo voice_code=/voice_id=."
            )
        if library_type:
            rec.setdefault("library_type", library_type)
        return rec

    def tts_task(
        self,
        text: str,
        voice: Any = None,
        *,
        voice_code: str = "",
        voice_id: str = "",
        library_type: Optional[str] = None,
        model_type: Optional[str] = None,
        model_version_text: Optional[str] = None,
        lang_name_code: str = "pl",
        speed: float = 1.0,
        volume: int = 100,
        pitch: str = "1",
        audio_format: str = "wav",
        audio_high_quality: int = 1,
        region: str = "",
        emotion: str = "",
        intensity: str = "",
        download: bool = True,
    ) -> TaskResult:
        """Synthesise ``text`` and return the full :class:`TaskResult`.

        This is the API for anything that has to account for what a render
        cost: the result carries the job id, the audio, the credits the server
        deducted and the quota snapshot that came back with them. :meth:`tts`
        is the same call for callers that only want the bytes.

        The voice may be given as ``voice_code``/``voice_id`` (used verbatim,
        no catalogue lookup), as an already-resolved record dict, or as a
        string to resolve (``S089``, ``standard_30002``, or a unique fragment
        of a display name). ``library_type``/``model_type`` are taken from the
        record when not given.

        Costs ``len(text) * credit_rate`` credits and one slot of the ~30-task
        verification budget. With ``download=False`` the job is created and
        awaited but the file is not fetched, which is what :meth:`probe` uses.
        """
        text = text if text is not None else ""
        if not text.strip():
            raise KikiError("Pusty tekst - nie ma czego syntezowac.")

        record = self._voice_record(voice, voice_code, voice_id, library_type)
        lib = (library_type or record.get("library_type") or "standard").lower()
        if model_type is None:
            model_type = "" if lib == "standard" else "kiki_core"
        if model_version_text is None:
            model_version_text = self.default_model_version(model_type) if model_type else ""

        # Measured against the BILLED length: the server replaces every pause marker with a single
        # space before it applies max_text_length, so a batch can carry markers for free. Guarding
        # on len(text) refuses batches the server accepts.
        limit = self.max_text_length(model_type, lib)
        billed = billed_length(text)
        if billed > limit:
            raise KikiError(
                f"Tekst ma {billed} naliczanych znakow ({len(text)} surowych), a limit dla "
                f"{model_type or 'library standard'} to {limit}. Podziel go."
            )

        if not (record.get("voice_code") or record.get("voice_id")):
            raise KikiError(
                "Glos nie ma ani voice_code, ani voice_id - serwer odrzuci takie zadanie."
            )

        form = {
            "text": text,
            "library_type": lib,
            "voice_code": str(record.get("voice_code") or ""),
            "voice_id": str(record.get("voice_id") or ""),
            "region": region,
            "pitch": str(pitch),
            "audio_format": audio_format,
            "audio_high_quality": str(audio_high_quality),
            "speed": f"{float(speed):.2f}",
            "volume": str(int(volume)),
            "lang_name_code": lang_name_code,
            "model_type": model_type or "",
            "model_version_text": model_version_text or "",
        }
        if emotion:
            form["emotion"] = emotion
        if intensity:
            form["intensity"] = str(intensity)

        rate = self.credit_rate(model_type, lib)
        self.say(
            f"TTS: {len(text)} znakow, glos {form['voice_code'] or form['voice_id']} "
            f"({record.get('display_name')}), biblioteka {lib}, "
            f"model {model_type or 'standard'}, szacunkowy koszt "
            f"{int(round(len(text) * rate))} kredytow"
        )

        started = time.monotonic()
        created = self._create_task(EP_TTS_CREATE, form, "create-new-tts-task")
        status = self._wait_for_job(
            created["job_id"],
            EP_TTS_STATUS,
            params={"library_type": lib},
            heartbeat_s=created.get("heartbeat_interval_seconds", 3),
            timeout_s=self._poll_timeout_for(text),
        )

        audio, ext, desc = b"", "", ""
        url = ""
        if download:
            url = self._audio_url_of(status)
            audio, ext, desc = self._download_audio(url)

        result = TaskResult(
            job_id=created["job_id"],
            audio=audio,
            audio_url=url,
            audio_ext=ext,
            audio_format=desc,
            deducted_credits=(created.get("quota_info") or {}).get("deducted_credits"),
            quota=dict(created.get("quota_info") or {}),
            elapsed_s=time.monotonic() - started,
            char_count=len(text),
            raw_create=created,
            raw_status=status,
        )
        self._remember_result(result)
        self.say(f"gotowe w {result.elapsed_s:.1f}s ({desc or 'nie pobrano pliku'})")
        return result

    def tts(self, text: str, voice: Any = "S089", **kw) -> bytes:
        """:meth:`tts_task` for callers that only want the audio bytes.

        A thin wrapper on purpose: there is exactly one implementation and one
        return convention per method name, so nothing has to guess whether a
        given call site gets bytes or a result object.
        """
        return self.tts_task(text, voice, **kw).audio

    # -- public: cloning ---------------------------------------------------

    @staticmethod
    def _sample_key(raw: bytes, denoise: int, asr: int) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        return f"{digest[:32]}|d{denoise}|a{asr}|sr{CLONE_SAMPLE_RATE}"

    def _convert_sample(self, path: str, trim_s: Optional[float] = None) -> bytes:
        """Normalise any audio file to mono 16-bit PCM WAV for upload.

        Sample-rate choice: 24000 Hz, matching the model's own output rate.
        Anything ffmpeg can open is accepted, so the caller can hand over
        m4a, mp3, opus, flac or whatever else they happen to have.
        """
        if not os.path.isfile(path):
            raise SampleError(f"Nie ma pliku probki: {path}")
        ffmpeg = _which_ffmpeg()
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-vn"]
        if trim_s:
            cmd += ["-t", f"{float(trim_s):.3f}"]
        cmd += [
            "-ac", "1",
            "-ar", str(CLONE_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-f", "wav",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            err = (proc.stderr or b"").decode("utf-8", "replace")[:400]
            raise SampleError(f"ffmpeg nie przekonwertowal probki: {err}")
        wav = proc.stdout

        duration = _wav_duration_s(wav)
        if duration is not None:
            if duration < CLONE_MIN_S:
                raise SampleError(
                    f"Probka ma {duration:.1f}s, a minimum to {CLONE_MIN_S:.0f}s. "
                    "Krotsza probka daje bardzo slaby klon."
                )
            if duration > CLONE_MAX_S:
                raise SampleError(
                    f"Probka ma {duration:.1f}s, a maksimum to {CLONE_MAX_S:.0f}s. "
                    "Uzyj --trim, zeby wziac tylko poczatek."
                )
            self.say(f"probka po konwersji: {duration:.1f}s, mono {CLONE_SAMPLE_RATE} Hz")
        return wav

    def _cloning_sig(self) -> dict:
        """Fetch a one-shot signature plus the microservice URLs."""
        self.require_uuid()
        body = self._get_json(EP_CLONE_SIG)
        self._check(body, "get-cloning-file-sig")
        payload = self._payload(body)
        if not payload.get("sig"):
            raise KikiError(f"Brak 'sig' w odpowiedzi: {body!r}", body=body)
        return payload

    def upload_reference(
        self,
        path: str,
        voice_name: Optional[str] = None,
        denoise: int = 1,
        asr: int = 1,
        trim_s: Optional[float] = None,
        force: bool = False,
        wait_for_asr: bool = True,
    ) -> CloneVoice:
        """Upload a reference sample and wait for its ASR transcript.

        Sample-agnostic on purpose: any readable audio file works, nothing
        about a particular recording is baked in. The result is cached in the
        session file under a hash of the file contents and re-used until the
        server-declared 24 h ttl is nearly up, so repeated runs on the same
        day do not re-upload.

        This whole path is ungated: it costs no credits and no slot of the
        verification budget. Only ``create-new-clone-task`` is gated.
        """
        with open(path, "rb") as fh:
            raw = fh.read()
        key = self._sample_key(raw, denoise, asr)

        if not force:
            cached = self.session.get_clone(key)
            if cached:
                voice = CloneVoice.from_dict(cached)
                left = voice.expires_at - _now()
                self.say(
                    f"uzywam wgranej wczesniej probki {voice.voice_id} "
                    f"(wazna jeszcze {left // 3600} h)"
                )
                return voice

        wav = self._convert_sample(path, trim_s=trim_s)
        name = voice_name or re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(path))[0])[:40]

        sig_info = self._cloning_sig()
        create_url = str(
            sig_info.get("kiki_voice_microservices_api_create_voice_url")
            or "https://custom-voice-upload.kikivoice.ai/create-voice"
        )
        self.say(f"wgrywam probke ({len(wav)} B) jako '{name}'")

        # The multipart field name is 'voice-file'. Sending 'file' returns
        # errcode -2 "invalid params"; this was read out of their own
        # frontend bundle, not guessed.
        def _files() -> dict:
            return {"voice-file": (f"{name}.wav", wav, "audio/wav")}

        resp = self._request(
            "POST",
            create_url,
            params={
                "voice_name": name,
                "denoise": str(denoise),
                "asr": str(asr),
                "sig": sig_info["sig"],
            },
            files=_files,
        )
        body = self._parse_json(resp)
        self._check(body, "create-voice")
        payload = self._payload(body)
        voice_id = str(payload.get("voice_id") or "")
        if not voice_id:
            raise KikiError(f"create-voice nie zwrocil voice_id: {body!r}", body=body)
        gender = str(payload.get("gender") if payload.get("gender") is not None else "0")

        voice = CloneVoice(
            voice_id=voice_id,
            gender=gender,
            uploaded_at=_now(),
            ttl_s=CLONE_TTL_S,
            voice_name=name,
        )
        self.say(f"probka wgrana: voice_id={voice_id}, gender={gender}")

        if wait_for_asr:
            text, lang = self._wait_for_asr(voice_id)
            voice.prompt_text = text
            voice.prompt_text_language = lang or "Polish"

        self.session.put_clone(key, voice.as_dict())
        return voice

    def list_reference_voices(self) -> list[dict]:
        """Server-side list of uploaded samples (ungated, free)."""
        sig_info = self._cloning_sig()
        list_url = str(
            sig_info.get("kiki_voice_microservices_api_voice_list_url")
            or "https://custom-voice-upload.kikivoice.ai/get-voice-list"
        )
        resp = self._request("GET", list_url, params={"sig": sig_info["sig"]})
        body = self._parse_json(resp)
        self._check(body, "get-voice-list")
        payload = self._payload(body)
        items = payload.get("voice_list")
        return items if isinstance(items, list) else []

    def _wait_for_asr(self, voice_id: str) -> tuple[str, str]:
        """Poll the voice list until the ASR transcript shows up.

        The transcript is sometimes ready on the first poll and sometimes
        not; without it the clone quality collapses, so this waits rather
        than proceeding with an empty prompt text.
        """
        deadline = time.monotonic() + ASR_TIMEOUT_S
        attempt = 0
        while True:
            attempt += 1
            for item in self.list_reference_voices():
                if str(item.get("voice_id")) != voice_id:
                    continue
                custom = item.get("custom_data")
                if isinstance(custom, str):
                    try:
                        custom = json.loads(custom or "{}")
                    except ValueError:
                        custom = {}
                if not isinstance(custom, dict):
                    custom = {}
                text = str(custom.get("prompt_text") or "")
                lang = str(custom.get("prompt_text_language") or "")
                if text:
                    self.say(f"transkrypt ASR (proba {attempt}): {text[:120]}")
                    return text, lang
            if time.monotonic() >= deadline:
                raise KikiError(
                    f"ASR nie zwrocil transkryptu probki w {ASR_TIMEOUT_S:.0f}s. "
                    "Sprawdz, czy w probce naprawde slychac mowe."
                )
            time.sleep(ASR_POLL_INTERVAL_S)

    def clone_task(
        self,
        text: str,
        *,
        sample_path: Optional[str] = None,
        voice: Optional[CloneVoice] = None,
        lang_name_code: str = "pl",
        model_type: str = "kiki_core",
        model_version_text: Optional[str] = None,
        speed: float = 1.0,
        volume: int = 100,
        audio_format: str = "wav",
        audio_high_quality: int = 1,
        emotion: str = "",
        intensity: str = "",
        denoise: int = 1,
        asr: int = 1,
        trim_s: Optional[float] = None,
        download: bool = True,
    ) -> TaskResult:
        """Synthesise ``text`` in a cloned voice, returning the full result.

        Pass either an already-uploaded ``voice`` (a :class:`CloneVoice` from
        :meth:`upload_reference`) or a ``sample_path`` to upload-or-reuse.
        Costs ``len(text) * credit_rate`` credits (2.0x on ``kiki_core``) and
        one slot of the verification budget. :meth:`clone` is the same call
        for callers that only want the bytes.
        """
        if not text or not text.strip():
            raise KikiError("Pusty tekst - nie ma czego syntezowac.")
        if voice is None:
            if not sample_path:
                raise KikiError("Podaj --sample albo gotowy obiekt CloneVoice.")
            voice = self.upload_reference(
                sample_path, denoise=denoise, asr=asr, trim_s=trim_s
            )
        if not voice.prompt_text:
            raise KikiError(
                "Brak transkryptu ASR dla probki. Bez niego klon brzmi fatalnie."
            )
        if model_version_text is None:
            model_version_text = self.default_model_version(model_type)

        limit = self.max_text_length(model_type, "llm")
        billed = billed_length(text)          # markers are stripped by the server, see billed_text
        if billed > limit:
            raise KikiError(
                f"Tekst ma {billed} naliczanych znakow ({len(text)} surowych), "
                f"a limit dla {model_type} to {limit}."
            )

        form = {
            "text": text,
            "clone_source_voice_custom_voice_id": voice.voice_id,
            "clone_source_voice_prompt_text": voice.prompt_text,
            "clone_source_voice_prompt_text_language": voice.prompt_text_language,
            "clone_source_voice_gender": str(voice.gender),
            "lang_name_code": lang_name_code,
            "model_type": model_type,
            "model_version_text": model_version_text or "default",
            "speed": f"{float(speed):.2f}",
            "volume": str(int(volume)),
            "audio_format": audio_format,
            "audio_high_quality": str(audio_high_quality),
        }
        if emotion:
            form["emotion"] = emotion
        if intensity:
            form["intensity"] = str(intensity)

        rate = self.credit_rate(model_type, "llm")
        self.say(
            f"KLON: {len(text)} znakow, model {model_type}, szacunkowy koszt "
            f"{int(round(len(text) * rate))} kredytow"
        )

        started = time.monotonic()
        created = self._create_task(EP_CLONE_CREATE, form, "create-new-clone-task")
        status = self._wait_for_job(
            created["job_id"],
            EP_CLONE_STATUS,
            heartbeat_s=created.get("heartbeat_interval_seconds", 3),
            timeout_s=self._poll_timeout_for(text),
        )

        audio, ext, desc, url = b"", "", "", ""
        if download:
            url = self._audio_url_of(status)
            audio, ext, desc = self._download_audio(url)

        result = TaskResult(
            job_id=created["job_id"],
            audio=audio,
            audio_url=url,
            audio_ext=ext,
            audio_format=desc,
            deducted_credits=(created.get("quota_info") or {}).get("deducted_credits"),
            quota=dict(created.get("quota_info") or {}),
            elapsed_s=time.monotonic() - started,
            char_count=len(text),
            raw_create=created,
            raw_status=status,
        )
        self._remember_result(result)
        self.say(f"gotowe w {result.elapsed_s:.1f}s ({desc or 'nie pobrano pliku'})")
        return result

    def clone(self, text: str, **kw) -> bytes:
        """:meth:`clone_task` for callers that only want the audio bytes."""
        return self.clone_task(text, **kw).audio


# ---------------------------------------------------------------------------
#  Batching: grouping cues into one paid request, and cutting the answer up
# ---------------------------------------------------------------------------
#
# One request per subtitle cue is affordable in credits and ruinous in the only
# currency that is actually scarce here: slots of the ~30-task budget between
# two human CAPTCHA solves. A 759-cue episode is ~26 interruptions. Twelve cues
# per request is three.
#
# The trick is to put an audible, unambiguous marker between the cues and cut
# the returned audio on it. Measured facts this rests on (see _audyt/03):
#   * the separator below yields real pauses of 1300-1320 ms, very regular;
#   * pause markers are stripped BEFORE the server charges, so they are free in
#     credits - but they DO count towards max_text_length, so they are counted
#     when planning;
#   * ffmpeg's silencedetect does NOT find these pauses, because a cloned voice
#     keeps playing the reference sample's room tone through them. The detector
#     therefore works on a RELATIVE floor derived from the clip itself.
#
# Nothing here talks to the network. It is pure planning and pure DSP, which is
# also why all of it is testable offline.

PAUSE_MS = 1200
SEP = f" ((⏱️={PAUSE_MS})) "   # U+23F1 U+FE0F, 13 characters in Python

RMS_HOP_MS = 20             # frame size of the envelope, no overlap
FLOOR_PERCENTILE = 90.0     # "loud" reference, robust against a stray peak
FLOOR_OFFSET_DB = -25.0     # a frame this far under the reference counts as silence
MIN_VOICED_MS = 120         # a segment with less than this above the floor is empty
CPS_BATCH = 18.5            # measured chars/second inside a batched read

# -- the acceptance band for a pause marker ---------------------------------
#
# The marker has a KNOWN ordered length (PAUSE_MS in the separator), so the
# test is absolute: a gap is a marker when its measured length falls inside
# [band_low, band_high] * PAUSE_MS. The old relative test ("take the N-1
# longest, then demand the loudest rejected gap be under half the shortest
# accepted one") is gone, because it fails on the shape the server really
# returns - see the numbers below.
#
# Measured on three real server batches (probki/*.wav, 24 kHz mono PCM), with
# the envelope walked at every floor offset in the grid (-20/-25/-30 dB):
#
#   ordered 1200 ms -> real markers      1280 .. 1440 ms  (1.067 .. 1.200 x)
#     t3_duzy_batch.wav, 24 cue, clone:  1280 .. 1360
#     batch_znaczniki.wav, 8 cue, clone: 1280 .. 1400
#     t2_standard_znaczniki.wav, 4 cue:  1320 .. 1440
#   longest NON-marker gap in the same clips:  400 ms (0.333 x)
#     and the project's own measurement over 476 prosodic pauses in real
#     subtitles has median 180, p90 510 and max 700 ms (0.583 x).
#
# So the two populations are separated by the whole band 0.4 s .. 1.28 s, and
# the bounds are placed inside that empty space with margin on both sides:
#
#   band_low  = 0.75 -> 900 ms. That is 200 ms above the worst prosodic pause
#                       ever measured here (700 ms) and 380 ms below the
#                       shortest marker ever measured (1280 ms).
#   band_high = 1.60 -> 1920 ms. That is 480 ms above the longest marker ever
#                       measured (1440 ms) and, more importantly, well below
#                       2560 ms - two markers with a swallowed cue between
#                       them. A batch where the model dropped a line must fail
#                       the count, not pass with a double-length "marker".
#
# The same bounds also make finding 3 of the production audit testable: an
# 800 ms silence pasted onto the head of a clip cannot be mistaken for a
# marker, and it is dropped as an edge run before the band is even applied.
MARKER_BAND_LOW = 0.75
MARKER_BAND_HIGH = 1.60

# How much of the marker's silence stays glued to each segment. The cut is
# made at the EDGES of the gap, not in its middle: the segment starts where
# the marker's silence ends and stops where the next one begins, plus this
# margin so that the attack of the first phoneme and the decay of the last one
# survive the frame quantisation of the envelope (one frame = RMS_HOP_MS).
CUT_MARGIN_MS = 40


class SplitError(KikiError):
    """The gap detector could not produce a result it is sure of.

    Deliberately NOT a transient error: the recovery for this is to re-render
    the batch in smaller pieces, never to ask the same question again.
    """


class BatchPlanError(KikiError):
    """A cue cannot be planned at all, e.g. it alone exceeds the model limit."""


@dataclass
class Cue:
    """One subtitle line, as the batcher sees it."""

    index: int
    text: str
    target_ms: Optional[int] = None
    gap_before_ms: int = 0


@dataclass
class Batch:
    """A group of cues bought as a single task."""

    cues: list = field(default_factory=list)
    text: str = ""
    key: str = ""
    char_count: int = 0

    @property
    def indices(self) -> list:
        return [c.index for c in self.cues]


@dataclass
class BatchLimits:
    """Everything that decides where a batch ends.

    ``char_budget`` is the model's ``max_text_length`` with headroom (0.85 of
    it in the specification): 3000 -> 2550 for the standard library, 2000 ->
    1700 for kiki_multilingual, 1000 -> 850 for kiki_core, which is the clone
    path. It counts the separators too, because the server does.
    """

    char_budget: int = 2550
    max_cues: int = 12
    min_cues: int = 4
    gap_break_ms: int = 1500
    separator: str = SEP
    # The model's real max_text_length, i.e. what the SERVER refuses above. A cue longer than
    # char_budget but shorter than this still fits in one request, so it gets a batch of its own
    # instead of killing the plan: char_budget is our own headroom, not the server's rule. Zero
    # means "same as char_budget", which is the conservative reading when nobody said.
    hard_limit: int = 0


@dataclass
class Gap:
    """A silent stretch, in milliseconds from the start of the clip."""

    start_ms: float
    end_ms: float

    @property
    def dur_ms(self) -> float:
        return self.end_ms - self.start_ms

    @property
    def mid_ms(self) -> float:
        return 0.5 * (self.start_ms + self.end_ms)


# The server does not charge for pause markers and does not count them against
# max_text_length: it replaces each one with a single space before it does either. Measured on
# four real batches, exactly:
#
#     raw 426, 7 markers  -> text 335 + 7  = 342 billed
#     raw 1198, 23 markers-> text 899 + 23 = 922 billed
#     raw 138, 3 markers  -> text  99 + 3  = 102 billed
#     raw 839, 23 markers -> text 540 + 23 = 563 billed
#
# Anything that compares a batch against the model limit, or predicts what a batch will cost, has
# to use this and not len(). A guard on the raw length refuses batches the server would have
# accepted; a cost estimate on the raw length overstates the bill by the markers.
_MARKER_RE = re.compile(r"\s*\(\(\s*[^()]*?=\s*\d+\s*\)\)\s*")


def billed_text(text: str) -> str:
    """The text as the server will see it once the pause markers are gone."""
    return _MARKER_RE.sub(" ", text or "")


def billed_length(text: str) -> int:
    """How many characters the server will charge for, and measure against max_text_length."""
    return len(billed_text(text))


RATE_LIMIT_RETRIES = 4
RATE_LIMIT_MAX_WAIT_S = 120.0


def _retry_after_seconds(resp) -> float | None:
    """Retry-After from a 429, clamped, or None when the server did not say."""
    raw = (resp.headers or {}).get("Retry-After")
    if not raw:
        return None
    try:
        return max(1.0, min(RATE_LIMIT_MAX_WAIT_S, float(raw)))
    except (TypeError, ValueError):
        return None


def plan_batches(cues: Iterable[Cue], limits: Optional[BatchLimits] = None) -> list[Batch]:
    """Group cues into batches that one request can carry.

    Boundaries are chosen from the SUBTITLE GRID, never from the text. The
    procedure is: start with the whole run as one batch, and while some batch is
    over budget, split it at the longest timeline gap inside it. That takes
    exactly as many boundaries as the budget forces and no more, and every one
    of them sits on a gap the subtitle file already had.

    Why that beats "close the batch at any gap >= N ms", which is what this used
    to do: on a measured episode (759 cues, 18 549 characters) the fixed rule
    fired every ~11 cues and produced 72 batches, because a quarter of all cue
    boundaries carry a gap of 1500 ms or more. Ranking the gaps instead produces
    38 batches on the same material - half the requests, and with the free tier
    allowing about 30 tasks per human verification, one fewer CAPTCHA per
    episode. Cache stability is unchanged and was measured the same way:
    lengthening one cue by 30 characters in the middle of the episode moved zero
    boundaries and invalidated exactly one batch.

    Two things stay out of the budget on purpose:

    * separators. The server strips the pause markers before it applies
      max_text_length and before it charges. Measured three times, e.g. 1198
      characters sent with 23 separators billed as 922 and accepted under a
      1000 limit. So ``char_budget`` counts the text the server will actually
      see, and the markers ride along free;
    * cues that are empty after stripping. They never enter a batch, they get an
      empty clip at render time, and they cost nothing. They are skipped rather
      than dropped, so segment N of a batch still maps back to the cue it came
      from - the batch carries the original ``Cue.index`` and nothing is
      renumbered.

    A cue longer than ``char_budget`` but still under ``hard_limit`` goes out
    alone rather than aborting the plan; one longer than ``hard_limit`` cannot
    be sent at all and raises.
    """
    lim = limits or BatchLimits()
    hard = int(lim.hard_limit or lim.char_budget)

    kept: list[Cue] = []
    for cue in cues:
        text = (cue.text or "").strip()
        if not text:
            continue
        if len(text) > hard:
            raise BatchPlanError(
                f"Kwestia {cue.index} ma {len(text)} znakow, a limit modelu to {hard}. "
                f"Zadne zadanie jej nie zmiesci - podziel ja w scenariuszu albo wybierz model "
                f"o wiekszym max_text_length."
            )
        kept.append(Cue(index=cue.index, text=text, target_ms=cue.target_ms,
                        gap_before_ms=cue.gap_before_ms))

    def billed(group: list[Cue]) -> int:
        return sum(len(c.text) for c in group)

    def over(group: list[Cue]) -> bool:
        return billed(group) > lim.char_budget or len(group) > lim.max_cues

    def divide(group: list[Cue]) -> list[list[Cue]]:
        """Split one group until every piece fits, always at its longest inner gap."""
        if len(group) <= 1 or not over(group):
            return [group]
        # Index 0 is not a candidate: a boundary before the first cue splits nothing.
        best = max(range(1, len(group)), key=lambda i: (group[i].gap_before_ms, -i))
        return divide(group[:best]) + divide(group[best:])

    runs: list[list[Cue]] = []
    current: list[Cue] = []
    for cue in kept:
        if len(cue.text) > lim.char_budget:
            # Between our headroom and the server's wall: one request can carry it, alone.
            if current:
                runs.append(current)
                current = []
            runs.append([cue])
            continue
        current.append(cue)
    if current:
        runs.append(current)

    out: list[Batch] = []
    for run in runs:
        for group in divide(run):
            if not group:
                continue
            text = lim.separator.join(c.text for c in group)
            out.append(Batch(
                cues=list(group),
                text=text,
                key=hashlib.sha256(("kikibatch-v1|" + text).encode("utf-8")).hexdigest(),
                char_count=len(text),
            ))
    return out

def _rms_db_envelope(samples, sr: int, hop_ms: int = RMS_HOP_MS) -> tuple[list, int]:
    """Frame energies in dBFS, one value per ``hop_ms``, plus the frame size.

    Plain Python on purpose: this module depends on nothing but the standard
    library and ``requests``, and one pass over a 30-second clip costs a few
    hundredths of a second. ``samples`` is any sequence of floats in [-1, 1].
    """
    hop = max(1, int(round(sr * hop_ms / 1000.0)))
    frames = len(samples) // hop
    out: list[float] = []
    for i in range(frames):
        base = i * hop
        acc = 0.0
        for j in range(base, base + hop):
            v = samples[j]
            acc += v * v
        out.append(20.0 * math.log10(math.sqrt(acc / hop) + 1e-12))
    return out, hop


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[min(max(idx, 0), len(ordered) - 1)]


def _runs_below(rms_db: list, floor_db: float, hop_ms: float) -> list[Gap]:
    """Every maximal run of frames under ``floor_db``, as gaps."""
    gaps: list[Gap] = []
    start = None
    for i, value in enumerate(rms_db):
        if value < floor_db:
            if start is None:
                start = i
        elif start is not None:
            gaps.append(Gap(start * hop_ms, i * hop_ms))
            start = None
    if start is not None:
        gaps.append(Gap(start * hop_ms, len(rms_db) * hop_ms))
    return gaps


def _band_ms(pause_ms: float, band_low: float, band_high: float) -> tuple[float, float]:
    return float(pause_ms) * float(band_low), float(pause_ms) * float(band_high)


def _classify_gaps(
    all_gaps: list,
    total_ms: float,
    lo_ms: float,
    hi_ms: float,
) -> tuple[list, list, list]:
    """Split every detected run of silence into (markers, edges, rejected).

    Edge runs go first and unconditionally. A silence that touches sample 0 or
    the last sample is the engine's own head/tail padding - measured on this
    API: a clone keeps the reference room tone running, so the tail is quiet
    but not digital silence, and its length has nothing to do with the marker.
    Letting it compete with the markers is what made a 660 ms tail pad throw a
    whole paid batch away (production audit, finding 2).
    """
    edges, inner = [], []
    for g in all_gaps:
        if g.start_ms <= 0.0 or g.end_ms >= total_ms - 1e-9:
            edges.append(g)
        else:
            inner.append(g)
    markers = [g for g in inner if lo_ms <= g.dur_ms <= hi_ms]
    rejected = [g for g in inner if not (lo_ms <= g.dur_ms <= hi_ms)]
    return markers, edges, rejected


def find_marker_gaps(
    samples,
    sr: int,
    *,
    pause_ms: float = PAUSE_MS,
    floor_offset_db: float = FLOOR_OFFSET_DB,
    band_low: float = MARKER_BAND_LOW,
    band_high: float = MARKER_BAND_HIGH,
    hop_ms: int = RMS_HOP_MS,
) -> list[Gap]:
    """Pause markers in one batch clip, in time order.

    Two decisions, both of them measured rather than guessed:

    * the floor is RELATIVE - ``percentile(rms_db, 90) + floor_offset_db``.
      An absolute threshold does not work on a cloned voice, which plays the
      reference recording's room tone straight through the pause; measured,
      ffmpeg's silencedetect at -60 dB finds none of these gaps while this
      envelope finds all of them;
    * the acceptance test is ABSOLUTE - the marker's length is ordered in the
      separator and comes back within a few percent, so a gap counts only
      inside ``[band_low, band_high] * pause_ms``. See the band constants for
      the measurements the bounds sit on.

    Silence touching either end of the clip is never a marker and is dropped
    before the band is applied.
    """
    rms_db, _hop = _rms_db_envelope(samples, sr, hop_ms)
    if not rms_db:
        return []
    floor_db = _percentile(rms_db, FLOOR_PERCENTILE) + floor_offset_db
    total_ms = len(rms_db) * float(hop_ms)
    lo_ms, hi_ms = _band_ms(pause_ms, band_low, band_high)
    markers, _edges, _rejected = _classify_gaps(
        _runs_below(rms_db, floor_db, hop_ms), total_ms, lo_ms, hi_ms)
    return markers


def split_batch(
    samples,
    sr: int,
    n_expected: int,
    *,
    texts: Optional[list] = None,
    pause_ms: float = PAUSE_MS,
    floor_offsets_db: Iterable[float] = (-25.0, -20.0, -30.0),
    band_low: float = MARKER_BAND_LOW,
    band_high: float = MARKER_BAND_HIGH,
    cut_margin_ms: float = CUT_MARGIN_MS,
    hop_ms: int = RMS_HOP_MS,
    min_voiced_ms: float = MIN_VOICED_MS,
    cps_batch: float = CPS_BATCH,
    trim_edges: bool = True,
) -> list:
    """Cut one batch into exactly ``n_expected`` segments, or refuse to guess.

    A wrong cut is worse than no cut: it silently puts half of one line at the
    end of another, and nothing downstream can notice. So the count is never
    the only test. A candidate split must also pass:

    * exactly ``n_expected - 1`` gaps inside the absolute marker band, after
      head/tail silence has been discarded. Not "the N-1 longest" - the N-1
      that are the right LENGTH. A 700 ms prosodic pause therefore never
      competes with a 1300 ms marker, and two markers merged around a
      swallowed cue (>= 2560 ms) are refused instead of accepted as one;
    * monotonic, in-range cut points;
    * a plausible length per segment, when ``texts`` is given (skipped for
      texts under 8 characters, where the ratio is wild by nature);
    * at least ``min_voiced_ms`` of material above the floor in every segment.

    WHERE IT CUTS. On the gap's EDGES, not in its middle, keeping
    ``cut_margin_ms`` of the silence on each side. Cutting in the middle gave
    every segment ~650 ms of leading room tone that lektor-ai's ``trim_silence``
    cannot remove - it works at peak - 45 dB while this detector works at
    p90(RMS) - 25 dB, and a cloned voice fills the gap with room tone that
    sits between the two thresholds. The result was every line in the episode
    entering half a second late (production audit, finding 3).

    With ``trim_edges`` the same treatment is applied to the clip's own head
    and tail silence, so segment 0 does not inherit whatever padding the
    engine put in front of the first word.

    The floor offsets are walked in a fixed order and the first one that
    yields exactly N-1 markers and a plausible split wins, so the result is
    reproducible. If none does, :class:`SplitError` is raised with what was
    seen at each offset, and the caller is expected to re-render the batch in
    smaller pieces - never to accept a best-effort cut.
    """
    if n_expected <= 0:
        raise SplitError("n_expected musi byc dodatnie.")

    rms_db, _hop = _rms_db_envelope(samples, sr, hop_ms)
    if not rms_db:
        raise SplitError("Klip jest krotszy niz jedna ramka analizy - nie ma czego ciac.")
    reference_db = _percentile(rms_db, FLOOR_PERCENTILE)
    total_ms = 1000.0 * len(samples) / float(sr)
    env_ms = len(rms_db) * float(hop_ms)
    lo_ms, hi_ms = _band_ms(pause_ms, band_low, band_high)
    wanted = n_expected - 1
    margin = max(0.0, float(cut_margin_ms))
    tried: list[str] = []

    for floor_offset in floor_offsets_db:
        floor_db = reference_db + floor_offset
        all_gaps = _runs_below(rms_db, floor_db, hop_ms)
        markers, edges, rejected = _classify_gaps(all_gaps, env_ms, lo_ms, hi_ms)

        if len(markers) != wanted:
            near = sorted((g.dur_ms for g in rejected), reverse=True)[:6]
            tried.append(
                f"floor{floor_offset:+.0f}dB: {len(markers)} przerw w pasmie "
                f"{lo_ms:.0f}-{hi_ms:.0f} ms, potrzeba {wanted}"
                + (f"; znalezione: {[round(g.dur_ms) for g in markers]}" if markers else "")
                + (f"; odrzucone (najdluzsze): {[round(d) for d in near]}" if near else "")
                + (f"; brzegowe: {[round(g.dur_ms) for g in edges]}" if edges else "")
            )
            continue

        # Cut on the edges of every marker, with the margin kept on both sides.
        head = 0.0
        tail = total_ms
        if trim_edges:
            for g in edges:
                if g.start_ms <= 0.0:
                    head = max(head, min(g.end_ms - margin, total_ms))
                if g.end_ms >= env_ms - 1e-9:
                    tail = min(tail, max(g.start_ms + margin, 0.0))
        head = max(0.0, head)
        tail = min(total_ms, tail)

        bounds: list[tuple[float, float]] = []
        cursor = head
        for g in markers:
            bounds.append((cursor, min(g.start_ms + margin, total_ms)))
            cursor = max(0.0, g.end_ms - margin)
        bounds.append((cursor, tail))

        bad = next((i for i, (a, b) in enumerate(bounds) if b <= a), None)
        if bad is not None:
            tried.append(f"floor{floor_offset:+.0f}dB: segment {bad} wyszedl pusty albo "
                         f"odwrocony ({bounds[bad][0]:.0f}-{bounds[bad][1]:.0f} ms)")
            continue

        segments = [samples[int(a * sr / 1000.0):int(b * sr / 1000.0)] for a, b in bounds]
        note = _judge_segments(segments, bounds, rms_db, floor_db, hop_ms,
                               texts, cps_batch, min_voiced_ms)
        if note:
            tried.append(f"floor{floor_offset:+.0f}dB: {note}")
            continue
        return segments

    raise SplitError(
        f"Detektor nie znalazl pewnego podzialu na {n_expected} segmentow "
        f"(pasmo znacznika {lo_ms:.0f}-{hi_ms:.0f} ms przy zamawianych {pause_ms:.0f} ms, "
        f"klip {total_ms:.0f} ms). Probowano: " + "; ".join(tried)
    )


def _judge_segments(segments, bounds, rms_db, floor_db, hop_ms, texts, cps_batch,
                    min_voiced_ms) -> str:
    """Empty string when the split is plausible, else the reason it is not."""
    for i, (a, b) in enumerate(bounds):
        first = max(0, int(a / hop_ms))
        last = min(len(rms_db), int(b / hop_ms))
        voiced = sum(1 for v in rms_db[first:last] if v >= floor_db) * hop_ms
        if voiced < min_voiced_ms:
            return (f"segment {i} ma tylko {voiced:.0f} ms materialu nad progiem "
                    f"(minimum {min_voiced_ms:.0f} ms)")
    if texts:
        if len(texts) != len(segments):
            return f"podano {len(texts)} tekstow na {len(segments)} segmentow"
        for i, (text, (a, b)) in enumerate(zip(texts, bounds)):
            chars = len((text or "").strip())
            if chars < 8:
                continue
            expected_ms = 1000.0 * chars / cps_batch
            ratio = (b - a) / expected_ms if expected_ms else 0.0
            if not (0.45 <= ratio <= 2.2):
                return (f"segment {i} trwa {b - a:.0f} ms przy {chars} znakach "
                        f"(stosunek {ratio:.2f}, dopuszczalne 0.45-2.20)")
    return ""


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_TEMPFAIL = 75          # EX_TEMPFAIL: retry the same command after GeeTest


def _read_text_arg(args: argparse.Namespace) -> str:
    if getattr(args, "text_file", None):
        if args.text_file == "-":
            return sys.stdin.read()
        with open(args.text_file, "r", encoding="utf-8") as fh:
            return fh.read()
    return args.text or ""


def _write_audio(path: str, data: bytes, ext: str, keep_name: bool) -> str:
    """Write the audio, fixing the extension when the container disagrees."""
    target = path
    have = os.path.splitext(path)[1].lower()
    if ext and have != ext and not keep_name:
        target = os.path.splitext(path)[0] + ext
        if have:
            sys.stderr.write(
                f"kikivoice: serwer zwrocil {ext}, a nie {have or '(brak rozszerzenia)'}; "
                f"zapisuje jako {target} (wylacz przez --keep-name)\n"
            )
    _atomic_write(target, data)
    return target


def _fmt_quota(quota: dict) -> str:
    if not quota:
        return "  brak danych o kredytach (kwota wraca tylko przy tworzeniu zadania)"
    used = quota.get("used_count")
    avail = quota.get("available_count")
    mx = quota.get("max_count")
    reset = quota.get("next_reset_days")
    seen = quota.get("observed_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(seen)) if seen else "?"
    pct = ""
    try:
        pct = f" ({100.0 * float(avail) / float(mx):.0f}% wolne)"
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return (
        f"  uzyte:     {used}\n"
        f"  dostepne:  {avail}{pct}\n"
        f"  maksimum:  {mx}\n"
        f"  reset za:  {reset} dni\n"
        f"  odczyt z:  {when}"
    )


def _cmd_voices(cli: KikiClient, args: argparse.Namespace) -> int:
    language = args.lang
    if language and str(args.library or "").lower() == "llm":
        # voice_is_timbre_only is True for the LLM library (the server says so
        # in caps): language_hint there describes the demo clip, not what the
        # voice can read. The language of an LLM render is chosen at synthesis
        # time by lang_name_code. Filtering on it hides every LLM voice and
        # tells the user there are no Polish ones, which is false.
        sys.stderr.write(
            "kikivoice: --lang nie dotyczy biblioteki llm (glosy sa tam tylko barwa, "
            "jezyk wybiera --lang przy syntezie); ignoruje filtr jezyka.\n"
        )
        language = None
    voices = cli.list_voices(
        library=args.library,
        language=language,
        category=args.category,
        gender=args.gender,
        query=args.query,
        refresh=args.refresh,
    )
    if args.json:
        print(json.dumps(voices[: args.limit] if args.limit else voices,
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    if not voices:
        print("Nic nie pasuje do tych filtrow.")
        return EXIT_OK
    shown = voices[: args.limit] if args.limit else voices
    print(f"{'KOD':<7} {'BIBL':<9} {'JEZYK':<7} {'PLEC':<5} {'NAZWA':<38} KATEGORIA")
    print("-" * 100)
    for v in shown:
        gender = {"0": "M", "1": "K"}.get(str(v.get("gender")), "?")
        print(
            f"{str(v.get('voice_code','')):<7} "
            f"{str(v.get('library_type','')):<9} "
            f"{str(v.get('language_hint','')):<7} "
            f"{gender:<5} "
            f"{str(v.get('display_name',''))[:37]:<38} "
            f"{str(v.get('category_name',''))}"
        )
    if len(shown) < len(voices):
        print(f"... i jeszcze {len(voices) - len(shown)} (uzyj --limit 0, zeby zobaczyc wszystkie)")
    print(f"\nRazem pasuje: {len(voices)}")
    return EXIT_OK


def _cmd_caps(cli: KikiClient, args: argparse.Namespace) -> int:
    caps = cli.model_capabilities(refresh=args.refresh)
    if args.json:
        print(json.dumps(caps, ensure_ascii=False, indent=2))
        return EXIT_OK
    models = caps.get("models") or {}
    print("BIBLIOTEKA STANDARD")
    print(f"  credit_rate:     {caps.get('standard_credit_rate')}")
    print(f"  max_text_length: {caps.get('standard_max_text_length')}")
    print()
    for name, info in models.items():
        if args.model and args.model != name:
            continue
        langs = info.get("supported_languages") or []
        has_pl = "pl" in [str(x).lower() for x in langs]
        print(f"MODEL {name}")
        print(f"  nazwa:            {info.get('name')}")
        print(f"  max_text_length:  {info.get('max_text_length')}")
        print(f"  credit_rate:      {info.get('credit_rate')} ({info.get('credit_rate_display')})")
        print(f"  jezykow:          {len(langs)}   polski: {'TAK' if has_pl else 'NIE'}")
        print(f"  emocje / akcenty: {info.get('supports_emotion')} / {info.get('supports_accent')}")
        if info.get("default_version"):
            versions = info.get("versions") or {}
            active = ", ".join(
                f"{k}{'' if v.get('enabled') else ' (wylaczona)'}"
                for k, v in versions.items()
            )
            print(f"  wersje:           {active}; domyslna: {info.get('default_version')}")
        print()
    notes = caps.get("notes") or {}
    if notes:
        print("UWAGI SERWERA")
        for k, v in notes.items():
            print(f"  {k}: {v}")
    return EXIT_OK


def _cmd_quota(cli: KikiClient, args: argparse.Namespace) -> int:
    quota = cli.quota
    if args.json:
        print(json.dumps(quota, ensure_ascii=False, indent=2))
        return EXIT_OK
    print("KREDYTY (ostatni znany stan)")
    print(_fmt_quota(quota))
    print(
        "\nUwaga: serwer podaje stan kredytow WYLACZNIE w odpowiedzi na "
        "utworzenie zadania,\nwiec powyzsze to migawka z ostatniego zadania, "
        "nie odczyt na zywo."
    )
    return EXIT_OK


def _cmd_verify(cli: KikiClient, args: argparse.Namespace) -> int:
    state = cli.verification_state()
    if args.json and not args.probe:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("TOZSAMOSC")
    print(f"  plik sesji:   {state['session_path']}")
    if not state["uuid_present"]:
        print("  uuid:         BRAK")
        print(
            "\nZdobadz uuid z przegladarki, w ktorej recznie rozwiazales GeeTest:\n"
            "  DevTools > Application > Cookies > https://kikivoice.ai > uuid\n"
            "  (ciasteczko jest HttpOnly, wiec document.cookie go nie pokaze)\n"
            "a potem: python3 kikivoice_client.py session set-uuid <wartosc>"
        )
        return EXIT_CONFIG
    print(f"  uuid:         ...{state['uuid_suffix']}  (zrodlo: {state['uuid_source'] or 'nieznane'})")
    accepted = state["uuid_accepted"]
    print(f"  cookie dociera do serwera: {'TAK' if accepted else 'NIE'}"
          + (f"   user_id={state['user_id']}" if state.get("user_id") else ""))
    if state.get("error"):
        print(f"  blad:         {state['error']}")

    print("\nBUDZET WERYFIKACJI (licznik lokalny)")
    print(f"  zadan od ostatniej weryfikacji: {state['tasks_since_verification']}")
    print(f"  zmierzony prog:                 {state['verify_budget']}")
    print(f"  szacunkowo zostalo:             {state['tasks_left']}")
    if state.get("last_777_at"):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["last_777_at"]))
        print(f"  ostatnie 777:                   {when}  <-- wymagana weryfikacja w przegladarce")

    print("\nKREDYTY")
    print(_fmt_quota(state["quota"]))

    print(
        "\nTo sprawdzenie kosztuje ZERO kredytow i zero zadan, ale dowodzi malo:\n"
        "get-cloning-file-sig odsyla KAZDE uuid z powrotem jako user_id (sprawdzone\n"
        "na zywo), wiec 'TAK' powyzej znaczy tylko tyle, ze ciasteczko dochodzi calo.\n"
        "Czy to prawdziwa, zweryfikowana tozsamosc, rozstrzyga dopiero prawdziwe\n"
        "zadanie: --probe (koszt: 1 zadanie z ~30 i okolo 5 kredytow)."
    )

    if args.probe:
        print("\nWysylam probne zadanie (5 znakow)...")
        cli.probe()
        print("Weryfikacja WAZNA - zadanie przyjete bez 777.")
    return EXIT_OK


def _cmd_whoami(cli: KikiClient, args: argparse.Namespace) -> int:
    info = cli.whoami()
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"user_id:        {info.get('user_id')}")
    print(f"business_type:  {info.get('business_type')}")
    print(f"max_voice_count:{info.get('max_voice_count')}")
    print(f"sig:            {str(info.get('sig'))[:24]}... (jednorazowy)")
    return EXIT_OK


def _cmd_session(cli: KikiClient, args: argparse.Namespace) -> int:
    store = cli.session
    if args.session_cmd == "path":
        print(store.path)
        return EXIT_OK
    if args.session_cmd == "set-uuid":
        store.set_uuid(args.value, source="browser")
        print(f"Zapisano uuid (...{store.uuid_prefix}) w {store.path}")
        print("Licznik weryfikacji i cache probek wyzerowane (nowa tozsamosc).")
        return EXIT_OK
    if args.session_cmd == "mint":
        print(
            "UWAGA: zmintowane uuid to nowa tozsamosc BEZ weryfikacji GeeTest.\n"
            "Nadaje sie do metadanych, nie do generowania."
        )
        cli.mint_uuid()
        return EXIT_OK
    if args.session_cmd == "reset-verify":
        store.reset_verification()
        print("Licznik zadan od weryfikacji wyzerowany.")
        return EXIT_OK
    # show
    data = dict(store.data)
    if data.get("uuid"):
        data["uuid"] = f"...{str(data['uuid'])[-6:]}"
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_tts(cli: KikiClient, args: argparse.Namespace) -> int:
    text = _read_text_arg(args)
    if not text.strip():
        print("Brak tekstu. Podaj --text albo --text-file.", file=sys.stderr)
        return EXIT_CONFIG
    result = cli.tts_task(
        text,
        voice=args.voice,
        library_type=args.library,
        model_type=args.model,
        lang_name_code=args.lang,
        speed=args.speed,
        volume=args.volume,
        audio_format=args.format,
        audio_high_quality=1 if args.hq else 0,
    )
    out = _write_audio(args.out, result.audio, result.audio_ext or ".wav", args.keep_name)
    print(f"Zapisano: {out}  ({len(result.audio)} B, {result.audio_format})")
    if result.deducted_credits is not None:
        print(f"Pobrano kredytow: {result.deducted_credits}")
    return EXIT_OK


def _cmd_clone(cli: KikiClient, args: argparse.Namespace) -> int:
    text = _read_text_arg(args)
    if not text.strip():
        print("Brak tekstu. Podaj --text albo --text-file.", file=sys.stderr)
        return EXIT_CONFIG
    result = cli.clone_task(
        text,
        sample_path=args.sample,
        lang_name_code=args.lang,
        model_type=args.model,
        speed=args.speed,
        volume=args.volume,
        audio_format=args.format,
        audio_high_quality=1 if args.hq else 0,
        denoise=1 if args.denoise else 0,
        trim_s=args.trim,
    )
    out = _write_audio(args.out, result.audio, result.audio_ext or ".wav", args.keep_name)
    print(f"Zapisano: {out}  ({len(result.audio)} B, {result.audio_format})")
    if result.deducted_credits is not None:
        print(f"Pobrano kredytow: {result.deducted_credits}")
    return EXIT_OK


def _cmd_sample(cli: KikiClient, args: argparse.Namespace) -> int:
    """Upload / inspect reference samples. Ungated, free."""
    if args.sample_cmd == "list":
        items = cli.list_reference_voices()
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return EXIT_OK
        if not items:
            print("Brak wgranych probek.")
            return EXIT_OK
        for item in items:
            custom = item.get("custom_data")
            if isinstance(custom, str):
                try:
                    custom = json.loads(custom or "{}")
                except ValueError:
                    custom = {}
            custom = custom if isinstance(custom, dict) else {}
            print(f"{item.get('voice_id')}")
            print(f"  nazwa:      {item.get('voice_name')}")
            print(f"  transkrypt: {str(custom.get('prompt_text') or '(brak)')[:120]}")
            print(f"  jezyk:      {custom.get('prompt_text_language')}")
        return EXIT_OK

    voice = cli.upload_reference(
        args.sample,
        voice_name=args.name,
        denoise=1 if args.denoise else 0,
        trim_s=args.trim,
        force=args.force,
    )
    print(json.dumps(voice.as_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kikivoice_client.py",
        description=(
            "Samodzielny klient HTTP kikivoice.ai (TTS + klonowanie glosu). "
            "Bez playwrighta, bez automatyzacji CAPTCHA."
        ),
        epilog=(
            "Bootstrap tozsamosci:\n"
            "  1. Wygeneruj recznie jedna probke na https://kikivoice.ai/text-to-speech\n"
            "     i rozwiaz GeeTest w swojej normalnej przegladarce.\n"
            "  2. DevTools > Application > Cookies > kikivoice.ai > uuid (HttpOnly).\n"
            "  3. python3 kikivoice_client.py session set-uuid <wartosc>\n"
            "  4. python3 kikivoice_client.py verify\n"
            "\n"
            "Kod wyjscia 75 oznacza: rozwiaz GeeTest w przegladarce i powtorz komende."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--session-file", default=None,
                   help=f"sciezka pliku sesji (domyslnie {DEFAULT_SESSION_PATH})")
    p.add_argument("--uuid", default=None,
                   help="uuid uzyte tylko w tym uruchomieniu (nadpisuje plik sesji)")
    p.add_argument("-q", "--quiet", action="store_true", help="bez logow na stderr")
    p.add_argument("--version", action="version", version=f"kikivoice_client {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("voices", help="katalog glosow, z filtrowaniem")
    sp.add_argument("--library", choices=["llm", "standard"], default=None)
    sp.add_argument("--lang", default=None,
                    help="prefiks jezyka, np. pl, en, es (ignorowane dla --library llm: "
                         "tam glos to sama barwa, a jezyk wybiera sie przy syntezie)")
    sp.add_argument("--category", default=None, help="fragment category_id albo nazwy")
    sp.add_argument("--gender", default=None, help="0/m/male albo 1/k/female")
    sp.add_argument("--query", default=None, help="fragment kodu, id, nazwy, opisu lub tagu")
    sp.add_argument("--limit", type=int, default=40, help="0 = bez limitu")
    sp.add_argument("--refresh", action="store_true", help="pomin cache katalogu")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_voices)

    sp = sub.add_parser("caps", help="mozliwosci modeli (limity, jezyki, mnozniki)")
    sp.add_argument("--model", default=None, help="tylko ten model")
    sp.add_argument("--refresh", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_caps)

    sp = sub.add_parser("quota", help="ostatni znany stan kredytow")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_quota)

    sp = sub.add_parser(
        "verify",
        help="stan tozsamosci i weryfikacji, jednym darmowym zadaniem",
    )
    sp.add_argument("--probe", action="store_true",
                    help="dodatkowo wyslij prawdziwe male zadanie (1 zadanie, ~5 kredytow)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_verify)

    sp = sub.add_parser("whoami", help="surowa odpowiedz get-cloning-file-sig")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_whoami)

    sp = sub.add_parser("session", help="zarzadzanie zapisana tozsamoscia")
    ss = sp.add_subparsers(dest="session_cmd", required=True)
    ss.add_parser("show", help="pokaz plik sesji (uuid skrocone)")
    ss.add_parser("path", help="wypisz sciezke pliku sesji")
    s2 = ss.add_parser("set-uuid", help="zapisz uuid wziete z przegladarki")
    s2.add_argument("value")
    ss.add_parser("mint", help="wygeneruj NOWE uuid (bez weryfikacji GeeTest)")
    ss.add_parser("reset-verify", help="wyzeruj licznik zadan od weryfikacji")
    sp.set_defaults(func=_cmd_session)

    sp = sub.add_parser("tts", help="synteza glosem z katalogu (PALI KREDYTY)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", default=None)
    g.add_argument("--text-file", default=None, help="plik z tekstem, '-' = stdin")
    sp.add_argument("--voice", default="S089", help="kod, id albo fragment nazwy glosu")
    sp.add_argument("--out", required=True)
    sp.add_argument("--library", choices=["llm", "standard"], default=None)
    sp.add_argument("--model", default=None, help="kiki_core | kiki_pro | kiki_multilingual")
    sp.add_argument("--lang", default="pl")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.add_argument("--volume", type=int, default=100)
    sp.add_argument("--format", default="wav")
    sp.add_argument("--hq", action=argparse.BooleanOptionalAction, default=True,
                    help="audio_high_quality=1 (domyslnie); --no-hq wysyla 0")
    sp.add_argument("--keep-name", action="store_true",
                    help="nie poprawiaj rozszerzenia pliku wyjsciowego")
    sp.set_defaults(func=_cmd_tts)

    sp = sub.add_parser("clone", help="synteza klonem z wlasnej probki (PALI KREDYTY)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", default=None)
    g.add_argument("--text-file", default=None, help="plik z tekstem, '-' = stdin")
    sp.add_argument("--sample", required=True, help="dowolny plik audio z probka glosu")
    sp.add_argument("--out", required=True)
    sp.add_argument("--lang", default="pl")
    sp.add_argument("--model", default="kiki_core")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.add_argument("--volume", type=int, default=100)
    sp.add_argument("--format", default="wav")
    sp.add_argument("--hq", action=argparse.BooleanOptionalAction, default=True,
                    help="audio_high_quality=1 (domyslnie); --no-hq wysyla 0")
    sp.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=True,
                    help="odszumianie probki (domyslnie wlaczone); --no-denoise wylacza")
    sp.add_argument("--trim", type=float, default=None, help="wez tylko pierwsze N sekund probki")
    sp.add_argument("--keep-name", action="store_true")
    sp.set_defaults(func=_cmd_clone)

    sp = sub.add_parser("sample", help="wgrywanie i podglad probek referencyjnych (za darmo)")
    ss = sp.add_subparsers(dest="sample_cmd", required=True)
    s2 = ss.add_parser("upload", help="wgraj probke i poczekaj na transkrypt ASR")
    s2.add_argument("sample")
    s2.add_argument("--name", default=None)
    s2.add_argument("--denoise", action=argparse.BooleanOptionalAction, default=True,
                    help="odszumianie probki (domyslnie wlaczone); --no-denoise wylacza")
    s2.add_argument("--trim", type=float, default=None)
    s2.add_argument("--force", action="store_true", help="wgraj mimo waznego cache")
    s3 = ss.add_parser("list", help="lista probek po stronie serwera")
    s3.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_sample)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cli = KikiClient(
        uuid=args.uuid,
        session_path=args.session_file,
        verbose=not args.quiet,
    )
    try:
        return int(args.func(cli, args) or EXIT_OK)
    except HumanVerificationRequired:
        # The instruction block was already printed by _check().
        sys.stderr.write(
            "kikivoice: przerywam. Po rozwiazaniu GeeTest uruchom te sama komende ponownie.\n"
        )
        return EXIT_TEMPFAIL
    except UuidRequired as exc:
        sys.stderr.write(f"kikivoice: {exc}\n")
        return EXIT_CONFIG
    except QuotaExhausted as exc:
        sys.stderr.write(f"kikivoice: {exc}\n")
        return EXIT_ERROR
    except CloudflareBlocked as exc:
        sys.stderr.write(f"kikivoice: {exc}\n")
        return EXIT_ERROR
    except SampleError as exc:
        sys.stderr.write(f"kikivoice: problem z probka: {exc}\n")
        return EXIT_CONFIG
    except KikiError as exc:
        sys.stderr.write(f"kikivoice: {exc}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:
        sys.stderr.write("\nkikivoice: przerwane przez uzytkownika.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
