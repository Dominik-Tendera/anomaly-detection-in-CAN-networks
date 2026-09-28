# Testy według części systemu

Wszystkie polecenia poniżej wykonuj z katalogu głównego repozytorium.
Aktywuj środowisko Python. Do pełnego zestawu zainstaluj
`python -m pip install -r results/requirements.txt`.

| Katalog | Co sprawdza | Uruchomienie |
| --- | --- | --- |
| `can_logger/` | Kod formatu binarnego C z `can_logger/Core/Src/` | `powershell -ExecutionPolicy Bypass -File tests/can_logger/run_tests.ps1` |
| `raspberry_pi/` | Bieżący detektor, odbiornik diagnostyczny i bufor | `python -m unittest discover -s tests/raspberry_pi -t .` |
| `generator/` | Generator UCCB i scenariusze | `python -m unittest discover -s tests/generator -t .` |
| `shared/` | Moduły `tools/can_detect` i starsze elementy `tools/can_generate` | `python -m unittest discover -s tests/shared -t .` |
| `integration/` | DBC, lokalizacja plików, uruchamianie narzędzi i synchronizacja czasu | `python -m unittest discover -s tests/integration -t .` |

Cały zestaw Python: `python -m unittest discover -s tests -t .`.
Nie uruchamia on kompilacji C. Testy samego Raspberry Pi można wykonać na PC,
bez UART; wymagają pełnego repozytorium i `raspberry_pi/requirements.txt`.
Testy generatora również nie otwierają fizycznego adaptera.

Alternatywnie użyj `tests/run_tests.ps1 -Suite raspberry_pi` albo
`sh tests/run_tests.sh raspberry_pi`. Bez wyboru zestawu uruchamiane są wszystkie
testy Python. Dostępne grupy: `raspberry_pi`, `generator`, `shared`, `integration`.

## Testy firmware i wspólnego formatu

Testy C wymagają GCC. Na Linuxie uruchom `sh tests/can_logger/run_tests.sh`.
Skrypt kompiluje oryginalny plik firmware (bez kopiowania jego implementacji),
sprawdza własności kodera, a następnie generuje wektory dla
`tests/integration/test_vectors.py` i sprawdza odtwarzanie w odbiorniku Python.
`tests/integration/check_replay.py` weryfikuje raport tej próby.
Te dwa sprawdzenia integracyjne wymagają najpierw wygenerowania wektorów przez C;
nie są wykonywane przez samo wyszukiwanie testów `unittest`.

Wektory celowo zawierają niepoprawne bajty i liczniki strat. Oczekiwany wynik
odtwarzania to zgłoszenie strat, a nie `lossless=True`. Skrypt sprawdza konkretne
liczniki i nie ignoruje dowolnych błędów odbiornika.
Pliki wynikowe pozostają w `tests/can_logger/` i są pomijane przez Git.

Testy C obejmują format strumienia, nie całe firmware, konfigurację peryferiów
ani działanie sprzętu. Testy Python nie dowodzą wydajności fizycznego UART/CAN.
`synthetic.py` dostarcza wspólne sztuczne ślady; nie należy kopiować go do każdej grupy.
