# Generator ruchu CAN — komputer

- `generate_can_traffic.py`: ruch referencyjny zgodny z DBC.
- `generate_anomaly_traffic.py`: kontrolowane epizody anomalii na tle tego ruchu.
- `uccb_generator.py`, `anomaly_scenarios.py`: silnik i konstrukcja wymuszeń.
- `scenarios/`: przykładowe plany, w tym 17 wariantów w `intensity_matrix.example.json`.
- `dbc/RTE_3.5_CAN1_CAR.dbc`: kopia konfiguracji używanej w badaniach.
- `captures/`: pliki wynikowe PC; nie mieszać ich z pomiarami odbiornika.

Polecenia poniżej zakładają katalog główny repozytorium:

```bash
python -m pip install -r generator/requirements.txt
python generator/generate_can_traffic.py --list-messages
python generator/generate_can_traffic.py --dry-run --duration 2 --seed 42 --max-fps 80 --session sprawdzenie
```

Przykład ruchu referencyjnego i późniejszej sesji anomalii:

```bash
python generator/generate_can_traffic.py --port COM4 --duration 210 --seed 42 --max-fps 80 --session odniesienie_42
python generator/generate_anomaly_traffic.py --port COM4 --duration 155 --seed 42 --max-fps 80 --scenario generator/scenarios/intensity_matrix.example.json --session anomalie_42
```

`COM4` jest przykładem — wybierz rzeczywisty port adaptera. Domyślna
przepływność CAN wynosi 1 Mbit/s, a portu adaptera 115200 baud.
Test bez sprzętu wymaga `--dry-run`; nie otwiera wtedy portu.
Program potrzebuje również współdzielonych bibliotek z `tools/`.
Przenoś więc cały klon repozytorium, nie tylko pojedynczy skrypt.

## Parametry i ograniczenia

- Okresy pochodzą z atrybutu `GenMsgCycleTime` lub `Period` w DBC.
  Przy ich braku używany jest `--default-period-ms` (100 ms).
  `--period-ms` jawnie nadpisuje okresy.
- Ziarno `--seed` ustala wybór wiadomości i ich fazy. Zachowaj je wraz
  z `--ids` lub `--max-messages`, trybem faz i limitami między próbami.
- Domyślne limity to 100 ramek/s oraz 40% oszacowanego łącza hosta;
  w kampanii stosowano m.in. `--max-fps 80`. To budżety, nie pomiar wydajności.
- Spóźnione transmisje są pomijane, bez nadrabiania ich gwałtowną serią.
- Sygnały o określonym zakresie zmieniają się w środkowych 80% zakresu.
  `--signals constant` wybiera wartości stałe. Kodowanie uwzględnia skalę,
  offset, znak i kolejność bitów z DBC.
- Generator nie symuluje dynamiki całego pojazdu ani wszystkich zależności
  aplikacyjnych. Nie obejmuje CAN FD i ramek extended.
- W scenariuszu `can_id: null` oznacza automatyczny dobór wiadomości.
  Można podać konkretne ID, `frequency_factor`, `count`, `interval_s`,
  `variant` (`shorter`/`longer` dla DLC), kierunek przekroczenia zakresu oraz `raw_steps`.
  Wiążące nazwy pól pokazują pliki JSON i `--help`.

## Pliki sesji

`config.json` zachowuje parametry i konfigurację, `tx.jsonl` przebieg
transmisji, `report.json` podsumowanie, a `truth.json` (dla anomalii)
plan i realizację epizodów. Potwierdzenie adaptera nie określa dokładnego
czasu odbioru na magistrali. Znaczniki CAN o ID 0x7FE służą do powiązania
osi czasu generatora i odbiornika; detektor je pomija.

Domyślny katalog to folder `captures/` obok skryptu.
`--output` wskazuje inne miejsce, a `--session` nazwę nowej sesji.
Kod zakończenia 2 oznacza pominięte terminy — sprawdź raport.
