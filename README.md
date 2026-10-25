# lektor-ai

Zamienia polskie napisy w **ścieżkę lektorską**: jeden płaski głos czytający skróconą listę
dialogową na tle oryginału, który schodzi pod niego i wraca między kwestiami. To nie jest druga,
cicha ścieżka TTS z dziurami – to **miks**.

```bash
python3 wsl/lektor.py run --only S01E01
```

Jedno polecenie przechodzi całą drogę: napisy → pomiar mowy → skrócenie tekstu → dosunięcie do
aktora → synteza → miks → **gotowa ścieżka `.mka` obok filmu**. Film **bez polskich napisów** nie
odpada: jego obcojęzyczne napisy są najpierw tłumaczone na polski
([docs/translate.md](docs/translate.md)), a dalej wszystko idzie tą samą drogą. Pomija każdy etap, którego wynik już leży na dysku, wypisuje plan
przed startem, a na końcu mówi co zrobił, co pominął, ile kosztowało i gdzie leży plik.

---

## Co ten pipeline robi inaczej

**Ducking jest spektralny.** Maskowanie to problem średnicy pasma, więc w dół idzie mocno tylko
300–4000 Hz, a reszta widma prawie się nie rusza. Film zachowuje ciężar i powietrze; oryginał siedzi
11 dB pod poziomem swojego dialogu, kiedy lektor mówi.

**Poziom liczony względem dialogu, nie całego filmu.** BS.1770 (LUFS), nie RMS. Na filmie z rzadkim
dialogiem średnia całości leży daleko pod samym dialogiem – „9.5 dB nad programem" wychodziło wtedy
około 18 dB nad scenami, które naprawdę trzeba przekryć. Zmierzone na siedmiu odcinkach: **+6.8 do
+7.2 LU** nad dialogiem, rozrzut 0.4 LU.

**Kwestie są skracane, nie czytane szybciej.** Od jednej trzeciej do połowy kart fizycznie nie da
się wypowiedzieć w swoim oknie. Cały odcinek idzie do modelu w **jednym** wywołaniu, więc każda
kwestia jest skracana w kontekście całego dialogu, a nie trzech sąsiadów.

**Napisy kłamią o tym, gdzie kończy się zdanie.** W jednym wydaniu 55% przerw między kartami to te
same 83 ms; w innym karty stykają się co do milisekundy. To fakt o narzędziu autorskim, nie o
aktorach. Forced alignment – lokalnie, za darmo, około 2 min GPU na odcinek – pokazuje, gdzie
aktorzy naprawdę zrobili pauzę i gdzie montażysta ciął.

**Lektor czeka na aktora** i nie kończy zdania na cięciu montażowym. Jedno i drugie za darmo, bez
zmiany tekstu.

**Ścieżki wybierane po tagu języka, nigdy po numerze.** Te wydania mają ponad czterdzieści ścieżek
napisów, a polski leży pod innym indeksem w różnych plikach tego samego sezonu – 30 w pięciu
odcinkach, 31 w szóstym, bo doszła ścieżka Forced.

**Jednostki imperialne przeliczane, ale z odwróconą logiką ryzyka.** Na 26 950 polskich linii
napisów z 44 filmów zwykłe wyrażenie na nazwy jednostek znalazło 22 trafienia, z czego **dwa** były
miarą. „Stopa" była częścią ciała 12 razy na 12, „funt" walutą 4 na 4, a „cali" przymiotnikiem –
naiwny konwerter przeczytałby „Wszyscy cali i zdrowi" jako „Wszyscy 2,54 cm i zdrowi". Ryzykiem nie
jest więc przeoczona konwersja, tylko zepsute poprawne zdanie, i stąd reguła: jednostka przelicza
się wyłącznie wtedy, gdy stoi tuż za liczebnikiem i zdanie nie pasuje do żadnego idiomu. Wynik jest
zaokrąglany tak, jak człowiek to wypowie – sześć stóp to „metr osiemdziesiąt", nie „1,8288 metra".
**Walut nie przelicza wcale**: kurs się zmienia, a lektor podający starą kwotę w dzisiejszych
złotówkach po prostu kłamie.

**Płacisz raz.** Każda kwestia TTS jest adresowana treścią na dysku, a skrócony tekst cache'owany
pod kluczem zawierającym hash samego promptu – zmienisz prompt, wszystko unieważnia się samo.

Szczegóły, liczby i przypadki brzegowe: **[docs/](docs/)**.

---

## Instalacja

Konfiguracja to **dwa pliki**. `lektor.toml` ma 84 linie i trzyma to, co zmieniasz przy każdym
serialu i po uchu: ścieżki, silnik, głos, poziom, model. W repo leży jako
`lektor.toml.example`; skopiuj go pod właściwą nazwą, bo ta jest w `.gitignore` i twoje
ścieżki nigdy nie wylądują w commicie. Dopóki kopii nie ma, pipeline czyta sam szablon. `lektor.tuning.toml` trzyma wszystkie 233
klucze wraz z pomiarem, który za każdym stoi. Pierwszy nadpisuje drugi, więc **skasowanie linijki
cofa cię do zmierzonej wartości domyślnej, a nie do pustki**.

TOML, a nie JSON, z jednego powodu: 84% starego pliku było prozą trzymaną w kluczach `_note`,
bo JSON nie ma komentarzy. Teraz są komentarzami. `tomllib` siedzi w bibliotece standardowej, więc
nie kosztuje to żadnej zależności. Codium i VS Code nie kolorują TOML-a bez rozszerzenia –
`tamasfe.even-better-toml`.

Wspólne dla wszystkich środowisk: **Python 3.11+**, trzy biblioteki i **ffmpeg**. Reszta jest
opcjonalna, a każda opcja mówi niżej, co przestaje działać bez niej.

**Najpierw wybierz swój układ, nie czytaj po kolei** – trzy poniższe sekcje wykluczają się
nawzajem:

| twój układ | co instalujesz |
|---|---|
| **Linux albo macOS** | wszystko na tej jednej maszynie |
| **Windows bez WSL-a** | wszystko natywnie na Windowsie |
| **Windows + WSL**, źródła na dysku windowsowym | **wszystko w WSL-u. Po stronie Windowsa nie instalujesz ani Pythona, ani uv, ani venva** – najwyżej ffmpega, i to tylko jeśli chcesz używać skryptów z `windows/`. |

`uv` w poniższych komendach to wygoda, nie wymóg. `python -m venv .venv` i `pip install` robią
dokładnie to samo, jeśli uv nie masz albo się zacina.

### Linux / macOS

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python numpy soundfile requests

sudo apt install ffmpeg          # Debian/Ubuntu
brew install ffmpeg              # macOS

cp .env.example .env             # klucze API, jeśli będziesz ich używać
cp lektor.toml.example lektor.toml   # twój config – ten plik jest w .gitignore
```

W `lektor.toml` ustaw **jeden** klucz:

```toml
[paths]
input_dir = '/home/ty/Filmy/Serial.S01'
```

### Windows, bez WSL-a

Działa – cały pipeline to zwykły Python plus wywołania ffmpega, a katalog `wsl/` nazywa się tak
wyłącznie z powodów historycznych. Potrzebujesz Pythona, ffmpega i jednego klucza w configu:

```toml
[paths]
input_dir = 'E:\Filmy\Serial.S01'
```

Komendy krok po kroku: **[docs/install.md](docs/install.md)**. Jedyna decyzja do podjęcia dotyczy
forced alignmentu – Docker Desktop stoi na WSL2, więc na maszynie, która ma być go pozbawiona,
wskaż lokalny aligner w `speech.aligner_venv` albo pogódź się z utratą dosuwania lektora do aktora.

### Windows + WSL, źródła na dysku windowsowym

Układ, w którym ten projekt wyrósł: gigabajty zostają po stronie Windowsa, myślenie dzieje się w
WSL-u, obie połowy czytają ten sam config i spotykają się w katalogu `_lektor`.

W `lektor.toml` ustaw **parę** kluczy, po jednym na stronę:

```toml
[paths]
input_dir_windows = 'E:\Filmy\Serial.S01'
input_dir_wsl     = '/mnt/e/Filmy/Serial.S01'
```

**Po stronie Windowsa nie stawiaj Pythona ani venva.** Cały pipeline chodzi w WSL-u, w tym
jednym venvie z sekcji Linux/macOS wyżej. Windows dostaje najwyżej ffmpega, i tylko na
potrzeby skryptów z `windows/` – a te nie są już potrzebne, bo `lektor.py extract` i `mux`
robią to samo natywnie. Jeśli trafiłeś tu z sekcji „Windows bez WSL-a" i zacząłeś stawiać
tam `uv`: to była inna instrukcja, do innego układu.

`paths.input_dir` zostaw pusty – wtedy każda strona weźmie swój klucz. Skrypty w `windows/` nadal
działają, ale nie są już potrzebne: `lektor.py extract` i `mux` robią to samo natywnie.

### Elementy opcjonalne

Komendy do każdego z nich są w [docs/install.md](docs/install.md) – tu jest tylko to, czego
wymagają i co przestaje działać bez nich.

| co | jak | bez tego |
|---|---|---|
| **Piper** – domyślny silnik, darmowy, offline | `uv pip install --python .venv/bin/python piper-tts` | brak darmowej syntezy |
| **ElevenLabs** | `ELEVENLABS_API_KEY` w `.env` | ten backend nie wystartuje |
| **Skracanie tekstu** (OpenRouter) | `OPENROUTER_API_KEY` w `.env` | kwestie nie są skracane i lektor nie mieści się w oknach; film bez polskich napisów nie zostanie przetłumaczony |
| **OmniVoice** – klonowanie głosu | osobny venv 3.11 z `torch` i `omnivoice` ([komendy](docs/install.md)), ścieżka w `voice.omnivoice.venv`, próbka 3–10 s w `reference_wav` | brak klonowania na GPU |
| **Chatterbox** | osobny venv 3.11 z `torch` i `chatterbox` ([komendy](docs/install.md)) | jak wyżej |
| **kikivoice** | plik sesji z przeglądarki | brak; rozliczany w tygodniowym wiaderku kredytów |
| **Forced alignment** | venv z `torch` i `torchaudio` wskazany w `speech.aligner_venv` ([komendy](docs/install.md)), **albo** kontener i `speech.aligner: "docker"` | zostają same cięcia montażowe: **bez dosuwania lektora na pierwsze słowo aktora i bez pauzowej połowy bramki scalania** |

---

## Silniki

Pięć, za jednym interfejsem. Dopasowanie do okna czasowego jest wiedzą silnika, nie pipeline'u –
każdy trafia w nie po swojemu, a etap `voice` raportuje, co naprawdę wyszło.

| | koszt | sprzęt | jak trafia w okno |
|---|---|---|---|
| **Piper** | darmowy | CPU, 20 s na odcinek | binarnie szuka `length_scale`; 597 z 598 kwestii w oknie |
| **OmniVoice** | darmowy | GPU, około 3 GiB, około 5 min na odcinek batchem | dostaje **długość w sekundach**; klonuje głos z próbki 3–10 s |
| **ElevenLabs** | $0.05–0.10 za 1000 znaków | sieć | przewiduje ze zmierzonego tempa, potem `voice_settings.speed` |
| **Chatterbox** | darmowy | GPU, około 42 min na odcinek | nijak – generuje i rozciąga |
| **kikivoice** | kredyty tygodniowe | sieć plus człowiek do CAPTCHA | okno domykane lokalnie, kwestia kupowana raz |

Porównanie uchem, a nie z tabelki:

```bash
python3 wsl/lektor.py sample --backends piper,omnivoice --seconds 60
```

---

## Etapy osobno

`run` składa je w całość, ale każdy da się odpalić sam:

| etap | robi |
|---|---|
| `extract` | wyciąga polskie napisy i zapisuje, z którego pliku pochodzą; gdy polskich nie ma – najlepsze obcojęzyczne tekstowe, do tłumaczenia |
| `translate` | tylko dla takich filmów: cały odcinek jednym wywołaniem LLM na polski `<base>.srt`, wiernie i z czasami karta w kartę; ręcznych poprawek nie nadpisuje bez `--force` |
| `speech` | forced alignment, ciszę między słowami, cięcia montażowe – lokalnie, za darmo |
| `script` | napisy na skróconą listę dialogową; **otwórz i popraw przed wydaniem na TTS** |
| `retime` | dosuwa wczesne kwestie na pierwsze słowo aktora; nic nie kosztuje |
| `voice` | synteza, dopasowanie do okien, ducking, miks 5.1, enkodowanie |
| `pack` | zapakowanie ścieżki w `.mka` z tagiem języka – **domyślny koniec łańcucha** |
| `mux` | remux do nowego `.mkv`, sam stream copy; **tylko na jawne `--to mux`** |
| `cost` | ile znaków i ile dolarów, bez dotykania API |
| `status` | co już leży na dysku |

Podgląd bez skutków ubocznych: `run --dry-run`. Zakres: `--from`, `--to`, `--skip`, `--redo`.
Polskie napisy z tłumaczenia – `<film>.pl.srt` obok filmu albo strumień w remuxie – tylko na
życzenie: `translate.subtitles` albo `--subs sidecar|mux` na jedno uruchomienie.

**Dlaczego łańcuch kończy się na `pack`, a nie na `mux`.** Odtwarzacz sam podnosi ścieżkę leżącą
obok filmu, więc remux nic nie kupuje, a kosztuje drugą kopię całego wideo – 3.6 GB przy odcinku
3.5 GB. `pack` robi stream copy samego dźwięku do Matroski, nadaje `language=pol`, tytuł i
dyspozycję domyślną. Kontener, a nie surowy `.eac3`, bo elementarny strumień nie niesie tagu
języka – a to właśnie tag sprawia, że odtwarzacz ustawiony na polski dźwięk wybiera tę ścieżkę
sam, zamiast pokazać bezimienne „Track 2". Plik musi nosić nazwę bazową filmu i leżeć w jego
katalogu, bo po tym działa automatyczne podnoszenie.

---

## Koszt

Około **$1.20–1.45 za odcinek** na ElevenLabs plus $0.15–0.20 za skracanie tekstu, albo **$0.00**
na Piperze i OmniVoice. Tłumaczenie filmu bez polskich napisów to osobny rachunek z OpenRouter,
jeszcze niezmierzony na prawdziwym odcinku – `cost` mówi to wprost, a `<base>.spend.json` zapisuje,
ile naprawdę kosztowało. Wszystko, co lokalne – alignment, detekcja cięć, bramka scalania,
retiming – kosztuje zero.

Szczegóły: [docs/cost.md](docs/cost.md).

---

## Dokumentacja

| plik | o czym |
|---|---|
| [docs/install.md](docs/install.md) | pełna instalacja, każde środowisko, każdy element opcjonalny |
| [docs/backends.md](docs/backends.md) | pięć silników, zmierzone, i jak je porównywać |
| [docs/subtitles.md](docs/subtitles.md) | wybór ścieżki, czyszczenie, scalanie kart, bramka dowodowa |
| [docs/translate.md](docs/translate.md) | film bez polskich napisów: tłumaczenie, plan i dziennik prac |
| [docs/script.md](docs/script.md) | skracanie, reguły promptu, drabina modeli, cache |
| [docs/timing-and-mix.md](docs/timing-and-mix.md) | kiedy lektor mówi, jak planowane jest tempo, jak zbudowany jest miks |
| [docs/cost.md](docs/cost.md) | znaki, dolary, kredyty, minuty GPU |
| [docs/rough-edges.md](docs/rough-edges.md) | co nie działa dobrze, powiedziane wprost |

Sekrety idą wyłącznie ze środowiska albo z `.env`, nigdy z pliku konfiguracyjnego:
`ELEVENLABS_API_KEY`, `OPENROUTER_API_KEY`.
