"""The two stages that open and write the .mkv container itself, in Python.

Until now `extract` and `mux` existed only as PowerShell (`windows/lektor.ps1`), which pinned both
ends of the pipeline to Windows even though everything between them is portable POSIX Python that
shells out to ffmpeg. This module is the native replacement: same ffmpeg invocations, same
selection rules, same `<base>.source.json` on disk, so a work dir can be produced by either side
and finished by the other.

The one rule in here that must not drift is `pick_subtitle`. These releases carry 43 subtitle
streams, Polish sits at a different index in different files of the SAME season (Ted Lasso S03E01
to E05 have pol at 30, S03E06 at 31 because a forced track was added ahead of it), and the index
next to it is Portuguese. Selection is by language tag, never by number, and two PowerShell
comparison semantics are load-bearing in the port: `-ne` on strings is case-insensitive, so a
stream tagged POL matches "pol"; and `-match` is a case-insensitive REGEX, which is why "forced"
and "Forced" in reject_title_patterns are the same rule written twice. Python's `!=` and
`re.search` are both case-SENSITIVE by default, so a straight transliteration would silently
change which track a 43-subtitle release yields.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import say
from .mix import encode_profile
from .speech import find_video

# `-c:s srt` transcodes the chosen stream into SubRip, which works for subrip/ass/ssa/mov_text and
# dies inside ffmpeg for anything bitmap. The PowerShell never checked, so a disc-sourced release
# failed with a codec error instead of saying which track was the problem.
BITMAP_SUBTITLE_CODECS = frozenset({
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "hdmv_text_subtitle",
})

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v")


# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------


def _binary(cfg, name: str) -> str:
    """ffmpeg/ffprobe from the config: the canonical key, then the legacy `_wsl` one, then PATH.

    One place, because the fallback string was re-derived in three separate modules
    (stages.source_duration_s, mix.source_channels, loudness) and that is where a rename goes
    wrong. `paths.ffmpeg_windows` is deliberately NOT a fallback: even on this box, where
    /mnt/c/Programy/ffmpeg/bin/ffmpeg.exe really is there, a Windows binary cannot open the POSIX
    paths every stage in this package hands it, so reaching for it would fail at the first file
    rather than at the first missing key.
    """
    return cfg.binary(name)


def ffmpeg_bin(cfg) -> str:
    return _binary(cfg, "ffmpeg")


def ffprobe_bin(cfg) -> str:
    return _binary(cfg, "ffprobe")


def native_path(value: str) -> Path:
    """A `X:\\...` string as this namespace sees it, when it has one.

    Backwards compatibility only: the sidecars written by the PowerShell carry `source_windows`,
    and under WSL `E:\\Filmy\\x.mkv` is `/mnt/e/Filmy/x.mkv`. On a box with no /mnt/e the string is
    returned untouched rather than turned into a path that resolves to nothing in particular.
    """
    m = re.fullmatch(r"([A-Za-z]):[\\/](.*)", value)
    if not m:
        return Path(value)
    drive = Path(f"/mnt/{m.group(1).lower()}")
    if not drive.is_dir():
        return Path(value)
    return drive.joinpath(*[p for p in m.group(2).replace("\\", "/").split("/") if p])


def windows_path(value: Path | str) -> str:
    """The inverse of native_path, for the one field the PowerShell mux reads.

    `/mnt/e/Filmy/ep.mkv` is `E:\\Filmy\\ep.mkv` over there. Anything not under /mnt/<letter> is
    returned unchanged: on a machine with no Windows side the value is a note, not an address.
    """
    text = str(value)
    m = re.fullmatch(r"/mnt/([a-zA-Z])(/.*)?", text)
    if not m:
        return text
    return f"{m.group(1).upper()}:" + (m.group(2) or "/").replace("/", "\\")


def run_ffmpeg(ffmpeg: str, args: list[str], what: str) -> None:
    """The port of Invoke-Ffmpeg: quiet, non-interactive, and loud about a non-zero exit.

    `-nostdin` is what keeps a stage from hanging forever on an overwrite prompt when it is run
    from a script that has no terminal attached.
    """
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{what} failed (exit {r.returncode}): {' / '.join(tail)}")


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


@dataclass
class Stream:
    """One ffprobe stream, flattened to the seven fields any stage here asks about."""

    index: int
    codec_type: str = ""
    codec_name: str = ""
    channels: int = 0
    language: str = ""
    title: str = ""
    disposition: dict = field(default_factory=dict)

    @property
    def forced(self) -> bool:
        return self.disposition.get("forced") == 1


def stream_from_ffprobe(raw: dict) -> Stream:
    """`index` is the ABSOLUTE stream index and stays that way: it is what `-map 0:<n>` takes.

    A stream with no `tags` object at all is normal on remuxes, so every tag read is defaulted
    rather than assumed – and an absent language tag stays the empty string, which `pick_subtitle`
    treats as "not a candidate", never as "unknown, might do".
    """
    tags = raw.get("tags") or {}
    return Stream(
        index=int(raw["index"]),
        codec_type=str(raw.get("codec_type") or ""),
        codec_name=str(raw.get("codec_name") or ""),
        channels=int(raw.get("channels") or 0),
        language=str(tags.get("language") or ""),
        title=str(tags.get("title") or ""),
        disposition=dict(raw.get("disposition") or {}),
    )


def streams_from_ffprobe(payload: dict) -> list[Stream]:
    """The `streams` array of an ffprobe -print_format json payload, flattened."""
    return [stream_from_ffprobe(s) for s in (payload.get("streams") or [])]


class ProbeError(RuntimeError):
    """ffprobe did not answer. Separate from "the file has no such streams", which is a list."""


def probe_streams(ffprobe: str, path: Path | str, select: str | None = None,
                  strict: bool = False) -> list[Stream]:
    """Every stream of a file, or only those of one type (`select='s'`, `'a'`, `'v'`).

    `strict` decides what a failed probe means, and the two callers need opposite answers:

      strict=False  an unreadable file is an empty list. The extract loop then reports "no Polish
                    subtitle stream" for that episode and carries on with the season, which is
                    what a traceback out of the middle of the loop would not do.
      strict=True   a failed probe raises ProbeError, because the answer is about to be used as a
                    NUMBER. Zero audio streams is a legal count, so a swallowed failure is
                    indistinguishable from a legitimate result and quietly retargets every
                    `-metadata:s:a:` and `-disposition:a:` in the mux onto the first ORIGINAL
                    track – the file muxes, plays, and has the English track titled
                    "Lektor AI (PL)" and flagged default.
    """
    args = [ffprobe, "-v", "error", "-print_format", "json", "-show_streams"]
    if select:
        args += ["-select_streams", select]
    # `--` so a filename that starts with a dash is a filename and not an option.
    args += ["--", str(path)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        if strict:
            tail = (r.stderr or "").strip().splitlines()[-3:]
            raise ProbeError(f"ffprobe failed on {path} (exit {r.returncode}): {' / '.join(tail)}")
        return []
    try:
        return streams_from_ffprobe(json.loads(r.stdout))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if strict:
            raise ProbeError(f"ffprobe returned unreadable JSON for {path}: {exc}")
        return []


def count_audio_streams(ffprobe: str, path: Path | str) -> int:
    """How many audio streams the source already has – the number the mux indexes everything off.

    Strict on purpose: see `probe_streams`. A guessed 0 here is not a smaller mux, it is a wrong
    one that looks right.
    """
    return len(probe_streams(ffprobe, path, "a", strict=True))


# ---------------------------------------------------------------------------
# Subtitle selection – pure, and the part of this module worth staring at
# ---------------------------------------------------------------------------


@dataclass
class SubtitleChoice:
    """The chosen stream. `index` is the absolute ffprobe index, i.e. the argument to `-map 0:`.

    It carries the title and the codec too because the extract stage prints both and writes the
    title into the sidecar, and because `rejected` is the difference between "this is the Polish
    dialogue track" and "this is the only pol stream there was, and it looks like signage".
    """

    index: int
    title: str = ""
    codec: str = ""
    rejected: bool = False
    reason: str = ""


def _compile_rejects(patterns) -> list[tuple[str, re.Pattern]]:
    """reject_title_patterns are REGEXES, not substrings – PowerShell `-match` is a regex operator.

    A pattern that will not compile is named in the error, because the alternative is a stage that
    quietly stops rejecting anything and picks up an SDH track for the rest of the season.
    """
    out = []
    for pat in patterns or []:
        text = str(pat)
        try:
            out.append((text, re.compile(text, re.IGNORECASE)))
        except re.error as exc:
            raise ValueError(f"extract.reject_title_patterns: {text!r} is not a valid regex ({exc})")
    return out


def pick_subtitle(streams, language: str, reject_title_patterns=None) -> SubtitleChoice | None:
    """The Polish dialogue track, by language tag, never by index. Port of Get-SubtitleStreamIndex.

    Rules, in the order they are applied and exactly as the PowerShell has them:

      1. A stream is a candidate only when its language tag equals `language`, compared
         case-insensitively (PowerShell `-ne`). No tag means no candidacy – not even as a
         last-resort fallback, because an untagged stream on these releases is as likely to be
         Portuguese as Polish.
      2. A candidate is REJECTED when any reject_title_patterns entry matches its title, searched
         case-insensitively anywhere in the string. Every pattern is tried; the PowerShell loop
         does not break on the first hit, so the effect is a plain OR.
      3. A candidate is REJECTED when disposition.forced is 1: a forced track carries only signage,
         not dialogue, and it is useless as a lektor script.
      4. The winner is the FIRST non-rejected candidate in ffprobe order. When every candidate is
         rejected the FIRST candidate is returned anyway, flagged – a rejected fallback beats
         skipping an episode that does have a Polish track.

    Returns None only when there is no candidate at all, which is the one case the caller logs and
    skips.
    """
    rejects = _compile_rejects(reject_title_patterns)
    wanted = str(language or "").casefold()

    candidates: list[SubtitleChoice] = []
    for s in streams:
        if s.language.casefold() != wanted:
            continue
        reason = ""
        # Titles only. Never the language, the codec or the filename: "pol" as a reject pattern
        # must not shoot down every Polish track there is.
        for text, rx in rejects:
            # No `if s.title` guard: PowerShell evaluates `'' -match $r` too, and for a pattern
            # that can match the empty string (`''`, `.*`, `^$`) it answers True. Real patterns
            # such as "SDH" miss an empty title on their own, exactly as re.search does.
            if rx.search(s.title):
                reason = f"title matches {text!r}"
        if s.forced:
            reason = "forced disposition (signage, not dialogue)"
        candidates.append(SubtitleChoice(index=s.index, title=s.title, codec=s.codec_name,
                                         rejected=bool(reason), reason=reason))

    if not candidates:
        return None
    for c in candidates:
        if not c.rejected:
            return c
    return candidates[0]


def pick_extractable_subtitle(streams, language: str, reject_title_patterns=None):
    """`pick_subtitle`, then a second look when the winner is a track `-c:s srt` cannot read.

    `pick_subtitle` stays a jot-for-jot port and must: it decides which track becomes the lektor.
    But the bitmap check bolted on after it turned "the first pol stream is PGS" into a SKIPPED
    EPISODE even on releases that also carry a pol subrip track further down – a Blu-ray remux
    with text subs added is exactly that shape. So when the winner is unusable, selection is run
    again over the text streams alone, with the same rules and the same rejected-fallback
    behaviour. Only when there is no text candidate at all does the unusable pick come back, so
    the caller can still log which codec it was.
    """
    choice = pick_subtitle(streams, language, reject_title_patterns)
    if choice is None or is_text_subtitle(choice.codec):
        return choice
    text_only = [s for s in streams if is_text_subtitle(s.codec_name)]
    return pick_subtitle(text_only, language, reject_title_patterns) or choice


def is_text_subtitle(codec_name: str) -> bool:
    """Whether `-c:s srt` can do anything with this stream.

    The one behaviour added rather than preserved: a bitmap track (PGS on a Blu-ray rip, VobSub on
    a DVD one) needs OCR, and saying so beats dying inside ffmpeg with 'Subtitle encoding currently
    only possible from text to text or bitmap to bitmap'.
    """
    return str(codec_name or "").lower() not in BITMAP_SUBTITLE_CODECS


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------


def find_sources(input_dir: Path, work_subdir: str, only: str | None = None) -> list[Path]:
    """Every source video under the input dir, work dir excluded, sorted by file name.

    Recursive, as the PowerShell is. Note the asymmetry it creates: `speech.find_video` looks only
    at `input_dir/<base>.<ext>`, so a season laid out one subdirectory per episode extracts fine
    and then has no video to detect shot cuts in or to take a 5.1 source from.
    """
    needle = str(work_subdir or "").casefold()
    found: list[Path] = []
    for p in input_dir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if needle and needle in str(p).casefold():
            continue
        if only and only not in p.name:
            continue
        found.append(p)
    return sorted(found, key=lambda p: (p.name.casefold(), str(p)))


def subtitle_args(video: Path, stream_index: int, out_srt: Path) -> list[str]:
    """Verbatim argv of the PowerShell's subtitle extract, so the two sides cannot drift."""
    return ["-y", "-i", str(video), "-map", f"0:{stream_index}", "-c:s", "srt", str(out_srt)]


def downmix_args(video: Path, out_flac: Path, audio_stream: str = "a:0",
                 channels: int = 2, rate: int = 48000) -> list[str]:
    """Verbatim argv of the PowerShell's audio downmix. FLAC at compression_level 5 because the
    stereo mix path reads this file end to end several times per episode and a lossless 300 MB
    local file beats demuxing 2.7 GB out of the mkv on every pass."""
    return ["-y", "-i", str(video), "-map", f"0:{audio_stream}", "-ac", str(channels),
            "-ar", str(rate), "-c:a", "flac", "-compression_level", "5", str(out_flac)]


def extract_subtitles(ffmpeg: str, video: Path, stream_index: int, out_srt: Path,
                      force: bool = False) -> bool:
    """Returns True when ffmpeg ran, False when the .srt was already there and force was not set."""
    if out_srt.exists() and not force:
        return False
    run_ffmpeg(ffmpeg, subtitle_args(video, stream_index, out_srt), "subtitle extract")
    return True


def extract_downmix(ffmpeg: str, video: Path, out_flac: Path, audio_stream: str = "a:0",
                    channels: int = 2, rate: int = 48000, force: bool = False) -> bool:
    if out_flac.exists() and not force:
        return False
    run_ffmpeg(ffmpeg, downmix_args(video, out_flac, audio_stream, channels, rate), "audio downmix")
    return True


def write_source_json(path: Path, base: str, video: Path, choice: SubtitleChoice,
                      language: str) -> dict:
    """The `<base>.source.json` sidecar – the only record of which .mkv a work-dir base came from.

    Shape is the PowerShell's, `source_windows` included and translated back to `E:\\...` where
    there is a drive to name, so `windows/lektor.ps1 -Stage mux` still reads a work dir this side
    produced. `source` is added beside it and holds the path in the namespace that wrote the file,
    which is the only one of the two a Linux or macOS box can use at all.
    """
    data = {
        "base": base,
        "source": str(video),
        "source_windows": windows_path(video),
        "subtitle_stream": choice.index,
        "subtitle_title": choice.title,
        "language": language,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def read_source_json(path: Path) -> dict:
    """utf-8-sig, not utf-8: Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM,
    and json.loads chokes on it with 'Expecting value: line 1 column 1'."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def log_extract_error(work_dir: Path, base: str, message: str) -> None:
    """Tab-separated, appended, same format the PowerShell writes: timestamp, base, what happened."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{base}\t{message}\n"
    with (work_dir / "error_log.txt").open("a", encoding="utf-8") as fh:
        fh.write(line)


def stage_extract(cfg, only: str | None = None, force: bool = False) -> None:
    ffmpeg, ffprobe = ffmpeg_bin(cfg), ffprobe_bin(cfg)
    ex = cfg["extract"]
    language = ex["subtitle_language"]
    work_dir = cfg.work_dir
    videos = find_sources(cfg.input_dir, cfg["paths"]["work_subdir"], only)
    if not videos:
        say(f"no source video under {cfg.input_dir}")
        return

    say(f"\nEXTRACT – {len(videos)} file(s), subtitle language '{language}'\n")
    for video in videos:
        base = video.stem
        say(base)
        try:
            subs = probe_streams(ffprobe, video, "s", strict=True)
        except ProbeError as exc:
            # Named, not swallowed into "no 'pol' subtitle stream": the season keeps going either
            # way, but only one of the two log lines sends anyone to the right file.
            say("  !! ffprobe could not read this file – skipped")
            log_extract_error(work_dir, base, f"ffprobe failed: {exc}")
            continue
        choice = pick_extractable_subtitle(subs, language, ex.get("reject_title_patterns"))
        if choice is None:
            say(f"  !! no '{language}' subtitle stream – skipped")
            log_extract_error(work_dir, base, f"no '{language}' subtitle stream")
            continue
        if not is_text_subtitle(choice.codec):
            say(f"  !! stream 0:{choice.index} is {choice.codec}, a bitmap track – OCR it first")
            log_extract_error(work_dir, base, f"bitmap subtitle stream ({choice.codec})")
            continue
        note = f" ({choice.title})" if choice.title else ""
        flag = f" – rejected fallback: {choice.reason}" if choice.rejected else ""
        say(f"  -> subtitle stream 0:{choice.index} [{choice.codec}]{note}{flag}")

        srt = work_dir / f"{base}.srt"
        flac = work_dir / f"{base}.orig.flac"
        if not extract_subtitles(ffmpeg, video, choice.index, srt, force):
            say("  -> srt exists, skipping (--force to redo)")
        if not extract_downmix(ffmpeg, video, flac, ex.get("audio_stream", "a:0"),
                               ex.get("downmix_channels", 2), ex.get("downmix_rate", 48000), force):
            say("  -> downmix exists, skipping (--force to redo)")
        write_source_json(work_dir / f"{base}.source.json", base, video, choice, language)
        say(f"  OK  {srt.name} + {flac.name}")

    say(f"\nWork dir: {work_dir}")
    say("Next:  python3 wsl/lektor.py script  &&  python3 wsl/lektor.py voice\n")


# ---------------------------------------------------------------------------
# Mux
# ---------------------------------------------------------------------------


def track_extension(cfg) -> str:
    """Which file the voice stage actually wrote. One source of truth, on purpose: the PowerShell
    kept its own copy of is_surround/encode_profile and the two disagreed when mix.channel_layout
    was null, so WSL wrote `<base>.lektor.eac3` and the mux looked for `.ac3` and reported no
    tracks."""
    return encode_profile(cfg)["extension"]


def find_tracks(work_dir: Path, ext: str, only: str | None = None,
                suffix: str = "") -> list[tuple[str, Path]]:
    """The finished lektor tracks, as (base, path), sorted by name."""
    tail = f".lektor{suffix}.{ext}"
    out = [(p.name[: -len(tail)], p) for p in sorted(work_dir.glob(f"*{tail}"))
           if p.name.endswith(tail) and (not only or only in p.name)]
    return out


def output_path(cfg, base: str, suffix: str = "") -> Path:
    """Must stay identical to what stage_status reports as MUXED, or the status view lies."""
    return cfg.input_dir / cfg["paths"]["output_subdir"] / f"{base}_PL_LEKTOR{suffix}.mkv"


def resolve_source(cfg, base: str) -> Path | None:
    """The .mkv this base was extracted from: the sidecar first, the input dir second.

    `source` is the native key; `source_windows` is translated only as a courtesy to work dirs the
    PowerShell filled in. A native extract also makes the sidecar optional – the file is right
    there next to the subtitles under its own name.
    """
    sidecar = cfg.work_dir / f"{base}.source.json"
    if sidecar.is_file():
        try:
            data = read_source_json(sidecar)
        except (OSError, ValueError):
            # ValueError covers both json.JSONDecodeError and the UnicodeDecodeError a sidecar
            # truncated mid-write raises, which the narrower catch let escape as a traceback.
            data = {}
        for key, translate in (("source", False), ("source_windows", True)):
            value = data.get(key)
            if not value:
                continue
            p = native_path(str(value)) if translate else Path(str(value))
            if p.is_file():
                return p
    return find_video(cfg, base)


def mux_args(source: Path, track: Path, n_audio: int, dest: Path) -> list[str]:
    """Verbatim argv of Invoke-Mux, in its order, because every part of it is load-bearing.

    `-map 0` keeps every original stream – all 43 subtitles, the attachments, the chapters – and
    the lektor is appended AFTER them, so its audio index is exactly however many audio streams the
    source already had. Hardcoding 1 (which `windows/lektor_mux.ps1` still does) retagged a
    commentary track as the lektor and made THAT the default on any multi-audio release, which is
    why both the metadata target and every disposition are computed from the probed count.

    The dispositions are zeroed on all originals and set to `default` on the new track, so the
    player reaches for the lektor without the viewer picking it out of a menu.
    """
    args = ["-y", "-i", str(source), "-i", str(track),
            "-map", "0", "-map", "1:a:0",
            "-c", "copy",
            f"-metadata:s:a:{n_audio}", "language=pol",
            f"-metadata:s:a:{n_audio}", "title=Lektor AI (PL)"]
    for i in range(n_audio):
        args += [f"-disposition:a:{i}", "0"]
    args += [f"-disposition:a:{n_audio}", "default", str(dest)]
    return args


def mux(ffmpeg: str, ffprobe: str, source: Path, track: Path, dest: Path,
        force: bool = False) -> Path | None:
    """Stream copy of the source plus the lektor into `dest`. None when there was nothing to do."""
    if dest.exists() and not force:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    n_audio = count_audio_streams(ffprobe, source)
    run_ffmpeg(ffmpeg, mux_args(source, track, n_audio, dest), "mux")
    return dest


def stage_mux(cfg, only: str | None = None, force: bool = False, suffix: str = "") -> None:
    ffmpeg, ffprobe = ffmpeg_bin(cfg), ffprobe_bin(cfg)
    ext = track_extension(cfg)
    tracks = find_tracks(cfg.work_dir, ext, only, suffix)
    if not tracks:
        say(f"no *.lektor{suffix}.{ext} in {cfg.work_dir} – run the voice stage first")
        return

    say(f"\nMUX – {len(tracks)} track(s)\n")
    for base, track in tracks:
        say(base)
        source = resolve_source(cfg, base)
        if source is None:
            say(f"  !! no source video for {base} – skipped")
            continue
        dest = output_path(cfg, base, suffix)
        out = mux(ffmpeg, ffprobe, source, track, dest, force)
        if out is None:
            say("  -> output exists, skipping (--force to redo)")
            continue
        say(f"  OK  {out.name} ({round(out.stat().st_size / 1048576)} MB)")

    say(f"\nOutput dir: {output_path(cfg, 'x').parent}\n")
