# tools

Working scripts that run the pipeline end to end while parts of them still live outside the
package. They were written in a scratch directory under `/tmp`, which was wiped once mid-session
and took the fixtures with it, so they are versioned here regardless of how finished they are.

| script | what it does | status |
|---|---|---|
| `render51.py` | The renderer that actually produces every episode: 5.1, lektor on the centre channel, spectral ducking, neighbour-smoothed speeds, presence shelf, streaming limiter. | **Ahead of the package.** `lektorlib.stages.stage_voice` is still stereo with broadband ducking; this is the thing to fold in. |
| `bench_condense.py` | Whole-episode condensation harness. Produced the model comparison quoted in `lektor.config.json`. | Reference; re-run it when changing model or prompt. |
| `voice_shootout.py` | Renders one hard 150 s window through N candidate voices, mixed exactly like an episode, so a voice is judged in context rather than on a dry sample. | Reference; re-run when changing `voice_id`. |

`render51.py` imports `lektorlib` for everything shared (trim, shelf, envelope, limiter) and keeps
only the 5.1 mixing loop of its own. That loop is the migration target: once `stage_voice` grows a
channel layout and a ducking mode, this file becomes a thin wrapper and then nothing at all.

Run them with the project venv and the project `.env`:

    set -a && . ./.env && set +a
    .venv/bin/python tools/render51.py "/mnt/e/Filmy/<show>/<episode>.mkv"
