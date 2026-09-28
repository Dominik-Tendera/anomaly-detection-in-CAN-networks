# Detekcja CAN na żywo na Raspberry Pi

Punkt wejścia: `tools/live_detect.py`. Korzysta z tego samego dekodera `tools/rpi_receiver/can_stream_protocol.py`, którego używa sprawdzony odbiornik z RPi. Surowe bajty są zapisywane i opróżniane z bufora pliku przed przekazaniem do analizy. Oddzielny wątek analizuje rekordy i dopisuje alarmy do pliku jeszcze w trakcie odbioru.

To samodzielny, minimalny wariant pomiarowy oparty na metodach z rozdziału 3. Nie wymaga uruchamiania starszego `can_stream_rx.py` ani pakietów ML. Nie należy otwierać tego samego UART dwoma programami jednocześnie.

## Instalacja na RPi

Gotowe archiwum `deployment/can-live-rpi.zip` zawiera katalog `can-live-rpi` z programem, dekoderem, DBC, testami i benchmarkiem. Można rozpakować go obok dotychczasowego repozytorium.

Na komputerze Windows, PowerShell (jedna linia):

```powershell
scp 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\deployment\can-live-rpi.zip' pi@rpi-telemetry:~/
```

Na RPi:

```bash
unzip can-live-rpi.zip
cd can-live-rpi
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements-live.txt
.venv/bin/python tools/live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --session pierwsza_proba
```

Wymagany Python >=3.10 i moduł `venv`. Jeżeli nazwa `rpi-telemetry` nie jest rozwiązywana, w komendzie `scp` użyj adresu IP RPi. Do każdej nowej próby wybierz inną nazwę sesji albo pomiń `--session`, aby program nadał nazwę z daty i czasu.

Alternatywnie można skopiować nowe pliki do istniejącego `~/RTE_3_0_CAN_LOGGER_2023`, zachowując strukturę katalogów, i wykonać te same polecenia w tym repozytorium. Potrzebne są: `tools/live_detect.py`, `tools/rpi_receiver/live_rules.py`, istniejący dekoder, `tools/requirements-live.txt` i DBC. Benchmark jest opcjonalny. Domyślna ścieżka DBC jest liczona względem skryptu, nie względem bieżącego katalogu.

## Pierwszy pomiar bez przygotowanego profilu

Uruchom normalny ruch na magistrali na co najmniej 210 sekund i uruchom program na RPi przed rozpoczęciem nadawania. Dla powtarzalnych prób generatora na PC dodaj `--seed 42 --max-fps 80 --duration 210`; zapas względem 100 ramek/s zostaje wtedy dla wstrzykiwanych ramek. Wybrane ID, okresy, tryb faz, limit i ziarno muszą być takie same w sesji odniesienia i w sesji testowej.

Kontrola ID, DLC i zakresów działa od pierwszej odebranej ramki. Domyślne 60 sekund pozostaje wygodne do prób funkcjonalnych, ale do właściwego profilu uruchom program z **`--learn-seconds 180`**. Czas jest liczony według urządzenia od pierwszej poprawnej ramki analizowanego kanału. W tej fazie podawaj ruch uznany za poprawny. Następnie program zapisuje `baseline.json`, zamraża profil i uruchamia reguły czasowe. Na konsoli pojawi się `Baseline ready; timing rules active`.

Okres wyznaczany jest tylko dla ID mającego co najmniej 100 poprawnych odstępów. ID nieobserwowane w ruchu odniesienia nie są traktowane jako obowiązkowe tylko dlatego, że istnieją w DBC. Brak takiego założenia jest istotny przy generatorze nadającym wybrany podzbiór wiadomości.

Jeżeli podczas uczenia wystąpi strata rekordów lub zmiana sesji loggera, uczenie zaczyna się ponownie. W raporcie widać `baseline_state: learning` albo `ready`. Przerwanie przed zakończeniem uczenia zapisuje profil z `ready: false`; taki plik nie może służyć jako gotowy profil kolejnej sesji.

Minuta daje około 600 odstępów dla wiadomości 100 ms, więc jej średni okres jest już oszacowany bardzo dobrze. Dłuższe uczenie stabilizuje odchylenie i ogony rozkładu jitteru, a jest szczególnie potrzebne dla wiadomości 1 s. Do właściwych pomiarów przyjęto 180 s jako rozsądny kompromis. Wiadomość 10 s da w tym czasie tylko około 18 odstępów i przy domyślnym `--min-intervals 100` nie wejdzie do profilu czasowego; dla takich ID trzeba wydłużyć uczenie albo osobno uzasadnić mniejszą wartość `--min-intervals`. Szybsza próba funkcjonalna jest możliwa przez `--learn-seconds 15 --min-intervals 50`.

## Następny pomiar z profilem odniesienia

```bash
.venv/bin/python tools/live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --baseline captures_live/pierwsza_proba/baseline.json --session test_01
```

W tym trybie reguły czasowe działają od rozpoczęcia analizy, bez ponownego uczenia i bez adaptowania profilu do wstrzykiwanych anomalii. Suma DBC i numer kanału muszą być zgodne z profilem. Profil nowego programu ma format `can-live-baseline-v1`; nie jest zamienny ze starymi plikami profilu z pakietu `can_detect`.

W czasie pomiaru, w drugim terminalu RPi:

```bash
tail -f captures_live/test_01/events.jsonl
```

Pusty plik przy poprawnym ruchu jest prawidłowym wynikiem. Zakończenie: Ctrl+C. Opcjonalnie `--seconds 120` kończy odbiór po wskazanym czasie hosta.

## Zaimplementowane reguły

| Alarm / metoda | Podstawa decyzji | Parametry domyślne |
| --- | --- | --- |
| `unknown_id` / `dbc` | ID nie występuje w DBC ani w wczytanym profilu. | ID `0x7FE` pomijane jako znacznik starszego generatora. |
| `dlc_mismatch` / `dbc` | DLC nie zgadza się z długością wiadomości w DBC. | Dane za krótkie nie są dekodowane jako sygnały. |
| `signal_out_of_range` / `dbc` | Zdekodowana wartość wychodzi poza zakres DBC. | `[0|0]` i brak zakresu oznaczają brak tej kontroli. |
| `missing_frame` / `timeout` | ID z wyznaczonym okresem nie pojawia się przez zbyt długi czas. | Ponad 3 × okres; `--missing-multiplier`. |
| `period_violation` / `period` | Odstęp między ramkami odbiega od średniego okresu. | ±30%; `--period-tolerance`. |
| `frequency_increase`, `frequency_decrease` / `frequency` | Liczba ramek w pełnym oknie odbiega od wartości wynikającej z profilu okresu. | Okno 1 s, ±50%, minimum 1 ramka tolerancji granic okna. |
| `statistical_deviation` / `three_sigma` | Odstęp różni się od średniej o ponad krotność odchylenia standardowego. | 3σ; wyłączone dla zerowej wariancji. |
| `burst` / `burst` | Kolejne ramki o tym samym ID są skupione w krótkim czasie. | 4 ramki, każdy odstęp <25% okresu. |
| `controller_counter_increase`, `controller_event` / `controller` | Przyrosty liczników błędów protokołu oraz zdarzenia kontrolera. | Porównanie kolejnych odczytów modulo 2³². |

Metody są jawnie zapisane w każdym alarmie i w raporcie, także z zerową liczbą alarmów. Okres i częstotliwość są regułami progowymi; 3σ stanowi proste porównanie statystyczne. Domyślne tolerancje są punktami startowymi, a nie potwierdzonymi eksperymentalnie progami. Zerowe odchylenie wyłącza 3σ, ale nie wyłącza reguły okresu.

Wielokrotne naruszenie tego samego warunku jest scalane: alarm pojawia się przy wejściu w stan naruszenia, kolejny po ustąpieniu i ponownym pojawieniu się naruszenia. Liczniki błędów kontrolera są raportowane przy każdym dodatnim przyroście. Metody mogą zgłaszać różne alarmy dla tego samego epizodu; nie oznacza to wielu niezależnych epizodów.

Zakres nie obejmuje ML, metod analogowych, analizy zależności fizycznych między sygnałami, kontroli zakresów sygnałów wyuczonych z profilu ani pełnego liczenia precision/recall/F1. Reguła częstotliwości wykorzystuje oczekiwaną liczność wynikającą z okresu, nie osobny wyuczony rozkład liczności. To celowy zakres pierwszego działającego pomiaru, a nie deklaracja ukończenia wszystkich wcześniejszych wymagań M.

## Pliki sesji i poprawność danych

| Plik | Znaczenie |
| --- | --- |
| `trace.canbin` | Wszystkie odebrane bajty UART, również fragmenty niepoprawnych rekordów; do powtórzenia analizy. |
| `events.jsonl` | Alarmy dopisywane w trakcie działania: metoda, typ, kanał, ID, czas urządzenia, pomiar, próg, numer sekwencji albo granice okna. |
| `quality.jsonl` | Straty na łączu, straty loggera, restart, nieciągłość czasu i pozostałe problemy kompletności danych. |
| `baseline.json` | Profil wyuczony lub kopia wczytanego profilu. |
| `config.json` | Ustawienia, sumy DBC/profilu/kodu, wersje i informacje o platformie. |
| `report.json` | Aktualizowany w czasie pomiaru raport: status, liczniki, liczby alarmów per metoda, kolejka i wydajność. |

Pliki trafiają do `captures_live/<sesja>/`; istniejąca sesja nie zostanie nadpisana. Zapisanie alarmu opróżnia bufor tekstowy, ale nie jest gwarancją odporności karty SD na nagłe odcięcie zasilania. Raport jest wymieniany przez plik tymczasowy.

W trybie live `trace.canbin` jest tworzony automatycznie w katalogu sesji. Każdy odebrany fragment UART jest zapisywany przed przekazaniem go do analizy, a przy zamknięciu sesji plik jest synchronizowany. Raport zawiera `trace_recording.bytes_written` i `trace_sha256`. W trybie `--replay` nowy ślad nie jest tworzony, ponieważ analizowany jest istniejący plik.

Baza czasu detekcji pochodzi z loggera. Czas 32-bitowych ramek jest odtwarzany z synchronizacji 64-bitowej. Restart resetuje synchronizację i stan reguł. Bez synchronizacji działają kontrole ID/DLC/sygnałów, a czasowe są pomijane. Niewiarygodny lub niedostępny RTC daje `absolute_time: null`.

Przy zaniku samych ramek CAN czas nadal płynie dzięki rekordom statystyk/synchronizacji, więc można wykryć brak oczekiwanej ramki. Jeżeli zamilknie cały UART, nie wiadomo, czy ustał ruch CAN, czy transmisja loggera: program nie tworzy wtedy alarmu CAN na podstawie zegara PC. W raporcie rośnie `input_idle_s`.

Przyrosty liczników liczone są względem pierwszego odczytu otrzymanego w tej sesji, a nie względem uruchomienia loggera. Zdarzenia utraty danych nie są przedstawiane jako dowód anomalii pojazdu. Strata resetuje stan reguł czasowych, aby nie porównywać odstępu przez znaną lukę.

Statystyka urządzenia może dopiero później ujawnić stratę dotyczącą już przeanalizowanego przedziału. Alarmy mają dlatego pole `quality: check_quality_intervals`. Podczas oceny skuteczności trzeba odrzucić alarmy i epizody zachodzące na przedziały problemów. Granica `null` oznacza, że nie da się jej ustalić z otrzymanych rekordów; wymaga ostrożnego wykluczenia lub sprawdzenia śladu. Plik zdarzeń zachowuje kolejność przetwarzania, a `session_index` rozdziela restarty urządzenia.

## Sprawdzenie działania na stanowisku

1. Nadaj poprawny ruch i naucz profil; dla kolejnych prób zachowaj te same ID.
2. Uruchom nową sesję z `--baseline`. Przez chwilę nadaj ruch o okresie odniesienia.
3. Zatrzymaj generator, pozostawiając logger i UART aktywne: oczekiwany alarm `missing_frame`, a następnie spadek liczności w pełnym oknie.
4. Nadaj ten sam zestaw częściej lub rzadziej: oczekiwane odchylenia okresu i częstotliwości. Przy generatorze z losowaniem zachowaj `--seed 42 --max-messages 10`. Dla okresu 50 ms podnieś też `--max-fps 200`, aby budżet nie usunął części ID.
5. Kontrole DLC, nieznanego ID i zakresów można sprawdzić osobnymi ramkami testowymi. Obecny generator ruchu odniesienia celowo nie nadaje niezgodnych wiadomości; do liczbowej oceny potrzebny będzie kontrolowany generator epizodów wraz z prawdą podstawową.

Zmiana całego losowego zestawu ID pomiędzy odniesieniem a testem oznacza zmianę badanej sieci i naturalnie spowoduje alarmy o zniknięciu starych wiadomości.

## Odtworzenie i porównanie wyników

```bash
.venv/bin/python tools/live_detect.py --replay captures_live/test_01/trace.canbin --baseline captures_live/pierwsza_proba/baseline.json --session test_01_replay
cmp captures_live/test_01/events.jsonl captures_live/test_01_replay/events.jsonl
```

Powtórz wszystkie użyte parametry reguł, np. `--window-seconds`, `--channel`, tolerancje. Jeżeli sesja live uczyła profil od początku, replay także uruchom **bez** `--baseline`, z tymi samymi parametrami uczenia. Sam fakt posiadania gotowego profilu po sesji nie oznacza, że był on dostępny w okresie uczenia (180 sekund w zalecanym eksperymencie).

Program porównuje konfigurację, sumy kodu, DBC i profilu ze źródłowym `config.json`; różnice są wypisane w `replay_comparison` raportu i oznaczają nieporównywalność. Bez źródłowego manifestu porównywalność jest niepotwierdzona. Można analizować również stare surowe `.canbin` zapisane przez sprawdzony odbiornik. Replay nie kopiuje ponownie dużego śladu.

## Wydajność

Analiza nie gromadzi wszystkich alarmów lub ramek w RAM. Jej stan zależy od ID, liczby sygnałów i bieżącego okna. Kolejka ma domyślnie 128 fragmentów po maksymalnie 4096 B (około 512 KiB danych, plus narzut obiektów). Profil jest uczony przez statystyki przyrostowe. Liczniki błędów i raport nie przechowują nieograniczonej historii.

Gdy kolejka się przepełni lub detektor ulegnie awarii, odbiór dalej zapisuje surowy ślad. Analiza nie jest wznawiana w środku strumienia z ukrytą luką; raport oznacza `analysis_complete: false`, a do pełnej oceny trzeba wykonać replay. Błąd zapisu śladu kończy odbiór. Dwa wątki separują operacje wejścia/wyjścia, ale nie są obietnicą równoległego wykonywania kodu Pythona na dwóch rdzeniach.

Na RPi wykonaj:

```bash
.venv/bin/python tools/benchmark_live_detect.py --frames 85000 --rate 8500
```

Benchmark tworzy syntetyczny strumień dla 10 wiadomości rzeczywistego DBC, obejmuje dekodowanie, reguły, kolejkę i zapis alarmów, następnie zapisuje `benchmark.json`. Mierzy przyspieszone odtwarzanie; nie sprawdza fizycznego UART ani jednoczesnego zapisu surowego śladu na kartę SD.

W pomiarze na komputerze Windows użytym podczas implementacji 85 000 ramek przetworzono w około **3,03 s**, czyli **28 tys. ramek/s**. To nie jest wynik RPi. Przy dotychczasowych 100–200 ramkach/s nie ma przesłanek, aby odrzucać proste reguły live, ale ostatecznym sprawdzianem jest rzeczywista sesja na RPi. Docelowe 8500 rekordów/s z wcześniejszych wymagań wymaga osobnego pomiaru sprzętowego; tego progu nie oznaczono jako spełnionego.

Obserwuj w `report.json`: `analysis_queue_overflow`, `queue_peak_chunks`, `queue_chunks`, `analysis_us_per_record`, `processing_fraction_of_capture_time` i `peak_resident_memory_bytes`. Wskaźnik czasu dotyczy odcinka analizy, nie całkowitego użycia CPU. Pamięć rezydentna jest mierzona na Linux; na Windows pole może być `null`. Zapełnianie kolejki, narastający odstęp od czasu odbioru lub wysoki udział czasu analizy oznaczają potrzebę ograniczenia ruchu bądź analizy z pliku. Sam brak przepełnienia w krótkiej sesji nie dowodzi trwałego zapasu wydajności.

## Testy bez urządzenia

```bash
.venv/bin/python -B -m unittest discover -s tools/tests -p test_live_detect.py -v
```

Testy obejmują reguły, uczenie i ponowne użycie profilu, granice czasu i sekwencji, reset, straty i licznik modulo, częściowe okna, rzeczywisty punkt wejścia z zastąpionym UART, alarm zapisany przed końcem wejścia, równoważność replay, przeciążenie, awarię detektora, błąd dysku i Ctrl+C. Są to testy programu; fizyczna sesja na Raspberry Pi pozostaje oddzielną weryfikacją.
