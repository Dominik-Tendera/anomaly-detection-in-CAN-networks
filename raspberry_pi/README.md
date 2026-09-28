# Odbiór i detekcja CAN na Raspberry Pi

`live_detect.py` odbiera surowy strumień UART, zapisuje go i analizuje w
oddzielnym wątku. Używa `rpi_receiver/live_rules.py` i wspólnego dekodera
`rpi_receiver/can_stream_protocol.py`. Nie uruchamiaj dwóch odbiorników
na tym samym porcie jednocześnie.

## Instalacja i pomiar

Bieżący detektor i benchmark można uruchomić z samego katalogu
`raspberry_pi/`, wraz z `rpi_receiver/`, `dbc/` i `requirements.txt`.
Nie kopiuj do nowej instalacji dużego katalogu historycznych `captures/`.
Starszy diagnostyczny `rpi_receiver/can_stream_rx.py` wymaga także `tools/`
z pełnego repozytorium.

Na Raspberry Pi, w katalogu `raspberry_pi/`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --learn-seconds 180 --session odniesienie_42
```

Najpierw uruchom odbiornik, następnie właściwy ruch referencyjny z PC.
Dla każdej wiadomości potrzeba co najmniej 100 poprawnych odstępów
(`--min-intervals`); 180 s nie wystarcza dla wiadomości o okresie 10 s.
Profil po uczeniu jest zamrażany. Zakończ sesję Ctrl+C.

Kolejna sesja z gotowym profilem:

```bash
.venv/bin/python live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --baseline captures/odniesienie_42/baseline.json --session anomalie_42
```

Domyślny DBC to `dbc/RTE_3.5_CAN1_CAR.dbc`, a wyniki trafiają do
`captures/<sesja>/` przy skrypcie, niezależnie od katalogu terminala.
Nie nadpisuje się istniejących sesji. `--dbc` i `--output` zmieniają te ścieżki.

## Zawartość pomiaru i reguły

| Plik | Znaczenie |
| --- | --- |
| `trace.canbin` | Surowe odebrane bajty, także niepoprawne rekordy. |
| `events.jsonl` | Nowe alarmy wraz z regułą, czasem, ID, pomiarem i progiem. |
| `quality.jsonl` | Problemy ciągłości, dekodowania i jakości danych. |
| `baseline.json` | Profil wyuczony lub kopia profilu wejściowego. |
| `config.json` | Parametry, sumy kontrolne DBC, profilu i kodu. |
| `report.json` | Liczniki, stan analizy i jej podsumowanie. |

Zaimplementowano kontrolę nieznanego ID, DLC i zakresów DBC, brak ramki,
odchylenie okresu, zmianę liczebności w oknie, odchylenie 3σ, burst oraz
informacje diagnostyczne kontrolera. Szczegóły progów podaje `--help`
i kod `Settings` w `live_rules.py`; nie zmieniono ich przy porządkowaniu.

Alarm jest zapisywany przy wejściu w naruszenie, a nie dla każdej kolejnej
błędnej ramki. Kontrole czasowe korzystają z czasu loggera, nie z chwili
odbioru przez Linux. Błędy jakości są rejestrowane osobno. Pojedyncze błędy
dekodowania i luki sekwencji przerywają ciągłość odstępów, ale zachowują
dotychczas zebrane poprawne statystyki uczenia. Restart i nieciągłość czasu
mogą ponownie rozpocząć uczenie.

## Ponowna analiza i benchmark

```bash
.venv/bin/python live_detect.py --replay captures/anomalie_42/trace.canbin --baseline captures/odniesienie_42/baseline.json --session ponowna_analiza_42
.venv/bin/python benchmark_live_detect.py --frames 85000
```

W trybie replay nie powstaje nowy surowy ślad. Zachowaj te same ustawienia
reguł co podczas pomiaru. Benchmark zapisuje wyniki w `benchmarks/`;
nie dowodzi bezstratnej akwizycji fizycznego UART.

Historyczne konfiguracje zawierają stare ścieżki i sumy kontrolne kodu.
Pozostawiono je bez zmian jako dokumentację pomiarów. Analiza kampanii
porównuje sumy z zachowaną wersją w `archive/deployment/`, a nie z kodem
zmodyfikowanym wyłącznie w celu zmiany ścieżek.
