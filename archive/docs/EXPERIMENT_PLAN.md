# Plan pomiarów detekcji anomalii CAN

## 1. Jeden plik DBC po obu stronach

Poprzedni pomiar używał dwóch różnych plików: generator miał SHA-256 `8247ff6c...`, a RPi `4683...`. Taki przebieg potwierdza transport i brak fałszywych alarmów, ale nie może być końcowym eksperymentem DBC.

Skopiuj na RPi plik dołączony do aktualnego pakietu, a następnie porównaj pełne sumy:

```powershell
Get-FileHash -Algorithm SHA256 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\CAN_PARSER\RTE_3.5_CAN1_CAR.dbc'
```

```bash
sha256sum ~/can-live-rpi/CAN_PARSER/RTE_3.5_CAN1_CAR.dbc
```

Oczekiwana suma bieżącego pliku to `8247ff6cef6e49ecc2c3680a4de5ef626a0efdc049910536efd62ccf8294c46a`.

## 2. Jednorazowy profil odniesienia

Minuta jest wystarczająca dla wiadomości 100 ms: daje około 600 odstępów na ID. Przy jitterze rzędu 5–7 ms błąd standardowy średniej jest wtedy w przybliżeniu 0,2–0,3 ms, a względna niepewność oszacowania odchylenia standardowego około 3%. Po 180 s spada ona do około 1,7%. Zysk z dalszego wydłużania szybko maleje.

Do pracy przyjmij **180 s uczenia** i 210 s ruchu, żeby zostawić zapas na uruchomienie. Profil ucz tylko raz dla jednej stałej konfiguracji, a potem wczytuj go w każdej sesji testowej. Nie ucz na przebiegu zawierającym anomalie.

Najpierw na RPi:

```bash
cd ~/can-live-rpi
.venv/bin/python tools/live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --learn-seconds 180 --min-intervals 100 --session baseline_dbc_seed42
```

Następnie na Windows:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --port COM4 --duration 210 --seed 42 --max-fps 80 --session baseline_dbc_seed42
```

Poczekaj na komunikat `Baseline ready`, zakończenie generatora i dopiero wtedy zatrzymaj RPi przez Ctrl+C. W `report.json` muszą być `baseline_state: ready`, brak przepełnienia i zgodna suma DBC.

Okres 10 s daje tylko około 18 odstępów w 180 s, więc takie ID nie spełni `--min-intervals 100`. Nie obniżaj globalnie tego progu tylko po to, by włączyć bardzo wolne ID. Do głównego porównania metod wybierz wiadomości o okresach do 1 s; bardzo wolne wiadomości można zbadać osobno dłuższą sesją.

## 3. Test wszystkich reguł

Przed każdym generatorem uruchom detektor z zamrożonym profilem:

```bash
cd ~/can-live-rpi
.venv/bin/python tools/live_detect.py --port /dev/ttyAMA0 --baud 2000000 --channel 1 --baseline captures_live/baseline_dbc_seed42/baseline.json --session anomalies_smoke_01
```

Na Windows uruchom ten sam zestaw ID i faz przez to samo ziarno:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_anomaly_traffic.py' --port COM4 --duration 120 --seed 42 --max-fps 80 --session anomalies_smoke_01
```

Wbudowany przebieg jest testem integracyjnym. Dla pomiaru czułości użyj macierzy intensywności:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_anomaly_traffic.py' --port COM4 --duration 155 --seed 42 --max-fps 80 --scenario 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\scenarios\intensity_matrix.example.json' --session anomalies_matrix_01
```

Macierz obejmuje:

| Rodzina | Warianty |
| --- | --- |
| zwiększenie częstotliwości | 1,25×, 2×, 4× |
| zmniejszenie częstotliwości | 0,8×, 0,5×, 0,25× |
| zanik wiadomości | krótki 1 s i długi 5 s |
| burst | 4 ramki co 20 ms i 20 ramek co 10 ms |
| nieznane ID | pojedyncza i powtarzana ramka |
| błędne DLC | krótsza i dłuższa ramka |
| sygnał poza zakresem | tuż powyżej, tuż poniżej i dalej od granicy |

## 4. Powtórzenia i kryteria przyjęcia

Wykonaj co najmniej 5 powtórzeń macierzy. Zmieniaj nazwę sesji, ale zachowaj ten sam DBC, ziarno, profil i parametry detektora. Potem powtórz serię dla 2–3 innych ziaren, aby sprawdzić wpływ innego zestawu wiadomości i faz.

Dla każdej sesji zachowaj razem:

- `config.json`, `tx.jsonl`, `truth.json` i `report.json` generatora;
- `baseline.json`, `events.jsonl`, `trace.canbin`, `quality.jsonl`, `config.json` i `report.json` RPi;
- pełną sumę SHA-256 DBC oraz ziarno.

Sesja nadaje się do końcowej analizy, gdy DBC i konfiguracja są zgodne, generator nie zgubił istotnych ramek epizodu, RPi nie zgłosiło przepełnienia ani utraty rekordów, a analiza zakończyła się poprawnie. `truth.json` rozróżnia liczbę ramek zaplanowanych, potwierdzonych i pominiętych. Jest to podstawa do liczenia wykrywalności i czasu detekcji, zamiast opierania się wyłącznie na planowanym scenariuszu.

Detekcja regułowa na żywo nie jest zbyt wymagająca dla Raspberry Pi. Dotychczasowy benchmark miał duży zapas względem 100 ramek/s; koszt zapisu UART i plików jest większym ryzykiem niż same porównania ID, DLC, okresów i zakresów. Każdy nowy poziom obciążenia potwierdzaj przez brak `queue_overflow`, brak utraty rekordów i benchmark wykonany na docelowym RPi.

wygenerowane Seeds:
829730
550994
222720
805238