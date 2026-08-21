#!/usr/bin/env python3
"""Refit the duration model on a length-stratified benchmark.

`timing.overhead_ms` and `timing.ms_per_char` are two parameters of one straight line, and the
corpus they were fitted on spanned 43 to 63 characters - a twenty-character window. Separating a
fixed per-utterance cost from a per-character cost over that span is ill-conditioned: the data
cannot tell the two apart, so the split is decided by noise and every extrapolation to a short or
long line inherits it. `stage_calibrate` never fitted them at all; it reports a single characters
per second ratio, which is a different model.

This measures what the pipeline actually consumes: the clip AFTER trim_silence, spoken at speed
1.0, over lengths from a single word to a long sentence.

    tools/fit_duration.py [--repeats 1]
"""

import argparse
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wsl"))

from lektorlib.audio import trim_silence          # noqa: E402
from lektorlib.backends import get_backend        # noqa: E402
from lektorlib.backends.base import Utterance     # noqa: E402
from lektorlib.config import Config               # noqa: E402

# Six bands, six lines each. Ordinary Polish prose, every line properly terminated - an
# unterminated line is a separate phenomenon and measuring it here would poison the fit.
CORPUS = [
    "Dobrze.", "Nie wiem.", "Chodźmy.", "Zaczekaj.", "To on.", "Naprawdę?",
    "Nie mam na to czasu.", "Wróciła wczoraj wieczorem.", "Zamknij za sobą drzwi.",
    "Powiedz mu, że czekam.", "To był bardzo długi dzień.", "Nikt się tego nie spodziewał.",
    "Poczekaj chwilę, muszę odebrać ten telefon.", "Trener poprosił o rozmowę w cztery oczy.",
    "To był zdecydowanie najgorszy tydzień w moim życiu.",
    "Powiedziałem mu wprost, że tak dalej być nie może.",
    "Zawodnicy wychodzą na murawę, a kibice zaczynają skandować.",
    "Nie mam pojęcia, o czym mówisz, ale brzmi to całkiem rozsądnie.",
    "Wrócił do domu dopiero nad ranem i nie odezwał się do nikogo ani słowem.",
    "Przez całą noc padało tak mocno, że rzeka wystąpiła z brzegów pod młynem.",
    "Kiedy wreszcie otworzyli bramę, na dziedzińcu nie było już żywej duszy.",
    "Obiecał, że wróci przed świtem, ale nikt z nas w to naprawdę nie wierzył.",
    "Zostawili go tam samego, bez konia, bez broni i bez najmniejszej nadziei.",
    "Mówił cicho, jakby bał się, że ktoś podsłuchuje pod drzwiami komnaty.",
    "Nikt nie pamiętał już, kiedy dokładnie zaczęła się ta wojna, ani o co tak naprawdę toczyła się od początku.",
    "Wszyscy zgodzili się, że trzeba coś zrobić, ale kiedy przyszło do działania, każdy patrzył w inną stronę.",
    "Opowiadał tę historię tak wiele razy, że przestał już odróżniać, co w niej wydarzyło się naprawdę.",
    "Przez lata żył w przekonaniu, że wystarczy poczekać, a sprawy jakoś ułożą się same, bez jego udziału.",
    "Kiedy zapadła cisza, wszyscy zrozumieli, że nie ma już nic do dodania i że decyzja została podjęta.",
    "Wracali w milczeniu przez pola, a za nimi, w oddali, dopalały się jeszcze zabudowania gospodarstwa.",
    "Wtedy jeszcze nie wiedział, że ta jedna decyzja, podjęta w pośpiechu i bez zastanowienia, będzie ciążyła mu przez resztę życia i odbierze wszystko, co zbudował.",
    "Powiedziano mu, że jeśli zawróci teraz i odda to, co zabrał, nikt nie będzie go ścigał, a cała sprawa zostanie zapomniana, zanim jeszcze ktokolwiek zdąży o niej usłyszeć.",
    "Siedzieli tam do późna, opowiadając sobie rzeczy, o których nigdy wcześniej nie mówili nikomu, i żaden z nich nie potrafił później wyjaśnić, dlaczego akurat tamtej nocy.",
    "Kiedy wreszcie dotarli pod mury, okazało się, że brama stoi otworem, straże zniknęły, a w całym mieście nie pali się ani jedno światło, jakby wszyscy odeszli już dawno.",
    "Tłumaczył cierpliwie, że nie chodzi o pieniądze ani o ziemię, tylko o to, żeby ktoś wreszcie powiedział głośno prawdę, której wszyscy w tej okolicy domyślali się od lat.",
    "Zapamiętał z tamtego dnia niewiele: zapach mokrej słomy, krzyk kobiety gdzieś za stodołą i to dziwne uczucie, że wszystko, co się właśnie dzieje, ogląda z bardzo daleka.",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=1)
    args = ap.parse_args()

    cfg = Config(ROOT / "lektor.toml")
    sr_cfg = cfg["mix"]["sample_rate"]
    b = get_backend(cfg.backend_name, cfg)
    b.prepare()
    rows = []
    try:
        for _ in range(args.repeats):
            for text in CORPUS:
                # target_ms=None so nothing asks the engine to hurry: this is its natural pace.
                r = b.render(Utterance(index=0, text=text, target_ms=None))
                raw_ms = r.audio.size / r.sample_rate * 1000.0
                trimmed = trim_silence(r.audio, r.sample_rate)
                rows.append((len(text), trimmed.size / r.sample_rate * 1000.0, raw_ms, text))
    finally:
        b.close()

    n = np.array([x[0] for x in rows], dtype=float)
    ms = np.array([x[1] for x in rows], dtype=float)
    A = np.vstack([np.ones_like(n), n]).T
    (overhead, per_char), *_ = np.linalg.lstsq(A, ms, rcond=None)
    pred = overhead + per_char * n
    resid = ms - pred
    ss = 1.0 - float(np.sum(resid ** 2) / np.sum((ms - ms.mean()) ** 2))

    print(f"  {len(rows)} linii, {int(n.min())}-{int(n.max())} znakow "
          f"(stary korpus: 6 linii, 43-63)")
    print(f"\n  DOPASOWANIE na klipach PO obcieciu ciszy, predkosc 1.0:")
    print(f"    overhead_ms  {overhead:8.1f}   (w configu {cfg['timing']['overhead_ms']})")
    print(f"    ms_per_char  {per_char:8.2f}   (w configu {cfg['timing']['ms_per_char']})")
    print(f"    R^2 {ss:.4f}, reszty: sd {resid.std():.0f} ms, najgorsza {np.abs(resid).max():.0f} ms")
    old = float(cfg['timing']['overhead_ms']) + float(cfg['timing']['ms_per_char']) * n

    # A negative fixed cost is not a thing an engine can do - it is the straight line paying for
    # curvature it cannot represent. So the constrained fit is reported beside it: if forcing the
    # intercept to zero costs almost nothing, that is the honest model to ship.
    best = None
    for oh in np.arange(0.0, 400.0, 5.0):
        pc = float(np.sum(n * (ms - oh)) / np.sum(n * n))
        r = ms - (oh + pc * n)
        s2 = float(np.sum(r ** 2))
        if best is None or s2 < best[0]:
            best = (s2, oh, pc)
    _, oh0, pc0 = best
    r0 = ms - (oh0 + pc0 * n)
    print(f"\n  wariant z nieujemnym kosztem stalym:")
    print(f"    overhead_ms  {oh0:8.1f}")
    print(f"    ms_per_char  {pc0:8.2f}")
    print(f"    R^2 {1 - np.sum(r0**2)/np.sum((ms-ms.mean())**2):.4f}, "
          f"reszty sd {r0.std():.0f} ms, najgorsza {np.abs(r0).max():.0f} ms")
    rold = ms - old
    print(f"\n  obecny model 47 + 73.5*n: R^2 "
          f"{1 - np.sum(rold**2)/np.sum((ms-ms.mean())**2):.4f}, "
          f"reszty sd {rold.std():.0f} ms, najgorsza {np.abs(rold).max():.0f} ms")

    for lo, hi in [(0, 15), (15, 32), (32, 55), (55, 80), (80, 115), (115, 400)]:
        s = (n >= lo) & (n < hi)
        if not s.any():
            continue
        print(f"    {lo:3d}-{hi:3d} zn. ({int(s.sum()):2d}): stary myli sie o "
              f"{np.median(old[s] - ms[s]):+7.0f} ms, nowy o {np.median(pred[s] - ms[s]):+6.0f} ms")


if __name__ == "__main__":
    main()
