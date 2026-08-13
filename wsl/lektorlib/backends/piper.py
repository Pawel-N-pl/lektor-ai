"""Piper backend: free, fast, and the only engine with a direct duration knob.

CPU only. Measured on this machine: CUDA renders a 598-cue episode in 136.5 s against CPU's
138.4 s, i.e. 1.4%, because short cues are dominated by per-call overhead rather than inference,
and the CUDA path has already failed once with an onnxruntime EP error. onnxruntime-gpu is not
installed and must not be; there is deliberately no `use_cuda` config key to invite it back.

Where the speed actually is: one ONNX session pinned to ONE intra-op thread, driven by a pool of
16 worker threads. Measured over 150 real cues from the episode --

      intra-op threads   workers   cues/s
      auto (16)                1     9.9
      1                        1     5.9
      1                        4    20.6
      1                        8    30.1
      1                       16    35.2   <- chosen
      1                       20    32.6
      2                       16    33.5

Per-utterance parallelism beats intra-op parallelism by 3.6x: these clips are 1-4 s long, so
onnxruntime's thread pool spends its time synchronising rather than working. That puts a whole
598-cue episode at roughly 17 s instead of the 138 s in the recon brief.

Duration fitting: `length_scale` is a near-perfectly linear duration knob (dur = A*ls + B, with B
the fixed head/tail cost), so this backend does not predict and does not calibrate. It speaks the
line once at the natural rate, and only if that overruns the window does it solve the line for the
rate that fits and re-speak. atempo stays at 1.0 throughout: resynthesising at the right
length_scale sounds better than stretching, and it is nearly free.

That linearity is only usable because `noise_w_scale` is pinned to 0. The stochastic duration
predictor is what makes a VITS model vary its phoneme lengths run to run, and at the model default
of 0.8 the same text at the same length_scale came out between 4783 ms and 5050 ms across eight
runs -- 5.4% of jitter against a 60 ms tolerance. At 0 the duration is bit-exact reproducible, and
a flat, unvarying delivery is the lektor profile anyway.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .. import say
from ..audio import shorten_pauses
from .base import Backend, Rendered, Utterance

VOICES_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "{family}/{lang}/{name}/{quality}/{lang}-{name}-{quality}{ext}"
)

# Polish voices in rhasspy/piper-voices, all single-speaker:
#   pl_PL-darkman-medium    male, flat, unexpressive        <- the lektor profile, the default
#   pl_PL-bass-high         male, "high" tier, ~3x slower to synthesise
#   pl_PL-mc_speech-medium  male, more clipped and clerical
#   pl_PL-gosia-medium      female
#   pl_PL-mls_6892-low      male, "low" tier, audibly rough
DEFAULT_VOICE = "pl_PL-darkman-medium"


def cache_key(text: str, voice: str, length_scale: float, syn: dict) -> str:
    blob = json.dumps(
        [text, voice, round(length_scale, 4), syn], sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PiperBackend(Backend):
    name = "piper"
    billable = False
    needs_calibration = False  # length_scale is measured per line, not predicted from a rate

    def __init__(self, cfg):
        super().__init__(cfg)
        self.v = cfg.backend_cfg("piper")
        self.cache = cfg.cache_dir
        self.voice = None
        self.sample_rate = 22050
        self.model_path: Path | None = None
        self.calls = 0          # syntheses actually run
        self.hits = 0           # cache reads
        self.iterations = 0     # syntheses across every search, cached or not
        self.lines = 0
        self.gen_s = 0.0
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def _voice_name(self) -> str:
        return str(self.v.get("voice") or DEFAULT_VOICE)

    def _voice_dir(self) -> Path:
        d = self.v.get("voice_dir") or "~/.cache/lektor-ai/piper-voices"
        return Path(str(d)).expanduser()

    def _resolve_model(self) -> Path:
        """Where the .onnx lives, downloading it on first use."""
        if self.v.get("model_path"):
            p = Path(str(self.v["model_path"])).expanduser()
            if not p.is_file():
                raise SystemExit(f"voice.piper.model_path points at nothing: {p}")
            return p

        name = self._voice_name()
        d = self._voice_dir()
        model = d / f"{name}.onnx"
        if model.is_file() and (d / f"{name}.onnx.json").is_file():
            return model

        d.mkdir(parents=True, exist_ok=True)
        say(f"    piper: downloading voice {name} into {d} (once, ~63 MB)")
        try:
            from piper.download_voices import download_voice

            download_voice(name, d)
        except Exception as exc:
            # piper skips a re-download for any file that exists and is non-empty, so a half
            # written .onnx would look complete on the next run. Clear the wreckage.
            for leftover in (model, d / f"{name}.onnx.json"):
                leftover.unlink(missing_ok=True)
            family, _, _ = name.partition("_")
            lang, _, tail = name.partition("-")
            vname, _, quality = tail.partition("-")
            url = VOICES_URL.format(
                family=family, lang=lang, name=vname, quality=quality, ext=".onnx"
            )
            raise SystemExit(
                f"could not download the piper voice {name}: {exc}\n"
                f"fetch it by hand into {d} (the .onnx.json next to it is required too):\n"
                f"  curl -L -o {d / f'{name}.onnx'} {url}\n"
                f"  curl -L -o {d / f'{name}.onnx.json'} {url}.json"
            ) from exc
        if not model.is_file():
            raise SystemExit(f"piper reported the voice downloaded but {model} is not there")
        return model

    def prepare(self) -> None:
        try:
            import onnxruntime
            from piper import PiperVoice
            from piper.config import PiperConfig
        except ImportError as exc:
            raise SystemExit(
                f"the piper backend needs the piper-tts package: {exc}\n"
                f"  uv pip install piper-tts        (CPU wheels; do NOT add onnxruntime-gpu)"
            ) from exc

        self.model_path = self._resolve_model()
        cfg_path = Path(str(self.model_path) + ".json")
        if not cfg_path.is_file():
            raise SystemExit(f"the voice config {cfg_path} is missing next to the .onnx")

        # PiperVoice.load() builds a default SessionOptions, which lets onnxruntime take every
        # core for one utterance. That is the slow arrangement here (see the table above), so the
        # session is built by hand and the dataclass assembled around it.
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = max(1, int(self.v.get("threads", 1)))
        opts.inter_op_num_threads = 1
        t0 = time.time()
        session = onnxruntime.InferenceSession(
            str(self.model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        voice_cfg = PiperConfig.from_dict(json.loads(cfg_path.read_text(encoding="utf-8")))
        self.voice = PiperVoice(session=session, config=voice_cfg)
        self.sample_rate = int(voice_cfg.sample_rate)
        say(f"    piper: {self.model_path.name} loaded in {time.time() - t0:.1f}s "
            f"({self.sample_rate} Hz, {opts.intra_op_num_threads} thread(s) x "
            f"{self.max_concurrency} workers)")

    @property
    def max_concurrency(self) -> int:
        return max(1, int(self.v.get("concurrency", 16)))

    def close(self) -> None:
        if self.lines:
            per = self.gen_s / max(1, self.calls)
            say(f"    piper: {self.lines} lines, {self.iterations} syntheses "
                f"({self.iterations / self.lines:.2f} per line), {self.hits} from cache, "
                f"{per * 1000:.0f} ms each")
        self.voice = None

    # -- synthesis ---------------------------------------------------------

    def _syn_kwargs(self) -> dict:
        """Everything except length_scale, so it can go into the cache key unchanged."""
        speaker = int(self.v.get("speaker", 0))
        return {
            # Every pl_PL voice is single-speaker; None then means "the voice's own default".
            "speaker_id": speaker if speaker > 0 else None,
            "noise_scale": float(self.v.get("noise_scale", 0.667)),
            "noise_w_scale": float(self.v.get("noise_w_scale", 0.0)),
            "normalize_audio": False,
        }

    def _cache_path(self, key: str) -> Path:
        d = self.cache / key[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.wav"

    def _post(self, audio: np.ndarray) -> np.ndarray:
        """Compress the pauses the model puts at commas, exactly as the paid backend does.

        Applied on the way out of the cache rather than on the way in: the cache holds what the
        model synthesised, and the pause settings are a mix decision that must be free to change
        without throwing away a voice's worth of synthesis. Doing it here also means the duration
        search below measures the audio that will really be laid down, not a longer draft of it.
        """
        return shorten_pauses(self.cfg, audio, self.sample_rate)

    def _speak(self, text: str, length_scale: float) -> tuple[np.ndarray, bool]:
        """One synthesis at one length_scale, through the disk cache. Returns (mono, cached)."""
        from piper import SynthesisConfig

        syn = self._syn_kwargs()
        path = self._cache_path(cache_key(text, self._voice_name(), length_scale, syn))
        if path.exists():
            with self._lock:
                self.hits += 1
                self.iterations += 1
            return self._post(_read_wav(path)), True

        cfg = SynthesisConfig(length_scale=float(length_scale), **syn)
        t0 = time.time()
        parts = [c.audio_float_array for c in self.voice.synthesize(text, syn_config=cfg)]
        elapsed = time.time() - t0
        audio = (np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)).astype(
            np.float32, copy=False
        )
        _write_wav(path, audio, self.sample_rate)
        with self._lock:
            self.calls += 1
            self.iterations += 1
            self.gen_s += elapsed
        return self._post(audio), False

    def _ms(self, audio: np.ndarray) -> float:
        return audio.size / self.sample_rate * 1000.0

    # -- rendering ---------------------------------------------------------

    def render(self, u: Utterance) -> Rendered:
        text = (u.text or "").strip()
        if not text:
            # A cue that cleaned down to nothing still occupies a slot in the clip list, so it
            # gets silence rather than a trip through espeak, which rejects empty input.
            return Rendered(np.zeros(0, dtype=np.float32), self.sample_rate, 1.0, 1.0, True, 0)

        lo = float(self.v.get("search_min", 0.6))
        # The natural rate doubles as the search ceiling: a lektor is only ever asked to hurry,
        # never to dawdle, so the search only ever moves down from here.
        base = _clamp(float(self.v.get("length_scale", 1.0)), lo,
                      float(self.v.get("search_max", 1.6)))
        tol = float(self.v.get("tolerance_ms", 60))
        budget = max(1, int(self.v.get("search_iterations", 6)))
        target = float(u.target_ms) if u.target_ms and u.target_ms > 0 else 0.0

        audio, cached = self._speak(text, base)
        ls = base
        hits_only = cached
        if target and self._ms(audio) > target:
            ls, audio, cached = self._search(text, target, base, self._ms(audio), lo, tol, budget)
            hits_only = hits_only and cached

        with self._lock:
            self.lines += 1

        # speed is reported the way the pipeline reads it -- how much faster than natural the line
        # was read -- so a line that hit the search_min clamp shows up as the clamp's speed.
        return Rendered(
            audio=audio,
            sample_rate=self.sample_rate,
            speed=round(base / ls, 3) if ls > 0 else 1.0,
            atempo=1.0,
            cached=hits_only,
            billable_chars=0,
        )

    def _search(self, text: str, target: float, base: float, base_ms: float,
                lo: float, tol: float, budget: int):
        """Find the largest length_scale whose duration still fits in target.

        Duration rises with length_scale in a straight line over any long span -- on a real cue
        0.6 -> 3042 ms, 1.0 -> 4807 ms, 1.6 -> 7012 ms -- but up close it is a staircase, because
        every phoneme length is rounded to a whole number of decoder frames. On a 30-character cue
        the steps are 20-80 ms wide, which is wider than the 60 ms tolerance, so the exact window
        is often not reachable at all and a search that insists on it burns its whole budget
        drifting away from the best answer it already had.

        Hence regula falsi over a bracket rather than a fit through every point: the two bracket
        ends are the closest measurements either side of the target, so the line drawn between
        them tracks the local staircase instead of the global slope, and the search stops as soon
        as the bracket is narrower than the staircase can resolve. Undershoot is harmless -- the
        gap after the line is just silence -- so what comes back is always the SLOWEST rate that
        still fits, never a faster one that happens to be nearer the target.
        """
        hi = base
        pts = [(hi, base_ms)]
        best: tuple[float, np.ndarray, bool] | None = None

        # The first correction is proportional. It cannot overshoot into chipmunk territory,
        # because ignoring the intercept always asks for less speed-up than is really needed.
        guess = _clamp(hi * target / base_ms, lo, hi)

        for _ in range(budget):
            audio, cached = self._speak(text, guess)
            ms = self._ms(audio)
            pts.append((guess, ms))

            if ms <= target:
                if best is None or guess > best[0]:
                    best = (guess, audio, cached)
                if ms >= target - tol:
                    break
            elif guess <= lo + 1e-6:
                # Unreachable: the line does not fit even flat out. Hand back the clamp and let
                # the mix stage deal with the overrun.
                if best is None:
                    best = (guess, audio, cached)
                break

            lo_b, hi_b = _bracket(pts, target, lo, hi)
            if hi_b - lo_b < 0.01:
                break
            # Regula falsi keeps one end of the bracket forever when the curve is convex over it,
            # and on a staircase that is the usual case: the estimate then creeps toward the
            # target from one side and the budget runs out short of the answer. Confining it to
            # the middle 80% of the bracket turns those steps into bisections and it converges.
            span = hi_b - lo_b
            nxt = _clamp(_falsi(pts, target), lo_b + 0.2 * span, hi_b - 0.2 * span)
            # A repeat means the staircase has gone flat under the solver; another synthesis of
            # the same thing would teach it nothing.
            if any(abs(nxt - x) < 1e-4 for x, _ in pts):
                break
            guess = nxt

        if best is None:
            # Every probe overran without ever reaching the clamp, which only happens when the
            # budget ran out mid-search. Flat out is the closest thing to a fit still available.
            audio, cached = self._speak(text, lo)
            best = (lo, audio, cached)
        return best


def _clamp(x: float, lo: float, hi: float) -> float:
    return round(min(hi, max(lo, x)), 4)


def _bracket(pts, target: float, lo: float, hi: float) -> tuple[float, float]:
    """The narrowest measured interval the answer is known to lie in."""
    under = [x for x, y in pts if y <= target]
    over = [x for x, y in pts if y > target]
    return (max(under) if under else lo, min(over) if over else hi)


def _falsi(pts, target: float) -> float:
    """length_scale for `target` ms, from the line through the two nearest measurements.

    Once the target is bracketed those two are its neighbours either side, which is what keeps
    the estimate on the local step rather than on the average slope of the whole curve. Before it
    is bracketed they are the two fastest overruns, i.e. the same line extrapolated downwards.
    """
    under = sorted((y, x) for x, y in pts if y <= target)
    over = sorted((y, x) for x, y in pts if y > target)
    if under:
        y0, x0 = under[-1]
        y1, x1 = over[0]
    elif len(over) >= 2:
        y0, x0 = over[0]
        y1, x1 = over[1]
    else:
        y1, x1 = over[0]
        return x1 * target / max(1.0, y1)
    if y1 - y0 < 1e-6:
        return (x0 + x1) / 2.0
    return x0 + (target - y0) * (x1 - x0) / (y1 - y0)


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """16-bit mono, written aside and renamed so 16 worker threads cannot read a half-file."""
    pcm = np.clip(audio, -1.0, 1.0) * 32767.0
    tmp = path.with_suffix(f".{threading.get_ident():x}.tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.astype(np.int16).tobytes())
    tmp.replace(path)
