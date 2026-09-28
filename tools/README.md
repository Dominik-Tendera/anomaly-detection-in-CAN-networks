# Moduły wspólne

Ten katalog nie jest już zbiorem wszystkich punktów wejścia.

- `can_generate/`: struktury epizodów, znaczniki czasu, starsze mechanizmy
  generacji i zapis prawdy referencyjnej; część jest używana przez aktualny generator.
- `can_detect/`: moduły dekodowania, DBC, profilu i analizy; część to starszy
  szkielet rozwoju, zachowany ze względu na zależności oraz testy.
- `repo_paths.py`: lokalizacja modułów między `generator/`,
  `raspberry_pi/` i `tools/`, bez zależności od katalogu uruchomienia.

Testy przeniesiono do głównego katalogu [`tests/`](../tests/README.md).
Testy tutejszych bibliotek są w `tests/shared/`, a bieżącego detektora
w `tests/raspberry_pi/`. W `tools/` nie przechowuje się drugiej kopii testów.

Nie myl starszego `can_detect` z bieżącym silnikiem pomiarowym
`raspberry_pi/rpi_receiver/live_rules.py`. Profile tych programów nie są zamienne.

Uruchom z katalogu głównego repozytorium:

```bash
python -m pip install -r results/requirements.txt
python -m unittest discover --start-directory tests --top-level-directory .
```

Minimalne zależności aktywnego toru: `generator/requirements.txt`
i `raspberry_pi/requirements.txt`. `tools/requirements.txt` zachowuje
szerszy, przypięty zestaw zależności z wcześniejszego planu rozwoju.
Pełne testy sprawdzają też uruchamianie wykresów, dlatego powyżej użyto
`results/requirements.txt`. Nie łącz go z historycznym zestawem
`tools/requirements.txt` w jednym środowisku (inne przypięcie NumPy).
