"""Chatterbox backend: free, GPU-bound, slow, and the only one that can clone a voice.

Both failures recorded in the recon were reproduced here and both are fixed. They had one root
cause: T3 had been cast to float16 to fit a WSL2 VRAM budget, and float16 cannot carry this
model's logits.

  1. Cloning raised `mat1 and mat2 must have the same dtype, but got Float and Half`. It is not a
     bad reference clip: prepare_conditionals() builds a fresh float32 T3Cond from the reference
     and hands it to float16 T3 weights. Fixed by casting the conditionals to T3's dtype at
     t3.inference, which is downstream of both places generate() rebuilds them.
  2. The sampler died at `torch.multinomial` in t3.py. In t3.py the CFG combine
     `cond + cfg * (cond - uncond)` and then `logits / temperature` amplify logit magnitude; past
     65504 float16 saturates to inf, softmax turns inf into nan and multinomial asserts. Fixed by
     running T3 in bfloat16, which carries the float32 exponent range at the same 2 bytes.
     The failure arrives as a CUDA device-side assert, which POISONS the context: the process
     cannot recover, so float16 is refused outright in prepare() rather than left as an option.

Measured on one machine (RTX 3090, WSL2), against the bench harness in the chatterbox venv:

  dtype     stability grid (24 gens)   VRAM delta   RTF          cloning
  float16   died on generation 1       2607 MiB     1.24-1.70    dtype crash
  bfloat16  24 / 24 passed             2607 MiB     1.40-2.11    works, cos 0.82-0.85
  float32   passed                     3716 MiB     1.23-1.58    works, cos 0.81-0.86

Cloning is real, not merely non-crashing: the model's own voice encoder puts a cloned clip at
cosine 0.81-0.86 to the reference against 0.63-0.67 for the builtin voice.

RTF ~1.6 means generation takes 1.6x longer than the audio lasts; Piper does the same work at
0.064, so this is still roughly 25x slower per second of speech and an episode is measured in
hours, not minutes. Nothing here is billable, so the cost of that is only time.

The model needs the python 3.11 venv at voice.chatterbox.venv (torch + chatterbox); the pipeline
runs on 3.13 and has no torch. So the model lives in a worker subprocess launched from that venv
and is spoken to over a JSON-per-line pipe. torch and chatterbox are therefore never imported into
this process at all, let alone at module import time.

Duration fitting: there is no length control whatsoever. Chars/second wandered between 8.4 and
13.6 across the stability grid for the same two lines, so nothing about a line's length is worth
predicting. Generate naturally, measure, then atempo toward the window, capped at
timing.max_atempo. needs_calibration stays False: the real duration is known before the stretch
is chosen, so there is no rate to calibrate.
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
from ..audio import decode_mono
from ..worker import LineReader
from .base import Backend, Rendered, Utterance

# Steady-state resident footprint, baseline -> after warmup, sampled with nvidia-smi. Per-process
# accounting is empty under WSL2, so the delta is the only honest way to isolate the model.
VRAM_CHATTERBOX_MIB = 2607      # bfloat16; float32 measured 3716 MiB
VRAM_TOLERANCE = 0.15

# Refused outright: fp16 logits overflow in the CFG combine and the resulting multinomial assert
# poisons the CUDA context, so it cannot even be caught and retried.
_DTYPES = frozenset({"bfloat16", "float32"})


def _file_identity(path: Path) -> str:
    """Content hash of the reference clip, so re-cutting the same speaker invalidates the cache."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def cache_key(text: str, params: dict, reference: str) -> str:
    blob = json.dumps([text, params, reference], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ChatterboxBackend(Backend):
    name = "chatterbox"
    billable = False
    needs_calibration = False  # duration is measured after generation, never predicted

    def __init__(self, cfg):
        super().__init__(cfg)
        self.v = cfg.backend_cfg("chatterbox")
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

    # -- lifecycle ---------------------------------------------------------

    def _params(self) -> dict:
        """Everything that changes the waveform. Also the cache key, so all of it must be here."""
        return {
            "dtype": str(self.v.get("dtype", "bfloat16")),
            "language_id": str(self.v.get("language_id", "pl")),
            "exaggeration": float(self.v.get("exaggeration", 0.5)),
            "cfg_weight": float(self.v.get("cfg_weight", 0.5)),
            "temperature": float(self.v.get("temperature", 0.8)),
            "seed": int(self.v.get("seed", 1234)),
        }

    def prepare(self) -> None:
        if self.proc is not None:
            return

        params = self._params()
        if params["dtype"] not in _DTYPES:
            why = ""
            if params["dtype"] == "float16":
                why = (
                    " float16 is refused on purpose, not for lack of support: its logits overflow "
                    "in the CFG combine and the multinomial assert that follows kills the CUDA "
                    "context outright (measured, 0 of 24 generations survived)."
                )
            raise SystemExit(
                f"voice.chatterbox.dtype {params['dtype']!r} is not supported -- use "
                f"{' or '.join(sorted(_DTYPES))}.{why}"
            )

        venv = Path(self.v.get("venv", "")).expanduser()
        python = venv / "bin" / "python"
        if not python.is_file():
            raise SystemExit(
                f"voice.chatterbox.venv does not hold a python at {python} -- point it at the "
                f"python 3.11 venv that has torch and chatterbox installed"
            )

        ref = str(self.v.get("reference_wav", "") or "")
        if ref:
            ref_path = Path(ref).expanduser()
            if not ref_path.is_file():
                raise SystemExit(f"voice.chatterbox.reference_wav does not exist: {ref_path}")
            self.reference = _file_identity(ref_path)
            ref = str(ref_path)

        self.tmpdir = tempfile.mkdtemp(prefix="lektor-chatterbox-")
        worker = Path(self.tmpdir) / "worker.py"
        worker.write_text(_WORKER_SRC, encoding="utf-8")

        # The model libraries write progress bars and deprecation warnings to stderr by the
        # screenful. Sent down a pipe nobody drains, that deadlocks the worker; a file cannot.
        self.log_path = Path(self.tmpdir) / "worker.log"
        log = self.log_path.open("wb")

        payload = dict(params)
        payload["reference_wav"] = ref
        env = dict(os.environ)
        env["TQDM_DISABLE"] = "1"

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

        # Loading and warming the model is the whole 30-60 s, and it is where a missing GPU
        # surfaces, so the ready handshake gets its own generous timeout.
        ready = self._exchange(None, float(self.v.get("load_timeout_s", 600)))
        load_s = time.perf_counter() - t0

        delta = int(ready.get("vram_delta_mib", 0))
        lo = VRAM_CHATTERBOX_MIB * (1 - VRAM_TOLERANCE)
        hi = VRAM_CHATTERBOX_MIB * (1 + VRAM_TOLERANCE)
        verdict = "within" if lo <= delta <= hi else "OUTSIDE"
        say(f"    chatterbox resident: {ready['dtype']} on {ready['device']} "
            f"vram_delta={delta}MiB ({verdict} +/-{int(VRAM_TOLERANCE * 100)}% of "
            f"{VRAM_CHATTERBOX_MIB}MiB), load {load_s:.1f}s")
        say(f"    voice: {'cloned from ' + Path(ref).name if ref else 'builtin'}, "
            f"warmup {ready['warmup_s']:.1f}s at RTF {ready['warmup_rtf']:.2f}")

    def set_chars_per_second(self, cps: float) -> None:
        """Ignored: this backend measures each line instead of predicting it."""

    @property
    def max_concurrency(self) -> int:
        # Fixed at 1 whatever the config says. There is one model on one GPU behind one worker
        # process with one request/response pipe, so extra threads would queue on that pipe for
        # nothing, and a second resident copy would cost another 2.6 GiB for no throughput.
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
                self._die("chatterbox worker died before it could be asked for the next line")

        # A reader thread, not select(): select accepts sockets only on Windows and raises on a
        # pipe, which was the single call standing between this pipeline and a native Windows run.
        if self._reader is None:
            self._reader = LineReader(self.proc)
        line = self._reader.read(timeout_s)
        if line is None:
            if self.proc.poll() is not None:
                self._die(f"chatterbox worker exited with code {self.proc.returncode}")
            self._die(f"chatterbox worker did not answer within {timeout_s:.0f}s")

        if not line:
            self._die("chatterbox worker closed its pipe without answering")
        try:
            reply = json.loads(line.decode("utf-8"))
        except ValueError:
            self._die(f"chatterbox worker sent a non-JSON reply: {line[:200]!r}")
        if not reply.get("ok"):
            self._die(f"chatterbox worker failed: {reply.get('error', 'no reason given')}")
        return reply

    # -- rendering ---------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        d = self.cache / key[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.wav"

    def render(self, u: Utterance) -> Rendered:
        if self.proc is None:
            raise RuntimeError("chatterbox backend used before prepare()")

        params = self._params()
        key = cache_key(u.text, params, self.reference)
        path = self._cache_path(key)
        cached = path.exists()

        if not cached:
            # Sampling is stochastic, so the seed is derived from the cache key: the same line
            # under the same settings re-renders to the same take after the cache is wiped.
            seed = int(key[:8], 16)
            reply = self._exchange(
                {"cmd": "render", "text": u.text, "out": str(path), "seed": seed},
                float(self.v.get("render_timeout_s", 300)),
            )
            self.generated += 1
            self.gen_seconds += float(reply.get("gen_s", 0.0))
            self.audio_seconds += float(reply.get("audio_s", 0.0))

        blob = path.read_bytes()
        audio = decode_mono(self.cfg, blob, self.sample_rate)

        # Nothing about generation could aim at the window, so the whole fit happens here, and
        # only ever by hurrying: a lektor is never asked to dawdle. Whatever max_atempo refuses
        # to absorb simply overruns into the next gap, which the mix stage already tolerates.
        atempo = 1.0
        if u.target_ms and u.target_ms > 0:
            window_s = u.target_ms / 1000.0
            actual_s = len(audio) / self.sample_rate
            if actual_s > window_s:
                atempo = min(float(self.timing["max_atempo"]), actual_s / window_s)
                if atempo > 1.01:
                    audio = decode_mono(self.cfg, blob, self.sample_rate, atempo=atempo)
                else:
                    atempo = 1.0

        return Rendered(
            audio=audio,
            sample_rate=self.sample_rate,
            speed=1.0,          # the model has no rate control; see the module docstring
            atempo=atempo,
            cached=cached,
            billable_chars=0,
        )

    def close(self) -> None:
        if self.generated:
            rtf = self.gen_seconds / self.audio_seconds if self.audio_seconds else 0.0
            say(f"    chatterbox: {self.generated} lines generated, "
                f"{self.gen_seconds / 60:.1f} min of GPU for {self.audio_seconds / 60:.1f} min "
                f"of speech (RTF {rtf:.2f})")
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

        # Keep the log if the worker died on us; there is nothing to read otherwise.
        tmpdir, self.tmpdir = self.tmpdir, None
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.log_path = None


# ---------------------------------------------------------------------------
# The worker. Runs under voice.chatterbox.venv's python 3.11, never under ours.
# ---------------------------------------------------------------------------

_WORKER_SRC = r'''
"""Hold one Chatterbox model on the GPU and speak one line per request.

Reads {"cmd": "render", "text": ..., "out": ..., "seed": ...} as one JSON object per line on
stdin and answers with one JSON object per line. Config arrives as argv[1].
"""

import json
import os
import subprocess
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


def vram_mib(samples=4):
    """nvidia-smi total-used sampling. Per-process accounting is empty under WSL2."""
    values = []
    for _ in range(samples):
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True)
        if r.returncode != 0:
            return -1
        values.append(int(r.stdout.strip().splitlines()[0]))
        time.sleep(0.15)
    return min(values)


CFG = json.loads(sys.argv[1])
BASELINE = vram_mib()

try:
    import torch
    import torchaudio
    from huggingface_hub import snapshot_download
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS, REPO_ID
    from chatterbox.models.t3.modules.cond_enc import T3Cond
except Exception as exc:
    fail("cannot import torch/chatterbox from voice.chatterbox.venv: %s: %s"
         % (type(exc).__name__, exc))

# GPU self-check. There is no CPU fallback on purpose: at RTF 1.6 on the GPU, CPU generation
# would take long enough to be indistinguishable from a hang.
if not torch.cuda.is_available():
    fail("torch.cuda.is_available() is False in %s -- the chatterbox backend is GPU-only, and "
         "falling back to CPU at this model's speed would look like a hang, so it refuses to "
         "start. Check the NVIDIA driver and that this venv's torch is a CUDA build (this one "
         "is torch %s)." % (sys.prefix, torch.__version__))
try:
    torch.zeros(1024, device="cuda").sum().item()
except Exception as exc:
    fail("CUDA is reported available but a trivial allocation failed: %s: %s"
         % (type(exc).__name__, exc))

DTYPE = getattr(torch, CFG["dtype"])
LANG = CFG["language_id"]
EXAG = CFG["exaggeration"]
CFGW = CFG["cfg_weight"]
TEMP = CFG["temperature"]

t0 = time.perf_counter()
try:
    ckpt = snapshot_download(
        repo_id=REPO_ID, repo_type="model", revision="main",
        allow_patterns=["ve.pt", "t3_mtl23ls_v2.safetensors", "s3gen.pt",
                        "grapheme_mtl_merged_expanded_v1.json", "conds.pt", "Cangjie5_TC.json"])
    model = ChatterboxMultilingualTTS.from_local(ckpt, "cpu")
    model.t3 = model.t3.to(device="cuda", dtype=DTYPE).eval()
    model.s3gen = model.s3gen.to("cuda").eval()
    model.ve = model.ve.to("cuda").eval()
    model.device = "cuda"
except torch.cuda.OutOfMemoryError as exc:
    fail("out of VRAM loading Chatterbox (%s needs about %d MiB): %s"
         % (CFG["dtype"], 3716 if CFG["dtype"] == "float32" else 2607, str(exc)[:200]))
except Exception as exc:
    fail("loading Chatterbox failed: %s: %s" % (type(exc).__name__, exc))


def cast_cond(cond):
    """Put a T3Cond in T3's own dtype.

    prepare_conditionals() builds float32 conditionals from the reference clip, and generate()
    rebuilds them again whenever exaggeration moves. Both paths meet the weights here, at T3's
    entry point, which is why the cast belongs here and not at either call site.
    """
    return T3Cond(
        speaker_emb=cond.speaker_emb.to(DTYPE),
        cond_prompt_speech_tokens=cond.cond_prompt_speech_tokens,
        emotion_adv=None if cond.emotion_adv is None else cond.emotion_adv.to(DTYPE),
    ).to(device="cuda")


_inference = model.t3.inference
model.t3.inference = lambda *a, t3_cond=None, **kw: _inference(*a, t3_cond=cast_cond(t3_cond), **kw)

reference = CFG.get("reference_wav") or ""
try:
    if reference:
        # Once, here, rather than per line: generate(audio_prompt_path=...) re-runs the whole
        # librosa load, resample and speaker-embedding pass on every single call.
        model.prepare_conditionals(reference, exaggeration=EXAG)
    model.conds = model.conds.to("cuda")
    model.conds.t3 = cast_cond(model.conds.t3)
except Exception as exc:
    fail("preparing the voice from %s failed: %s: %s" % (reference or "the builtin speaker",
                                                         type(exc).__name__, exc))


def generate(text, seed):
    torch.manual_seed(seed)
    started = time.perf_counter()
    wav = model.generate(text, language_id=LANG, exaggeration=EXAG,
                         cfg_weight=CFGW, temperature=TEMP)
    return wav, time.perf_counter() - started


try:
    torch.manual_seed(CFG["seed"])
    warm, warm_s = generate("Rozgrzewka lektora.", CFG["seed"])
    torch.cuda.synchronize()
except Exception as exc:
    fail("the warmup generation failed: %s: %s" % (type(exc).__name__, exc))

after = vram_mib()
emit({"ok": True, "event": "ready", "device": torch.cuda.get_device_name(0),
      "torch": torch.__version__, "dtype": CFG["dtype"], "sample_rate": model.sr,
      "vram_baseline_mib": BASELINE, "vram_after_mib": after,
      "vram_delta_mib": (after - BASELINE) if after > 0 and BASELINE > 0 else 0,
      "load_s": round(time.perf_counter() - t0, 2), "warmup_s": round(warm_s, 2),
      "warmup_rtf": round(warm_s / (warm.shape[-1] / model.sr), 3),
      "cloned": bool(reference)})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    if req.get("cmd") == "quit":
        break
    try:
        wav, gen_s = generate(req["text"], int(req["seed"]))
        torchaudio.save(req["out"], wav, model.sr)
    except Exception as exc:
        message = "%s: %s" % (type(exc).__name__, exc)
        if "CUDA" in message:
            # A device-side assert poisons the context for good; every later call would fail the
            # same way, so the worker stops instead of pretending it can carry on.
            fail("generation hit a CUDA fault and the context is now unusable -- %s" % message, 2)
        emit({"ok": False, "error": "generating %r failed: %s" % (req["text"][:60], message)})
        continue
    emit({"ok": True, "gen_s": round(gen_s, 3),
          "audio_s": round(wav.shape[-1] / model.sr, 3)})
'''
