"""The transcribe stage: a film with no text subtitle at all, turned into one to translate from.

Path 3 of docs/translate.md. When extract finds neither a Polish text track nor a foreign one - or
translate.source says 'asr' - it records `subtitle_origin = "asr"` in `<base>.source.json`, and
this stage writes the foreign subtitle a translation is made from out of the film's own audio:

  subs/<base>.asr.<lang>.srt   cues laid out the way subtitles are, which `translate` reads
                               exactly as it reads a track extract pulled out of the film
  <base>.asr.json              the raw segments and words, which of them the filters dropped
                               and why, the language and how it was decided, and the settings

What it does, in the order it matters:

  * faster-whisper runs OUTSIDE this interpreter, by the route forced alignment already takes:
    a script fed on stdin to a venv's python or to a container's, JSON back. The pipeline process
    never imports torch, ctranslate2 or faster-whisper. A machine with neither route gets one
    message saying how to install one, before any audio is cut.
  * The audio is speech's: the centre channel through speech.centre_filter, the downmix when
    there is no centre or it is silent (speech.centre_min_mean_db). No source separation - Demucs
    was measured to make alignment worse in the sibling project, and this reuses the same cut.
  * Whisper's output is filtered in plain Python before anything is written: segments it marks
    as no speech, repetition loops, phrases Whisper is known to invent over silence, music, and
    segments its own VAD says sit outside speech. Nothing here has been measured on this
    project's material - there is no GPU where it was written. The silence pair, the compression
    ratio and the VAD numbers are Whisper's or faster-whisper's own; the loop, repeat, overlap,
    blacklist-share, pause, linger and timeout values were chosen here and are untested.
    lektor.tuning.toml says which is which.
  * Word timestamps are regrouped into subtitle-shaped cues: at most two lines of 42 characters,
    at most ~6 s, split at sentence ends first, then at pauses, then at commas, never inside a
    word. Whisper's word times were measured 316 ms early at utterance starts (speech.py), so
    these cues are a GRID, not a placement: `speech` aligns an English transcript against the
    audio afterwards, and `retime` moves a line onto the aligned first word exactly as it does
    for an authored subtitle, which leads speech on purpose anyway. A transcript in any other
    language is not aligned - the acoustic model is English, and a multilingual aligner is left
    to phase 3 - so it ships Whisper's early grid as it is. transcribe.onset_shift_ms can shift
    every cue start later, and ships at 0.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import say
from .media import (NO_LANGUAGE, language_key, resolve_source, translate_mode,
                    translate_settings, translate_source)
from .speech import (ScriptFailed, docker_problem, extract_centre, run_script_docker,
                     run_script_local, tmp_root)
from .srt import SENTENCE_END
from .translate import _ts, _write_atomic, candidate_bases, file_sha256, set_aside, source_info
from .venvs import venv_python

__all__ = ["stage_transcribe", "asr_json_path", "transcript_info", "runtime_problem",
           "build_transcript", "filter_segments", "segment_words"]

# Runs OUTSIDE this interpreter - in transcribe.venv, or in the container - fed on stdin to its
# python, exactly like speech._FA_SCRIPT and for the same reason: it needs a model and a runtime
# (ctranslate2, CUDA) the pipeline must not import. A faster-whisper that will not import - missing,
# or installed against a numpy, av or ctranslate2 it cannot load - exits 3 rather than with a
# traceback, so the caller can tell "no runtime" from "crashed", say how to fix it, and pass on
# the import error that says which of the two it was.
#
# condition_on_previous_text defaults to off in the config: the previous segment's text in the
# prompt is what lets one hallucinated phrase repeat for minutes, which is the loop the filters
# below are partly there to catch. That is widely reported Whisper behaviour, not a measurement
# made here.
_ASR_SCRIPT = r'''
import json, sys, time
try:
    from faster_whisper import WhisperModel
except Exception as exc:
    sys.stderr.write("faster-whisper is not importable here: %s\n" % exc)
    sys.exit(3)

WAV, OUT = sys.argv[1], sys.argv[2]
opts = json.loads(sys.argv[3])
vad = dict(opts.get("vad_parameters") or {})

t0 = time.perf_counter()
model = WhisperModel(opts["model"], device=opts["device"], compute_type=opts["compute_type"])
load_s = time.perf_counter() - t0

t1 = time.perf_counter()
segments, info = model.transcribe(
    WAV, language=opts.get("language") or None, beam_size=int(opts["beam_size"]),
    vad_filter=bool(opts["vad"]), vad_parameters=vad or None, word_timestamps=True,
    condition_on_previous_text=bool(opts["condition_on_previous_text"]))
rows = []
for s in segments:
    rows.append({
        "id": s.id, "start": round(s.start * 1000), "end": round(s.end * 1000), "text": s.text,
        "avg_logprob": round(float(s.avg_logprob), 4),
        "no_speech_prob": round(float(s.no_speech_prob), 4),
        "compression_ratio": round(float(s.compression_ratio), 3),
        "temperature": s.temperature,
        "words": [[round(w.start * 1000), round(w.end * 1000), w.word,
                   round(float(w.probability), 3)] for w in (s.words or [])],
    })
transcribe_s = time.perf_counter() - t1

# The VAD's own speech regions, for the filter that drops segments sitting outside them. Asked for
# separately because TranscriptionInfo does not carry them; a version that moved these functions
# simply leaves the filter without evidence, it does not fail the transcription.
speech = None
if opts["vad"]:
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        audio = decode_audio(WAV, sampling_rate=16000)
        speech = [[round(c["start"] / 16), round(c["end"] / 16)]
                  for c in get_speech_timestamps(audio, VadOptions(**vad))]
    except Exception as exc:
        sys.stderr.write("no VAD regions: %s\n" % exc)

device = getattr(getattr(model, "model", None), "device", opts["device"])
json.dump({"meta": {"model": opts["model"], "device": str(device),
                    "compute_type": opts["compute_type"], "beam_size": int(opts["beam_size"]),
                    "vad": bool(opts["vad"]), "vad_parameters": vad,
                    "condition_on_previous_text": bool(opts["condition_on_previous_text"]),
                    "language_requested": opts.get("language") or "",
                    "language": info.language,
                    "language_probability": round(float(info.language_probability), 3),
                    "audio_dur_s": round(float(info.duration), 2),
                    "speech_dur_s": round(float(getattr(info, "duration_after_vad",
                                                        info.duration)), 2),
                    "load_s": round(load_s, 2), "transcribe_s": round(transcribe_s, 2),
                    "rtf_x": round(info.duration / transcribe_s, 1) if transcribe_s else None},
           "segments": rows, "speech": speech},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
print(json.dumps({"ok": True, "n_segments": len(rows)}))
'''

# The exit code the script uses for "faster-whisper will not import here".
NO_RUNTIME_EXIT = 3


def settings(cfg) -> dict:
    """The [transcribe] table, or an empty one for a config that predates it. Read through `raw`
    for the same reason media.translate_settings is: the tests' stand-in configs carry only that."""
    return dict((getattr(cfg, "raw", None) or {}).get("transcribe") or {})


# ---------------------------------------------------------------------------
# Paths and what the rest of the pipeline reads
# ---------------------------------------------------------------------------


def asr_json_path(cfg, base: str) -> Path:
    return cfg.work_dir / f"{base}.asr.json"


def asr_srt_path(work_dir: Path, base: str, language: str) -> Path:
    """`subs/<base>.asr.<lang>.srt`: in subs/, beside phase 1's source, and for the same reason -
    every stage finds its episodes with `*.srt` in the work dir itself."""
    return work_dir / "subs" / f"{base}.asr.{language}.srt"


def load_asr(cfg, base: str) -> dict:
    """`<base>.asr.json`, or {} when there is none or it will not parse."""
    p = asr_json_path(cfg, base)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def transcript_info(cfg, base: str) -> dict:
    """What a finished transcription tells `translate` and `speech`: the subtitle it wrote and its
    language, in the sidecar's own keys. {} until there is a transcript whose .srt is on disk.

    Kept in `<base>.asr.json` rather than written back into `<base>.source.json`, so the sidecar
    has one writer - extract - and an extract rerun cannot drop what transcription learned.
    """
    data = load_asr(cfg, base)
    rel = str(data.get("srt") or "")
    if not rel or not (cfg.work_dir / rel).is_file():
        return {}
    return {"source_srt": rel, "source_language": str(data.get("language") or "")}


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

# Every language Whisper decodes (faster-whisper's tokenizer list, 100 codes, all two letters but
# its own 'haw' and 'yue'), each with the ISO 639-2 spellings mkv tags and this config use -
# bibliographic first, terminology after it where the two differ. The alias table in media folds
# only the thirty languages a subtitle pick is likely to meet; a Croatian film tagged 'hrv' must
# still be told 'hr', not left to a detection that can answer 'sr' or 'bs'.
_WHISPER_LANGUAGES = """
en eng | zh chi zho | de ger deu | es spa | ru rus | ko kor | fr fre fra | ja jpn | pt por
tr tur | pl pol | ca cat | nl dut nld | ar ara | sv swe | it ita | id ind | hi hin | fi fin
vi vie | he heb | uk ukr | el gre ell | ms may msa | cs cze ces | ro rum ron | da dan | hu hun
ta tam | no nor nob | th tha | ur urd | hr hrv | bg bul | lt lit | la lat | mi mao mri | ml mal
cy wel cym | sk slo slk | te tel | fa per fas | lv lav | bn ben | sr srp | az aze | sl slv
kn kan | et est | mk mac mkd | br bre | eu baq eus | is ice isl | hy arm hye | ne nep | mn mon
bs bos | kk kaz | sq alb sqi | sw swa | gl glg | mr mar | pa pan | si sin | km khm | sn sna
yo yor | so som | af afr | oc oci | ka geo kat | be bel | tg tgk | sd snd | gu guj | am amh
yi yid | lo lao | uz uzb | fo fao | ht hat | ps pus | tk tuk | nn nno | mt mlt | sa san
lb ltz | my bur mya | bo tib bod | tl tgl | mg mlg | as asm | tt tat | haw | ln lin | ha hau
ba bak | jw jav | su sun | yue
"""
_WHISPER_NAMES = [names.split() for names in re.split(r"[|\n]", _WHISPER_LANGUAGES)
                  if names.strip()]
_WHISPER_CODE = {spelling: names[0] for names in _WHISPER_NAMES for spelling in names}
# Codes ISO withdrew that still turn up in old tags, and the standard code for the one language
# Whisper spells its own way. "nb" (Bokmål) is "no" to Whisper, which has no separate entry.
_WHISPER_CODE.update({"iw": "he", "in": "id", "ji": "yi", "jv": "jw", "nb": "no"})


def whisper_code(tag: str) -> str:
    """The code Whisper is given for a language tag, or '' when Whisper has no such language.

    Any spelling of a language Whisper knows - 'hr', 'hrv', 'deu', the withdrawn 'iw' - and
    nothing else: model.transcribe() rejects a code it does not know with a ValueError, after the
    audio was cut and the model loaded, so an unknown one must not get that far.
    """
    t = str(tag or "").strip().casefold()
    return _WHISPER_CODE.get(t) or _WHISPER_CODE.get(language_key(t), "")


def tag_for(code: str) -> str:
    """The language key a Whisper code is filed under: its 639-2 spelling, folded as media folds
    tags - 'en' -> 'eng', 'hr' -> 'hrv' - so a transcript is named and compared like a track."""
    key = str(code or "").strip().casefold()
    for names in _WHISPER_NAMES:
        if names[0] == key:
            return language_key(names[1] if len(names) > 1 else key)
    return language_key(key)


def configured_language(cfg) -> str:
    """translate.source_language as written, or '' for 'auto'."""
    wanted = str(translate_settings(cfg).get("source_language", "auto") or "auto").strip()
    return "" if wanted.lower() == "auto" else wanted


def requested_language(cfg, info: dict) -> tuple[str, str]:
    """(Whisper code, where it came from) - 'config', 'tag', or ('', 'detect').

    translate.source_language first, because whoever set it knows the film. Then the audio
    track's own tag, which extract recorded. Only then Whisper's detection, which reads the first
    30 s and can be fooled by a cold open in another language - not measured here, but the reason
    a tag that exists is trusted over it. A value Whisper has no language for is passed over, and
    `unknown_language` says so.
    """
    code = whisper_code(configured_language(cfg))
    if code:
        return code, "config"
    code = whisper_code(str(info.get("audio_language") or ""))
    if code:
        return code, "tag"
    return "", "detect"


def unknown_language(cfg, info: dict) -> str:
    """Which language setting `requested_language` had to pass over, said in a sentence, or ''.

    A typo in translate.source_language, or a tag such as 'qaa', is not an error worth stopping a
    season for - the tag or the detection still gives Whisper a language - but nobody should find
    out from the transcript that what they set was not used.
    """
    wanted = configured_language(cfg)
    tag = str(info.get("audio_language") or "")
    _, how = requested_language(cfg, info)
    then = "the audio track's tag is used" if how == "tag" else "Whisper detects the language"
    if wanted and not whisper_code(wanted):
        return (f"translate.source_language '{wanted}' is no language Whisper knows – "
                f"{then} instead")
    if how == "detect" and tag and tag.casefold() not in NO_LANGUAGE:
        return f"the audio track's tag '{tag}' is no language Whisper knows – it detects one"
    return ""


# ---------------------------------------------------------------------------
# Hallucination filters - pure, unit-tested in wsl/tests/test_transcribe.py
# ---------------------------------------------------------------------------

_NORM_RE = re.compile(r"[^\w\s]|_", re.UNICODE)
_MUSIC_RE = re.compile(r"^[\s♪♫♬#*\-–.]*$|^\s*[\[(*]\s*(music|musik|musique|música|muzyka)"
                       r"[^\])*]*[\])*]\s*$", re.IGNORECASE)


def normalise(text: str) -> str:
    """Case, punctuation and spacing folded away, so "Thanks for watching!" is one phrase."""
    return " ".join(_NORM_RE.sub(" ", str(text or "").casefold()).split())


def longest_loop(tokens: list[str], max_n: int) -> tuple[int, int]:
    """(n, repeats) of the n-gram repeated back to back the most words' worth, or (0, 0).

    "I don't know, I don't know, I don't know" is (3, 3). Only consecutive repeats count: a word
    that comes back across a sentence is language, the same words over and over are a decoder
    stuck in a loop.
    """
    best = (0, 0)
    for n in range(1, max(1, max_n) + 1):
        for i in range(0, len(tokens) - 2 * n + 1):
            gram = tokens[i:i + n]
            k = 1
            while tokens[i + k * n:i + (k + 1) * n] == gram:
                k += 1
            if k >= 2 and n * k > best[0] * best[1]:
                best = (n, k)
    return best


def blacklisted(text: str, phrases, min_cover: float) -> str:
    """The blacklisted phrase this segment essentially consists of, or ''.

    Contained as whole words AND covering most of the segment: "Thanks for watching!" alone over
    the end credits is Whisper filling silence, while "thanks for watching my back" is a line.
    """
    seg = normalise(text)
    if not seg:
        return ""
    for phrase in phrases or []:
        p = normalise(phrase)
        if p and f" {p} " in f" {seg} " and len(p) >= min_cover * len(seg):
            return str(phrase)
    return ""


def speech_overlap(start: int, end: int, regions) -> float:
    """How much of [start, end] the VAD's speech regions cover, 0..1."""
    if end <= start:
        return 0.0
    covered = 0
    for a, b in regions:
        lo, hi = max(start, int(a)), min(end, int(b))
        if hi > lo:
            covered += hi - lo
    return covered / (end - start)


def drop_reason(seg: dict, t: dict, speech=None) -> str:
    """Why this segment must not become a subtitle, or '' to keep it. First reason wins."""
    text = str(seg.get("text") or "")
    if not text.strip():
        return "empty"
    if _MUSIC_RE.search(text):
        return "music"
    phrase = blacklisted(text, t.get("blacklist", []), float(t.get("blacklist_min_cover", 0.6)))
    if phrase:
        return f"blacklisted phrase {phrase!r}"
    nsp = float(seg.get("no_speech_prob", 0.0) or 0.0)
    lp = float(seg.get("avg_logprob", 0.0) or 0.0)
    # Both halves, as Whisper's own decoder uses them: a high no-speech probability alone also
    # fires on quiet, confidently decoded lines, and only the pair says "nothing was said here".
    if nsp > float(t.get("max_no_speech_prob", 0.6)) and lp < float(t.get("min_avg_logprob", -1.0)):
        return f"no speech (no_speech_prob {nsp:.2f}, avg_logprob {lp:.2f})"
    cr = float(seg.get("compression_ratio", 0.0) or 0.0)
    if cr > float(t.get("max_compression_ratio", 2.4)):
        return f"compression ratio {cr:.2f} - repetitive text"
    n, k = longest_loop(normalise(text).split(), int(t.get("loop_max_ngram", 6)))
    if k >= int(t.get("loop_min_repeats", 4)) and n * k >= int(t.get("loop_min_words", 12)):
        return f"loop: a {n}-word phrase repeated {k} times"
    if speech is not None:
        start, end = int(seg.get("start", 0)), int(seg.get("end", 0))
        share = speech_overlap(start, end, speech)
        if share < float(t.get("min_speech_overlap", 0.3)):
            return f"outside speech ({share:.0%} of it inside the VAD's speech regions)"
    return ""


def filter_segments(segments: list[dict], t: dict, speech=None) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Every dropped segment carries its reason under "dropped".

    One rule spans segments rather than looking inside one: the same text several segments in a
    row. Whisper stuck in a loop across its 30 s windows produces exactly that, and each copy can
    pass every per-segment check. The first `max_identical_run` copies are kept - "Yeah." "Yeah."
    is speech - and the rest go.
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    run_text, run_len = "", 0
    max_run = int(t.get("max_identical_run", 2))
    for seg in segments:
        why = drop_reason(seg, t, speech)
        if not why:
            norm = normalise(seg.get("text", ""))
            run_len = run_len + 1 if norm == run_text else 1
            run_text = norm
            if run_len > max_run:
                why = f"the same text {run_len} segments in a row"
        if why:
            dropped.append(dict(seg, dropped=why))
        else:
            kept.append(seg)
    return kept, dropped


# ---------------------------------------------------------------------------
# Re-segmentation into subtitle cues - pure, unit-tested in wsl/tests/test_transcribe.py
# ---------------------------------------------------------------------------


@dataclass
class Word:
    start: int
    end: int
    # As Whisper hands it over, leading space included: joining these with "" is right for every
    # language, including the ones that put no space between words.
    text: str
    prob: float = 1.0


@dataclass
class TranscriptCue:
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] = field(default_factory=list)


def words_of(segments: list[dict]) -> list[Word]:
    """Every word of the kept segments, in time order.

    A segment that came back with no word timestamps - which faster-whisper can do for a segment
    it could not align - is spread over its own span by character count, so it still splits like
    words and is never silently lost.
    """
    out: list[Word] = []
    for seg in segments:
        ws = seg.get("words") or []
        if ws:
            for w in ws:
                start, end, text = int(w[0]), int(w[1]), str(w[2])
                prob = float(w[3]) if len(w) > 3 else 1.0
                if text.strip():
                    out.append(Word(start, max(start, end), text, prob))
            continue
        tokens = re.findall(r"\s*\S+", str(seg.get("text") or ""))
        start, end = int(seg.get("start", 0)), int(seg.get("end", 0))
        total = sum(len(x.strip()) for x in tokens) or 1
        at = 0
        for tok in tokens:
            a = start + (end - start) * at // total
            at += len(tok.strip())
            out.append(Word(a, start + (end - start) * at // total, tok))
    out.sort(key=lambda w: (w.start, w.end))
    return out


def cue_text(words: list[Word]) -> str:
    return re.sub(r"\s+", " ", "".join(w.text for w in words)).strip()


def fits_lines(text: str, width: int, max_lines: int) -> bool:
    """Can this text be laid out as at most `max_lines` lines of at most `width` characters?"""
    lines, cur = 0, -1
    for word in text.split():
        if len(word) > width:
            return False
        if cur < 0 or cur + 1 + len(word) > width:
            lines, cur = lines + 1, len(word)
        else:
            cur += 1 + len(word)
    return lines <= max_lines


def layout(text: str, width: int = 42, max_lines: int = 2) -> str:
    """One cue as subtitle lines: as is when it fits one, else two balanced lines that both fit.

    Balanced rather than greedy, as `translate.wrap_subtitle` breaks: two even lines read better
    than a full one and a stub. Text that cannot fit two lines - one cue that is a single huge
    word, say - is wrapped greedily and left as long as it is.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= width or " " not in text:
        return text
    if max_lines >= 2:
        best = None
        for m in re.finditer(" ", text):
            a, b = text[:m.start()], text[m.end():]
            if len(a) <= width and fits_lines(b, width, max_lines - 1):
                score = abs(len(a) - len(b))
                if best is None or score < best[0]:
                    best = (score, a, b)
        if best is not None:
            return best[1] + "\n" + layout(best[2], width, max_lines - 1)
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    lines.append(cur)
    return "\n".join(lines)


_CLAUSE_END = re.compile(r"[,;:–—、，；：][\"'”»)\]」』）]*\s*$")
# srt.SENTENCE_END reads only [.!?], which is right for the Polish text the other stages check.
# Whisper writes Chinese, Japanese and Cantonese with the full-width marks, and a transcript in
# one of those must split at its sentence ends too.
_WIDE_SENTENCE_END = re.compile(r"[。！？][\"'”»)\]」』）]*\s*$")


def split_kind(prev: Word, nxt: Word, t: dict) -> int:
    """How good a place the seam between two words is to end a cue: 0 best, 3 worst.

    0 after a sentence end, 1 at a pause in the delivery, 2 after a comma or similar, 3 anywhere
    else. Sentence first because a card that ends mid-sentence makes the reader wait for the next
    one; the pause second because it is where the actor breathed, which is also where a lektor
    can; the comma last because it marks grammar, not timing.
    """
    if SENTENCE_END.search(prev.text) or _WIDE_SENTENCE_END.search(prev.text) \
            or prev.text.rstrip().endswith("…"):
        return 0
    if nxt.start - prev.end >= int(t.get("split_pause_ms", 250)):
        return 1
    if _CLAUSE_END.search(prev.text):
        return 2
    return 3


def fits(words: list[Word], t: dict) -> bool:
    if not words:
        return True
    if words[-1].end - words[0].start > int(t.get("max_cue_ms", 6000)):
        return False
    return fits_lines(cue_text(words), int(t.get("max_line_chars", 42)),
                      int(t.get("max_lines", 2)))


def best_split(words: list[Word], t: dict, until: int | None = None) -> int:
    """Where to cut a run of words that does not fit one cue: the index the second half starts at.

    The best kind of seam wins (see split_kind). Among pauses the longest one; among the rest the
    one nearest the middle by characters, so the two halves come out even rather than one full
    card and a stub - recursing on both halves then finds the next seam inside each.

    Before any of that, a seam must leave each half min_cue_room_ms on screen: a card ends
    min_gap_ms before the next one starts, so a short sentence whose next word follows at once -
    " Yes." and the next line 10 ms later - would otherwise be cut off as a card one millisecond
    long, and `translate` copies that timing to the Polish card. `until` is where the word after
    this run starts, which bounds the second half; None when nothing follows.
    """
    total = len(cue_text(words)) or 1
    gap_ms = int(t.get("min_gap_ms", 80))
    room_ms = int(t.get("min_cue_room_ms", 400))
    best_key, best_i = None, len(words) // 2
    for i in range(1, len(words)):
        prev, nxt = words[i - 1], words[i]
        kind = split_kind(prev, nxt, t)
        balance = abs(len(cue_text(words[:i])) - len(cue_text(words[i:]))) / total
        gap = nxt.start - prev.end
        room = nxt.start - words[0].start
        if until is not None:
            room = min(room, until - nxt.start)
        cramped = room - gap_ms < room_ms
        key = (cramped, kind, -gap if kind == 1 else 0, balance)
        if best_key is None or key < best_key:
            best_key, best_i = key, i
    return best_i


def _fit(words: list[Word], t: dict, until: int | None = None) -> list[list[Word]]:
    if len(words) <= 1 or fits(words, t):
        return [words]
    i = best_split(words, t, until)
    return _fit(words[:i], t, words[i].start) + _fit(words[i:], t, until)


def segment_words(words: list[Word], t: dict) -> list[list[Word]]:
    """Words into cue-sized groups. Never splits a word, because it only ever splits between them.

    A silence longer than max_pause_in_cue_ms always ends a cue, whatever room is left: a card
    that stays up across a long pause is a card shown over nobody speaking. Everything else is cut
    only as far as it has to be to fit.
    """
    groups: list[list[Word]] = []
    cur: list[Word] = []
    hard = int(t.get("max_pause_in_cue_ms", 1000))
    for w in words:
        if cur and w.start - cur[-1].end >= hard:
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    out: list[list[Word]] = []
    for k, g in enumerate(groups):
        out.extend(_fit(g, t, groups[k + 1][0].start if k + 1 < len(groups) else None))
    return out


def time_cues(groups: list[list[Word]], t: dict) -> list[TranscriptCue]:
    """Start and end for each group: the word span, plus a little air, never overlapping.

    The start is the first word's, moved later by onset_shift_ms (0 as shipped - see the module
    note on why the early onset is left to alignment). The end stays up linger_ms past the last
    word and at least min_cue_ms after the start, both only as far as the next cue allows: the gap
    kept between cards is min_gap_ms. Neither number was measured on transcribed material.
    """
    shift = int(t.get("onset_shift_ms", 0))
    linger = int(t.get("linger_ms", 300))
    min_ms = int(t.get("min_cue_ms", 1000))
    gap = int(t.get("min_gap_ms", 80))
    starts = [min(g[0].start + shift, g[-1].end) for g in groups]
    out: list[TranscriptCue] = []
    for i, g in enumerate(groups):
        start = max(starts[i], out[-1].end_ms + gap) if out else max(0, starts[i])
        end = max(g[-1].end + linger, start + min_ms)
        if i + 1 < len(groups):
            end = min(end, starts[i + 1] - gap)
        end = max(end, start + 1)
        out.append(TranscriptCue(start, end, cue_text(g), g))
    return out


@dataclass
class Transcript:
    cues: list[TranscriptCue]
    kept: list[dict]
    dropped: list[dict]


def build_transcript(raw: dict, t: dict) -> Transcript:
    """What the script returned, filtered and regrouped into cues. Pure: no file, no subprocess."""
    segments = [s for s in (raw.get("segments") or []) if isinstance(s, dict)]
    speech = raw.get("speech")
    if not isinstance(speech, list) or not speech:
        # No regions is no evidence, not evidence of silence: a VAD that returned nothing, or a
        # faster-whisper too old to be asked, must not drop every segment as "outside speech".
        speech = None
    kept, dropped = filter_segments(segments, t, speech)
    cues = time_cues(segment_words(words_of(kept), t), t)
    return Transcript(cues=[c for c in cues if c.text], kept=kept, dropped=dropped)


def format_srt(cues: list[TranscriptCue], t: dict) -> str:
    width, lines = int(t.get("max_line_chars", 42)), int(t.get("max_lines", 2))
    return "\n".join(f"{n}\n{_ts(c.start_ms)} --> {_ts(c.end_ms)}\n{layout(c.text, width, lines)}\n"
                     for n, c in enumerate(cues, 1))


# ---------------------------------------------------------------------------
# Running the model
# ---------------------------------------------------------------------------


class RuntimeMissing(RuntimeError):
    """No interpreter here can run faster-whisper. One message, and the stage stops: every other
    episode would only meet the same missing install."""


def runtime(cfg) -> tuple[str, str]:
    """('local', venv) or ('docker', container), chosen the way speech.forced_align chooses.

    'auto' takes the venv when one is configured and has an interpreter, the container otherwise.
    The container defaults to speech.asr_container: the one this project borrows already has
    faster-whisper in it, because its resident service is a faster-whisper large-v3.
    """
    t = settings(cfg)
    choice = str(t.get("runner", "auto") or "auto").strip().lower()
    venv = str(t.get("venv", "") or "")
    if choice not in ("auto", "local", "docker"):
        raise ValueError(f"transcribe.runner: {choice!r} – pick from auto, local, docker")
    if choice == "local" or (choice == "auto" and venv and venv_python(venv).is_file()):
        return "local", venv
    return "docker", str(t.get("container") or cfg["speech"].get("asr_container", "asr"))


def runtime_problem(cfg, probe: bool = True) -> str:
    """Why transcription cannot run on this machine, or '' when nothing is known to be missing.

    `probe` asks docker whether the container is up; the plan passes False and checks only that a
    docker binary exists, so printing a plan never waits on a daemon. Whether faster-whisper is
    actually installed only the script itself can say.
    """
    kind, where = runtime(cfg)
    if kind == "local":
        if not where:
            return "transcribe.runner is 'local' but transcribe.venv is empty"
        python = venv_python(where)
        return "" if python.is_file() else f"no interpreter at {python}"
    if not probe:
        return "" if shutil.which("docker") else "docker is not on PATH"
    return docker_problem(where)


def missing_runtime_message(problem: str) -> str:
    return (f"no runtime for transcription: {problem}. transcribe runs faster-whisper outside "
            f"this interpreter - install it into a venv and point transcribe.venv at it, or set "
            f"transcribe.runner = 'docker' with a running container that has it "
            f"(transcribe.container). docs/install.md, 'transcription', has the commands.")


def script_options(cfg, language: str) -> dict:
    t = settings(cfg)
    vad = {"threshold": float(t.get("vad_threshold", 0.5)),
           "min_silence_duration_ms": int(t.get("vad_min_silence_ms", 2000)),
           "speech_pad_ms": int(t.get("vad_speech_pad_ms", 400))}
    return {"model": str(t.get("model", "large-v3")), "device": str(t.get("device", "auto")),
            "compute_type": str(t.get("compute_type", "default")),
            "beam_size": int(t.get("beam_size", 5)), "vad": bool(t.get("vad", True)),
            "vad_parameters": vad,
            "condition_on_previous_text": bool(t.get("condition_on_previous_text", False)),
            "language": language}


def run_asr(cfg, wav: Path, tmp: Path, language: str) -> dict:
    """The script's JSON for this wav. RuntimeMissing when there is nothing to run it with."""
    t = settings(cfg)
    problem = runtime_problem(cfg)
    if problem:
        raise RuntimeMissing(missing_runtime_message(problem))
    kind, where = runtime(cfg)
    opts = [json.dumps(script_options(cfg, language))]
    out = tmp / "asr.json"
    timeout = float(t.get("timeout_s", 7200))
    try:
        if kind == "local":
            return run_script_local(venv_python(where), _ASR_SCRIPT, [wav], out, opts, timeout,
                                    "transcription")
        return run_script_docker(where, _ASR_SCRIPT, [wav], out, opts, timeout,
                                 float(cfg["speech"].get("docker_timeout_s", 600)),
                                 "transcription")
    except ScriptFailed as exc:
        if exc.code == NO_RUNTIME_EXIT:
            # Not "is not installed": a broken install exits the same way, and reinstalling over
            # it is the wrong advice when the import error names a numpy or a missing library.
            what = f"the container {where!r}" if kind == "docker" else f"the venv {where}"
            err = exc.stderr.strip()[-400:]
            raise RuntimeMissing(missing_runtime_message(
                f"faster-whisper cannot be imported in {what}"
                + (f" ({err})" if err else ""))) from exc
        raise


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def stale_transcript(cfg, base: str, info: dict | None = None) -> str:
    """Why the existing transcript was made for another language than the one asked for now, or ''.

    The language is the one thing that changes what Whisper writes and that a user changes in the
    config: a transcript decoded as English after translate.source_language became 'ger' would
    otherwise sit there as "exists, skipping" forever.
    """
    data = load_asr(cfg, base)
    if not data:
        return ""
    info = source_info(cfg, base) if info is None else info
    now, _ = requested_language(cfg, info)
    was = str(data.get("language_requested") or "")
    if now != was:
        return (f"it was transcribed as '{was or 'detected'}' and the language asked for is now "
                f"'{now or 'detected'}'")
    return ""


def is_done(cfg, base: str) -> bool:
    """A transcript is on disk, its .srt is there, and it was made for the language asked now."""
    return bool(transcript_info(cfg, base)) and not stale_transcript(cfg, base)


def stage_transcribe(cfg, only: str | None, force: bool = False) -> dict[str, str]:
    """Write the transcript of every episode extract marked as needing one. Returns base -> what
    happened ("written", "kept", "subs", "track", "never", "subs-only", "failed").

    Exits non-zero, after doing every episode it can, when one could not be transcribed - the same
    contract as translate, for the same reason: nothing downstream may run without the text. A
    missing runtime stops at once, with the one message that says how to install one.
    """
    t = settings(cfg)
    mode = translate_mode(cfg)
    result: dict[str, str] = {}
    failed: list[str] = []
    bases = candidate_bases(cfg, only)
    if not bases:
        say(f"no <base>.source.json in {cfg.work_dir} – run extract first")
        return result

    for base in bases:
        info = source_info(cfg, base)
        org = str(info.get("subtitle_origin") or "track")
        if org != "asr":
            say(f"{base}: " + ("a text subtitle to translate from – nothing to transcribe"
                               if org == "subs" else
                               "Polish subtitle track in the film – nothing to transcribe"))
            result[base] = org
            continue
        if mode == "never":
            say(f"{base}: translate.mode is 'never' – skipped")
            result[base] = "never"
            continue
        if translate_source(cfg) == "subs":
            # An earlier config sent this film here; extract under 'subs' finds no subtitle and
            # leaves that sidecar as it was. The audio is ruled out all the same.
            say(f"{base}: translate.source is 'subs' – the audio is not transcribed, and the film "
                f"has no text subtitle to translate from")
            result[base] = "subs-only"
            continue
        stale = stale_transcript(cfg, base, info)
        if transcript_info(cfg, base) and not stale and not force:
            say(f"{base}: {asr_json_path(cfg, base).name} exists, skipping (--force to redo)")
            result[base] = "kept"
            continue
        if stale and not force:
            say(f"{base}: {stale} – transcribing it again")

        say(f"\n{base}")
        video = resolve_source(cfg, base)
        if video is None:
            failed.append(f"{base}: no source video – {base}.source.json names none that exists "
                          f"and there is none in {cfg.input_dir}")
            result[base] = "failed"
            continue
        language, how = requested_language(cfg, info)
        unknown = unknown_language(cfg, info)
        if unknown:
            say(f"  !! {unknown}")
        problem = runtime_problem(cfg)
        if problem:
            # Before the audio is cut: a minute of ffmpeg spent on a machine that cannot transcribe
            # is a minute spent learning nothing.
            raise SystemExit(missing_runtime_message(problem))

        tmp = tmp_root(cfg) / f"lektor-asr-{base[:40]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            wav = tmp / "centre16k.wav"
            t0 = time.perf_counter()
            kind = extract_centre(cfg, video, wav)
            extract_s = time.perf_counter() - t0
            runner = runtime(cfg)
            say(f"  {kind} audio in {extract_s:.1f} s, transcribing with faster-whisper "
                f"{t.get('model', 'large-v3')} ({runner[0]}: {runner[1]}), language "
                + (f"'{language}' from the {how}" if language else "detected by Whisper"))
            raw = run_asr(cfg, wav, tmp, language)
        except RuntimeMissing as exc:
            raise SystemExit(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - reported per episode, like translate's
            failed.append(f"{base}: {exc}")
            result[base] = "failed"
            say(f"  !! {exc}")
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        meta = raw.get("meta") or {}
        got = tag_for(str(meta.get("language") or language or ""))
        target = language_key(cfg["extract"]["subtitle_language"])
        tr = build_transcript(raw, t)
        say(f"  {len(raw.get('segments') or [])} segments in {meta.get('transcribe_s')} s "
            f"({meta.get('rtf_x')}x realtime) on {meta.get('device', '?')}, language '{got}'"
            + (f" (p={meta.get('language_probability')})" if how == "detect" else "")
            + f"; {len(tr.dropped)} dropped by the filters")
        written = bool(got and tr.cues) and got != target
        # Where nothing is written every reason is said here, since no <base>.asr.json will hold
        # them - and an earlier run's file, if there is one, holds other segments.
        shown = tr.dropped[:8] if written else tr.dropped
        for seg in shown:
            say(f"    - {_ts(seg.get('start', 0))} {str(seg.get('text', '')).strip()[:48]!r}: "
                f"{seg['dropped']}")
        if len(tr.dropped) > len(shown):
            say(f"    ... and {len(tr.dropped) - len(shown)} more, all in "
                f"{asr_json_path(cfg, base).name}")
        if not got or not tr.cues:
            why = "no language came back" if not got else "nothing survived the filters"
            failed.append(f"{base}: {why} – no transcript written")
            result[base] = "failed"
            continue
        if got == target:
            # A film spoken in the target language needs no lektor, and translating it into
            # itself would only fail translate's own "came back untranslated" check line by line.
            failed.append(f"{base}: Whisper hears '{got}', the language the lektor speaks – "
                          f"nothing to translate (set translate.source_language if it is wrong)")
            result[base] = "failed"
            continue

        srt = asr_srt_path(cfg.work_dir, base, got)
        srt.parent.mkdir(parents=True, exist_ok=True)
        old = load_asr(cfg, base)
        old_srt = cfg.work_dir / str(old.get("srt") or "") if old.get("srt") else None
        if old_srt is not None and old_srt.is_file() and old.get("srt_sha256") \
                and file_sha256(old_srt) != old["srt_sha256"]:
            # A corrected transcript - a name Whisper misheard, say - is somebody's work.
            keep = set_aside(old_srt)
            say(f"  previous {old_srt.name} was edited by hand – copied aside as {keep.name}")
        _write_atomic(srt, format_srt(tr.cues, t))
        if old_srt is not None and old_srt != srt and old_srt.is_file():
            # The transcript of an earlier language, untouched: nothing reads it any more -
            # source_srt finds a transcript only through <base>.asr.json - and left beside the new
            # one it would look as current as it.
            old_srt.unlink()
        payload = {
            "base": base,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": {"video": video.name, "audio": kind},
            "runner": {"kind": runner[0], "where": runner[1]},
            "language": got,
            "language_requested": language,
            "language_source": how if language else "detected",
            "language_probability": meta.get("language_probability"),
            "srt": srt.relative_to(cfg.work_dir).as_posix(),
            "srt_sha256": file_sha256(srt),
            "cues": len(tr.cues),
            "settings": t,
            "meta": meta,
            "speech": raw.get("speech"),
            # Every segment Whisper returned, in time order, a dropped one with its reason - so a
            # line missing from the .srt can be traced to the rule that took it out.
            "segments": sorted(tr.dropped + [dict(s, dropped=None) for s in tr.kept],
                               key=lambda s: (s.get("start", 0), s.get("id", 0))),
            "_note": ("Written by the transcribe stage. Cue timings come from Whisper's word "
                      "times, which run early at utterance starts; "
                      + ("speech aligns this English transcript against the audio and retime "
                         "moves lines onto the first aligned word." if got == "eng" else
                         "only an English transcript is aligned, so these stay as Whisper "
                         "placed them.")),
        }
        _write_atomic(asr_json_path(cfg, base), json.dumps(payload, ensure_ascii=False, indent=1))
        say(f"  -> {srt.parent.name}/{srt.name} ({len(tr.cues)} cues) + "
            f"{asr_json_path(cfg, base).name} – `translate` turns it into {base}.srt")
        result[base] = "written"

    if failed:
        raise SystemExit("\ntranscription did not finish for:\n  " + "\n  ".join(failed)
                         + "\nNo transcript was written for these episodes, so nothing downstream "
                           "will run on them.")
    return result
