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
  napisami. Ręczne poprawki w `<base>.srt` są wykrywane i nigdy nadpisywane. Model dostaje też
  słownik serii i podpisy mówiących z napisów SDH, jeśli są (niżej). Odcinek za długi na jedno
  wywołanie idzie kawałkami, a każdy kawałek widzi po `context_cues` kwestii sąsiadów.

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
glossary = "auto"          # auto: <input_dir>/lektor.glossary.toml, jeśli jest; ścieżka; off
context_cues = 20          # tylko dla odcinka ciętego na kawałki: kontekst z sąsiadów

[speech]
multilingual_aligner = false   # MMS_FA dla referencji nie po angielsku; licencja CC-BY-NC
```

## Słownik serii

Jeden plik na serial, `lektor.glossary.toml` obok filmów (albo inny, wskazany w
`translate.glossary`). Rozstrzyga raz to, co model musiałby zgadywać w każdym odcinku od nowa –
i co zgadywałby różnie: płeć postaci, `ty` czy `pan`/`pani` między parą postaci, stały przekład
terminu.

```toml
[characters.Ted]
gender = 'm'                      # 'm' albo 'f'; pomiń, gdy serial tego nie mówi
aliases = ['Coach', 'Lasso']
note = 'amerykański trener, do wszystkich mówi po imieniu'

[address]
default = 'ty'
'Nate -> Ted' = 'pan'             # jak pierwszy zwraca się do drugiego
'Ted -> Rebecca' = 'pani'

[terms]
'Greyhounds' = 'Charty'
```

- Do promptu odcinka idzie tylko to, czego odcinek może użyć: postać, której imię (alias, podpis
  SDH) w nim pada, para dwóch takich postaci, termin, który w nim pada.
- **Koszt zmiany jest częścią klucza cache.** Zmieniony termin tłumaczy od nowa tylko kwestie, w
  których pada. Zmieniona postać albo forma zwracania się – cały odcinek, w którym ta postać jest:
  kto mówi do kogo, model ustala z kontekstu, nie ze słów kwestii, więc węższa reguła byłaby
  zgadywaniem. Wpis, którego odcinek nie używa, nie kosztuje go nic. Odcinek bez słownika ma
  klucze dokładnie takie jak przed fazą 3.
- Tłumaczenie zrobione z inną wersją używanej części słownika jest nieaktualne jak tłumaczenie z
  podmienionego źródła: `translate` i `run` robią je jeszcze raz, płacąc tylko za to, co zmiana
  dosięga. Ręcznie poprawiony `<base>.srt` dalej wymaga `--force`.
- `script`, `voice`, `pack` i `mux` sprawdzają tylko, czy ich plik jest, więc po nowym
  `<base>.srt` lektor dalej czyta stare tłumaczenie. Plan i `run` to mówią; usłyszeć zmianę
  pozwala `--redo script,voice,pack` (i `mux`), które płaci jeszcze raz za skracanie i za TTS
  kwestii spoza cache – dlatego nie dzieje się samo, a skrypt może mieć ręczne poprawki.
- Plik, którego nie da się przeczytać – zły TOML, płeć `male`, forma `sir`, nieznana tabela –
  zatrzymuje etap, zanim cokolwiek zostanie kupione.
- `lektor.py glossary [--only X] [--force]` prosi model raz o szkic z jednego odcinka i zapisuje go
  do `lektor.glossary.suggested.toml`. **Szkic nie jest używany**, dopóki człowiek go nie poprawi i
  nie zapisze jako `lektor.glossary.toml`; istniejący szkic jest nadpisywany tylko z `--force`.

## Kto mówi: podpisy SDH i myślniki

Polski pokazuje płeć mówiącego w czasowniku. Gdzie źródło samo to mówi, model dostaje to wprost:

- Podpis `JOHN:` (wielkimi literami, też `TED (V.O.):`), `Mary:` dla imienia ze słownika i
  `[Mary]` albo `(Mary)` dla imienia, które zna słownik albo inny podpis w odcinku, jest wyjmowany z
  tekstu kwestii i pokazywany przed nią jako wskazówka `{mówi: Mary, kobieta}`. Wcześniej `JOHN:`
  szedł do tłumaczenia i lektor go czytał.
- Nawias na początku linii to znacznie częściej dźwięk niż imię, więc `[Laughs]` zostaje tekstem.
- Karta z dwoma podpisami: `{mówią po kolei: Ted (mężczyzna), Rebecca (kobieta)}`; podpisy w
  liniach bez myślników dostają myślniki w prompcie i w układzie polskiej karty. Karta, której rip
  zgubił pierwszy myślnik: `{mówiących: 2}`. Karta dialogowa z podpisem tylko przy jednej połowie
  (SDH często podpisuje tylko mówiącego spoza kadru) nie dostaje imienia – przeszłoby na drugą
  połowę razem z płcią – tylko wskazówkę jak karta bez podpisów. Ten sam podpis w kolejnej linii
  to ta sama kwestia: bez nowego myślnika. Podpis, po którym na karcie nie ma już żadnej linii
  (ostatnia linia albo tuż przed następnym podpisem), nie jest mówiącym: ani myślnika, ani imienia.
- Prompt każe trzymać rodzaj konsekwentnie i brać płeć ze słownika. Odcinek bez podpisów i bez
  zgubionych myślników idzie bez żadnej wskazówki i z tymi samymi kluczami cache co przedtem.

## Alignment poza angielskim

Aligner `speech` jest angielski (WAV2VEC2_ASR_LARGE_LV60K_960H, MIT; na tym materiale wygrał z
MMS_FA 88 do 57 z 97 kwestii) i taki zostaje dla angielskiego. Referencja w innym języku –
transkrypt niemieckiego filmu albo niemieckie napisy filmu mówionego po niemiecku – idzie do
torchaudio MMS_FA, **tylko gdy `speech.multilingual_aligner = true`**:

- Domyślnie wyłączone, bo wagi MMS_FA są na licencji **CC-BY-NC 4.0** (niekomercyjnej); włączenie
  klucza to świadoma zgoda na te warunki. Wyłączone – film nie po angielsku dostaje same cięcia
  montażowe, jak dotąd, a plan i log mówią dlaczego. Odcinek, który `speech` już zmierzył z samymi
  cięciami, zachowuje ten plik i po włączeniu klucza – trzeba `--redo speech`, o czym mówi plan.
- Język bierze się z `source.json` (napisy) albo `asr.json` (transkrypt). Napisy są referencją
  tylko w języku ścieżki audio – hiszpańskie napisy angielskiego filmu to tłumaczenie – i wtedy
  przed angielskimi wzorcami. Ścieżka audio bez tagu niczego nie dowodzi. Takim napisom klucz nie
  pomaga, więc plan i log radzą wtedy angielskie napisy, nie MMS_FA.
- Romanizacja: pismo łacińskie składane do a–z tutaj (bez akcentów, `ß` → `ss`, `ł` → `l`), bez
  instalowania czegokolwiek. Słowo zachowuje znaki łączące (virama, znaki samogłosek, harakat,
  akcent w NFD) – `\w` Pythona ich nie zna i bez tego `नमस्ते` szło do uromana jako `नमस` i `त`.
  Inne pismo wymaga pakietu `uroman` w interpreterze alignera (torchaudio go nie ma); bez niego
  odcinek dostaje same cięcia, z głośnym komunikatem.

## Fazy

1. **Napisy → napisy (ścieżka 2) + opcjonalne napisy.** `translate`, provenance w `source.json`,
   `satisfied()` i plan w `pipeline.py`, napisy w `mux_args`, testy.
2. **ASR (ścieżka 3).** `transcribe`, segmentacja, filtry halucynacji, plik ASR jako referencja.
3. **Jakość.** Słownik serii (imiona, ty/pan), wskazówki rodzaju gramatycznego mówiącego,
   aligner wielojęzyczny (MMS_FA) dla oryginałów spoza angielskiego – za kluczem, z powodu
   licencji – i kontekst sąsiadów dla odcinka ciętego na kawałki.

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
- ~~`translate.source = "asr"` jest przyjmowane, zgłaszane jako niezaimplementowane i odcinek jest
  pomijany – nie podmieniane po cichu na `subs`.~~ Nieaktualne od fazy 2: `asr` wysyła film do
  `transcribe`.
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

- Faza 3. (~~`source = "asr"` czeka na fazę 2~~ – zrobione w fazie 2.)
- ~~Odcinek za długi na jedno wywołanie jest cięty na kawałki, z których każdy widzi tylko siebie –
  bez kontekstu sąsiadów.~~ – zrobione w fazie 3 (`translate.context_cues`).
- `mode = "always"` na filmie, którego polski `<base>.srt` był już wyciągnięty wcześniej, trzyma
  ten plik do `--redo translate` (albo `--force`).
- Pierwszy przebieg na prawdziwym filmie i prawdziwym API: zmierzyć koszt odcinka i wpisać go do
  `measured_usd_per_episode`, sprawdzić granice długości, model i fallback.
- ~~README podaje liczbę linii `lektor.toml` i kluczy w `lektor.tuning.toml`, która była
  nieaktualna już przed tą fazą~~ – poprawione po fazie 2 (171 linii, 302 klucze), razem z
  `docs/install.md`.

### Faza 2 – ASR (ścieżka 3): zrobiona

**Co jest.**

- `extract`: film bez polskiego tracku tekstowego i bez obcego (albo z `source = "asr"`) nie
  odpada, tylko dostaje downmix i sidecar z `subtitle_origin = "asr"` i tagiem języka audio.
  Niczego tu nie transkrybuje.
- `transcribe` (`lektorlib/transcribe.py`), krok między `extract` a `translate`, także osobna
  komenda (`lektor.py transcribe [--only] [--force]`): kanał centralny przez
  `speech.extract_centre` (downmix, gdy centrum brak albo jest ciche), bez Demucsa;
  faster-whisper poza interpreterem pipeline'u – skrypt na stdin do pythona z `transcribe.venv`
  albo kontenera, tą samą drogą co forced alignment (`speech.run_script_local/docker`), JSON z
  powrotem. Brak faster-whisper w tamtym interpreterze to osobny kod wyjścia i jeden komunikat,
  jak go zainstalować.
- Filtry halucynacji w czystym Pythonie: `no_speech_prob` z `avg_logprob`, współczynnik
  kompresji, pętle n-gramów, ten sam tekst w kolejnych segmentach, same nuty / `[Music]`, segment
  poza regionami mowy z VAD, czarna lista fraz. Każdy odrzucony segment zostaje w
  `<base>.asr.json` z powodem.
- Cięcie na karty jak w napisach: ≤ 2 × 42 znaki, ≤ 6 s, najpierw na końcu zdania, potem na
  pauzie, potem po przecinku, nigdy w środku słowa; chińskie i japońskie znaki pełnej szerokości
  (`。！？`, `、，；：`) liczą się tak samo. Żadne cięcie nie zostawia karcie mniej niż
  `min_cue_room_ms` (400 ms, niezmierzone) do następnej – bez tego krótkie „Yes.” tuż przed
  następnym słowem dostawało kartę 1 ms, a `translate` kopiuje czasy 1:1. Wynik
  `subs/<base>.asr.<lang>.srt`, który `translate` czyta tak samo jak track z `extract`.
- Język: `translate.source_language`, potem tag ścieżki audio, potem detekcja Whispera; który i
  skąd – zapisane. Każdy zapis języka znanego Whisperowi (setka kodów, 639-1 i 639-2 B/T, wycofane
  `iw`/`in`/`ji`) jest tłumaczony na jego kod; nieznany jest pomijany z komunikatem w planie i
  logu etapu, zamiast trafić do modelu, który odrzuciłby go dopiero po załadowaniu. Transkrypt
  zrobiony dla innego języka niż teraz żądany jest robiony od nowa, a stary plik usuwany; do
  tego czasu nikt go nie czyta – `translate` każe uruchomić `transcribe`, także gdy ponowna
  transkrypcja się nie udała i stary plik został na dysku.
- `run`, `cost`, `status`, stopka: transkrypcja jako darmowy krok lokalny; plan mówi, kiedy
  transkrypcja jest pewna, a kiedy zależy od tego, co znajdzie `extract`, i że po nowym
  transkrypcie `translate` przetłumaczy odcinek jeszcze raz. Napisy (`sidecar`, `mux`) obejmują
  też film przetłumaczony z transkryptu. Extract i mux z PowerShella omijają film z
  `subtitle_origin = "asr"` jak przetłumaczony (nieuruchomione: w środowisku nie ma PowerShella).
- Konfiguracja: nowa tabela `[transcribe]` w `lektor.tuning.toml` – gdzie (`runner`, `venv`,
  `container`; pusty `container` to `speech.asr_container`), model i dekodowanie (`model =
  "large-v3"`, `device`, `compute_type`, `beam_size`, `vad*`, `condition_on_previous_text`,
  `timeout_s`), progi filtrów (`max_no_speech_prob`, `min_avg_logprob`, `max_compression_ratio`,
  `loop_*`, `max_identical_run`, `min_speech_overlap`, `blacklist`, `blacklist_min_cover`) i
  kształt kart (`max_line_chars`, `max_lines`, `max_cue_ms`, `min_cue_ms`, `min_gap_ms`,
  `min_cue_room_ms`, `max_pause_in_cue_ms`, `split_pause_ms`, `linger_ms`, `onset_shift_ms`);
  w `lektor.toml.example` tylko `runner`, `venv`, `model`, `device`. Instrukcja instalacji
  runtime'u w `docs/install.md`.
- Artefakty: `subs/<base>.asr.<lang>.srt` (karty) i `<base>.asr.json` (ścieżka, język i jego
  pochodzenie, ustawienia, segmenty przyjęte i odrzucone z powodem).

**Odstępstwa od planu.**

- Plan mówił, że timestampy Whispera są ~316 ms za wczesne i że start ustala alignment. Nie są
  korygowane: `transcribe.onset_shift_ms = 0`. Karty z transkryptu to siatka; `speech` wyrównuje
  angielski transkrypt do audio, a `retime` przesuwa kwestię na pierwsze wyrównane słowo, tak samo
  jak przy autorskich napisach.
- Angielski transkrypt jest referencją do alignmentu **przed** skonfigurowanymi wzorcami, nie po
  nich jak źródło tłumaczenia z fazy 1: `retime` dopasowuje karty po czasie startu, a tylko
  transkrypt ma ich siatkę.
- Audio, które Whisper słyszy jako polskie, jest odrzucane – tłumaczenie na samego siebie oblałoby
  kontrolę „wróciło nieprzetłumaczone” linia po linii.
- Pod `mode = "always"` film z polskim trackiem jest transkrybowany tylko przy `source = "asr"`;
  `auto` zostawia mu jego track.
- Film, którego jedyny polski track to bitmapa (PGS/VobSub) i który nie ma nic innego, jest w
  `auto` transkrybowany, zamiast odpaść z „OCR it first”; `source = "subs"` zostawia stare
  pominięcie.
- Kolumna kroku w planie jest o znak szersza („transcribe” ma 10 liter) – to zmieniło oczekiwane
  napisy w 8 starych testach planu.
- `source = "subs"` wyłącza transkrypcję także dla filmu, który wcześniejsza konfiguracja wysłała
  już do `transcribe`: etap go pomija, a `run` zatrzymuje się na nim z powodem.

**Czego nie zmierzono.** Nic. Transkrypcja nie przeszła przez żaden prawdziwy film: w
środowisku nie ma GPU, faster-whisper ani kontenera ASR. Niezmierzone są: czas odcinka i
realtime factor, VRAM drugiej kopii modelu w pożyczonym kontenerze, ile segmentów filtry zrzucają
na prawdziwym audio, jak często detekcja języka myli się na filmie bez tagu, jakość tłumaczenia z
transkryptu i lektora czytającego z niego. Wartości `[transcribe]` są domyślnymi biblioteki albo
powszechną praktyką Whispera tam, gdzie komentarz to mówi; pozostałe (pętle, powtórzenia, udziały
mowy i czarnej listy, pauzy, `linger_ms`, `timeout_s`) to nieprzetestowane wybory tego projektu.
Sam skrypt ASR był uruchomiony tylko przeciw atrapie pakietu `faster_whisper`. Kroki instalacji
CUDA w `docs/install.md` są wzięte z dokumentacji faster-whisper, nie wykonane tutaj.

**Kluczowe decyzje.**

- Wynik transkrypcji siedzi w `<base>.asr.json`, nie w `<base>.source.json`: sidecar ma jednego
  pisarza – `extract` – i jego ponowne uruchomienie nie zgubi tego, czego nauczyła się
  transkrypcja.
- Brak runtime'u nie degraduje niczego, tylko zatrzymuje etap: film bez napisów nie ma innego
  tekstu. Venv, docker i działający kontener są sprawdzane przed wycięciem dźwięku; brak samego
  faster-whisper wychodzi dopiero z uruchomionego skryptu.
- `condition_on_previous_text = false` wbrew domyślnemu Whispera: podawanie poprzedniego tekstu
  jako promptu pozwala jednej halucynacji powtarzać się minutami (szeroko opisywane, tu
  niezmierzone).
- Ręcznie poprawiony transkrypt jest odkładany na `.bak-*`, zanim `--force` go zastąpi.

**Co sprawdzono.** Pełny zestaw testów: 582 passed, 3 skipped (po fazie 1: 441). Testy
`test_transcribe*.py` idą offline: filtry i cięcie na karty na skonstruowanych segmentach, etap z
podmienionym wycinaniem dźwięku i wywołaniem modelu, plan i `run` na prawdziwym `Config`, skrypt
ASR przeciw atrapie `faster_whisper`. Żaden nie dotyka sieci ani GPU.

**Otwarte.**

- Pierwszy przebieg na prawdziwym filmie: czas, VRAM, odsetek zrzuconych segmentów, jakość cięcia
  na karty; dopiero potem strojenie progów.
- Dosuwanie kart do cięć ujęć: `speech` mierzy cięcia dopiero po tym etapie.
- Ponowne cięcie na karty z istniejącego `<base>.asr.json` bez uruchamiania Whispera od nowa.
- Whisper nie daje mówców, więc karta z transkryptu nigdy nie ma myślnika zmiany mówcy.
- ~~Faza 3: słownik serii, rodzaj gramatyczny mówiącego, aligner wielojęzyczny (MMS_FA) – dziś
  transkrypt w innym języku niż angielski nie jest referencją do alignmentu~~ – zrobione w fazie 3.
  Bez `speech.multilingual_aligner` nadal nie jest, i jego karty zostają tak wczesne, jak postawił
  je Whisper (`onset_shift_ms = 0`).
- Linie karty z transkryptu łamią się tylko na spacjach: tekst chiński czy japoński bez spacji
  mieści się w karcie tylko jako jedna linia (`layout`/`fits_lines`).

### Faza 3 – jakość: zrobiona

**Co jest.**

- Kontekst sąsiadów: odcinek cięty na kawałki (`translate.max_prompt_chars`) pokazuje każdemu
  kawałkowi `translate.context_cues` (20) kwestii z każdej strony do przeczytania, nie do
  zwrócenia. Odcinek w jednym wywołaniu idzie jak dotąd.
- Słownik serii (`lektorlib/glossary.py`, `translate.glossary`): postacie z płcią i aliasami,
  formy zwracania się w parach, terminy. Do promptu idzie tylko to, czego odcinek może użyć, a
  klucz cache każdej kwestii obejmuje tylko to, co ją dosięga – zmiana terminu tłumaczy od nowa
  jego kwestie, zmiana postaci albo pary cały odcinek, w którym postać jest. Odcinek bez słownika
  ma stare klucze. `stale_source` i plan mówią, że tłumaczenie powstało bez słownika, z inną jego
  wersją albo ze słownikiem, którego już nie ma; `<base>.translated.json` trzyma linie sprzed
  zmiany, więc powrót do poprzedniej wersji nic nie kosztuje.
- Szkic słownika: `lektor.py glossary [--only X] [--force]`, jedno wywołanie przez `script._ask`
  z własnym schematem i parserem, rachunek w `<base>.spend.json` jako `"stage": "glossary"`. Pisze
  tylko `lektor.glossary.suggested.toml`.
- Podpisy mówiących (`lektorlib/speakers.py`): podpis SDH wychodzi z tekstu kwestii i staje
  przed nią jako wskazówka `{mówi: …}`, `{mówią po kolei: …}` albo `{mówiących: N}`, z `HINT_RULES`
  w prompcie systemowym tylko dla odcinka z choć jedną wskazówką.
- Alignment poza angielskim: `speech.multilingual_aligner` (domyślnie wyłączony, licencja
  CC-BY-NC 4.0) wysyła referencję w innym języku do MMS_FA – transkrypt albo napisy w języku
  ścieżki audio. Pismo łacińskie składane do a–z tutaj, inne przez `uroman` w interpreterze
  alignera, z kodem języka ISO 639-3. Angielski dostaje dokładnie stary model i stare argumenty.
  Plan prosi o `--redo speech`, gdy `<base>.speech.json` ma same cięcia, a referencja dała się
  przeczytać dopiero później.

**Odstępstwa od planu.**

- Karta dialogowa z podpisem tylko przy jednej połowie nie dostaje imienia: przeszłoby na
  niepodpisaną połowę razem z płcią. Ten sam podpis w kolejnej linii nie otwiera nowej kwestii.
- `Mary:` (nie wielkimi literami) jest podpisem tylko dla imienia ze słownika; „zna je inny
  podpis” wystarcza tylko nawiasom.

**Czego nie zmierzono.** Nic z modelem. Żaden odcinek nie został przetłumaczony ze słownikiem
ani ze wskazówkami – nie wiadomo, czy model trzyma się płci i form z promptu, ani ile kosztuje
szkic słownika. Żaden prawdziwy odcinek nie był dość długi, by go ciąć, więc `context_cues = 20`
to wybór, nie pomiar. MMS_FA nie wyrównał tu ani jednego odcinka: brak GPU, torcha i wag; jego
jakość, szybkość, VRAM i skala jego `score` wobec `min_score` są nieznane. Skrypt alignera
przeszedł tylko przez atrapę torcha i torchaudio, bez `uroman`; API `uroman` wzięte z jego
dokumentacji. Sam `uroman` 1.3.1.1 był uruchomiony tylko poza skryptem, żeby sprawdzić kod
języka: `βιβλίο` daje `vivlio` z `ell` i `biblio` z `gre`. Ile
`WARNING:`-podobnych słów zostaje wziętych za podpis `NAME:`, nie sprawdzono na żadnym pliku.

**Poprawki po przeglądzie.** Słowa referencji z niełacińskim pismem zachowują znaki łączące.
Podpis bez linii po nim na karcie nie jest osobnym mówiącym. Napisy w innym języku niż audio (albo
przy audio bez tagu) nie dostają rady o MMS_FA, który by ich nie przyjął. Plan i `run` mówią, że
po nowym `<base>.srt` skrypt i ścieżka lektora są ze starego tłumaczenia. `<base>.translated.json`
zapisuje hash promptu z regułami słownika i wskazówek, jak jego klucze. Dodane testy przypinają:
reguły i szablony w kluczu cache, zgodność `stale_source` z etapem dla postaci znanej tylko z
podpisu, drogę przez uromana w etapie `speech` i kod języka w skrypcie, parę `Ted -> team` i
komendę `glossary` w CLI.

**Co sprawdzono.** Pełny zestaw testów: 696 passed, 3 skipped (po fazie 2: 582). Nowe testy w
`test_glossary*.py`, `test_speakers.py`, `test_translate_glossary.py` i
`test_multilingual_align.py` idą offline: odpowiedzi modelu z atrapy `requests.post`, skrypt
alignera na atrapie torcha i torchaudio, plan na prawdziwym `Config`. Żaden nie
dotyka sieci ani GPU.

**Otwarte.**

- Pierwszy przebieg ze słownikiem i podpisami na prawdziwym odcinku: czy rodzaj i formy się
  zgadzają, i co kosztuje szkic.
- Pierwszy przebieg MMS_FA: ile kwestii się wyrównuje, i czy `min_score` z angielskiego modelu
  ma dla niego sens.
- Karta z podpisem tylko przy jednej połowie mogłaby nazwać tę połowę (`?` dla drugiej) – wymaga
  zmiany `HINT_RULES`, a więc kluczy każdego odcinka ze wskazówkami.
