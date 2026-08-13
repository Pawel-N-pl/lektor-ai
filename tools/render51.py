#!/usr/bin/env python3
"""Full episode lektor, 5.1, voice on the centre channel.

Reads the original E-AC-3 5.1 straight from the mkv, condenses the subtitles into a lektor
script, reads it with ElevenLabs, and writes a new 5.1 E-AC-3 in which every channel ducks
under the lektor and the lektor itself lands only on FC -- where dialogue lives, and where a
stereo downmix folds it evenly into both speakers.

Standalone on purpose: lektorlib's write_mix is stereo, and the pipeline's own 5.1 support has
not landed yet. Everything here is meant to be folded back into it.
"""
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

SC = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path("/home/pawel/projects/lektor-ai")
sys.path.insert(0, str(SC))
import lektor_original as L  # noqa: E402
sys.path.insert(0, "/home/pawel/projects/lektor-ai/wsl")
from lektorlib.audio import trim_silence as TRIM, apply_shelf, shorten_pauses  # noqa: E402
from lektorlib.mix import limit_block  # noqa: E402
# Ducking comes from the package, not from the pre-refactor module: the hold that stops the
# envelope pumping between neighbouring lines lives there.
from lektorlib.mix import build_duck_envelope, assemble_voice  # noqa: E402

# Episode to render: pass the mkv path, or fall back to the one this was first written for.
_DIR = pathlib.Path("/mnt/e/Filmy/Ted.Lasso.S02.PLSUB.1080p.ATVP.WEB-DL.DDPA5.1.H.264-NTb")
SRC = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
    _DIR / "Ted.Lasso.S02E01.Goodbye.Earl.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb.mkv")
WORK = SRC.parent / "_lektor"
WORK.mkdir(exist_ok=True)
OUT = WORK / f"{SRC.stem}.lektor51.eac3"
CACHE = SC / "tts_cache"
CACHE.mkdir(exist_ok=True)

SR = 48000
CH = 6                      # FL FR FC LFE SL SR -- ffmpeg's 5.1(side) order
FC = 2
VOICE_OVER_DB = 7.0 + 3.0   # +3 because one channel replaces two: L+R summed 3 dB the stereo mix did not
DUCK_TO_DB = -7.0
PROGRAM_GAIN_DB = 4.0
CEILING = L.db_to_lin(-1.0)
CPS = 14.5

EL_KEY = os.environ["ELEVENLABS_API_KEY"]
OR_KEY = os.environ["OPENROUTER_API_KEY"]
CFG = json.loads((ROOT / "lektor.config.json").read_text())
EL = CFG["voice"]["elevenlabs"]
M = CFG["mix"]
MAX_SPEED = float(CFG["timing"]["max_speed"])
MAX_ATEMPO = float(CFG["timing"]["max_atempo"])
STABILITY = 1.0             # the setting that stopped Bill acting


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# --- cues -------------------------------------------------------------------

def load_script():
    """Take the lektor script the pipeline's `script` stage already produced.

    Condensation lives in the pipeline now, re-rolls included; this file is only here for the 5.1
    mix that the pipeline cannot do yet.
    """
    p = WORK / f"{SRC.stem}.lektor.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    cues = []
    for d in data["cues"]:
        c = L.Cue(idx=d["id"], start_ms=d["start_ms"], end_ms=d["end_ms"], raw=d["text"])
        c.text = d["text"]
        c.window_ms = d["window_ms"]
        c.budget_chars = d["budget_chars"]
        cues.append(c)
    over = sum(1 for c in cues if len(c.text) > c.budget_chars)
    log(f"skrypt z pipeline: {len(cues)} kwestii, {sum(len(c.text) for c in cues)} znakow, "
        f"{over} nadal ponad limit")
    old = dict(CFG)
    old["paths"] = dict(CFG["paths"], input_dir_wsl=str(SC), work_subdir="testwork")
    old["voice"] = dict(EL, concurrency=8)
    cp = SC / "full_cfg.json"
    cp.write_text(json.dumps(old))
    return L.Config(cp), cues


def load_cues():
    old = dict(CFG)
    old["paths"] = dict(CFG["paths"], input_dir_wsl=str(SC), work_subdir="testwork")
    old["voice"] = dict(EL, concurrency=8)
    p = SC / "full_cfg.json"
    p.write_text(json.dumps(old))
    C = L.Config(p)
    cues = [c for c in L.parse_srt(SC / "testwork/Ted.Lasso.S02E01.Test.srt") if not L.should_skip(c.raw, C)]
    for c in cues:
        c.text = L.clean_text(c.raw, C)
    cues = [c for c in cues if c.text]
    L.compute_windows(cues, C)
    for c in cues:
        c.budget_chars = max(12, int(c.window_ms / 1000 * CPS * C["timing"]["safety"]))
    return C, cues


# --- condensation -----------------------------------------------------------

SCHEMA = {"type": "json_schema", "json_schema": {"name": "lektor_cues", "strict": True, "schema": {
    "type": "object", "properties": {"cues": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"], "additionalProperties": False}}},
    "required": ["cues"], "additionalProperties": False}}}


def condense(cues):
    import bench_condense as B
    targets = [i for i, c in enumerate(cues) if len(c.text) > c.budget_chars]
    log(f"kondensacja: {len(targets)} z {len(cues)} kwestii nie miesci sie w oknie")
    body = {"model": CFG["script"]["openrouter_model"], "temperature": 0.3, "max_tokens": 48000,
            "response_format": SCHEMA, "reasoning": {"effort": CFG["script"]["reasoning_effort"]},
            "messages": [{"role": "system", "content": B.SYSTEM},
                         {"role": "user", "content": B.whole_prompt(cues, targets)}]}
    req = urllib.request.Request(
        f"{CFG['script']['openrouter_base']}/chat/completions",
        headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/local/lektor-ai", "X-Title": "lektor-ai"},
        data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=900) as f:
        r = json.load(f)
    got = {int(c["id"]): c["text"].strip()
           for c in json.loads(r["choices"][0]["message"]["content"])["cues"]}
    applied = fit = 0
    for i in targets:
        c = cues[i]
        new = got.get(c.idx)
        if new and len(new) < len(c.text):
            c.text = new
            applied += 1
        if len(c.text) <= c.budget_chars:
            fit += 1
    u = r["usage"]
    log(f"  skrocono {applied}, w limicie {fit}/{len(targets)}, koszt ${u.get('cost', 0):.4f}")
    (WORK / f"{SRC.stem}.lektor.json").write_text(json.dumps(
        {"base": SRC.stem, "cues": [{"id": c.idx, "start_ms": c.start_ms, "end_ms": c.end_ms,
                                     "window_ms": c.window_ms, "budget_chars": c.budget_chars,
                                     "text": c.text} for c in cues]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return u.get("cost", 0)


# --- speech -----------------------------------------------------------------

def tts(text, speed, prev, nxt):
    key = hashlib.sha256(
        f"{text}|{EL['voice_id']}|{STABILITY}|{speed}|{prev}|{nxt}|{EL['model_id']}".encode()).hexdigest()
    p = CACHE / f"{key}.mp3"
    if p.exists():
        return p.read_bytes(), 0
    body = {"text": text, "model_id": EL["model_id"], "previous_text": prev, "next_text": nxt,
            "voice_settings": {"stability": STABILITY, "similarity_boost": 0.75, "style": 0.0,
                               "use_speaker_boost": False, "speed": round(speed, 2)}}
    for att in range(6):
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{EL['voice_id']}?output_format={EL['output_format']}",
            headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
            data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as f:
                d = f.read()
            p.write_bytes(d)
            return d, len(text)
        except urllib.error.HTTPError as e:
            if e.code in (409, 429) or e.code >= 500:
                time.sleep(1.5 * (att + 1) + random.random())
                continue
            raise
    raise RuntimeError("TTS retries exhausted")


def plan_speeds(cues, C):
    """Per-line speed, smoothed across neighbours instead of spiking on one line.

    A line at 1.2 between lines at 1.0 is heard as the lektor suddenly hurrying and then stopping.
    Spreading part of the speed-up over everything within speed_smooth_s turns that step into a
    gradient, and because the neighbours now finish earlier the crowded line has more room than it
    had alone - so the peak itself comes down, which is what speed_relax banks.
    """
    t = C["timing"]
    span = float(t.get("speed_smooth_s", 0.0))
    relax = float(t.get("speed_relax", 1.0))
    raw = []
    for c in cues:
        w = c.window_ms / 1000.0 * t["safety"]
        raw.append(min(MAX_SPEED, max(1.0, (len(c.text) / CPS) / w)) if w > 0 else 1.0)
    if span <= 0:
        return [round(x, 2) for x in raw]

    mid = [c.start_ms / 1000.0 for c in cues]
    out = []
    for i in range(len(cues)):
        best = raw[i]
        j = i - 1
        while j >= 0 and mid[i] - mid[j] <= span:
            best = max(best, 1.0 + (raw[j] - 1.0) * (1.0 - (mid[i] - mid[j]) / span))
            j -= 1
        j = i + 1
        while j < len(cues) and mid[j] - mid[i] <= span:
            best = max(best, 1.0 + (raw[j] - 1.0) * (1.0 - (mid[j] - mid[i]) / span))
            j += 1
        out.append(round(min(MAX_SPEED, 1.0 + (best - 1.0) * relax), 2))
    return out


def synthesise(C, cues, total_samples):
    done = [0, 0]
    speeds = plan_speeds(cues, C)
    bumped = sum(1 for i, c in enumerate(cues)
                 if speeds[i] > 1.005 and (len(c.text) / CPS) <= c.window_ms / 1000.0 * C["timing"]["safety"])
    log(f"  tempo: {sum(1 for x in speeds if x > 1.005)} kwestii przyspieszonych, "
        f"z czego {bumped} wylacznie dla wygladzenia sasiadow")

    def work(i):
        c = cues[i]
        prev = cues[i - 1].text if i > 0 else ""
        nxt = cues[i + 1].text if i + 1 < len(cues) else ""
        w = c.window_ms / 1000.0 * C["timing"]["safety"]
        sp = speeds[i]
        mp3, n = tts(c.text, sp, prev, nxt)
        # Pause compression before trimming: it changes the length, and the trim decides the ends.
        au = TRIM(shorten_pauses(C, L.decode(C, mp3, SR), SR), SR)
        at = 1.0
        if MAX_ATEMPO > 1.01 and len(au) / SR > w:
            at = min(MAX_ATEMPO, (len(au) / SR) / w)
            au = TRIM(shorten_pauses(C, L.decode(C, mp3, SR, atempo=at), SR), SR)
        done[0] += 1
        done[1] += n
        if done[0] % 50 == 0:
            log(f"  {done[0]}/{len(cues)}")
        return L.Clip(cue={"start_ms": c.start_ms, "window_ms": c.window_ms, "text": c.text},
                      audio=au, speed=sp, atempo=at)

    with ThreadPoolExecutor(max_workers=8) as pool:
        clips = list(pool.map(work, range(len(cues))))
    voice, spans = assemble_voice(C, clips, total_samples)
    log(f"  {done[0]} kwestii, {done[1]} znakow nowych (~${done[1] / 1000 * 0.05:.2f})")
    return voice, spans, clips, done[1]


# --- level ------------------------------------------------------------------

def program_level_stereo():
    """RMS of a stereo fold of the source, so the level matches what was tuned by ear."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(SRC), "-map", "0:a:0",
                          "-ac", "2", "-ar", str(SR), "-f", "f32le", "pipe:1"],
                         stdout=subprocess.PIPE)
    acc = 0.0
    n = 0
    while True:
        b = p.stdout.read(1 << 22)
        if not b:
            break
        x = np.frombuffer(b, dtype=np.float32)
        acc += float(np.sum(np.square(x, dtype=np.float64)))
        n += x.size
    p.wait()
    return 20.0 * np.log10(max(1e-9, np.sqrt(acc / max(1, n))))


# --- mix --------------------------------------------------------------------

def _decoder(fg=None, label=None):
    """One decode of the source audio, optionally through a filtergraph, as raw 6-channel float."""
    a = ["ffmpeg", "-v", "error", "-i", str(SRC)]
    if fg:
        a += ["-filter_complex", fg, "-map", label]
    else:
        a += ["-map", "0:a:0"]
    a += ["-ar", str(SR), "-f", "f32le", "pipe:1"]
    return subprocess.Popen(a, stdout=subprocess.PIPE)


def _crossover(band):
    """Filtergraphs for the speech band and for everything else.

    A Linkwitz-Riley crossover, because the two halves have to sum back to the original when their
    gains are equal. The obvious shortcut - band-pass the middle and call the difference "the rest"
    - does not: an IIR band-pass shifts phase, so the difference still carries the midrange at full
    level and the dip vanishes the moment the two are given different gains. Measured, that shortcut
    left the speech band 6.8 dB LOUDER than broadband ducking instead of quieter.
    """
    split = f"acrossover=split={band[0]} {band[1]}"
    mid = f"[0:a]{split}[a][b][c];[a]anullsink;[c]anullsink"
    rest = f"[0:a]{split}[a][b][c];[b]anullsink;[a][c]amix=inputs=2:normalize=0[r]"
    return (mid, "[b]"), (rest, "[r]")


def mix_51(voice, env_mid, env_rest, total_samples):
    """Mix the lektor in, ducking either the whole programme or only the band it competes for.

    Spectral mode reads the source twice: once whole and once band-passed to the speech range. The
    rest of the spectrum is then the DIFFERENCE of the two, which reconstructs exactly - no phase
    error to worry about, unlike splitting into filtered bands and adding them back up.
    """
    band = M.get("duck_band_hz", [300, 4000])
    spectral = M.get("duck_mode", "broadband") == "spectral"
    dec = _decoder()
    if spectral:
        (fg_mid, lab_mid), (fg_rest, lab_rest) = _crossover(band)
        dmid = _decoder(fg_mid, lab_mid)
        drest = _decoder(fg_rest, lab_rest)
    else:
        dmid = drest = None

    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y",
                            "-f", "f32le", "-ar", str(SR), "-channel_layout", "5.1(side)",
                            "-i", "pipe:0", "-c:a", "eac3", "-b:a", "768k", str(OUT)],
                           stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    block = SR * 5
    pos = 0
    ctrl = 200
    peak_hits = 0
    release_ms = float(M.get("limiter_release_ms", 120.0))
    release_coef = float(np.exp(-1.0 / (release_ms / 1000.0 * SR)))
    carry = 0.0
    try:
        while pos < total_samples:
            want = min(block, total_samples - pos)
            raw = dec.stdout.read(want * CH * 4)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.float32).reshape(-1, CH).copy()
            got = x.shape[0]
            idx = np.linspace(pos / SR * ctrl, (pos + got) / SR * ctrl, got)
            gm = np.interp(idx, np.arange(env_mid.size), env_mid).astype(np.float32)

            if spectral:
                def _read(dec):
                    raw = dec.stdout.read(got * CH * 4)
                    z = np.frombuffer(raw, dtype=np.float32).reshape(-1, CH).copy()
                    return np.pad(z, ((0, got - z.shape[0]), (0, 0))) if z.shape[0] < got else z
                mid = _read(dmid)
                rest = _read(drest)
                gr = np.interp(idx, np.arange(env_rest.size), env_rest).astype(np.float32)
                x = rest * gr[:, None] + mid * gm[:, None]
            else:
                x *= gm[:, None]

            x[:, FC] += voice[pos:pos + got]
            before = float(np.max(np.abs(x))) if x.size else 0.0
            x, carry = limit_block(x, CEILING, release_coef, carry)
            if before > CEILING:
                peak_hits += 1
            enc.stdin.write(x.astype(np.float32).tobytes())
            pos += got
    finally:
        enc.stdin.close()
        err = enc.stderr.read().decode()
        for d in (dec, dmid, drest):
            if d:
                d.stdout.close()
                d.wait()
        if enc.wait() != 0:
            raise RuntimeError(f"encode failed: {err[:400]}")
    return peak_hits


def main():
    C, cues = load_script()
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "csv=p=0", str(SRC)], capture_output=True, text=True).stdout)
    total = int(dur * SR)
    or_cost = 0.0
    log("synteza mowy")
    voice, spans, clips, chars = synthesise(C, cues, total)

    log("pomiar glosnosci programu")
    prog = program_level_stereo()
    # Level the spoken parts against the programme, not against dBFS. Silence between lines is
    # excluded so a sparse episode does not push the voice up.
    voice = apply_shelf(C, voice, SR, float(M.get("voice_presence_db", 0.0)),
                        float(M.get("voice_presence_hz", 4000.0)))
    ch = [voice[s:e] for s, e in spans if e > s]
    rms = float(np.sqrt(np.mean(np.square(np.concatenate(ch), dtype=np.float64))))
    voice *= L.db_to_lin(prog + VOICE_OVER_DB) / max(1e-9, rms)
    voice *= L.db_to_lin(float(M.get("voice_trim_db", 0.0)))
    log(f"  program {prog:.1f} dB -> lektor {prog + VOICE_OVER_DB:.1f} dB na FC, "
        f"pod nim {DUCK_TO_DB} dB, w przerwach +{PROGRAM_GAIN_DB} dB")

    # build_duck_envelope reads duck_to_db and program_gain_db and returns absolute gains, so the
    # two envelopes differ only in how far their floor sits.
    spectral = M.get("duck_mode", "broadband") == "spectral"
    mid_db = M["duck_mid_db"] if spectral else DUCK_TO_DB
    C.raw["mix"]["duck_to_db"] = mid_db
    env_mid = build_duck_envelope(C, spans, total)
    env_rest = None
    if spectral:
        C.raw["mix"]["duck_to_db"] = M["duck_rest_db"]
        env_rest = build_duck_envelope(C, spans, total)
        log(f"  ducking pasmowy {M['duck_band_hz'][0]}-{M['duck_band_hz'][1]} Hz: "
            f"srodek {mid_db} dB, reszta {M['duck_rest_db']} dB")

    log("miks 5.1 + kodowanie E-AC-3")
    hits = mix_51(voice, env_mid, env_rest, total)
    sped = sum(1 for c in clips if c.speed > 1.005)
    stretched = sum(1 for c in clips if c.atempo > 1.01)
    log(f"GOTOWE {OUT}  ({OUT.stat().st_size // 1024 // 1024} MB)")
    log(f"  {sped} przyspieszonych, {stretched} rozciagnietych, {hits} blokow limitowanych")
    log(f"  koszt: OpenRouter ${or_cost:.4f} + ElevenLabs {chars} znakow (~${chars / 1000 * 0.05:.2f})")


if __name__ == "__main__":
    main()
