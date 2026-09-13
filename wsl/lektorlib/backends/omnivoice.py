"""OmniVoice backend: free, GPU-bound, clones a voice, and takes the duration in seconds.

This is the first engine here that is asked for a LENGTH rather than a rate. Piper reaches its
window by binary-searching `length_scale`, six generations a line; ElevenLabs predicts from a
measured chars/second, misses, and buys the line a second time. OmniVoice's `generate()` takes
`duration=<seconds>` directly, so one generation lands inside the window and nothing has to be
searched, predicted or re-bought.

Measured here on an RTX 3090 under WSL2, bfloat16, batch of one, against the six calibration
lines and a 6 s reference recording:

  regime                                    raw output              after trim_silence
  duration below natural (compression)      target + 200 ms, 6/6    median 55 ms, worst 300 ms
  duration at or above natural              unreliable, see below   -

**The +200 ms is padding, not overrun, and it must not be subtracted from the ask.** Under
compression the raw clip came back at exactly target + 200 ms every single time, zero variance.
That tail is SILENCE and trim_silence takes it off downstream, so after trimming the speech lasts
about as long as the duration asked for - median error 55 ms, inside the 60 ms piper searches for.
Subtracting the pad from the ask as well, as the first version did, spends those 200 ms twice and
every line comes out a fifth of a second short. See `_ask_for`, which also floors the ask at
timing.max_speed so no window can demand a scramble.

**Compression is the only regime that matters and the only one that is reliable.** Asked for MORE
than a line naturally takes, the model sometimes stretches (2.84 s line asked for 3.6 got 3.65)
and sometimes just reads it and stops (2.72 s line asked for 4.0 got 2.54). That is harmless here:
a lektor is only ever asked to hurry, never to dawdle, so a line that finishes early inside its
window is exactly right and the ceiling behaviour is what we want.

**The advertised RTF is not what this box does, and batching is where the rest of it is.**
Upstream quotes RTF 0.025 - 40x realtime - which is batch 8 on an H100 with FlashInfer. Measured
here one line at a time on a 3090: **RTF 1.38**, i.e. about 21 minutes of GPU for the ~17 minutes
of speech in an episode. The same six lines in ONE batched call: **RTF 0.317, 4.4x faster**, which
takes the episode to roughly 4.5 minutes. `generate()` takes lists for both `text` and `duration`,
so a batch carries a separate window per line and accuracy holds (median 65 ms after trimming
against 55 ms unbatched).

This backend still renders one line per call, because that is the shape of `Backend.render()`.
Batching needs a hook that hands the backend a slice of the episode at once, and it is the single
change that would matter most here: 4.5 minutes an episode against 21 puts this within sight of
piper (0.0185, 20 seconds) rather than in chatterbox's territory (2.37, 42 minutes).

**VRAM, and a warning about sharing the GPU.** 3.03 GiB of weights, 3.96 GiB peak at batch 6. But
with three other CUDA containers resident the load fails outright rather than degrading - see the
worker for the allocation that gets refused and the fallback that survives it.

Nothing is billable, so the cost of all that is only time - which is why generated audio is filed
in the same content-addressed cache the paid backend uses. A re-render, a mix retune or a sample
costs nothing twice.

The model needs the python 3.11 venv at voice.omnivoice.venv (torch + omnivoice); the pipeline
runs on 3.13 and has no torch. So the model lives in a worker subprocess launched from that venv
and is spoken to over a JSON-per-line pipe, exactly as chatterbox does, and torch is never
imported into this process at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import say
import numpy as np

from ..audio import decode_mono, trim_silence
from ..srt import predicted_ms
from ..worker import LineReader
from .base import Backend, Rendered, Utterance

# The model's own output rate. Everything is resampled to mix.sample_rate on the way in.
MODEL_SR = 24000

# bfloat16 rather than float16 for the same reason chatterbox refuses float16: the exponent range
# is what these models need, and a saturating logit becomes a CUDA assert that poisons the context.
_DTYPES = frozenset({"bfloat16", "float32"})


def _file_identity(path: Path) -> str:
    """Content hash of the reference clip, so re-cutting the same speaker invalidates the cache."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def cache_key(text: str, params: dict, reference: str, duration_s: float | None) -> str:
    """Everything that changes the waveform, and duration is one of those things.

    This is the difference from chatterbox, whose key deliberately ignores the window: there the
    window is applied afterwards by atempo, so the same line always generates the same audio. Here
    the window is a GENERATION parameter - the model speaks differently to fit it - so two
    renders of one line into two different windows are two different takes and must not share a
    key. Rounded to 10 ms because that is finer than the model's own accuracy and keeps a window
    that moved by a sample from missing the cache.
    """
    d = None if duration_s is None else round(duration_s, 2)
    blob = json.dumps([text, params, reference, d], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class OmniVoiceBackend(Backend):
    name = "omnivoice"
    billable = False
    # It is told the length it must hit, so there is no rate to predict and nothing to calibrate.
    needs_calibration = False
    # And no planned speed to follow: a speed decided across the episode would only fight the
    # duration the same line is being given.
    wants_speed_plan = False

    def __init__(self, cfg):
        super().__init__(cfg)
        self.v = cfg.backend_cfg("omnivoice")
        self.timing = cfg["timing"]
        self.sample_rate = cfg["mix"]["sample_rate"]
        self.cache = cfg.cache_dir
        self.proc: subprocess.Popen | None = None
        self.tmpdir: str | None = None
        self.log_path: Path | None = None
        self._reader: LineReader | None = None
        self.reference = "builtin"
        self.generated = 0
        self.gen_seconds = 0.0
        self.audio_seconds = 0.0
        self.empty_takes = 0
        self.unconstrained = 0
        self.kept_short = 0

    # -- lifecycle ---------------------------------------------------------

    def _params(self) -> dict:
        """Everything that changes the waveform. Also part of the cache key, so all of it here."""
        return {
            "model": str(self.v.get("model", "k2-fsa/OmniVoice")),
            "dtype": str(self.v.get("dtype", "bfloat16")),
            "language": str(self.v.get("language", "pl")),
            "num_step": int(self.v.get("num_step", 32)),
            "seed": int(self.v.get("seed", 1234)),
        }

    def prepare(self) -> None:
        if self.proc is not None:
            return

        params = self._params()
        if params["dtype"] not in _DTYPES:
            raise SystemExit(
                f"voice.omnivoice.dtype {params['dtype']!r} is not supported -- use "
                f"{' or '.join(sorted(_DTYPES))}"
            )

        venv = str(self.v.get("venv", "") or "")
        if not venv:
            raise SystemExit(
                "voice.omnivoice.venv is empty -- point it at a python 3.11 venv that has torch "
                "and omnivoice installed (pip install omnivoice)"
            )
        python = Path(venv).expanduser() / "bin" / "python"
        if not python.is_file():
            raise SystemExit(f"no python at {python} -- voice.omnivoice.venv must be a venv")

        ref = str(self.v.get("reference_wav", "") or "")
        if ref:
            ref_path = Path(ref).expanduser()
            if not ref_path.is_file():
                raise SystemExit(f"voice.omnivoice.reference_wav does not exist: {ref_path}")
            self.reference = _file_identity(ref_path)
            ref = str(ref_path)

        self.tmpdir = tempfile.mkdtemp(prefix="lektor-omnivoice-")
        worker = Path(self.tmpdir) / "worker.py"
        worker.write_text(_WORKER_SRC, encoding="utf-8")

        # Model libraries write progress bars and deprecation warnings to stderr by the screenful.
        # Sent down a pipe nobody drains, that deadlocks the worker; a file cannot.
        self.log_path = Path(self.tmpdir) / "worker.log"
        log = self.log_path.open("wb")

        payload = dict(params)
        payload["reference_wav"] = ref
        env = dict(os.environ)
        env["TQDM_DISABLE"] = "1"
        # Not a cargo-culted flag. See the worker for the allocation this box refuses outright.
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        t0 = time.perf_counter()
        try:
            self.proc = subprocess.Popen(
                [str(python), "-u", str(worker), json.dumps(payload)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env,
            )
        except OSError:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = self.log_path = None
            raise
        finally:
            log.close()

        ready = self._exchange(None, float(self.v.get("load_timeout_s", 900)))
        load_s = time.perf_counter() - t0
        say(f"    omnivoice resident: {ready['dtype']} on {ready['device']}, "
            f"{ready.get('vram_gib', 0):.2f} GiB of weights, load {load_s:.1f}s "
            f"({ready.get('load_path', '?')})")
        say(f"    voice: {'cloned from ' + Path(ref).name if ref else 'builtin'}, "
            f"language {params['language']}, {params['num_step']} diffusion steps")

    def set_chars_per_second(self, cps: float) -> None:
        """Ignored: this backend is given the length, so it has no rate to match."""

    @property
    def max_concurrency(self) -> int:
        # Fixed at 1. One model on one GPU behind one worker with one request/response pipe, so
        # extra threads would queue on that pipe for nothing.
        return 1

    # -- worker protocol ---------------------------------------------------

    def _log_tail(self, lines: int = 12) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.strip().splitlines()[-lines:])

    def _die(self, reason: str) -> None:
        tail = self._log_tail()
        self.close()
        raise RuntimeError(reason + (f"\n--- worker log ---\n{tail}" if tail else ""))

    def _exchange(self, request: dict | None, timeout_s: float) -> dict:
        """Send one request (or just wait for the ready line) and read exactly one reply."""
        assert self.proc is not None
        if request is not None:
            try:
                self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                self._die("omnivoice worker died before it could be asked for the next line")

        # A reader thread, not select(): select accepts sockets only on Windows and raises on a
        # pipe, which was the single call standing between this pipeline and a native Windows run.
        if self._reader is None:
            self._reader = LineReader(self.proc)
        line = self._reader.read(timeout_s)
        if line is None:
            if self.proc.poll() is not None:
                self._die(f"omnivoice worker exited with code {self.proc.returncode}")
            self._die(f"omnivoice worker did not answer within {timeout_s:.0f}s")

        if not line:
            self._die("omnivoice worker closed its pipe without answering")
        try:
            reply = json.loads(line.decode("utf-8"))
        except ValueError:
            self._die(f"omnivoice worker sent a non-JSON reply: {line[:200]!r}")
        if not reply.get("ok"):
            self._die(f"omnivoice worker failed: {reply.get('error', 'no reason given')}")
        return reply

    # -- rendering ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        d = self.cache / key[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.wav"

    def _ask_for(self, u: Utterance) -> float | None:
        """The duration to request for one line, and the floor under how fast it may be said.

        The ask is simply the window times the pipeline's safety factor, and getting that wrong is
        what made the first version unlistenable. The model's constant 200 ms tail is SILENCE, and
        trim_silence removes it downstream, so after trimming the speech lasts about as long as
        the duration that was asked for. Subtracting the pad from the ask as well spent that
        200 ms twice: every line came out a fifth of a second shorter than its window allowed, and
        on a short card that is most of the card. Measured on the real script, cue 14 "Rywali
        razem." has a 1334 ms window, keeps 784 ms after lead_in, and was being asked to fit
        553 ms against a natural length of about 1280 ms - 43%, which is not fast speech, it is a
        scramble.

        The floor is the second half of the fix, and it is not a new invention: `timing.max_speed`
        is the ceiling every other engine in this project obeys, and the fitted duration model says
        how long the words take at 1.0. So this asks for no less than the same words at max_speed,
        and a window tighter than that is simply overrun - which the mix already tolerates and the
        report already counts. An overrun is recoverable; an unintelligible line is not.
        """
        if not u.target_ms or u.target_ms <= 0:
            return None
        want_ms = u.target_ms * float(self.timing.get("safety", 1.0))
        # The engine's own pad, kept as a knob for anyone who turns trimming off. See the note in
        # the config: while trim_silence runs, this must stay at 0.
        want_ms -= float(self.v.get("duration_pad_ms", 0.0))
        fastest = float(self.timing.get("max_speed", 1.2)) or 1.0
        want_ms = max(want_ms, predicted_ms(u.text, self.timing) / fastest)
        return max(float(self.v.get("duration_floor_ms", 400.0)), want_ms) / 1000.0

    def _long_enough(self, text: str, secs: float) -> bool:
        """Did the model actually SAY the line, or did it stop partway through?

        This is the check that matters most in the whole backend, and the first version got it
        wrong by asking only whether anything came back at all. The model does not merely return
        nothing sometimes - it returns a STUB. Measured over one full episode: 100 of 759 lines
        came back under half the length the words need at the fastest rate this project allows,
        most of them around 110 ms of audio for a whole sentence, and 217 were under 85%. A 50 ms
        floor waved every one of them through, and because a stub trivially fits any window they
        were then counted as lines that landed inside their slot. "Nie. To było FBI." was asked
        for 1113 ms, came back as 870 ms, and plays as "to było F".

        So the floor is proportional to the TEXT, not a constant: the fitted duration model says
        how long the words take, timing.max_speed is the fastest they may be said, and anything
        materially under that is truncation rather than brisk delivery. min_fill_ratio leaves room
        for the model to be genuinely quicker than an average fit.
        """
        need = predicted_ms(text, self.timing) / (float(self.timing.get("max_speed", 1.2)) or 1.0)
        floor = max(float(self.v.get("min_audio_s", 0.05)) * 1000.0,
                    need * float(self.v.get("min_fill_ratio", 0.95)))
        return secs * 1000.0 >= floor

    def _plan(self, u: Utterance) -> tuple[str, Path, float | None]:
        """The key, the file and the ask for one line. One spelling, used by both paths."""
        duration_s = self._ask_for(u)
        key = cache_key(u.text, self._params(), self.reference, duration_s)
        return key, self._cache_path(key), duration_s

    def prewarm(self, utterances: list[Utterance]) -> None:
        """Generate the episode in batches, into the cache the render loop then reads.

        This is where this backend earns its place. Batched, the model ran 4.4x faster than one
        line at a time on the same six lines (RTF 0.317 against 1.38), which takes an episode from
        about twenty minutes of GPU to about five. Every line still gets its OWN duration, because
        `generate()` takes a list for `duration` as well as for `text` - so batching costs nothing
        in fit: measured accuracy after trimming was 65 ms batched against 55 ms unbatched.

        Anything that fails or comes back empty is simply left out of the cache. render() then
        generates it on its own with the retry it already has, so a bad batch degrades into the
        slow path rather than into a silent line.
        """
        size = max(1, int(self.v.get("batch_size", 8)))
        if self.proc is None or size == 1:
            return
        todo = []
        for u in utterances:
            key, path, duration_s = self._plan(u)
            if not path.exists():
                todo.append({"text": u.text, "out": str(path), "duration": duration_s,
                             "seed": int(key[:8], 16) % (2 ** 31)})
        if not todo:
            say(f"    omnivoice: all {len(utterances)} lines already cached")
            return

        say(f"    omnivoice: generating {len(todo)} of {len(utterances)} lines in batches of {size}")
        t0 = time.perf_counter()
        made = empty = 0
        audio_s = 0.0
        for start in range(0, len(todo), size):
            chunk = todo[start:start + size]
            # Timeout scales with the batch: one line's budget is not a batch's budget.
            timeout = float(self.v.get("render_timeout_s", 300)) * len(chunk)
            try:
                reply = self._exchange({"cmd": "render_batch", "items": chunk}, timeout)
            except RuntimeError:
                raise
            got = reply.get("audio_s") or []
            for item, secs in zip(chunk, got):
                if self._long_enough(item["text"], float(secs)):
                    made += 1
                    audio_s += float(secs)
                else:
                    empty += 1
                    Path(item["out"]).unlink(missing_ok=True)
            self.generated += len(chunk)
            self.gen_seconds += float(reply.get("gen_s", 0.0))
            self.audio_seconds += sum(float(s) for s in got)
            if (start // size) % 5 == 0 or start + size >= len(todo):
                say(f"      {min(start + size, len(todo))}/{len(todo)}")
        dt = time.perf_counter() - t0
        rtf = dt / audio_s if audio_s else 0.0
        say(f"    omnivoice: {made} lines in {dt / 60:.1f} min of GPU for {audio_s / 60:.1f} min "
            f"of speech (RTF {rtf:.3f})"
            + (f"; {empty} came back empty or truncated and go to the slow path" if empty else ""))

    def render(self, u: Utterance) -> Rendered:
        if self.proc is None:
            raise RuntimeError("omnivoice backend used before prepare()")

        key, path, duration_s = self._plan(u)
        cached = path.exists()

        if not cached:
            # Sampling is stochastic in two senses. The ordinary one: the seed is derived from the
            # cache key, so the same line in the same window re-renders to the same take after the
            # cache is wiped. The other one is why this is a loop.
            #
            # This model occasionally returns NOTHING - a zero-length array for a perfectly
            # ordinary line. Measured on a 60 s slice: 1 of 23 lines came back empty, and re-running
            # the same nine short lines twice each reproduced it zero times, so it is the sampler
            # rolling badly rather than anything about the text. At that rate a 759-line episode
            # would silently lose about thirty lines, and the only trace would be one word in the
            # clip report. So an empty take is re-rolled on a fresh seed and never cached: a failure
            # gets another chance instead of becoming permanent, exactly as the paid backend treats
            # a failed call.
            attempts = max(1, int(self.v.get("max_retries", 3)))
            for attempt in range(attempts):
                reply = self._exchange(
                    {"cmd": "render", "text": u.text, "out": str(path),
                     "duration": duration_s, "seed": (int(key[:8], 16) + attempt) % (2 ** 31)},
                    float(self.v.get("render_timeout_s", 300)),
                )
                self.generated += 1
                self.gen_seconds += float(reply.get("gen_s", 0.0))
                got_s = float(reply.get("audio_s", 0.0))
                self.audio_seconds += got_s
                if self._long_enough(u.text, got_s):
                    break
                self.empty_takes += 1
                path.unlink(missing_ok=True)
                if attempt == attempts - 1:
                    # Last resort: ask for the line with NO duration at all. Three empties in a
                    # row is not the ~3% base rate rolling badly (that would be 0.003%), so
                    # something about this particular text-and-length is degenerate, and the
                    # unconstrained regime is the one the model is most reliable in. The line may
                    # then overrun its window - which the mix tolerates and the report counts.
                    # An overrun is recoverable; a silent line is not, and it is the one failure
                    # nobody notices until they are watching the episode.
                    reply = self._exchange(
                        {"cmd": "render", "text": u.text, "out": str(path),
                         "duration": None, "seed": (int(key[:8], 16) + attempts) % (2 ** 31)},
                        float(self.v.get("render_timeout_s", 300)),
                    )
                    self.generated += 1
                    self.gen_seconds += float(reply.get("gen_s", 0.0))
                    self.audio_seconds += float(reply.get("audio_s", 0.0))
                    if self._long_enough(u.text, float(reply.get("audio_s", 0.0))):
                        self.unconstrained += 1
                        say(f"    omnivoice: {u.text[:40]!r} would not fit its window at all, "
                            f"read at natural length instead")
                        break
                    # Everything above has failed, and the choice left is between a line that is
                    # too short and no line at all. Keep the take. The gate exists to catch a stub
                    # standing in for a whole sentence, but its floor is proportional to the TEXT,
                    # and on a one-syllable card the text predicts so little that a take failing
                    # the ratio is still most of the word - while a hole is the whole card gone,
                    # silently, with nothing in the report to point at it. The same reasoning the
                    # unconstrained attempt above already follows: an overrun is recoverable, a
                    # silent line is not, and so is a short one. Only a genuinely EMPTY take, which
                    # is the failure this retry loop was written for, still ends as silence.
                    if path.exists() and path.stat().st_size > 0:
                        self.kept_short += 1
                        say(f"    omnivoice: {u.text[:40]!r} came back short {attempts + 1}x, "
                            f"kept anyway rather than dropped")
                        break
                    path.unlink(missing_ok=True)
                    say(f"    !! omnivoice returned nothing {attempts + 1}x for {u.text[:48]!r}, "
                        f"including once with no length constraint -- this line will be silent")
                    return Rendered(audio=np.zeros(0, dtype=np.float32),
                                    sample_rate=self.sample_rate, speed=1.0, atempo=1.0,
                                    cached=False, billable_chars=0)

        audio = decode_mono(self.cfg, path.read_bytes(), self.sample_rate)
        # The 200 ms tail is silence the model pads with, not speech. It has to go before the mix
        # measures anything: assemble_voice places clips by their length, so a padded clip would
        # push its own neighbours and widen every gap by a fifth of a second.
        audio = trim_silence(audio, self.sample_rate, cfg=self.cfg)

        return Rendered(
            audio=audio,
            sample_rate=self.sample_rate,
            speed=1.0,      # the rate is an outcome of the duration asked for, not a setting
            atempo=1.0,     # nothing is stretched after the fact; the generation already fits
            cached=cached,
            billable_chars=0,
        )

    def close(self) -> None:
        if self.generated:
            rtf = self.gen_seconds / self.audio_seconds if self.audio_seconds else 0.0
            say(f"    omnivoice: {self.generated} lines generated, "
                f"{self.gen_seconds / 60:.1f} min of GPU for {self.audio_seconds / 60:.1f} min "
                f"of speech (RTF {rtf:.2f})"
                + (f", {self.empty_takes} empty take(s) re-rolled" if self.empty_takes else "")
                + (f", {self.unconstrained} read unconstrained after repeated failures"
                   if self.unconstrained else "")
                + (f", {self.kept_short} kept short rather than dropped"
                   if self.kept_short else ""))
            self.generated = 0

        self._reader = None
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.stdin.write(b'{"cmd": "quit"}\n')
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=15)
            except Exception:
                proc.kill()
                proc.wait(timeout=15)
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except Exception:
                    pass

        tmpdir, self.tmpdir = self.tmpdir, None
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.log_path = None


# ---------------------------------------------------------------------------
# The worker. Runs under voice.omnivoice.venv's python 3.11, never under ours.
# ---------------------------------------------------------------------------

_WORKER_SRC = r'''
"""Hold one OmniVoice model on the GPU and speak one line per request.

Reads {"cmd": "render", "text": ..., "out": ..., "duration": ..., "seed": ...} as one JSON object
per line on stdin and answers with one JSON object per line. Config arrives as argv[1].
"""

import json
import os
import sys
import time

# The protocol owns a private duplicate of fd 1 and fd 1 itself is pointed at stderr, so that
# nothing a model library decides to print can land in the middle of the stream.
_OUT = os.dup(1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def emit(obj):
    os.write(_OUT, (json.dumps(obj) + "\n").encode("utf-8"))


def fail(message, code=1):
    emit({"ok": False, "error": message})
    sys.exit(code)


CFG = json.loads(sys.argv[1])

try:
    import numpy as np
    import soundfile as sf
    import torch
    from omnivoice import OmniVoice
except Exception as exc:
    fail("cannot import torch/omnivoice from voice.omnivoice.venv: %s: %s"
         % (type(exc).__name__, exc))

if not torch.cuda.is_available():
    fail("torch.cuda.is_available() is False in %s -- this backend is GPU-only. Check the NVIDIA "
         "driver and that this venv's torch is a CUDA build (this one is torch %s)."
         % (sys.prefix, torch.__version__))

DTYPE = getattr(torch, CFG["dtype"])
REF = CFG.get("reference_wav") or None
LANG = CFG["language"]
STEPS = int(CFG["num_step"])

# Two load paths, and the second one is not paranoia. device_map="cuda:0" makes transformers run
# caching_allocator_warmup(), which asks CUDA for the whole model as ONE contiguous block - 2.28
# GiB for this checkpoint. On a GPU this process shares with other tenants that block is refused
# outright, and the error is a liar: measured with three other CUDA containers resident, a 1 GiB
# allocation succeeded and 2.28 GiB failed while both nvidia-smi and torch reported 22.75 GiB free
# and zero bytes allocated by torch. With those containers stopped the same call loads in 2.4 s.
# Loading onto the CPU and moving afterwards copies tensor by tensor, which the allocator serves
# whatever else is resident - 3.03 GiB, 1.1 s - so it is the fallback rather than the default:
# the direct path is faster when the GPU is ours, and this way a busy GPU degrades instead of
# failing.
model = None
try:
    model = OmniVoice.from_pretrained(CFG["model"], device_map="cuda:0", torch_dtype=DTYPE)
    load_path = "direct"
except Exception:
    try:
        model = OmniVoice.from_pretrained(CFG["model"], device_map="cpu", torch_dtype=DTYPE)
        model = model.to("cuda:0")
        load_path = "cpu-then-move"
    except Exception as exc:
        fail("could not load %s by either path: %s: %s"
             % (CFG["model"], type(exc).__name__, exc))

emit({"ok": True, "dtype": CFG["dtype"], "device": "cuda:0", "load_path": load_path,
      "vram_gib": torch.cuda.memory_allocated() / 2**30})

SR = 24000
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        req = json.loads(raw)
    except ValueError:
        fail("worker got a non-JSON request: %r" % raw[:200])
    if req.get("cmd") == "quit":
        break

    if req.get("cmd") == "render_batch":
        # One call, N lines, N separate durations. That last part is what makes batching free
        # here: the model is not given one window to share, every line keeps its own.
        items = req["items"]
        try:
            torch.manual_seed(int(items[0].get("seed", 0)) % (2 ** 31))
            t0 = time.perf_counter()
            with torch.inference_mode():
                outs = model.generate(text=[it["text"] for it in items],
                                      language=[LANG] * len(items),
                                      ref_audio=[REF] * len(items),
                                      duration=[it.get("duration") for it in items],
                                      generation_config=None, num_step=STEPS)
            gen_s = time.perf_counter() - t0
            secs = []
            for it, out in zip(items, outs):
                audio = np.asarray(out, dtype=np.float32)
                secs.append(len(audio) / SR)
                # A zero-length take is not written at all: the caller re-renders it one at a
                # time, and a file on disk would look like a cached silence forever.
                if audio.size:
                    sf.write(it["out"], audio, SR, subtype="PCM_16")
            emit({"ok": True, "gen_s": gen_s, "audio_s": secs})
        except Exception as exc:
            emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        continue

    if req.get("cmd") != "render":
        fail("unknown command %r" % req.get("cmd"))

    try:
        torch.manual_seed(int(req.get("seed", 0)) % (2**31))
        t0 = time.perf_counter()
        with torch.inference_mode():
            out = model.generate(text=req["text"], language=LANG, ref_audio=REF,
                                 duration=req.get("duration"),
                                 generation_config=None, num_step=STEPS)
        gen_s = time.perf_counter() - t0
        audio = np.asarray(out[0], dtype=np.float32)
        sf.write(req["out"], audio, SR, subtype="PCM_16")
        emit({"ok": True, "gen_s": gen_s, "audio_s": len(audio) / SR})
    except Exception as exc:
        emit({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
'''
