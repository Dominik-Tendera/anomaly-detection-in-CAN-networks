# CAN Logger — firmware, generator i detekcja anomalii

Repozytorium obejmuje firmware loggera STM32, generator ruchu na komputerze,
odbiór i detekcję na Raspberry Pi oraz analizę i prezentację wyników.
Autor pierwotnego firmware: **kubak**, 2023. Rozbudowa do badań detekcji CAN:
Dominik Tendera.

## Układ repozytorium

| Katalog | Zawartość |
| --- | --- |
| `can_logger/` | Projekt firmware STM32CubeIDE: źródła C, biblioteki, konfiguracja sprzętu i linkera. |
| `generator/` | Generator ruchu referencyjnego i anomalii, scenariusze JSON, kopia DBC i instrukcja. |
| `generator/captures/` | Zapis po stronie komputera: konfiguracja, potwierdzone transmisje, plan epizodów i raport. |
| `raspberry_pi/` | Bieżący odbiornik i detektor, dekoder strumienia, benchmark, kopia DBC i instrukcja. |
| `raspberry_pi/captures/` | Pomiary odbiornika: surowy ślad, alarmy, jakość danych, profil i raport. |
| `results/` | Skrypty analizy kampanii oraz generowania raportów, tabel i wykresów. |
| `results/reports/` | Wyniki pochodne; kampania z pracy znajduje się w `campaign_2026-09-20/`. |
| `tools/` | Współdzielone biblioteki `can_generate`, `can_detect` i obsługa ścieżek. |
| `tests/` | Oddzielne zestawy dla firmware, Raspberry Pi, generatora, bibliotek i integracji. |
| `docs/` | Specyfikacje protokołu binarnego i tekstowych logów diagnostycznych. |
| `archive/` | Historyczne programy, dokumenty, wczesne pomiary i poprzednia wersja raportów. |

Projekt STM32CubeIDE znajduje się w `can_logger/`. W IDE importuj ten katalog
jako istniejący projekt. Zachowano jego wewnętrzną nazwę
`RTE_3_0_CAN_LOGGER_2023`, używaną przez konfigurację kompilacji i debugowania;
nie musi ona odpowiadać nazwie repozytorium. Ścieżki wewnątrz projektu są
względne wobec projektu, a nie katalogu głównego repozytorium.
Nie kopiuj samego `can_logger/` w celu uruchamiania programów Python:
generator i analiza wyników wymagają także `tools/`.

## Gdzie jest DBC?

Aktualna konfiguracja używana przez generator:
`generator/dbc/RTE_3.5_CAN1_CAR.dbc`.
Raspberry Pi ma identyczną kopię:
`raspberry_pi/dbc/RTE_3.5_CAN1_CAR.dbc`.
Obie pochodzą z dotychczasowego `CAN_PARSER/`; podczas porządkowania nie
zmieniono ich zawartości. Po zmianie konfiguracji trzeba zaktualizować obie kopie.
Starszy parser oraz konfiguracja CAN2 DEBUG są w `archive/CAN_PARSER/`.
Pozostawiona kopia `can_logger/CAN_PARSER/` również jest starszym parserem,
nie punktem wejścia bieżącego generatora ani detektora.

## Powiązania i kopie plików

| Część | Kod i dane, z których korzysta |
| --- | --- |
| Firmware `can_logger/` | Własne źródła i biblioteki STM32; nie wymaga programów Python do kompilacji. |
| Generator `generator/` | Własny silnik i scenariusze, lokalna kopia DBC, wspólne moduły w `tools/`. |
| Bieżący detektor `raspberry_pi/live_detect.py` | `raspberry_pi/rpi_receiver/live_rules.py`, dekoder i lokalna kopia DBC. |
| Starszy odbiornik `raspberry_pi/rpi_receiver/can_stream_rx.py` | Ten sam dekoder oraz moduły raportowania w `tools/can_detect/`. |
| Analiza `results/` | Pomiary PC i Raspberry Pi, wspólne moduły, bieżący silnik do odtworzenia detekcji. |

Dwie aktywne kopie DBC umożliwiają osobne wdrożenie generatora i detektora.
Test integracyjny sprawdza ich zgodność z wejściem użytym w badaniach.
Pliki w `archive/deployment/` są historycznym zapisem wersji kodu, a nie drugą
aktywną instalacją. Analiza kampanii korzysta z ich sum kontrolnych do
weryfikacji pochodzenia pomiarów. Nie należy aktualizować ich razem z kodem bieżącym.
`tools/can_detect/` to odrębny, starszy zestaw modułów, nie kopia bieżącego
`live_rules.py`; pozostaje potrzebny wybranym narzędziom i testom.

## Szybki start

Wymagany Python 3.10 lub nowszy. Polecenia poniżej wykonuj z katalogu głównego
repozytorium, we własnym środowisku wirtualnym. Na Linuxie można użyć `python3`.

```bash
python -m pip install -r generator/requirements.txt
python generator/generate_can_traffic.py --dry-run --duration 2 --seed 42 --max-fps 80 --session proba_bez_sprzetu
```

Generator zapisuje dane w `generator/captures/`, a detektor w
`raspberry_pi/captures/`, niezależnie od bieżącego katalogu terminala.
Opcja `--output` pozwala wskazać inny katalog. Nazwy nowych sesji muszą być
unikalne — pomiary nie są nadpisywane.

Szczegółowe instrukcje:

- [Generator na komputerze](generator/README.md)
- [Odbiór i detekcja na Raspberry Pi](raspberry_pi/README.md)
- [Analiza oraz wykresy](results/README.md)
- [Współdzielone moduły i testy](tools/README.md)
- [Zawartość archiwum i mapa przeniesień](archive/README.md)

## Przebieg pomiaru

1. Uruchom odbiornik na Raspberry Pi i generator ruchu referencyjnego na PC.
2. Zbierz profil odniesienia, np. przez 180 s; dla wolniejszych wiadomości
   czas musi wystarczyć na wymaganą liczbę odstępów.
3. Uruchom nową sesję detektora z zapisanym profilem oraz generator anomalii.
   Zachowaj DBC, identyfikatory, ziarno i parametry ruchu referencyjnego.
4. Zachowaj oba zapisy: PC i Raspberry Pi. Potwierdzenie adaptera nie jest
   dowodem odebrania ramki przez logger.
5. Analizuj dane skryptami w `results/`. Nie poprawiaj ręcznie surowych
   `tx.jsonl`, `trace.canbin`, profili ani konfiguracji pomiarów.

Nowy alarm oznacza wejście reguły w stan naruszenia. Utrzymujący się stan
nie powoduje kolejnych alarmów; liczba alarmów nie jest liczbą błędnych ramek.
Aktualny detektor nie implementuje modeli ML ani pomiarów analogowych.

## Testy

Zestawy rozdzielono według odpowiedzialności:

- `tests/can_logger/` — koder i dekoder C, kompilowane na komputerze bez STM32;
- `tests/raspberry_pi/` — bieżący detektor, odbiornik i buforowanie;
- `tests/generator/` — bieżący generator PC i scenariusze anomalii;
- `tests/shared/` — biblioteki z `tools/`, również starsze mechanizmy;
- `tests/integration/` — powiązania katalogów, zgodność DBC, synchronizacja
  czasu oraz porównanie formatu C/Python.

Instrukcja uruchamiania osobnych zestawów: [tests/README.md](tests/README.md).
Plik `tests/synthetic.py` jest wspólnym generatorem danych testowych,
nie programem do wysyłania ruchu na fizyczną magistralę.

Pełny zestaw sprawdza także uruchamianie skryptów wykresów, dlatego wymaga
zależności analizy wyników (obejmują również zależności generatora):

```bash
python -m pip install -r results/requirements.txt
python -m unittest discover --start-directory tests --top-level-directory .
```

Testy kodera C wymagają GCC:
`powershell -ExecutionPolicy Bypass -File tests/can_logger/run_tests.ps1` na Windows
lub `sh tests/can_logger/run_tests.sh` na Linuxie. Python wskazany w PATH musi mieć
zainstalowane zależności. Pełna lista zależności starszych modułów jest
w `tools/requirements.txt`; do generatora i bieżącej detekcji wystarczą
ich własne, krótsze pliki wymagań.
Nie instaluj równocześnie `tools/requirements.txt` i `results/requirements.txt`
w jednym środowisku: historyczny zestaw przypina inną wersję NumPy.
Do opisanej tu weryfikacji używaj `results/requirements.txt`.

Testy Python obejmują również uruchomienie skryptów z obcego katalogu oraz
generowanie ruchu bez sprzętu (`--dry-run`). Testy C kompilują źródło z
`can_logger/Core/Src/` i porównują format binarny z dekoderem Python.
Nie zastępują kompilacji firmware w STM32CubeIDE ani pomiaru na fizycznym UART/CAN.

Przy weryfikacji migracji poprawiono też starszy odbiornik diagnostyczny:
zapis gotowego raportu sesji nie jest już mylony z budowaniem raportu zbiorczego.
Dzięki temu zachowuje on informację o przerwaniu sesji i zebrane liczniki.
Reguł bieżącego detektora ani historycznych danych pomiarowych nie zmieniano.
