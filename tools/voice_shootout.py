#!/usr/bin/env python3
"""Ten candidate voices reading the same hard 150 s, mixed exactly like a real episode.

The window is chosen to break a voice rather than flatter it: English names it has to pronounce
(Fieldstone, Obisanya, Jeff Stelling, Chris Kamara, Richard Cole, Daphne) and lines that invite
acting (insults, shouting, a football commentary handover). A lektor must stay flat through all
of it, so anything the voice adds shows up immediately.
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
sys.path.insert(0, str(ROOT / "wsl"))
import lektor_original as L  # noqa: E402
from lektorlib.audio import trim_silence as TRIM  # noqa: E402
from lektorlib.mix import limit_block  # noqa: E402

D = pathlib.Path("/mnt/e/Filmy/Ted.Lasso.S02.PLSUB.1080p.ATVP.WEB-DL.DDPA5.1.H.264-NTb")
BASE = "Ted.Lasso.S02E03.Do.the.Right-est.Thing.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb"
SRC = D / f"{BASE}.mkv"
OUT = D / "_lektor" / "glosy"
OUT.mkdir(parents=True, exist_ok=True)
CACHE = SC / "tts_cache"

START_S, LEN_S = 1574, 150
SR, CH, FC = 48000, 6, 2
CFG = json.loads((ROOT / "lektor.config.json").read_text())
EL = CFG["voice"]["elevenlabs"]
M = CFG["mix"]
T = CFG["timing"]
VOICE_OVER_DB = M["voice_over_program_db"] + 3.0     # single channel replaces two
CEILING = L.db_to_lin(M["limiter_ceiling_db"])
STABILITY = 1.0
KEY = os.environ["ELEVENLABS_API_KEY"]

CANDIDATES = [
    ("01_Dariusz_podcast",   "mGixAdrjHFNdj6tN93zP", "calm, deep, subdued"),
    ("02_Kamil_dokumentalny", "lJCQUqoqYCHl7IixYEcL", "documentary-narrator delivery"),
    ("03_Mateusz_baryton",   "MM9TwXLP67IpAXY0UOGg", "low baritone, calm"),
    ("04_Szymon_spokojny",   "b3cTWKROgsbssFdmLi25", "calm, medium-low"),
    ("05_Pawel_nonfiction",  "OyB9t957VxUtN0puqGPg", "mature, nonfiction narrator"),
    ("06_Janski_zlozony",    "eQFIuH2hmjAMzpf75y69", "deep, composed"),
    ("07_Michal_neutralny",  "sNh3wakVwZdRw8WRiPsv", "neutral mazovian"),
    ("08_Gregor_kinowy",     "xPYhbnzKk9nCh7pnUehm", "deep, authoritative"),
    ("09_Marcin_glęboki",    "B5tmTXp0L7DzqmlGqIMJ", "deep, authoritative"),
    ("10_Ryszard_rzeczowy",  "Ixz7CmQyEkyuYwwkAAVm", "composed, firm"),
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def tts(text, speed, vid, prev, nxt):
    key = hashlib.sha256(
        f"{text}|{vid}|{STABILITY}|{speed}|{prev}|{nxt}|{EL['model_id']}".encode()).hexdigest()
    p = CACHE / f"{key}.mp3"
    if p.exists():
        return p.read_bytes(), 0
    body = {"text": text, "model_id": EL["model_id"], "previous_text": prev, "next_text": nxt,
            "voice_settings": {"stability": STABILITY, "similarity_boost": 0.75, "style": 0.0,
                               "use_speaker_boost": False, "speed": round(speed, 2)}}
    for att in range(6):
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format={EL['output_format']}",
            headers={"xi-api-key": KEY, "Content-Type": "application/json"},
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


def program_level():
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(START_S), "-t", str(LEN_S),
                        "-i", str(SRC), "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
                        "-f", "f32le", "pipe:1"], capture_output=True)
    x = np.frombuffer(p.stdout, dtype=np.float32)
    return 20.0 * np.log10(max(1e-9, float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))))


def render(label, vid, cues, prog, C):
    total = LEN_S * SR
    spent = [0]

    def work(item):
        i, c = item
        prev = cues[i - 1]["text"] if i > 0 else ""
        nxt = cues[i + 1]["text"] if i + 1 < len(cues) else ""
        w = c["window_ms"] / 1000.0 * T["safety"]
        sp = round(min(float(T["max_speed"]), max(1.0, (len(c["text"]) / 14.5) / w)), 2)
        mp3, n = tts(c["text"], sp, vid, prev, nxt)
        spent[0] += n
        return L.Clip(cue={"start_ms": c["start_ms"] - START_S * 1000,
                           "window_ms": c["window_ms"], "text": c["text"]},
                      audio=TRIM(L.decode(C, mp3, SR), SR))

    with ThreadPoolExecutor(max_workers=3) as pool:
        clips = list(pool.map(work, enumerate(cues)))

    voice, spans = L.assemble_voice(C, clips, total)
    chunks = [voice[s:e] for s, e in spans if e > s]
    rms = float(np.sqrt(np.mean(np.square(np.concatenate(chunks), dtype=np.float64))))
    voice *= L.db_to_lin(prog + VOICE_OVER_DB) / max(1e-9, rms)

    C.raw["mix"]["duck_db"] = M["duck_to_db"] - M["program_gain_db"]
    env = L.build_duck_envelope(C, spans, total) * L.db_to_lin(M["program_gain_db"])

    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", str(START_S), "-t", str(LEN_S),
                            "-i", str(SRC), "-map", "0:a:0", "-ar", str(SR),
                            "-f", "f32le", "pipe:1"], stdout=subprocess.PIPE)
    out = OUT / f"GLOS_{label}.mp3"
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR),
                            "-channel_layout", "5.1(side)", "-i", "pipe:0",
                            "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", str(out)],
                           stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    rel = float(np.exp(-1.0 / (M.get("limiter_release_ms", 120.0) / 1000.0 * SR)))
    carry, pos = 0.0, 0
    block = SR * 5
    while pos < total:
        raw = dec.stdout.read(min(block, total - pos) * CH * 4)
        if not raw:
            break
        x = np.frombuffer(raw, dtype=np.float32).reshape(-1, CH).copy()
        got = x.shape[0]
        g = np.interp(np.linspace(pos / SR * 200, (pos + got) / SR * 200, got),
                      np.arange(env.size), env).astype(np.float32)
        x *= g[:, None]
        x[:, FC] += voice[pos:pos + got]
        x, carry = limit_block(x, CEILING, rel, carry)
        enc.stdin.write(x.astype(np.float32).tobytes())
        pos += got
    enc.stdin.close()
    dec.stdout.close()
    dec.wait()
    enc.wait()
    return spent[0]


def main():
    cfg = dict(CFG)
    cfg["paths"] = dict(CFG["paths"], input_dir_wsl=str(SC), work_subdir="testwork")
    cfg["voice"] = dict(EL, concurrency=3)
    p = SC / "shootout_cfg.json"
    p.write_text(json.dumps(cfg))
    C = L.Config(p)

    data = json.loads((D / "_lektor" / f"{BASE}.lektor.json").read_text(encoding="utf-8"))
    cues = [c for c in data["cues"]
            if START_S * 1000 <= c["start_ms"] < (START_S + LEN_S - 3) * 1000]
    log(f"{len(cues)} kwestii, {sum(len(c['text']) for c in cues)} znakow na glos")
    prog = program_level()
    log(f"glosnosc fragmentu {prog:.1f} dB")

    total = 0
    for label, vid, note in CANDIDATES:
        n = render(label, vid, cues, prog, C)
        total += n
        log(f"  {label:<22} {note:<32} {n:>5} znakow")
    log(f"razem {total} znakow (~${total / 1000 * 0.05:.2f})")


if __name__ == "__main__":
    main()
