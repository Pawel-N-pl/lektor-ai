"""kikivoice backend: free credits instead of money, and a human in the loop instead of a key.

What makes this engine different from every other backend in lektor-ai:

  * It is not billed in money, it is billed in a WEEKLY CREDIT BUCKET (80 000 credits per 7 days,
    1 credit per character times a per-model rate). There is no way to buy more, so an unattended
    run that burns the bucket costs a week of production rather than a few dollars. That is why
    this backend refuses to start a render it cannot afford and stops hard at a credit floor.
  * Task creation is gated by a GeeTest challenge that a HUMAN has to solve in a real browser,
    roughly every 30 accepted tasks. Nothing here automates that challenge, and nothing here
    touches the TLS/JS fingerprint: when the gate closes the backend stops, prints what the user
    has to do, and exits 75 (EX_TEMPFAIL) so the same command resumes from cache afterwards.
  * The API has NO usable per-call duration control for our purposes. `speed` is an exact linear
    time scalar, measured, which means asking the server to read faster and stretching the cached
    bytes locally with atempo are almost the same operation in the 1.0-1.2 band we actually use
    (spectral-envelope correlation 0.93). So the request is always made at speed 1.0 and the
    window is met LOCALLY, for free, off the cached bytes. A second reading is never bought.

Strategy, in order:
  1. Ask for the line at speed 1.0 exactly once, and put the returned bytes in a content-addressed
     cache on disk.
  2. Decode, compress the pauses, trim the ends.
  3. If it still overruns its window, stretch it with atempo off the CACHED bytes.
Step 3 is free, which is the whole design: with credits this scarce, nothing may be bought twice.

IMPORTING THIS FILE
-------------------
It is written to work from two places without edits:

  a) standalone, in /home/pawel/projects/kikivoice next to kikivoice_client.py. Then
     `from kikivoice_client import ...` resolves because both files sit in the same directory.
     lektorlib itself is NOT importable in that mode, so importing this module standalone fails
     unless /home/pawel/projects/lektor-ai/wsl is on sys.path (or KIKIVOICE_LEKTOR_DIR points at
     it). That is expected: standalone use goes through the CLI in kikivoice_client.py, not here.
  b) copied into lektor-ai/wsl/lektorlib/backends/ together with kikivoice_client.py. Then the
     relative imports `from .kikivoice_client import ...` and `from ..audio import ...` win.

Both directions are tried, relative first, and if neither works the config's
`voice.kikivoice.client_dir` is appended to sys.path and the client import is retried once. No pip
install, no vendoring, no copies of the client kept in two places.

WHAT THIS MODULE USES FROM kikivoice_client.py
----------------------------------------------
This is the surface as the client actually exposes it, verified by test_kikivoice.py, which
builds a client with exactly these arguments and calls exactly these methods against a stubbed
HTTP layer. It is a contract now, not a wish list: if it drifts, that test fails offline, before
anything is spent.

    KikiClient(session_path=None, connect_timeout=, read_timeout=, max_retries=,
               first_poll_delay_s=, poll_interval_s=)
        .session                       -> KikiSession with .uuid, .uuid_source,
                                          .tasks_since_verification, .quota (dict), .save()
        .default_model_version(model_type)                 -> str ("default" when unknown)
        .max_text_length(model_type, library_type)         -> int
        .tts_task(text, *, voice_code, voice_id, library_type, model_type, model_version_text,
                  lang_name_code, speed, volume, pitch, audio_format, audio_high_quality,
                  region="")                               -> TaskResult
        .clone_task(text, *, voice=CloneVoice, lang_name_code, model_type, model_version_text,
                    speed, volume, audio_format, audio_high_quality)  -> TaskResult
        .upload_reference(path, *, denoise=1, asr=1, trim_s=None)     -> CloneVoice
        .close()
    TaskResult: .audio (bytes), .job_id, .audio_url, .deducted_credits (int|None),
                .quota (dict), .elapsed_s (float), .char_count (int)
    CloneVoice: .voice_id, .gender, .prompt_text, .prompt_text_language
    Exceptions:  KikiError, HumanVerificationRequired (.validation_url, .public_ip),
                 UuidRequired, QuotaExhausted, TransientKikiError

The naming convention is one line long and holds in both directions: ``*_task`` returns a
TaskResult, the bare name returns bytes. This adapter needs the credits and the quota out of
every call, so it uses ``*_task`` throughout and never touches ``client.last_result``, which with
a thread pool holds whichever task happened to finish last.

The tolerant accessors below (_need/_opt) stay, but they are a seatbelt, not the design: they
turn a renamed FIELD into a readable message. They cannot catch a renamed METHOD or a changed
signature, which is what actually broke this seam before - only the offline test does that.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import numpy as np

# -- lektorlib imports: package-relative when installed as a backend, flat when not -------------
try:  # (b) copied into lektorlib/backends/
    from .. import say
    from ..audio import decode_mono, shorten_pauses, trim_silence
    from .base import Backend, Rendered, Utterance
except ImportError:  # (a) standalone, or any layout where lektorlib is a top-level package
    _lek = os.environ.get("KIKIVOICE_LEKTOR_DIR", "/home/pawel/projects/lektor-ai/wsl")
    if _lek and _lek not in sys.path and Path(_lek).is_dir():
        sys.path.insert(0, _lek)
    from lektorlib import say
    from lektorlib.audio import decode_mono, shorten_pauses, trim_silence
    from lektorlib.backends.base import Backend, Rendered, Utterance


# The client is imported lazily, because resolving it may need a path from the config, and a
# config is only available once the backend is constructed.
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _load_client(client_dir: str | None):
    """Import kikivoice_client from wherever it happens to live. Cached, thread-safe."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        try:
            from . import kikivoice_client as mod  # copied next to this file inside the package
        except ImportError:
            try:
                import kikivoice_client as mod  # same directory, standalone
            except ImportError:
                d = str(client_dir or "").strip()
                if not d or not Path(d, "kikivoice_client.py").is_file():
                    raise SystemExit(
                        "kikivoice: nie znalazlem kikivoice_client.py.\n"
                        "  Ustaw voice.kikivoice.client_dir na katalog, w ktorym lezy ten plik "
                        "(domyslnie /home/pawel/projects/kikivoice),\n"
                        "  albo skopiuj kikivoice_client.py obok kikivoice_backend.py."
                    ) from None
                if d not in sys.path:
                    sys.path.append(d)
                import kikivoice_client as mod  # noqa: F811
        _CLIENT = mod
        return mod


# ---------------------------------------------------------------------------
# small tolerant accessors
# ---------------------------------------------------------------------------

def _need(obj, *names):
    """First attribute of `obj` that exists, or a readable failure naming what was expected."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    raise SystemExit(
        f"kikivoice: {type(obj).__name__} nie ma zadnego z pol {names!r} - "
        f"kikivoice_client.py rozjechal sie z adapterem (patrz INTEGRACJA.md, sekcja Zalozenia)"
    )


def _opt(obj, name, default=None):
    v = getattr(obj, name, default)
    return default if v is None else v


def _sub(d: dict, key: str) -> dict:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# cache key
# ---------------------------------------------------------------------------

def cache_key(text: str, *, library_type: str, voice_code: str, voice_id: str, model_type: str,
              model_version_text: str, lang_name_code: str, speed: float, volume: str,
              pitch: str, audio_format: str, audio_high_quality: int,
              clone_fingerprint: str = "") -> str:
    """Everything the API was asked for, and therefore everything that decides what came back.

    Deliberately absent: prev/next text. This API has no neighbour-context field at all, so the
    same string always comes back the same way and putting neighbours in the key would only split
    the cache into entries that hold identical audio.

    Deliberately present even though the request never varies it: `speed`. It stays in the key so
    that flipping voice.kikivoice.timing.request_speed_mode away from "fixed" some day cannot
    serve a 1.0 reading for a 1.15 request.

    clone_fingerprint is the transcript + language + gender of the reference sample, NOT the
    custom_voice_id: the id is re-minted every 24 h when the sample is re-uploaded, and keying on
    it would throw the whole episode away once a day for no change in the audio.
    """
    blob = json.dumps(
        ["kikivoice-v1", text, library_type, voice_code, voice_id, model_type,
         model_version_text, lang_name_code, round(float(speed), 3), str(volume), str(pitch),
         audio_format, int(audio_high_quality), clone_fingerprint],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class KikiVoiceBackend(Backend):
    name = "kikivoice"

    # False, and the distinction matters: `billable` in this pipeline means "priced in dollars by
    # pricing.py". stages.py would otherwise value every character at the ElevenLabs rate and
    # print a dollar figure that is simply untrue. The real bill is in credits and this backend
    # reports it itself, in close().
    billable = False

    # False. stage_calibrate spends SIX separate tasks (stages.py:59-66,86) to measure a speaking
    # rate, i.e. a fifth of the ~30-task budget between two human CAPTCHA solves - and it would
    # measure the wrong number anyway: single short calls read at ~16.0 chars/s while a batched
    # read runs 18-20 chars/s, and this backend never needs the prediction in the first place. It
    # asks at speed 1.0 unconditionally and fits the window afterwards from the cached bytes, so
    # there is nothing for a predicted duration to decide. set_chars_per_second() is still
    # implemented, because the number is useful as a plausibility check on a cut segment.
    needs_calibration = False

    # False. A planned per-line speed is worth having only for an engine that must commit to a
    # rate before it hears the result and cannot revise it for free. Here revision IS free, and
    # under batching one request covers many cues, so a per-line plan could not be honoured even
    # in principle. speed_hint therefore arrives as None and render() ignores it.
    #
    # It is safe even if stages.py's second pass hands one over anyway: the request does not
    # change, so the same cached bytes come back, `got < clip.achieved_ms` is false, stages.py
    # discards the result and nothing is spent. This looks like an oversight and is not.
    wants_speed_plan = False

    # Set by prepare(); declared here so a fetch() before prepare() fails with a sentence rather
    # than an AttributeError raised inside a worker thread.
    _mod = None

    def __init__(self, cfg):
        super().__init__(cfg)
        self.v = cfg.backend_cfg("kikivoice")
        self.timing = cfg["timing"]
        self.sample_rate = cfg["mix"]["sample_rate"]
        self.cache = cfg.cache_dir

        self.b_net = _sub(self.v, "net")
        self.b_budget = _sub(self.v, "budget")
        self.b_timing = _sub(self.v, "timing")
        self.b_clone = _sub(self.v, "clone")
        self.b_batch = _sub(self.v, "batch")

        self.mode = str(self.v.get("mode", "library")).strip().lower()
        self.library_type = str(self.v.get("library_type", "standard"))
        self.voice_code = str(self.v.get("voice_code", ""))
        self.voice_id = str(self.v.get("voice_id", ""))
        self.model_type = str(self.v.get("model_type", ""))
        self.lang_name_code = str(self.v.get("lang_name_code", "pl"))
        self.audio_format = str(self.v.get("audio_format", "wav"))
        self.audio_high_quality = int(self.v.get("audio_high_quality", 1))
        # volume and pitch are pinned, and this is not laziness. level_clips (mix.py:409-437)
        # re-levels every cue to the median with a +-4 dB bound, so any constant gain is undone
        # anyway, while volume > 100 risks clipping inside the returned 16-bit PCM, which nothing
        # can take back. pitch moves the formants and destroys speaker identity between lines.
        self.volume = str(self.v.get("volume", "100"))
        self.pitch = str(self.v.get("pitch", "1"))
        self.model_version_text = ""
        self.clone_fingerprint = ""
        self.clone = None

        self.client = None
        self._cps = float(self.v.get("chars_per_second", 18.5))

        # Accounting. chars_spent is what the CONTRACT calls billable_chars (characters), credits
        # is what the SERVICE actually took (deducted_credits from quota_info). They differ by the
        # model's credit_rate and, once batching is in play, by the pause markers the server
        # strips before charging - so neither can be derived from the other and both are tracked.
        self._lock = threading.Lock()
        self.tasks = 0
        self.chars_spent = 0
        self.credits_spent = 0
        self.hits = 0
        self.lines = 0
        self.net_seconds = 0.0
        self.quota = None
        self.max_text_length = 0

        # Tasks that have been authorised but whose answer has not come back yet, and the
        # characters they are going to be charged for. Both are what makes the budget gates mean
        # anything under concurrency: a gate that compares only the counter the LAST finished task
        # left behind lets every worker through at once. See _reserve_task_slot.
        self._inflight = 0
        self._inflight_chars = 0

        # One lock per cache key, so two workers handed the same line do not buy it twice.
        # Repeats are not an edge case in subtitles: "Tak.", "Nie.", "Dziekuje." recur all episode.
        self._key_locks: dict[str, threading.Lock] = {}

        # Cleared the moment anything decides the run must stop (human verification due, credit
        # floor hit). Every worker checks it before it would create a task, so the pool drains
        # instead of firing another 20 requests into a closed gate.
        self._gate = threading.Event()
        self._gate.set()
        self._halt_code = 0
        self._halt_printed = False

        # Optional external batcher (see attach_batcher). None = one request per line.
        self._batcher = None
        self._batched = False

        # prewarm() is a hook base.Backend does not have yet (see INTEGRACJA.md), so on an
        # unpatched lektor-ai it is never called and _preflight would be dead code - i.e. the one
        # moment where the week could still be saved would silently not happen. fetch() therefore
        # runs a reduced version of it, once, on the first line that has to be bought.
        self._preflight_done = False
        self._too_long: list = []

    # -- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        mod = _load_client(self.v.get("client_dir"))
        self._mod = mod

        if self.mode not in ("library", "clone"):
            raise SystemExit(f"voice.kikivoice.mode {self.mode!r} - dozwolone: library, clone")
        if self.mode == "library" and not (self.voice_code and self.voice_id):
            raise SystemExit("voice.kikivoice.voice_code / voice_id sa puste w configu")

        # ONE place decides what model_type really is, and everything downstream reads the
        # normalised value: the credit rate, max_text_length, the cache key and the request.
        #
        # This is finding 19 of the repair audit, which the previous fix only appeared to close.
        # fetch() already sent `model_type or "kiki_core"` for a clone, but _credit_rate() and the
        # max_text_length lookup did not apply the same fallback, and INTEGRACJA.md's own example
        # config ships `"model_type": ""`. Measured on that config: rate 1.0 instead of the real
        # 2.0 (so the credit floor fires half an episode too late) and a limit of 3000 instead of
        # the real 1000 (so preflight waved through 1500 characters that the server then refused
        # mid-render). Normalising here means the number that gates the run and the number that is
        # sent can no longer disagree.
        if self.mode == "clone" and not self.model_type:
            self.model_type = "kiki_core"
            say("    kikivoice: mode=clone bez model_type – przyjmuje kiki_core "
                "(limit znakow i rate 2.0x licza sie od tego)")

        try:
            self.client = mod.KikiClient(
                session_path=self.v.get("session_path") or None,
                connect_timeout=float(self.b_net.get("connect_timeout_s", 10)),
                read_timeout=float(self.b_net.get("read_timeout_s", 60)),
                max_retries=int(self.b_net.get("max_retries", 4)),
                first_poll_delay_s=float(self.b_net.get("first_poll_delay_s", 2.5)),
                poll_interval_s=float(self.b_net.get("poll_interval_s", 1.5)),
            )
        except TypeError as exc:  # constructor signature drifted
            raise SystemExit(
                f"kikivoice: nie moge zbudowac KikiClient ({exc}).\n"
                "  Sygnatura klienta rozjechala sie z adapterem - uruchom "
                "python3 test_kikivoice.py, on sprawdza ten szew offline."
            ) from None

        sess = _need(self.client, "session")
        uuid = str(_opt(sess, "uuid", "") or "")
        if not uuid:
            raise SystemExit(
                "kikivoice: brak ciasteczka uuid.\n"
                "  Wygeneruj raz cokolwiek recznie na https://kikivoice.ai/text-to-speech, "
                "skopiuj cookie `uuid`\n"
                "  (DevTools -> Application -> Cookies, jest HttpOnly) i wpisz je:\n"
                "      python kikivoice_client.py session set-uuid <wartosc>\n"
                "  albo KIKIVOICE_UUID=<wartosc> w .env projektu."
            )
        # A uuid this script minted for itself was never in a browser, so no human ever solved a
        # challenge for it and it can never be verified. Metadata calls work with it; rendering
        # does not, and finding that out after a 4-minute wait is the wrong place to find it out.
        if str(_opt(sess, "uuid_source", "browser")) == "minted":
            raise SystemExit(
                "kikivoice: uuid jest typu `minted` (zmintowane przez skrypt).\n"
                "  Taka tozsamosc nigdy nie przejdzie weryfikacji - potrzebne jest uuid z "
                "przegladarki, w ktorej czlowiek rozwiazal GeeTest."
            )

        self._release_verification_hold(sess)

        # model_version_text is a server-side default that can change under us; it goes into the
        # cache key, so it is read once here and then held for the whole run - reading it per line
        # would let a mid-run change split the episode across two cache generations.
        if self.model_type:
            try:
                self.model_version_text = str(
                    self.client.default_model_version(self.model_type) or "default")
            except Exception as exc:  # noqa: BLE001 - metadata is a nicety, not a precondition
                # "default" and not "": the empty string is a value the server accepts and
                # interprets, and it is not the same value the frontend sends. Falling back to it
                # silently risks MODEL_VERSION_DEPRECATED, or worse, a different model reading the
                # episode - and it splits the cache across two generations for nothing.
                self.model_version_text = "default"
                say(f"    kikivoice: nie odczytalem model_version_text ({exc}) – "
                    f"jade z 'default'")

        # The model's own character limit, read once. It goes into every batching decision and
        # into the dry run in _preflight; a line over the limit must be found before the rest of
        # the episode has already paid, not after.
        try:
            self.max_text_length = int(
                self.v.get("max_text_length")
                or self.client.max_text_length(self.model_type, self.library_type))
        except Exception as exc:  # noqa: BLE001 - caps is metadata, the render is not
            self.max_text_length = 1000 if self.model_type else 3000
            say(f"    kikivoice: nie odczytalem max_text_length ({exc}) – "
                f"przyjmuje {self.max_text_length}")

        if self.mode == "clone":
            self._prepare_clone()

        # An empty dict means "never seen a task response yet", which is NOT the same as
        # "no credits" and must not be allowed to look like a number to the gates.
        self.quota = _opt(sess, "quota", None) or None

        # Attach the batcher here rather than making the caller do it. Without one this backend
        # sends one request per line, and on a 759-cue episode that is 759 tasks against a budget
        # of ~30 per human verification - about 25 CAPTCHAs. With one it is 47 tasks and 2. The
        # batcher is what makes this engine usable at all, so it is the default, and the config
        # turns it OFF explicitly rather than ON.
        if self.b_batch.get("enabled", True) and self._batcher is None:
            try:
                pause_ms = float(self.b_batch.get("pause_ms", 1200))
                # The batcher lives in its own module: it needs numpy and an ffmpeg binary to cut
                # the returned audio, and the client deliberately does not. _load_client has
                # already put client_dir on sys.path by the time we get here.
                try:
                    from . import kikivoice_batch as bmod  # copied into the package
                except ImportError:
                    import kikivoice_batch as bmod  # found via client_dir
                self._batcher = bmod.KikiBatcher(
                    limits=mod.BatchLimits(
                        char_budget=int(self.b_batch.get("char_budget", 850)),
                        hard_limit=int(self.max_text_length or 0),
                        max_cues=int(self.b_batch.get("max_cues", 40)),
                        separator=f" ((\u23f1\ufe0f={int(pause_ms)})) ",
                    ),
                    separator=f" ((\u23f1\ufe0f={int(pause_ms)})) ",
                    pause_ms=pause_ms,
                    cut_margin_ms=float(self.b_batch.get("cut_margin_ms", 40)),
                    max_cues=int(self.b_batch.get("max_cues", 40)),
                    log=say,
                )
            except Exception as exc:  # noqa: BLE001 - a missing batcher is slow, not wrong
                say(f"    kikivoice: nie zbudowalem batchera ({exc}) – jade linia po linii, "
                    f"co przy tym odcinku znaczy okolo 25 recznych weryfikacji")

        self._report_budget_header()

    def _release_verification_hold(self, sess) -> None:
        """Let a run that stopped ITSELF at the verification budget start again.

        Finding 6 of the repair audit, the half that was missing. The counter is zeroed when a 777
        really comes back from the server - but with verify_reserve = 1 the preventive gate in
        reserve_task always fires FIRST, so the server never gets asked and nothing ever zeroes
        anything. Measured on the previous build: the run stopped after 29 bought lines, and runs
        2 and 3 exited 75 without a single create-task leaving the machine. The counter sat at 29
        forever, and the only escape was `session reset-verify` - which the message actively told
        the user not to use.

        The hold is written by reserve_task when it stops, and released here, once, at the start
        of the NEXT run. Releasing is safe even if the human ignored the message: the next
        create-task then answers 777, which costs neither a credit nor a slot, and that path zeroes
        the counter on its own. A local estimate must never be able to out-vote the server.
        """
        if sess is None:
            return
        release = getattr(sess, "clear_verification_prompt", None)
        if not callable(release):
            # An older client without the API. Say so rather than deadlocking in silence.
            if int(_opt(sess, "tasks_since_verification", 0) or 0) >= \
                    int(self.b_budget.get("verify_budget", 30)) - \
                    int(self.b_budget.get("verify_reserve", 1)):
                say("    kikivoice: !! licznik weryfikacji stoi na progu, a ten klient nie ma "
                    "clear_verification_prompt – jesli render nie ruszy, uzyj "
                    "'python3 kikivoice_client.py session reset-verify'")
            return
        was = int(release() or 0)
        if was:
            say(f"    kikivoice: poprzedni run zatrzymal sie sam na {was} zadaniach i poprosil "
                f"o GeeTest – zwalniam licznik i ruszam dalej")

    def _prepare_clone(self) -> None:
        sample = str(self.b_clone.get("sample_path", "")).strip()
        if not sample:
            raise SystemExit("voice.kikivoice.clone.sample_path jest puste, a mode = clone")
        p = Path(sample).expanduser()
        if not p.is_file():
            raise SystemExit(f"voice.kikivoice.clone.sample_path: nie ma pliku {p}")
        trim = self.b_clone.get("trim_s")
        try:
            self.clone = self.client.upload_reference(
                str(p),
                denoise=int(self.b_clone.get("denoise", 1)),
                asr=int(self.b_clone.get("asr", 1)),
                trim_s=float(trim) if trim else None,
            )
        except AttributeError as exc:  # the client, not the sample
            raise SystemExit(
                f"kikivoice: kikivoice_client.py nie ma metody, ktorej potrzebuje adapter "
                f"({exc}). Uruchom python3 test_kikivoice.py."
            ) from None
        except Exception as exc:  # noqa: BLE001 - surfaced with the path that caused it
            raise SystemExit(f"kikivoice: upload probki {p} nie wyszedl: {exc}") from None
        prompt = str(_need(self.clone, "prompt_text"))
        lang = str(_opt(self.clone, "prompt_text_language", self.lang_name_code))
        gender = str(_opt(self.clone, "gender", ""))
        if not prompt:
            raise SystemExit(
                "kikivoice: ASR nie zwrocil transkryptu probki - bez niego klon nie zagra"
            )
        # See cache_key(): the fingerprint is what determines the TIMBRE, and nothing that is
        # re-minted on the daily re-upload.
        self.clone_fingerprint = json.dumps([prompt, lang, gender], ensure_ascii=False,
                                            sort_keys=True)
        say(f"    kikivoice: klon gotowy, transkrypt {len(prompt)} zn., jezyk {lang}, {gender}")
        if self._credit_rate() >= 2.0:
            say("    kikivoice: klon idzie po 2.0 kredytu za znak – jeden odcinek to ~46% "
                "tygodniowego limitu")

    def set_chars_per_second(self, cps: float) -> None:
        # needs_calibration is False, so the pipeline never calls this on its own. It exists for
        # the standalone `calibrate` path, and the value is used only as a sanity yardstick when
        # judging whether a returned clip has a plausible length for its text.
        if cps and cps > 0:
            self._cps = float(cps)

    @property
    def max_concurrency(self) -> int:
        """Two different numbers, and confusing them is the mistake to avoid.

        Without a batcher every render() is a network task, and the network number governs: 3, cap
        4. Measured, 24 tasks at concurrency 3 finished in 27 s with nothing refused; the cluster
        reports 7 nodes. Above 4 we start looking like the traffic a rate limiter exists to catch,
        and the stake is the identity, not two minutes.

        Once a batcher has already bought the whole episode, render() is a disk read plus ffmpeg,
        so the CPU number governs.
        """
        if self._batched:
            return int(self.b_net.get("cpu_concurrency", 8))
        n = int(self.b_net.get("concurrency", 3))
        return max(1, min(n, int(self.b_net.get("concurrency_max", 4))))

    def close(self) -> None:
        # Called twice in some paths (chatterbox.py:303-310 is the precedent), so the counters are
        # zeroed after printing and the second call says nothing.
        with self._lock:
            tasks, credits, chars = self.tasks, self.credits_spent, self.chars_spent
            hits, lines, secs, quota = self.hits, self.lines, self.net_seconds, self.quota
            self.tasks = self.credits_spent = self.chars_spent = self.hits = self.lines = 0
            self.net_seconds = 0.0
        # NO early return here. This is finding 22 of the repair audit: the previous fix put
        # `if not (tasks or hits or lines): return` above the client teardown, so the one scenario
        # where a leak is most likely - prepare() opened HTTP sessions for caps and for the clone
        # upload, then the run halted on a budget gate without rendering a single line - was
        # exactly the scenario that skipped closing them. Verified: client._sessions still held an
        # open session after prepare() + close(). The summary stays conditional; the teardown does
        # not.
        if tasks or hits or lines:
            self._say_summary(tasks, credits, chars, hits, lines, secs, quota)
        self._close_client()

    def _say_summary(self, tasks, credits, chars, hits, lines, secs, quota) -> None:
        rate = self._credit_rate()
        say(f"    kikivoice: {lines} kwestii, {tasks} zadan API, {hits} z cache, "
            f"{chars} znakow platnych, {credits} kredytow (rate {rate:g}x), "
            f"{secs:.0f} s w sieci")
        if isinstance(quota, dict):
            avail = quota.get("available_count")
            mx = quota.get("max_count")
            days = quota.get("next_reset_days")
            if avail is not None and mx:
                pct = 100.0 * float(avail) / float(mx)
                say(f"    kikivoice: zostalo {avail} z {mx} kredytow ({pct:.0f}%), "
                    f"reset za {days} dni")
        sess = getattr(self.client, "session", None)
        if sess is not None:
            n = _opt(sess, "tasks_since_verification", None)
            if n is not None:
                budget = int(self.b_budget.get("verify_budget", 30))
                say(f"    kikivoice: {n}/{budget} zadan od ostatniej weryfikacji GeeTest")

    def _close_client(self) -> None:
        # requests.Session is per worker thread and holds open sockets; the pipeline builds and
        # tears down two thread pools per run, so without this they sit around until the garbage
        # collector notices. close() on the client is idempotent, like this one.
        closer = getattr(self.client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - teardown must not fail a finished render
                pass

    # -- optional batching seam -------------------------------------------

    def attach_batcher(self, batcher) -> None:
        """Hand the backend something that can buy a whole episode in a handful of requests.

        The contract is deliberately tiny, because the batching module is a separate concern
        (planning, RMS gap detection, cutting) with its own dependencies:

            batcher.prefetch(backend, utterances) -> report

        The report is an object, not a number, and that is the fix for finding 10 of the previous
        audit. A count of tasks says nothing about whether the episode is COVERED: a batch that
        failed gap detection spends a task and leaves its twelve cues uncached. What prewarm needs
        to know is the hole, so the report exposes:

            report.complete   -> bool, every cue is on disk
            report.missing    -> list of cue indices that are not
            report.summary()  -> one line for the human
            int(report)       -> tasks spent, for the older `-> int` shape

        Even so, prewarm re-checks the CACHE rather than trusting the report. The report is the
        batcher's own account of what it did; the cache is what render() will actually find.

        Obligations on the batcher, all of them things this class cannot enforce:

        * every task must go through backend.request_task(text), which reserves the slot, applies
          the character limit, handles 777 and books the credits. Calling the client directly makes
          both budget gates blind to the most expensive traffic there is;
        * pieces are filed with backend.put_segment(text, wav_bytes), under the ORIGINAL cue text,
          because that is the key fetch() will look up;
        * it must download each job's audio while the URL is fresh. The links expire in about an
          hour, so collecting job ids and fetching them all at the end turns a paid batch into
          nothing. kikivoice_client's tts_task/clone_task already download inline, which is the
          behaviour to keep.

        Planning and cutting helpers live in kikivoice_client (plan_batches, find_marker_gaps,
        split_batch); this class deliberately knows nothing about them. The reference
        implementation is kikivoice_batch.KikiBatcher.
        """
        self._batcher = batcher

    def prewarm(self, us: list) -> None:
        """Pipeline hook, called once with the whole episode before any render().

        Two jobs, in order:
          1. Refuse a render we cannot pay for, BEFORE the first request. This is the only moment
             where the answer to "will this eat the week" is still useful.
          2. Let a batcher, if one is attached, buy the episode in a few requests instead of ~760.

        Backend.prewarm does not exist in base.py yet; see INTEGRACJA.md for the two-line addition
        to stages.py that calls it. Until then this is simply never called, and the backend works
        line by line - correct, just expensive in tasks and therefore in human CAPTCHA solves.
        """
        missing = [u for u in us if (u.text or "").strip() and not self.segment_cached(u.text)]
        # With a batcher attached the number of TASKS is not the number of missing lines; it is
        # roughly missing/max_cues. Passing the line count would make _preflight predict ~760
        # CAPTCHA-consuming tasks for a run that will spend 64, and scare the user off a render
        # that is fine. The character total, which is what the credit gate cares about, is the
        # same either way.
        planned_tasks = len(missing)
        if self._batcher is not None and missing:
            per_task = max(1, int(getattr(self._batcher, "max_cues", 1) or 1))
            planned_tasks = max(1, -(-len(missing) // per_task))
        self._preflight([u.text for u in missing], planned_tasks)

        if not missing:
            # The whole episode is already on disk - a re-run, or a mix rebuilt after a level
            # change. render() is a disk read plus ffmpeg from here on, so the CPU number governs
            # whether or not a batcher is attached. Leaving this at False made every resumed run
            # crawl at network concurrency 3 for no reason at all.
            self._batched = True
            say(f"    kikivoice: caly odcinek ({sum(1 for u in us if (u.text or '').strip())} "
                f"kwestii) jest juz w cache – zero zadan, zero kredytow")
            return

        if self._batcher is not None:
            report = self._batcher.prefetch(self, us)
            # NOT an unconditional True, and NOT the batcher's word for it either. What decides is
            # the cache, re-read here: a batch that failed gap detection spends a task and leaves
            # its cues uncached, and believing otherwise raises max_concurrency from 3 network
            # requests to 8. Those leftovers would then fly as 8 concurrent tasks - the exact
            # traffic profile a rate limiter exists to catch, with the identity as the stake.
            left = [u for u in us if (u.text or "").strip() and not self.segment_cached(u.text)]
            self._batched = not left
            spent = int(report) if report is not None else 0
            line = getattr(report, "summary", None)
            say(f"    kikivoice: batcher kupil odcinek w {spent} zadaniach")
            if callable(line):
                say(f"    kikivoice: {line()}")
            if left:
                say(f"    kikivoice: {len(left)} kwestii zostalo do pojedynczego renderu "
                    f"(wspolbieznosc pozostaje sieciowa, kazda z nich to osobne zadanie API)")
                # The report should already have named them; if it did not, the disagreement
                # between what the batcher thinks it did and what is on disk is worth saying.
                claimed = list(getattr(report, "missing", []) or [])
                if report is not None and len(claimed) != len(left):
                    say(f"    kikivoice: !! batcher zglosil {len(claimed)} niepokrytych kwestii, "
                        f"a w cache brakuje {len(left)} – wierze cache")

    # -- budget gates ------------------------------------------------------

    def _credit_rate(self) -> float:
        """Credits charged per character. 1.0 for the standard library, 2.0 for kiki_core and
        kiki_multilingual, 3.0 for kiki_pro. Overridable, because a rate change on their side must
        not require a code change on ours."""
        r = self.v.get("credit_rate")
        if r:
            return float(r)
        if self.mode == "clone":
            # A clone is never billed at 1.0, whatever model_type says. prepare() normalises the
            # empty string to kiki_core, and this line holds even if it is called before that.
            return {"kiki_multilingual": 2.0, "kiki_pro": 3.0}.get(self.model_type, 2.0)
        if not self.model_type and self.library_type == "standard":
            return 1.0
        return {"kiki_core": 2.0, "kiki_multilingual": 2.0, "kiki_pro": 3.0}.get(
            self.model_type, 2.0)

    def _quota_field(self, name: str):
        q = self.quota if isinstance(self.quota, dict) else None
        if q is None:
            sess = getattr(self.client, "session", None)
            q = _opt(sess, "quota", None) if sess is not None else None
        return q.get(name) if isinstance(q, dict) else None

    def _report_budget_header(self) -> None:
        avail = self._quota_field("available_count")
        mx = self._quota_field("max_count") or 80000
        if avail is None:
            return
        ratio = float(self.b_budget.get("credit_warn_ratio", 0.2))
        if float(avail) < ratio * float(mx):
            say(f"    kikivoice: !! zostalo tylko {avail} z {mx} kredytow, reset za "
                f"{self._quota_field('next_reset_days')} dni")

    def _preflight(self, texts: list, planned_tasks: int) -> None:
        """The one place where the week can still be saved. Runs before the first request."""
        self._preflight_done = True
        planned_chars = sum(len(t or "") for t in texts)
        self._too_long = [(i, t) for i, t in enumerate(texts)
                          if self.max_text_length and len(t or "") > self.max_text_length]
        rate = self._credit_rate()
        planned = int(planned_chars * rate)
        avail = self._quota_field("available_count")
        mx = float(self._quota_field("max_count") or 80000)
        say(f"    kikivoice: do kupienia {planned_chars} znakow = ~{planned} kredytow "
            f"({rate:g}x), {planned_tasks} zadan")
        # The floor belongs in this comparison. Without it preflight waves through a render that
        # lands on exactly zero credits, and then reserve_task stops it halfway - which is the
        # interruption preflight exists to prevent, arrived at by agreeing with itself.
        floor = int(self.b_budget.get("credit_floor", 3000))
        # `planned > 0` belongs in front. Without it a render that costs NOTHING - everything in
        # cache, e.g. a mix rebuilt after a level change - is refused whenever the remaining
        # credits happen to sit under the floor: 0 > 2000 - 3000 is true. The gate exists to stop
        # spending, and there is nothing to stop.
        if planned > 0 and avail is not None and planned > float(avail) - floor:
            raise SystemExit(
                f"kikivoice: ten render potrzebuje ~{planned} kredytow, a zostalo {avail} "
                f"przy progu bezpieczenstwa {floor} (do wydania {max(0.0, float(avail) - floor):.0f}).\n"
                f"  Reset limitu za {self._quota_field('next_reset_days')} dni. "
                f"Nic nie wyslano, nic nie wydano."
            )
        if planned > 0.5 * mx:
            say(f"    kikivoice: !! to zje ponad polowe tygodniowego limitu ({planned} z {mx:.0f})")

        # The model's own character limit, checked dry. A line over it is refused by the server
        # AFTER the rest of the episode has already paid, and with a batcher in play a batch over
        # the limit is not a hypothesis but a certainty. Nothing has been sent yet at this point.
        if self.max_text_length and self._too_long:
            worst = sorted(self._too_long, key=lambda t: -len(t[1]))[:5]
            listing = "\n".join(f"      #{i}: {len(t)} znakow: {t[:60]}..." for i, t in worst)
            raise SystemExit(
                f"kikivoice: {len(self._too_long)} kwestii przekracza limit "
                f"{self.max_text_length} znakow modelu "
                f"{self.model_type or 'library standard'}:\n{listing}\n"
                f"  Podziel je w scenariuszu albo wybierz model o wiekszym limicie. "
                f"Nic nie wyslano, nic nie wydano."
            )

        # Verification budget: the user is going to be interrupted, and they would rather know
        # that now than after four minutes of waiting.
        sess = getattr(self.client, "session", None)
        done = _opt(sess, "tasks_since_verification", 0) if sess is not None else 0
        budget = int(self.b_budget.get("verify_budget", 30))
        if planned_tasks and int(done) + planned_tasks > budget:
            left = max(0, budget - int(done))
            say(f"    kikivoice: !! {planned_tasks} zadan przy {left} do progu weryfikacji – "
                f"spodziewaj sie ~{max(1, (int(done) + planned_tasks) // budget)} przerw na GeeTest")

    def _check_gate(self) -> None:
        """Raise, quietly, if something already decided this run stops. The first thread to
        decide printed the reason; the rest must not repeat it 20 times."""
        if not self._gate.is_set():
            raise SystemExit(self._halt_code or 75)

    def _halt(self, code: int, message: str) -> None:
        with self._lock:
            first = not self._halt_printed
            self._halt_printed = True
            self._halt_code = code
        self._gate.clear()
        if first:
            say(message)
        raise SystemExit(code)

    @contextlib.contextmanager
    def reserve_task(self, chars: int):
        """Book one API task before sending it, and give the slot back afterwards.

        Both budget gates live in here, and both are evaluated against `done + in_flight`, not
        against `done`. That is the whole fix: the counters they read are only updated when a task
        COMES BACK, so at concurrency 3 with two tasks left in the budget, three workers used to
        read the same stale number, all pass, and all send. The verification reserve of one then
        prevented nothing and the credit floor was overshot by concurrency * chars * rate.

        Public, because a batcher creates tasks too and must go through the same door.
        """
        rate = self._credit_rate()
        budget = int(self.b_budget.get("verify_budget", 30))
        reserve = int(self.b_budget.get("verify_reserve", 1))
        warn_at = int(self.b_budget.get("verify_warn_at", 24))
        floor = int(self.b_budget.get("credit_floor", 3000))
        sess = getattr(self.client, "session", None)
        done = int(_opt(sess, "tasks_since_verification", 0) or 0) if sess is not None else 0

        halt: tuple | None = None
        prompt = False
        with self._lock:
            committed = done + self._inflight
            planned_chars = self._inflight_chars + max(0, int(chars))
            avail = self._quota_field("available_count")
            if committed >= budget - reserve:
                # Stop one task short of the wall on purpose: hitting 777 mid-flight wastes
                # nothing, but stopping deliberately lets the in-flight tasks finish and be
                # cached first.
                #
                # The flag is what makes this reversible. Nothing else in the system learns that
                # this stop happened - the server was never asked - so without it the counter
                # stays at the wall and every later run stops here again without sending a byte.
                halt = (75, self._verify_message_preventive(committed, budget))
                prompt = True
            elif avail is not None and float(avail) - planned_chars * rate < floor:
                halt = (75, (
                    f"\nkikivoice: STOP – zostalo {avail} kredytow, prog bezpieczenstwa to "
                    f"{floor}, a w locie jest {planned_chars} znakow po {rate:g}x.\n"
                    f"  Reset limitu za {self._quota_field('next_reset_days')} dni.\n"
                    f"  Wszystko, co juz zrobione, siedzi w cache – wznowienie nie kosztuje "
                    f"kredytow.\n"
                    f"  Uruchom te sama komende po resecie.   [kod wyjscia 75]"
                ))
            else:
                self._inflight += 1
                self._inflight_chars += max(0, int(chars))
        if halt is not None:
            if prompt and sess is not None:
                marker = getattr(sess, "note_verification_prompt", None)
                if callable(marker):
                    try:
                        marker()
                    except Exception:  # noqa: BLE001 - never turn a clean stop into a traceback
                        pass
            self._halt(*halt)
        if committed >= warn_at:
            say(f"    kikivoice: {committed}/{budget} zadan od weryfikacji – "
                f"CAPTCHA za ~{budget - committed}")
        try:
            yield
        finally:
            with self._lock:
                self._inflight = max(0, self._inflight - 1)
                self._inflight_chars = max(0, self._inflight_chars - max(0, int(chars)))

    def _verify_url(self, url, public_ip) -> tuple[str, str]:
        sess = getattr(self.client, "session", None)
        uid = str(_opt(sess, "uuid", "") or "")
        tail = uid[-4:] if uid else "????"
        url = url or (f"https://kikivoice.ai/jsapi/auth/geetest-validation-page/{public_ip}"
                      if public_ip else "https://kikivoice.ai/text-to-speech")
        return url, tail

    def _verify_message(self, url, public_ip, done, budget) -> str:
        """The server really answered 777. Here the counter WAS zeroed, by the client."""
        url, tail = self._verify_url(url, public_ip)
        return (
            f"\nkikivoice: HUMAN_VERIFICATION_REQUIRED po {done} zadaniach (limit {budget}).\n"
            f"  1. Otworz w TEJ SAMEJ przegladarce, z ktorej pochodzi cookie uuid (...{tail}):\n"
            f"     {url}\n"
            f"  2. Rozwiaz GeeTest.\n"
            f"  3. Uruchom ponownie te sama komende. Licznik zadan zostal wyzerowany w chwili\n"
            f"     odmowy (777 to zdarzenie weryfikacyjne), wiec ruszy dalej sam – nie trzeba\n"
            f"     niczego kasowac ani wolac 'session reset-verify'.\n"
            f"  Ukonczone kwestie sa w cache – wznowienie nie kosztuje kredytow.  "
            f"[kod wyjscia 75]"
        )

    def _verify_message_preventive(self, done, budget) -> str:
        """We stopped ourselves before the wall. The server said nothing, so say so.

        The old build used the 777 message here too, which told the user that the counter had
        already been zeroed "at the moment of refusal" - and there was no refusal, and it had not
        been zeroed. That message made the deadlock look like a working feature. This one names
        the mechanism that actually releases the hold, and names the manual escape as well.
        """
        url, tail = self._verify_url(None, None)
        return (
            f"\nkikivoice: STOP przed progiem weryfikacji – {done} zadan od ostatniego "
            f"GeeTest (limit {budget}).\n"
            f"  Serwer NIC nie odmowil; zatrzymalem sie sam jedno zadanie przed sciana, zeby "
            f"to, co\n"
            f"  jest w locie, zdazylo trafic do cache.\n"
            f"  1. Otworz w TEJ SAMEJ przegladarce, z ktorej pochodzi cookie uuid (...{tail}):\n"
            f"     {url}\n"
            f"  2. Wygeneruj cokolwiek recznie i rozwiaz GeeTest.\n"
            f"  3. Uruchom ponownie te sama komende. Ten postoj zostal zapisany w sesji, wiec\n"
            f"     nastepny start zwolni licznik automatycznie i render ruszy dalej.\n"
            f"     (Awaryjnie recznie: python3 kikivoice_client.py session reset-verify)\n"
            f"  Ukonczone kwestie sa w cache – wznowienie nie kosztuje kredytow.  "
            f"[kod wyjscia 75]"
        )

    # -- cache -------------------------------------------------------------

    def segment_key(self, text: str, speed: float | None = None) -> str:
        return cache_key(
            text,
            library_type=self.library_type, voice_code=self.voice_code, voice_id=self.voice_id,
            model_type=self.model_type, model_version_text=self.model_version_text,
            lang_name_code=self.lang_name_code,
            speed=self._request_speed() if speed is None else speed,
            volume=self.volume, pitch=self.pitch, audio_format=self.audio_format,
            audio_high_quality=self.audio_high_quality,
            clone_fingerprint=self.clone_fingerprint,
        )

    def _key_lock(self, key: str) -> threading.Lock:
        """The lock that guards buying ONE cache key. Created on first use."""
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = self._key_locks[key] = threading.Lock()
            return lk

    def _cache_path(self, key: str) -> Path:
        """Where one cue's audio lives. A READ-ONLY question, so it creates nothing.

        It used to mkdir on every call, which meant segment_cached - a pure predicate - created
        directories. The batcher asks it a few thousand times per episode before a single byte is
        bought (once per cue while planning, once per cue while checking coverage), so those were
        a few thousand syscalls to answer "is this file there".
        """
        return self.cache / key[:2] / f"{key}.kv.wav"

    def segment_cached(self, text: str) -> bool:
        p = self._cache_path(self.segment_key(text))
        try:
            # A zero-length file is a crash artefact, not a cache hit. Reporting it as cached is
            # how an episode ends up with silent lines that nothing re-buys.
            return p.stat().st_size > 0
        except OSError:
            return False

    def put_segment(self, text: str, wav: bytes) -> None:
        """Store audio for one cue. The seam a batcher writes through; also used by fetch()."""
        self._write_atomic(self._cache_path(self.segment_key(text)), wav)

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        # Written aside and renamed, as the other backends do: a plain write leaves a truncated
        # file behind when the disk fills, and two workers rendering duplicate cue texts hash to
        # the same path and would otherwise interleave two files into one.
        #
        # fsync before the rename, matching _atomic_write in the client. Without it a hard reset
        # can leave the rename durable and the CONTENT not, i.e. a zero-length file where paid
        # audio should be - and every zero-length file is a second purchase. This is the single
        # thing that makes "Ctrl+C never re-buys" true for a batch that was mid-write, and it is
        # why segment_cached above refuses to count an empty file.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid():x}.{threading.get_ident():x}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)

    def note_task(self, result) -> None:
        """Fold one TaskResult into the accounting. Public, because a batcher creates tasks too."""
        credits = int(_opt(result, "deducted_credits", 0) or 0)
        quota = _opt(result, "quota", None)
        elapsed = float(_opt(result, "elapsed_s", 0.0) or 0.0)
        with self._lock:
            self.tasks += 1
            self.credits_spent += credits
            self.net_seconds += elapsed
            if isinstance(quota, dict) and quota:
                self.quota = quota
        # No session.save() here, deliberately. The client already persisted the counter and the
        # quota inside _create_task, under a cross-process flock. Writing again from here would
        # push this thread's in-memory snapshot over whatever another process wrote in the
        # meantime - and a verification counter that goes BACKWARDS is exactly how a render walks
        # into an unexpected 777. It also rewrote and fsynced the whole JSON once per line.

    # -- synthesis ---------------------------------------------------------

    def _request_speed(self) -> float:
        """Always 1.0 in the default "fixed" mode, and that is the point.

        `speed` on this API is an exact linear time scalar (measured: 1.15 -> 0.8696x duration,
        2.0 -> 0.5x, clamped there), and in the 1.0-1.2 band it is nearly indistinguishable from a
        local atempo of the same factor (spectral-envelope correlation 0.93). Asking at 1.0 always
        means the window can be re-fitted later for free from the cached bytes: re-timing a line
        to a different window costs ZERO credits. ElevenLabs cannot do that, because there speed
        is part of the request and therefore part of the cache key.
        """
        mode = str(self.b_timing.get("request_speed_mode", "fixed"))
        if mode == "fixed":
            return float(self.b_timing.get("request_speed", 1.0))
        return float(self.b_timing.get("request_speed", 1.0))

    def fetch(self, text: str) -> tuple[bytes, bool]:
        """Cached bytes for one line. Returns (audio, came_from_cache). Spends credits on a miss.

        The lock around the miss path is not decoration. Repeated lines are the norm in subtitles
        ("Tak.", "Nie.", "Dziekuje."), they hash to one cache key, and two workers arriving at the
        same key at the same time used to buy the same audio twice - two charges, two slots of the
        ~30 between CAPTCHA solves, for one file. Whoever gets the lock buys; everyone else waits
        and then finds the file on disk, which is why the second caller correctly reports
        cached=True: nothing was paid for that line on their behalf.
        """
        speed = self._request_speed()
        key = self.segment_key(text, speed)
        path = self._cache_path(key)
        if path.exists():
            with self._lock:
                self.hits += 1
            return path.read_bytes(), True

        with self._key_lock(key):
            # Re-check inside the lock: while we waited, the other worker may have bought it.
            if path.exists():
                with self._lock:
                    self.hits += 1
                return path.read_bytes(), True

            res = self.request_task(text, speed=speed)
            audio = _need(res, "audio", "audio_bytes")
            self._write_atomic(path, audio)
            return audio, False

    def request_task(self, text: str, *, speed: float | None = None):
        """Buy ONE task and return the client's TaskResult. The only door to the network.

        Public because the batcher goes through it too, and going through it is not optional: this
        is where the verification gate, the credit floor, the character limit, the 777 handling and
        the credit accounting all live. A batcher that called client.tts_task() directly would
        make every one of them blind to two thirds of the traffic - and the traffic it hides is
        the expensive kind, because one batch is twelve lines' worth of characters.

        It deliberately does NOT touch the cache. The caller decides what key the bytes belong
        under: fetch() files them under the line, the batcher cuts the clip up first and files each
        piece under its own line. Both then use put_segment / _write_atomic, so both are atomic.

        Raises RuntimeError for a task the server refused, SystemExit (through _halt) for anything
        that ends the run: verification due, quota exhausted, identity rejected.
        """
        speed = self._request_speed() if speed is None else speed
        self._check_gate()
        mod = self._mod
        if mod is None or self.client is None:
            raise SystemExit("kikivoice: render przed prepare() – backend nie jest gotowy")
        self._late_preflight(text)

        # Against the BILLED length, not the raw one: the server replaces every pause marker with
        # a single space before it applies max_text_length and before it charges. Measured on four
        # real batches. A guard on len(text) refuses batches the server accepts - which is exactly
        # what happened on the first production run, where a 1069-character batch whose billed
        # length was well under 1000 was thrown away by this check.
        billed = mod.billed_length(text) if hasattr(mod, "billed_length") else len(text)
        if self.max_text_length and billed > self.max_text_length:
            # Refused here, before the request. The server would refuse it too, but only after
            # every other line in the episode had already paid. With a batcher in play this is not
            # a hypothesis: a batch is built up to a budget, and a budget derived from a limit that
            # was read wrong is a batch the server throws away with the credits already taken.
            raise RuntimeError(
                f"kikivoice: tekst ma {billed} naliczanych znakow ({len(text)} surowych), "
                f"a limit modelu {self.model_type or 'library standard'} to {self.max_text_length}"
            )

        with self.reserve_task(billed):
            try:
                if self.mode == "clone":
                    res = self.client.clone_task(
                        text, voice=self.clone, lang_name_code=self.lang_name_code,
                        model_type=self.model_type or "kiki_core",
                        model_version_text=self.model_version_text,
                        speed=speed, volume=self.volume, audio_format=self.audio_format,
                        audio_high_quality=self.audio_high_quality,
                    )
                else:
                    res = self.client.tts_task(
                        text, library_type=self.library_type, voice_code=self.voice_code,
                        voice_id=self.voice_id, model_type=self.model_type,
                        model_version_text=self.model_version_text,
                        lang_name_code=self.lang_name_code, speed=speed, volume=self.volume,
                        pitch=self.pitch, audio_format=self.audio_format,
                        audio_high_quality=self.audio_high_quality,
                    )
            except mod.HumanVerificationRequired as exc:
                # Never a retry. The task was refused, so it cost neither a credit nor a slot of
                # the ~30 between two solves: the same text will be bought after the human comes
                # back. The client zeroed the counter when it saw the 777, so re-running the same
                # command really does resume - which is what the message promises.
                sess = getattr(self.client, "session", None)
                done = int(_opt(sess, "tasks_since_verification", 0) or 0) if sess else 0
                self._halt(75, self._verify_message(
                    getattr(exc, "validation_url", None), getattr(exc, "public_ip", None),
                    done, int(self.b_budget.get("verify_budget", 30))))
            except mod.UuidRequired as exc:
                self._halt(1, f"kikivoice: UUID_REQUIRED – tozsamosc odrzucona ({exc}). "
                              f"Ustaw cookie uuid z przegladarki.")
            except mod.QuotaExhausted as exc:
                self._halt(75, f"kikivoice: kredyty wyczerpane ({exc}). "
                               f"Reset za {self._quota_field('next_reset_days')} dni. "
                               f"Cache zachowany, wznow po resecie.  [kod wyjscia 75]")
            except AttributeError as exc:
                # A missing METHOD on the client, which _need cannot catch because there is no
                # object to inspect. Named here so it does not surface as a bare AttributeError
                # from inside a worker thread.
                raise SystemExit(
                    f"kikivoice: kikivoice_client.py nie ma metody, ktorej potrzebuje "
                    f"adapter ({exc}). Uruchom python3 test_kikivoice.py."
                ) from None
            except mod.KikiError:
                # Re-raised AS IS, deliberately. The batcher's fallback cascade catches KikiError
                # to split a failed group into halves and then into single cues; wrapping it in a
                # RuntimeError here made that cascade unreachable and turned one recoverable task
                # failure into a dead run. Measured in production: a 1333-character clone batch
                # came back AI_SERVER_ERROR after the credits were already taken, and the whole
                # episode stopped instead of retrying the group in halves.
                raise

        audio = _need(res, "audio", "audio_bytes")
        if not audio:
            # Also a KikiError, for the same reason: recoverable by splitting, not fatal.
            raise mod.KikiError("kikivoice: zadanie zakonczone bez audio")
        self.note_task(res)
        with self._lock:
            self.chars_spent += len(text)
        return res

    def _late_preflight(self, text: str) -> None:
        """Budget header for the case where prewarm() was never called.

        Line-by-line rendering cannot know what the whole episode will cost - that is exactly what
        prewarm() is for - but it can still say what is left before the first credit goes, instead
        of leaving _preflight as dead code on an unpatched lektor-ai.
        """
        with self._lock:
            if self._preflight_done:
                return
            self._preflight_done = True
        avail = self._quota_field("available_count")
        say(f"    kikivoice: prewarm() nie zostal wywolany (patrz INTEGRACJA.md §4) – "
            f"renderuje linia po linii, bez wyceny calego odcinka"
            + (f"; zostalo {avail} kredytow" if avail is not None else ""))
        self._report_budget_header()

    def _finish(self, blob: bytes, atempo: float | None = None) -> np.ndarray:
        """Decode, compress the pauses the engine put inside the line, then trim its ends.

        The order is load-bearing and is the same one elevenlabs.py uses: compressing a pause
        changes the length of the clip, and the trim is what decides where the clip starts and
        stops. Doing it the other way round trims an utterance that is then rewritten underneath
        the decision.

        decode_mono resamples to the mix rate, so the engine's native 24 kHz is lifted to 48 kHz
        here and resample_linear in stages.py becomes a no-op. Nothing is gained by the upsample -
        the material is 24 kHz whatever the header says - but the pipeline mixes at one rate.
        """
        audio = decode_mono(self.cfg, blob, self.sample_rate, atempo=atempo)
        return trim_silence(shorten_pauses(self.cfg, audio, self.sample_rate), self.sample_rate)

    def render(self, u: Utterance) -> Rendered:
        # A cue that is empty after cleaning is not worth a request, and asking for one would
        # spend a slot out of the ~30 between two CAPTCHA solves on silence.
        if not (u.text or "").strip():
            return Rendered(np.zeros(0, np.float32), self.sample_rate, 1.0, 1.0, True, 0)

        blob, cached = self.fetch(u.text)
        try:
            audio = self._finish(blob)
        except RuntimeError:
            if not cached:
                raise
            # A cache entry that will not decode is a bad write from an earlier run, not a verdict
            # on the text. Left in place it fails every run from here on, so it is dropped and the
            # line is bought once more rather than making the user delete the file by hand.
            say(f"    .. unreadable cache entry for line {u.index} – refetching")
            self._cache_path(self.segment_key(u.text)).unlink(missing_ok=True)
            blob, cached = self.fetch(u.text)
            audio = self._finish(blob)

        # Fit the window locally, from the cached bytes, so a miss here costs no credits. This is
        # the whole duration strategy for this backend: never buy a second reading.
        #
        # timing.max_atempo (the global one) is deliberately NOT consulted. It is set to 1.0 in
        # this project by an owner decision taken about ElevenLabs, where a faster HONEST reading
        # was available and atempo was the worse of two options. Here there is no second reading to
        # prefer: either atempo fits the line or the line runs long. The ceiling is the backend's
        # own local_atempo_max.
        at = 1.0
        cap = float(self.b_timing.get("local_atempo_max", 1.20))
        if u.target_ms and u.target_ms > 0:
            window_s = u.target_ms / 1000.0
            actual_s = len(audio) / self.sample_rate
            if actual_s > window_s:
                at = min(cap, actual_s / window_s)
                if at > 1.005:
                    audio = self._finish(blob, atempo=at)
                else:
                    at = 1.0

        with self._lock:
            self.lines += 1

        return Rendered(
            audio=audio,
            sample_rate=self.sample_rate,
            # speed is what the SERVER was asked to read at (1.0 in the default fixed mode) and
            # atempo is the local stretch. They are two different facts and the contract in
            # base.py:42-43 names them as two different facts, so they are reported separately
            # even though putting the stretch in both fields would suppress a pass in stages.py.
            #
            # What that costs, honestly: stages.py:186 selects its second pass on
            # `speed < max_speed - 0.005`, so every clip that overran now qualifies. It re-renders
            # them, gets a cache hit (free, no credits, no task, no CAPTCHA slot), produces
            # identical audio, finds `got < clip.achieved_ms` false and discards it. Wasted ffmpeg
            # time, nothing else. The one-line fix on the lektor-ai side is in INTEGRACJA.md §6:
            # select on max(speed, atempo). It is not worth lying in a data structure that
            # pricing, reports and any future analysis all read.
            speed=round(self._request_speed(), 3),
            atempo=round(at, 3),
            cached=cached,
            # The contract says CHARACTERS, and the pipeline's own reports read it that way. What
            # the service actually took is credits = chars * credit_rate, tracked separately in
            # note_task and printed by close(); the two must not be conflated.
            billable_chars=0 if cached else len(u.text),
        )
