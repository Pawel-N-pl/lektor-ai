# Installing lektor-ai

Every environment it runs in, what each one needs, and what stops working when an optional piece is missing.

[< back to the README](../README.md)

---

## Setup

Two layouts. Pick one; the difference is only who opens the .mkv.

### (a) Linux or macOS, on its own

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python numpy soundfile requests
uv pip install --python .venv/bin/python piper-tts        # only for the piper backend
source .venv/bin/activate                                  # every command below assumes this

sudo apt install ffmpeg          # Debian/Ubuntu: gives both ffmpeg and ffprobe
brew install ffmpeg              # macOS

cp .env.example .env             # then put the two keys in it
```

Then two keys in `lektor.toml`: `paths.input_dir_wsl` is the directory holding the `.mkv`
files (the name is historical, it is simply "the input directory as this OS sees it"), and
`paths.ffmpeg_wsl` / `paths.ffprobe_wsl` stay as `"ffmpeg"` / `"ffprobe"` when both are on PATH.
The `paths.*_windows` twins are read only by `windows/lektor.ps1`; nothing in Python ever reaches
for them, because a Windows binary cannot open the POSIX paths every stage hands it.

`uv venv` does not activate anything and the dependencies go into `.venv` only, so a bare `python3`
in a fresh shell is the system interpreter and exits with `missing dependency (No module named
'soundfile')`. Either activate as above, or spell every command `.venv/bin/python wsl/lektor.py …`.

`.env.example` lists the two names: `OPENROUTER_API_KEY` (the script stage) and
`ELEVENLABS_API_KEY` (the elevenlabs backend). They are read from `<project root>/.env`
automatically, so nothing has to be exported first, and an already-exported variable still wins
over the file. Only those two names are ever taken from `.env`; anything else in it is ignored, so
a shared file cannot inject `PATH` into a subprocess.

**ffmpeg and ffprobe are not optional and not only for extract.** `config`, `media`, `mix`,
`loudness`, `speech` and `stages` all shell out to them: decoding the source, measuring BS.1770
loudness, detecting shot cuts, stretching a clip, encoding the track.

### (b) Windows, natively, with no WSL at all

Nothing in the Python half needs WSL. The package directory is called `wsl/` because that is where
it grew up, not because it depends on it: everything in there is Python, `subprocess` calls to
ffmpeg, and HTTPS. Three things used to stand in the way and all three are gone -

  * `select.select` on the worker pipe, which on Windows accepts sockets only and raises on a pipe.
    It was the single call keeping the two GPU backends from talking to their models at all;
    `worker.py` now uses a reader thread and a queue, which behaves the same on every platform.
  * `<venv>/bin/python`, spelled that way in three places. `venvs.py` tries both that and
    `<venv>\Scripts\python.exe`, because the layout follows the venv that was created rather than
    the machine reading it.
  * `Config.input_dir`, which read the WSL key unconditionally.

```powershell
winget install Gyan.FFmpeg
winget install astral-sh.uv

uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe numpy soundfile requests
copy .env.example .env
```

Set one path key and nothing else:

```json
"paths": { "input_dir": "E:\\Filmy\\Serial.S01" }
```

`paths.ffmpeg` and `paths.ffprobe` are optional - without them the binaries are taken from `PATH`,
which is what winget just put them on. The `_windows` keys are for the PowerShell half only and are
never consulted by the Python one, because a Windows binary cannot open the POSIX paths that half
hands it.

```powershell
.venv\Scripts\python.exe wsl\lektor.py run --only S01E01
```

**Forced alignment is the one thing to decide here.** Docker Desktop runs Linux containers on a
WSL2 or Hyper-V backend, so `speech.aligner: "docker"` reintroduces the dependency this whole
section exists to avoid. Point `speech.aligner_venv` at a venv with `torch` and `torchaudio`
instead - that is all the aligner needs, it runs the identical script, and it copies nothing in or
out because the audio is already on this filesystem. With neither, the stage still runs and writes
an artefact carrying shot cuts only, which costs the onset-based retiming and the pause half of the
merge gate.

### (c) Windows + WSL, sources on `E:`

The current layout here, and the reason the PowerShell exists: the sources sit on an NTFS drive,
Windows does the gigabyte-scale I/O natively, WSL does everything else, and both sides meet in the
same `_lektor` directory seen from both worlds.

```powershell
winget install Gyan.FFmpeg          # then point paths.ffmpeg_windows / ffprobe_windows at the exe
.\windows\lektor.ps1 -Stage extract
.\windows\lektor.ps1 -Stage mux
.\windows\lektor.ps1 -Stage status
```

WSL still needs its own ffmpeg (`sudo apt install ffmpeg`) and its own venv, exactly as in (a):
`paths.input_dir_windows` and `paths.input_dir_wsl` are two hand-maintained strings that must name
the same directory, and nothing verifies that they do. `<base>.source.json` carries both spellings
of the source path, so a work dir produced by either extract can be finished by either mux.

### Optional pieces, and what stops working without them

**piper voice** – downloaded on first use into `~/.cache/lektor-ai/piper-voices`, 63 MB, once. The
Polish shelf is `pl_PL-darkman-medium` (male, flat – the lektor profile), `pl_PL-bass-high`,
`pl_PL-mc_speech-medium`, `pl_PL-gosia-medium` (female) and `pl_PL-mls_6892-low`. If the download
fails the backend prints the two exact `curl` commands and deletes the half-written file.

**chatterbox** – its own python 3.11 venv, pointed at by `voice.chatterbox.venv`. **GPU only**,
2607 MiB resident in bfloat16 (3716 MiB in float32).

```bash
uv venv --python 3.11 ~/.cache/lektor-ai-chatterbox/venv-chatterbox
uv pip install --python ~/.cache/lektor-ai-chatterbox/venv-chatterbox/bin/python \
    torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python ~/.cache/lektor-ai-chatterbox/venv-chatterbox/bin/python chatterbox-tts
```

The model is held by a worker subprocess launched from that venv and spoken to over a JSON pipe, so
the pipeline itself never imports torch. `voice.chatterbox.dtype` must stay `bfloat16` or
`float32`; `float16` is refused by name.

**omnivoice** – same shape, its own python 3.11 venv at `voice.omnivoice.venv`, and the worker
imports **torch, omnivoice, numpy and soundfile**, failing with a named message if any of the four
is missing.

```bash
uv venv --python 3.11 ~/.cache/lektor-ai-omnivoice/venv-omnivoice
uv pip install --python ~/.cache/lektor-ai-omnivoice/venv-omnivoice/bin/python \
    torch --index-url https://download.pytorch.org/whl/cu124
uv pip install --python ~/.cache/lektor-ai-omnivoice/venv-omnivoice/bin/python \
    omnivoice numpy soundfile
```

**GPU only, and it means it**: the worker exits with a sentence when `torch.cuda.is_available()`
is False, naming the torch build it found. `prepare()` sets `PYTORCH_CUDA_ALLOC_CONF=
expandable_segments:True` for it, and the worker falls back from `device_map="cuda:0"` to loading
on CPU and moving, because on a shared GPU the single contiguous 2.28 GiB warmup allocation is
refused outright while a 1 GiB one succeeds. `voice.omnivoice.reference_wav` is the voice to clone,
3–10 s, kept outside the repo on purpose: it is a recording of a real person, not source code.

**kikivoice** – no venv, but the client lives in another repository. `voice.kikivoice.client_dir`
must point at the directory holding `kikivoice_client.py` and `kikivoice_batch.py` (here:
`/home/pawel/projects/kikivoice`); it is deliberately not vendored, so `--backend kikivoice`
without it exits with a named error rather than an ImportError. `voice.kikivoice.clone.sample_path`
is the voice sample to clone from, 5–120 s. The budget is a **weekly bucket of 80 000 credits**,
not money, and a render can **stop and exit 75** waiting for a human to solve a GeeTest challenge
in a browser – roughly every 30 accepted tasks. Re-running the same command resumes from cache.

**forced alignment for `speech`** – optional, and the only step that needs more than ffmpeg. Two
delivery routes, chosen by `speech.aligner` (`auto` by default):

* `local` – a venv named by `speech.aligner_venv` with torch and torchaudio in it, and nothing
  else. No container, no daemon, nothing copied: the wav and the cues are already on this
  filesystem. This is the only route a Windows or macOS box without a container runtime has.
* `docker` – hand the same script to a running container named by `speech.asr_container`
  (`asr` here). That container is **borrowed, not owned**: it belongs to `~/projects/yt-translator`
  and is used only for the torchaudio 2.6 already installed there, so the torch pin must not be
  disturbed. This repo installs nothing into it and writes nothing into its source tree.
* `auto` – local when a usable venv is configured, docker otherwise.

Without either, the stage says so and writes an artefact carrying **shot cuts only**: the merge
gate degrades to cut-only, `retime` has no onsets to move anything onto, and the pipeline is
otherwise byte-identical to a run without the artefact. Shot detection itself is plain ffmpeg
(`scdet`) and always works. Alignment also needs an English reference subtitle under
`speech.reference_srt_patterns` – the acoustic model is English – and without one the stage says
`no English reference subtitle` and skips straight to the cuts.

Everything else lives in `lektor.toml`, which every side reads, and nearly every key carries
a `_<key>_note` explaining what it is and what was measured to land on that value. No secrets in it.
