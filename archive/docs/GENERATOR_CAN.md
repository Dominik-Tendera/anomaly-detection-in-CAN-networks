# Generator ruchu CAN przez adapter uCCB

Uruchamiany program: **`generate_can_traffic.py`**. Starszy `generate_traffic.py` pochodzi z poprzedniej implementacji; poniższe instrukcje dotyczą nowego programu.

Generator czyta DBC i nadaje standardowe ramki CAN 11-bit przez port USB adaptera uCCB. Inicjalizacja pochodzi ze sposobu użycia protokołu w `uCCBViewer-p2.6/python/uccbviewer/usbtin.py`: zamknięcie kanału, odczyt wersji, ustawienie bitrate i otwarcie kanału aktywnego. Viewer nie musi być uruchomiony. Musi zwolnić port COM, którego używa skrypt.

## Pierwsze uruchomienie na tym komputerze

Przygotowano lokalne środowisko `K:\Praca Magisterska\.venv-can` z `cantools` i `pyserial`.

W PowerShell najpewniejsze są polecenia jednowierszowe. Skopiuj całą linię razem ze znakiem `&`:

```powershell
# Sprawdź dostępne porty.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --list-ports

# Sprawdź generator bez podłączania adaptera.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --dry-run --duration 5

# Rozpocznij rzeczywiste nadawanie; zastąp COM4 właściwym portem.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --port COM4 --bitrate 1000000 --duration 60
```

Na obecnym komputerze `--list-ports` pokazuje adapter USB jako `COM4`. Nie trzeba aktywować środowiska `venv` ani zmieniać katalogu. Jeżeli PowerShell wyświetla znak zachęty `>>`, polecenie zostało wklejone jako niedomknięte — naciśnij Ctrl+C i użyj jednowierszowej wersji powyżej.

Bitrate musi odpowiadać loggerowi i magistrali. Stanowisko wymaga prawidłowego połączenia CANH/CANL, odniesienia masy, terminacji i aktywnego odbiornika potwierdzającego ramki CAN. To generator do syntetycznych pomiarów na stole: automatyczny zestaw może obejmować również wiadomości poleceń z DBC.

Ctrl+C kończy nadawanie, zamyka kanał i zapisuje częściowy raport. Awaria lub brak odpowiedzi adaptera zatrzymuje sesję; program nie ponawia automatycznie komend TX, których skutek jest nieznany.

## Co jest generowane

- Domyślny plik: `CAN_PARSER/RTE_3.5_CAN1_CAR.dbc`, rozwiązywany względem skryptu, niezależnie od bieżącego katalogu.
- Dobór ID jest losowy: przed wyborem tasowana jest lista wiadomości. Program wybiera maksymalnie 20 wiadomości, ale tylko tyle, ile mieści się w limitach. Domyślnie każda sesja ma nowe ziarno. Użyte ziarno jest wypisywane i zapisywane w `config.json`.
- Parametr `--seed 42` odtwarza ten sam wybór przy tych samych pozostałych ustawieniach. Do sesji odniesienia i testowej użyj tego samego ziarna oraz limitu `--max-messages 10`, albo jawnej listy `--ids`, żeby nie zmieniać przypadkowo badanej sieci.
- Domyślny budżet: 100 ramek/s, do 40% szeregowego łącza hosta 115200 baud oraz do 30% oszacowanego obciążenia CAN. Liczba wybranych wiadomości zależy teraz od ich rzeczywistych okresów. Zajętość łącza zależy od DLC i okresów wylosowanych wiadomości i jest wypisywana przed startem. Budżety są obliczeniami, a nie zmierzoną wydajnością adaptera.
- Okres bierze się ze standardowego atrybutu DBC `GenMsgCycleTime` albo z użytego w tym projekcie atrybutu `Period`. Obecny DBC zawiera m.in. okresy 4, 5, 10, 30, 100, 200, 1000 i 10000 ms. Dopiero gdy wiadomość nie ma żadnego z tych atrybutów, używane jest `--default-period-ms 100`. `--period-ms` świadomie nadpisuje okres wszystkich wiadomości.
- Domyślny `--phase-mode random` losuje pierwszą fazę każdej wiadomości przy użyciu zapisanego ziarna. Usuwa to sztuczne, równe odstępy 10 ms widoczne w poprzedniej sesji, a jednocześnie pozwala dokładnie powtórzyć eksperyment. Dostępne są też tryby `staggered` i `zero`.
- Spóźnione terminy są pomijane i raportowane, bez nadrabiania całej zaległości serią ramek.
- Zwykłe sygnały z określonym zakresem zmieniają się sinusoidalnie w środkowych 80% zakresu, po kwantyzacji do wartości możliwych do zakodowania. Okres zmian wynosi 10 s.
- Sygnały wyliczeniowe pozostają na pierwszej dopuszczalnej wartości. Sygnały bez określonego zakresu, w tym zapisane jako `[0|0]`, otrzymują surowe zero, jeśli jest dopuszczalne. Multiplekser wybiera jedną dopuszczalną gałąź. `--signals constant` ustawia stałe wartości sygnałów.
- Pakowanie wykonuje `cantools`, z uwzględnieniem kolejności bitów, znaku, skali i offsetu. Ramki extended, CAN FD i sygnały float są poza zakresem tego generatora; pominięcia automatycznego doboru są zapisane w konfiguracji.

To ruch zgodny ze strukturą DBC i obsługiwanymi zakresami sygnałów, a nie symulator zachowania całego pojazdu. Program nie odtwarza zależności fizycznych pomiędzy sygnałami ani aplikacyjnych liczników i checksum. `generate_can_traffic.py` generuje ruch odniesienia, a `generate_anomaly_traffic.py` używa tego samego planu do kontrolowanych testów anomalii.

## Scenariusze anomalii

Najpierw wykonaj krótki test bez sprzętu:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_anomaly_traffic.py' --dry-run --duration 120 --seed 42 --max-fps 80 --session proba_anomalii
```

Wbudowany pakiet 120 s zawiera łagodne i silne zwiększenie częstotliwości, zmniejszenie częstotliwości, zanik wiadomości, dwa rodzaje burst, nieznane ID, błędne DLC i sygnał poza zakresem DBC. Test sprzętowy:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_anomaly_traffic.py' --port COM4 --duration 120 --seed 42 --max-fps 80 --session anomalie_smoke_01
```

Rozszerzona macierz ma 17 epizodów o kilku intensywnościach i wymaga 155 s:

```powershell
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_anomaly_traffic.py' --port COM4 --duration 155 --seed 42 --max-fps 80 --scenario 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\scenarios\intensity_matrix.example.json' --session anomalie_macierz_01
```

W pliku scenariusza `can_id: null` oznacza automatyczny wybór odpowiedniej wiadomości z planu. Można wpisać konkretne ID. Obsługiwane parametry to `frequency_factor`, `count`, `interval_s`, jawne `dlc`, wariant DLC `shorter`/`longer` oraz dla zakresu `direction` (`above`/`below`), `raw_steps` i opcjonalne `signal_name`.

Każdy przebieg zapisuje `truth.json` z zaplanowanymi i faktycznie potwierdzonymi ramkami każdego epizodu. Ramki znacznika 0x7FE pozwalają powiązać czas generatora z czasem urządzenia; detektor domyślnie ignoruje to ID. Kod zakończenia 2 oznacza pominięte terminy. Do końcowych statystyk preferuj sesje bez pominięć; w pozostałych sprawdź rzeczywistą intensywność w `truth.json`.

## Wybór wiadomości i intensywności

```powershell
# Lista dostępnych ID, nazw i DLC.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --list-messages

# Dwie konkretne wiadomości co 100 ms.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --port COM4 --ids 0x200 0x201 --period-ms 100

# Więcej wiadomości: zwiększony budżet, nadal sprawdzany limit łącza.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --port COM4 --max-messages 20 --max-fps 200

# Próba automatycznego wyboru do 124 wiadomości, ale rzadsze nadawanie.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --dry-run --max-messages 124 --max-fps 200 --period-ms 1000

# Stałe wartości sygnałów i inny DBC.
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\tools\generate_can_traffic.py' --port COM4 --dbc 'K:\dane\siec.dbc' --signals constant
```

Przy jawnym `--ids` program nie usuwa po cichu ID przekraczających budżet: zgłasza błąd przed otwarciem portu. Zwiększ wtedy okres lub ogranicz zestaw ID. Wszystkie jawne ID muszą być obecne w DBC. Notacja `0x201` oznacza ID szesnastkowe, `513` — dziesiętne.

Rozpocznij od ustawień domyślnych. Zwiększaj intensywność po sprawdzeniu raportu i śladu loggera. Brak pominiętych terminów po stronie PC nie jest jeszcze dowodem braku strat na magistrali lub w loggerze.

## Pliki wynikowe

Każda sesja tworzy nowy katalog `captures/<data_czas>/`. Można podać `--output` i `--session`; istniejący katalog sesji nie zostanie nadpisany.

| Plik | Zawartość |
| --- | --- |
| `config.json` | Parametry, suma SHA-256 DBC, wybrane wiadomości, źródła okresów, fazy, budżety i przyczyny pominięcia pozostałych ID. |
| `tx.jsonl` | Zapis kolejnych komend TX potwierdzonych przez adapter: ID, DLC, dane, czas planowany, początek komendy i czas odpowiedzi. Zapis na bieżąco. |
| `report.json` | Status, liczby komend i pominiętych terminów, osiągnięta częstość potwierdzeń, liczniki per ID i błąd, jeśli wystąpił. Aktualizacja w trakcie sesji i przy zakończeniu. |

Znaczniki czasu są względnym czasem monotonicznym PC. Odpowiedź SLCAN potwierdza obsługę komendy przez adapter; **nie jest pomiarem momentu transmisji na CAN ani potwierdzeniem odbioru przez logger**. Do wyników detekcji potrzebny jest ślad z loggera. Generator anomalii dodatkowo zapisuje `truth.json` i wysyła znaczniki początku/końca do wyrównania osi czasu z RPi.

W trybie `--dry-run` pliki mają `mode: dry_run`, a potwierdzenia i upływ czasu są symulowane. Ten tryb testuje kodowanie, harmonogram i zapis plików; nie testuje USB, CAN, sterownika ani opóźnień Windows.

Kody zakończenia: 0 — pełny przebieg; 2 — przebieg z pominiętymi terminami; 1 — błąd; 130 — przerwanie Ctrl+C. Szczegół znajduje się w `status` raportu.

## Instalacja na innym komputerze

```powershell
python -m venv .venv
& .venv/Scripts/python.exe -m pip install -r tools/requirements-generator.txt
& .venv/Scripts/python.exe tools/generate_can_traffic.py --dry-run --duration 5
```

Wymagany Python 3.10 lub nowszy. Skrypt oraz `uccb_generator.py` należy trzymać w tym samym katalogu.

## Weryfikacja

```powershell
Set-Location 'K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023'
& 'K:\Praca Magisterska\.venv-can\Scripts\python.exe' -B -m unittest discover -s tools/tests -p test_uccb_generator.py -v
```

Testy obejmują kodowanie i dekodowanie wszystkich 124 wiadomości lokalnego DBC, zakresy i zmiany sygnałów, budżet nadawania, harmonogram, przeciążenie, inicjalizację SLCAN, odpowiedzi TX przeplatane RX, BELL, timeout, częściowe wyniki po błędzie i Ctrl+C oraz ochronę przed nadpisaniem sesji. Testy nie wymagają adaptera. Próbę sprzętową trzeba wykonać na stanowisku.
