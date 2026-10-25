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
