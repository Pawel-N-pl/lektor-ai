# Piper recon — measurements salvaged from the killed recon agent

Real numbers produced on this machine. Trust them; do not re-derive unless something contradicts.

## Speed (598-cue episode, the real S02E01 cue list)

| mode | model load | per-cue median | RTF | whole episode |
|---|---|---|---|---|
| CPU, all 16 threads | 0.87 s | 0.244 s | 0.064 | **138.4 s (2.31 min)** |
| CPU, 1 thread | 1.13 s | 0.389 s | 0.081 | slower |
| CUDA (RTX 3090) | 1.22 s | 0.088 s | 0.063 | **136.5 s (2.27 min)** |

Per-cue latency on CPU across 200 cues: mean 275 ms, p50 265 ms, p90 419 ms, p99 553 ms.

## The decisive finding: CUDA buys nothing

Per long cue CUDA is 3x faster (54.8x realtime vs 19.7x), but across a whole episode the two are
within 1.4% of each other (136.5 s vs 138.4 s) — short cues are dominated by per-call overhead, not
by inference. A later CUDA attempt also failed outright with an onnxruntime EP Error
(`cuda_call.cc:129`), i.e. the GPU path is both pointless here and more fragile.

**Therefore: run Piper on CPU. Do not add onnxruntime-gpu. VRAM delta on CUDA was only 533 MiB, so
this is not a memory decision — it is a "the GPU does not help and can break the run" decision.**

## Open items the killed agent had not reached

- Exact package/import/class names and the pl_PL voice download URLs still need confirming — but the
  benchmark above ran, so a working recipe exists; re-establish it quickly and cheaply.
- The length_scale -> duration relationship was NOT measured before the agent died. This is the single
  most important thing to establish for the binary search. Measure it first.
