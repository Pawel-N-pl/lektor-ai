# Chatterbox recon — measurements salvaged from the killed recon agent

Real numbers produced on this machine. A partially built venv survives at
`~/.cache/lektor-ai-chatterbox/venv-chatterbox` (python 3.11, chatterbox installed,
`chatterbox/mtl_tts.py` present) along with bench scripts and a cut reference clip in `out/`.
Reuse it — do not reinstall from scratch.

## Measured

- Model load: **45.0 s**. Warmup generation: **43.5 s**.
- VRAM: baseline 9909 MiB -> after load 12276 MiB. Steady-state **delta 2607 MiB**.
- Generation speed, builtin voice, Polish (`language_id="pl"`), 3 runs:
  `gen_s=12.9 audio_s=4.88 rtf=2.644` · `gen_s=11.77 audio_s=4.48 rtf=2.626` · `gen_s=15.22 audio_s=5.68 rtf=2.680`

**RTF ≈ 2.65 means generation takes 2.65x longer than the audio lasts.** Piper does the same work at
RTF 0.064. Chatterbox is therefore roughly **40x slower per second of speech**, before counting the
45 s model load.

## Two crashes, both reproducible in the transcript

1. **Voice cloning failed.** `run("clone_wav24k", audio_prompt_path=REF)` raised inside
   `chatterbox/mtl_tts.py`. The reference clip was a valid 8 s mono 24 kHz PCM wav cut from the
   episode. Cloning is the main reason to want Chatterbox at all, so this must be made to work or
   honestly reported as broken.
2. **Temperature sweep crashed the CUDA kernel:**
   `probability tensor contains either inf, nan or element < 0`, preceded by
   `forcing EOS token ... token_repetition=True`. That is a model-stability failure, not a usage error.

## Honest read

Chatterbox is slow, memory-hungry, and crashed on two of the three things that would justify it.
Do not force it. A backend that raises a clear NotImplementedError naming the exact blocker is a
better deliverable than one that pretends to work.
