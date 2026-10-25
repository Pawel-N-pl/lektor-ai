# Tłumaczenie oryginału na polskiego lektora

Plan i dziennik prac: jak z filmu **bez polskich napisów** zrobić tę samą ścieżkę lektorską, a przy
okazji – tylko kiedy się o to poprosi – polskie napisy.

[< back to the README](../README.md)

---

## Zasada

Nie budujemy drugiego pipeline'u. Wszystko od `speech` do `mux` czyta `<base>.srt` – polską listę
dialogową – i nie obchodzi go, skąd się wzięła. Brakuje tylko **źródła** tego pliku, gdy w mkv nie
ma polskiego tracku. Dziś `extract` pisze wtedy `no 'pol' subtitle stream – skipped` i odcinek
odpada. Po zmianie w to miejsce wchodzi tłumaczenie, a skracanie, dosuwanie do aktora, synteza,
ducking i miks działają bez jednej zmienionej linii.

## Źródła polskiego tekstu, w kolejności

| # | źródło | timing | koszt |
|---|---|---|---|
| 1 | polski track w mkv (jak dziś) | autorski | 0 |
| 2 | tekstowe napisy w innym języku → tłumaczenie LLM | autorski, karta w kartę | centy / odcinek |
| 3 | brak napisów: ASR z audio → tłumaczenie LLM | z alignmentu | GPU lokalnie + centy |

Ścieżka 2 daje najwięcej przy najmniejszym ryzyku: siatka kart zostaje autorska, a `speech` i tak
używa angielskiego SRT jako referencji do forced alignmentu.

## Etapy

`STEPS = extract, transcribe, translate, speech, script, retime, voice, pack, mux`

- **extract** – gdy nie ma polskiego tracku, a tłumaczenie jest włączone, nie odpada, tylko
  wyciąga najlepszy obcojęzyczny track tekstowy (`<base>.src.<lang>.srt`) i zapisuje w
  `source.json` pochodzenie: `subtitle_origin = "track" | "subs" | "asr"`.
- **transcribe** (tylko ścieżka 3) – faster-whisper na kanale centralnym z `speech.centre_filter`
  (bez Demucsa, zmierzony jako szkodliwy), VAD, filtr halucynacji, podział na karty jak w
  prawdziwych napisach (≤ 2 × 42 znaki, ≤ ~6 s, cięcia na pauzach). Wynik
  `<base>.asr.<lang>.srt` jest od razu referencją dla `speech`. Timestampy Whispera są ~316 ms za
  wczesne – start i koniec ustala alignment, nie Whisper.
- **translate** – cały odcinek w jednym wywołaniu OpenRouter (kontekst całego dialogu), JSON schema
  `{id: tekst}`, walidacja każdej odpowiedzi, retry i fallback model, cache po hashu, koszt z
  `usage.cost` do `spend.json`. Tłumaczenie jest **wierne, w stylu napisów, nieskracane** – skraca
  dalej istniejący `condense`, którego promptu nie ruszamy. Ten sam plik jest więc też polskimi
  napisami. Ręczne poprawki w `<base>.srt` są wykrywane i nigdy nadpisywane.

## Napisy: czasem tak, czasem nie

`translate.subtitles = "off" | "sidecar" | "mux"` (+ flaga `--subs`).
`sidecar` kładzie `<film>.pl.srt` obok filmu, `mux` dodaje strumień `language=pol`,
`title=Polski (AI)`, bez flagi default. Do napisów idzie wierne tłumaczenie, nie skrócona lista
lektorska. Dokładamy je tylko wtedy, gdy sami tłumaczyliśmy.

## Konfiguracja

```toml
[translate]
mode = "auto"              # auto: tylko gdy brak 'pol'; always; never
source = "auto"            # auto: napisy > asr; subs; asr
source_language = "auto"   # tag strumienia, potem detekcja Whispera
openrouter_model = '...'
subtitles = "off"          # off | sidecar | mux
```

## Fazy

1. **Napisy → napisy (ścieżka 2) + opcjonalne napisy.** `translate`, provenance w `source.json`,
   `satisfied()` i plan w `pipeline.py`, napisy w `mux_args`, testy.
2. **ASR (ścieżka 3).** `transcribe`, segmentacja, filtry halucynacji, plik ASR jako referencja.
3. **Jakość.** Słownik serii (imiona, ty/pan), wskazówki rodzaju gramatycznego mówiącego,
   aligner wielojęzyczny (MMS_FA) dla oryginałów spoza angielskiego.

---

## Dziennik

_Każda faza dopisuje tu, co zrobiła, co zmierzyła i czego nie dało się sprawdzić._

### Faza 1 – napisy → napisy + opcjonalne napisy: zrobiona

**Co jest.**

- `extract`: gdy nie ma polskiego tracku tekstowego (albo `mode = "always"`), wybiera obcojęzyczny
  track tekstowy – najpierw w języku audio, potem angielski, potem reszta; zwykły dialog przed
  SDH; nigdy forced ani bitmapa – i zapisuje go do `subs/<base>.src.<lang>.srt`, a w
  `<base>.source.json` `subtitle_origin = "subs"`, język, codec i flagę SDH. Film z polskim
  trackiem idzie starą ścieżką i dostaje dokładnie stary sidecar.
- `translate` (`lektorlib/translate.py`): cały odcinek w jednym wywołaniu przez maszynerię
  `script.py` (`_ask`, `CallStats`, `spend.json`), walidacja każdej linii, jeden celowany retry
  (`retry_misses`), potem fallback model; odcinek z choć jedną nieprzetłumaczoną kwestią nie
  dostaje `<base>.srt` wcale. Timingi 1:1 ze źródła. Linie raz kupione leżą w
  `<base>.translated.json`; hash zapisanego pliku w `<base>.translate.json` chroni ręczne
  poprawki przed nadpisaniem bez `--force`.
- `run`: krok między `extract` a `speech`, plan mówi, czy i czym tłumaczy, stopka rozdziela
  rachunek na tłumaczenie i skracanie. `speech` bierze źródło tłumaczenia jako referencję do
  alignmentu. `cost` i `status` pokazują tłumaczenie osobno.
- Napisy: `translate.subtitles` / `--subs` – `sidecar` i `mux` jak w planie, tylko dla pliku,
  który napisał `translate`.

**Odstępstwa od planu.**

- `source_language = "auto"` to nie „tag, potem Whisper”, tylko ranking tracków: język audio,
  angielski, reszta. Whisper wchodzi dopiero w fazie 2.
- Źródło leży w `subs/<base>.src.<lang>.srt`, nie obok `<base>.srt`: każdy etap globuje `*.srt`
  w katalogu roboczym i wziąłby źródło za osobny odcinek.
- `translate.source = "asr"` jest przyjmowane, zgłaszane jako niezaimplementowane i odcinek jest
  pomijany – nie podmieniane po cichu na `subs`.
- `mode = "always"` bez niczego do tłumaczenia bierze jednak polski track filmu i mówi to
  głośno, zamiast gubić odcinek. W tym trybie film zachowuje swój polski track, a tłumaczenie
  idzie obok jako drugi, inny tekst.
- Zmiana `mode` i ponowny `extract` odsuwa `<base>.srt` z poprzedniego trybu na
  `<base>.srt.bak-*`, zamiast zostawić go pod nową etykietą.
- Zmiana źródła – inny `source_language` albo `extract --force`, który wybrał inny track – i
  ponowny `translate` (albo `run`) tłumaczy odcinek od nowa, zamiast pominąć go jako „exists”.
  `<base>.translate.json` pamięta ścieżkę, język i hash źródła; linie, których klucz się nie
  zmienił, nadal idą z `<base>.translated.json` za darmo. Ręcznie poprawiony `<base>.srt` dalej
  wymaga `--force`.
- `translate --redraw` oznacza też `--force`, jak w `script`. W `run` dotyczy `script`, a
  `translate` tylko wtedy, gdy jest w `--redo` albo jest jedynym z tych dwóch kroków w zakresie –
  samo `run --redraw` nie kupuje po cichu tłumaczenia drugi raz.
- Mux z PowerShella (`lektor.ps1 -Stage mux`) nie dokłada napisów; ostrzega i odsyła do muxa z
  WSL. Extract z PowerShella zostawia w spokoju odcinek, którego `source.json` ma
  `subtitle_origin = "subs"` – nie zna `translate.mode` i nadpisałby sidecar bez tego klucza. Ta
  ścieżka nie była uruchomiona: w środowisku nie ma PowerShella.

**Czego nie zmierzono.** Nic nie przeszło jeszcze przez prawdziwy odcinek: brak GPU, kontenera ASR
i dostępu do LLM w środowisku, w którym to powstało, więc wszystko jest sprawdzone wyłącznie
testami jednostkowymi na atrapach. Niezmierzone są: koszt odcinka (`measured_usd_per_episode = 0`
i `cost` mówi to wprost), wybór modelu i fallbacku, `max_tokens`, granice `max_len_ratio` /
`min_len_ratio`, `temperature`, to, co daje celowany retry, oraz jakość tłumaczenia dzielonego na
kawałki (żaden znany odcinek nie zbliża się do `max_prompt_chars`).

**Kluczowe decyzje.**

- Film z polskim trackiem tekstowym (i `mode` inny niż `always`) nie zmienia się wcale: sidecar
  bez `subtitle_origin` znaczy „polski track”, co obejmuje też każdy sidecar napisany przez
  skrypty PowerShella. Polski track wyłącznie jako bitmapa w `auto` liczy się jak brak polskiego
  – film idzie do tłumaczenia zamiast odpaść.
- Kody języków dwu- i trzyliterowe oraz pary w rodzaju `ger`/`deu` są tym samym językiem, bo mkv
  używają obu zapisów.
- `translate` ma ten sam kształt odpowiedzi co skracanie, więc bierze `call_openrouter`, retry i
  throttle bez kopii; `script._ask` dostał jeden argument na schemat odpowiedzi. Przebiegi
  tłumaczenia trafiają do tego samego `<base>.spend.json` z etykietą etapu, a `read_spend` umie
  zsumować jeden etap – wcześniej `cost` liczył cały plik jako skracanie.
- Klucz linii w cache to tekst źródła, język, model, effort, hash promptu i czas startu. Czas
  startu, bo to samo „I'm ready.” może powiedzieć kobieta i mężczyzna, a polski pokazuje rodzaj.
- Plik spoza `translate` albo ręcznie poprawiony po tłumaczeniu nie jest nadpisywany bez
  `--force`; z `--force` najpierw odkładany na `<base>.srt.bak-<czas>`.
- Referencja do alignmentu: angielskie źródło tłumaczenia wchodzi dopiero po wszystkich
  skonfigurowanych wzorcach, a angielski SDH, który nie był źródłem, `speech` nadal wyciąga i
  woli, jak dotąd.
- Napisy w muxie: strumień otagowany jako polski, tytuł „Polski (AI)”, nie domyślny. `.pl.srt`
  obok filmu jest kopiowany przy każdym pakowaniu, żeby późniejsze poprawki do niego dochodziły.
  Z wyłączonymi napisami komenda muxa jest dokładnie stara.
- Literówka w `translate.mode` albo `translate.subtitles` to błąd użycia, nie wyjątek.

**Co sprawdzono.** Pełny zestaw testów: 441 passed, 3 skipped (przed fazą 1: 283). Nowe testy w
`test_translate*.py` idą offline na atrapach i fixture'ach; żaden nie dotyka sieci.

**Otwarte.**

- Fazy 2 i 3; `source = "asr"` czeka na fazę 2.
- Odcinek za długi na jedno wywołanie jest cięty na kawałki, z których każdy widzi tylko siebie –
  bez kontekstu sąsiadów. Do zrobienia, jeśli prawdziwy odcinek kiedyś się nie zmieści.
- `mode = "always"` na filmie, którego polski `<base>.srt` był już wyciągnięty wcześniej, trzyma
  ten plik do `--redo translate` (albo `--force`).
- Pierwszy przebieg na prawdziwym filmie i prawdziwym API: zmierzyć koszt odcinka i wpisać go do
  `measured_usd_per_episode`, sprawdzić granice długości, model i fallback.
- README podaje liczbę linii `lektor.toml` i kluczy w `lektor.tuning.toml`, która była nieaktualna
  już przed tą fazą; nie poprawiona.
