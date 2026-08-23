#!/usr/bin/env python3
"""Whole-episode condensation: the tested prompt shape and the scorer behind the model comparison.

This is the harness that produced the numbers quoted in the build brief. Read SYSTEM and
whole_prompt() and reuse them in lektorlib/script.py rather than inventing new ones.

Measured on the real S02E01 cue list (598 cues, 210 needing shortening), reasoning effort medium:

    openai/gpt-5.6-luna           $0.0077/ep    67 s   210/210 returned   206 in budget   <- default
    openai/gpt-5.6-luna-pro       $0.0168/ep    95 s   210/210            208
    google/gemini-3.5-flash-lite  $0.0140/ep    11 s   206/210            120
    openai/gpt-5.6-terra          $0.0373/ep    31 s   210/210            158
    google/gemini-3.6-flash       $0.1963/ep    93 s   207/210            196

Batching the same work on gemini-3.6-flash cost $0.433/ep, so whole-episode is 2.2x cheaper on an
identical model AND gives every cue the full episode as context.

{"reasoning":{"enabled":false}} returns HTTP 400 on these models; only {"effort": ...} works.
OpenRouter reports real spend in usage.cost -- log that, never recompute from token prices.
"""
import argparse
import json, os, sys, time, pathlib, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

SC = pathlib.Path(__file__).resolve().parent
ROOT = SC.parent

# The srt helpers live in the project. Prefer the refactored package; fall back to the pre-refactor
# module kept beside this file so the harness stays runnable mid-refactor.
sys.path.insert(0, str(ROOT / "wsl"))
sys.path.insert(0, str(SC))
try:
    from lektorlib import srt as S, config as CFG  # noqa: N812
    _new = True
except Exception:
    import lektor_original as S  # type: ignore
    CFG = S
    _new = False

def api_key() -> str:
    """The key, looked up when it is needed rather than when the module loads.

    At module level this was `os.environ[...]`, which raised KeyError on every invocation - `--help`
    included - and did so before Config had run, so the project .env that supplies the key
    everywhere else had not been read yet. Two failures for the price of one: the script could not
    describe itself, and it could not see a key the rest of the pipeline could.
    """
    CFG.Config(ROOT / "lektor.toml")          # loads the project .env as a side effect
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set (put it in the project .env or export it)")
    return key

URL = "https://openrouter.ai/api/v1/chat/completions"

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "lektor_cues",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "cues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["cues"],
            "additionalProperties": False,
        },
    },
}

SYSTEM = """Jesteś doświadczonym autorem tekstów lektorskich do polskiej telewizji.

Dostajesz PEŁNĄ listę dialogową jednego odcinka serialu, w kolejności chronologicznej.
Większość kwestii służy wyłącznie jako kontekst - masz je przeczytać, żeby rozumieć scenę,
postacie i wątki, ale ICH NIE ZWRACASZ.

Zwracasz wyłącznie kwestie oznaczone znacznikiem >>> SKROC DO N ZNAKOW <<<.

Zasady skracania:
- Zachowaj sens, intencję i ton. Nie wolno zgubić informacji potrzebnej do zrozumienia fabuły.
- Tekst lektorski to nie napisy. Skracaj tak, jak robi to lektor: wywalaj wypełniacze,
  powtórzenia i redundancję, którą widz i tak widzi na ekranie. Upraszczaj składnię.
- BEZWZGLĘDNIE zachowaj imiona, nazwiska, nazwy własne i liczby. Wywalenie imienia to błąd.
- Zachowaj polskie znaki diakrytyczne. Nigdy nie zapisuj "remisow" zamiast "remisów".
- Naturalna polszczyzna mówiona, poprawna fleksyjnie. Bez didaskaliów, nawiasów i cudzysłowów.
- Wynik MUSI mieć nie więcej znaków niż podany limit. To twarde ograniczenie.
- Jeśli nie da się zejść do limitu bez utraty sensu, zwróć najkrótszą wierną wersję.
  Lepiej minimalnie przekroczyć limit niż zmienić znaczenie.
- Wykorzystuj kontekst sąsiednich kwestii: jeśli imię padło zdanie wcześniej, można użyć zaimka.

Odpowiadasz wyłącznie obiektem JSON: {"cues":[{"id":<int>,"text":"<skrócona kwestia>"}]}"""


def whole_prompt(cues, target_indices):
    """Every cue in order; only the ones needing work carry a limit marker."""
    ts = set(target_indices)
    return "\n".join(
        f"[{c.idx}] >>> SKROC DO {c.budget_chars} ZNAKOW <<< {c.text}" if i in ts else f"[{c.idx}] {c.text}"
        for i, c in enumerate(cues)
    )


def load_cues(srt_path=None, cps=14.5, safety=0.96):
    # Through the package loader, not json.loads: the config is a TOML PAIR now, and reading
    # lektor.toml as JSON raised on every invocation of this script, --help included. Config._read
    # also merges lektor.tuning.toml underneath, which the old monolithic JSON carried inline - so
    # this is what keeps the 200 measured constants in the benchmark instead of only the 33 knobs.
    # The temp file below stays JSON on purpose: _read still accepts a flat legacy .json, and a
    # temp .toml would need a matching .tuning.toml beside it to mean the same thing.
    cfg = CFG.Config(ROOT / "lektor.toml").raw
    cfg["paths"]["input_dir_wsl"] = str(SC)
    cfg["paths"]["work_subdir"] = "testwork"
    p = SC / "bench_cfg.json"
    p.write_text(json.dumps(cfg))
    C = CFG.Config(p)
    srt_path = srt_path or (SC / "testwork/Ted.Lasso.S02E01.Test.srt")
    cues = [c for c in S.parse_srt(srt_path) if not S.should_skip(c.raw, C)]
    for c in cues:
        c.text = S.clean_text(c.raw, C)
    cues = [c for c in cues if c.text]
    S.compute_windows(cues, C)
    for c in cues:
        c.budget_chars = max(12, int(c.window_ms / 1000 * cps * safety))
    return cues


def call(model, effort, user, max_tokens=48000):
    body = {
        "model": model, "temperature": 0.3, "max_tokens": max_tokens,
        "response_format": SCHEMA, "reasoning": {"effort": effort},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        URL,
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/local/lektor-ai", "X-Title": "lektor-ai"},
        data=json.dumps(body).encode(), method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as f:
            return {"resp": json.load(f), "secs": time.time() - t0}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}", "secs": time.time() - t0}


def run(model, effort, cues, targets):
    r = call(model, effort, whole_prompt(cues, targets))
    if "error" in r:
        return {"model": model, "effort": effort, "error": r["error"]}, {}
    u = r["resp"]["usage"]
    ch = r["resp"]["choices"][0]
    base = {"model": model, "effort": effort, "secs": r["secs"], "cost": u.get("cost", 0),
            "reasoning_tokens": u.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
            "finish": ch.get("finish_reason"), "n": len(targets)}
    try:
        got = {int(c["id"]): c["text"].strip() for c in json.loads(ch["message"]["content"])["cues"]}
    except json.JSONDecodeError:
        # A reasoning model can spend the whole max_tokens budget thinking and get cut off
        # mid-string. Production code has to survive this, so the benchmark records it rather
        # than dying on it.
        return {**base, "returned": 0, "fit": 0, "error": f"truncated JSON (finish={base['finish']})"}, {}
    fit = sum(1 for i in targets if len(got.get(cues[i].idx, "x" * 999)) <= cues[i].budget_chars)
    return {**base, "returned": sum(1 for i in targets if cues[i].idx in got), "fit": fit}, got


def main():
    # There was no argument parsing at all, and the corpus it defaults to - a test .srt that lived
    # in the wiped scratch directory - has no history in this repo. So the script could only fail,
    # with a FileNotFoundError naming a path nobody can restore. Any episode's subtitles work.
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--srt", type=pathlib.Path, default=None,
                    help="subtitles to benchmark against (default: the vanished test corpus)")
    # Kept positional: this is how the script has always been given its model list, and adding
    # argparse around it must not take that away.
    ap.add_argument("combos", nargs="?", default=None,
                    help='JSON list of [model, effort] pairs, e.g. \'[["openai/gpt-5.6-luna","medium"]]\'')
    args = ap.parse_args()
    if args.srt and not args.srt.is_file():
        raise SystemExit(f"no subtitles at {args.srt}")
    if not args.srt:
        fallback = SC / "testwork/Ted.Lasso.S02E01.Test.srt"
        if not fallback.is_file():
            raise SystemExit(
                f"no corpus: {fallback} is not in this repository - it lived in a scratch "
                f"directory that was wiped. Pass --srt pointing at any episode's .srt instead.")
    cues = load_cues(srt_path=args.srt)
    targets = [i for i, c in enumerate(cues) if len(c.text) > c.budget_chars]
    print(f"odcinek: {len(cues)} kwestii, {sum(len(c.text) for c in cues)} znakow, "
          f"{len(targets)} do skrocenia  (srt helpers: {'lektorlib' if _new else 'lektor_original'})\n")
    combos = json.loads(args.combos) if args.combos else [["openai/gpt-5.6-luna", "medium"]]
    # Never more than 3 at once, and the pipeline's own throttle spaces them further apart.
    with ThreadPoolExecutor(max_workers=min(3, len(combos))) as pool:
        results = list(pool.map(lambda c: run(c[0], c[1], cues, targets), combos))
    print(f"{'model':<30}{'ef':<7}{'czas':>6}{'$':>9}{'$/sezon':>9}{'rozum.':>8}{'zwroc':>8}{'w limicie':>11}  finish")
    for i, (r, got) in enumerate(results):
        if "error" in r:
            print(f"{r['model']:<30}{r['effort']:<7}{r.get('secs',0):>5.0f}s{r.get('cost',0):>9.4f}"
                  f"{'':>9}{r.get('reasoning_tokens',0):>8}{'':>8}{'BLAD':>11}  {r['error'][:40]}")
            continue
        print(f"{r['model']:<30}{r['effort']:<7}{r['secs']:>5.0f}s{r['cost']:>9.4f}{r['cost']*12:>9.2f}"
              f"{r['reasoning_tokens']:>8}{r['returned']}/{r['n']:<4}{r['fit']:>11}  {r['finish']}")
        out = SC / f"bench_out_{r['model'].replace('/', '_')}_{r['effort']}_{i}.json"
        out.write_text(json.dumps(got, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
