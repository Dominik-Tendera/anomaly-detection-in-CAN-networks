# Generowanie i prezentacja wyników

Skrypty czytają pomiary z `generator/captures/` i `raspberry_pi/captures/`.
Wyniki pochodne trafiają do `results/reports/campaign_2026-09-20/`.
Nie zmieniają źródłowych pomiarów.

Uruchom z katalogu głównego repozytorium, w środowisku z zależnościami:

```bash
python -m pip install -r results/requirements.txt
python -X utf8 results/analyze_measurement_campaign.py
python -X utf8 results/render_results_report.py
python -X utf8 results/render_detection_heatmap.py
python -X utf8 results/render_timing_results.py
```

| Skrypt | Zadanie |
| --- | --- |
| `analyze_measurement_campaign.py` | Dopasowanie sesji i znaczników czasu, kontrola kompletności i sum kontrolnych, CSV oraz `analysis.json`. |
| `campaign_trace_audit.py` | Funkcje odczytu śladów, dopasowania ramek i odtworzenia alarmów; moduł pomocniczy. |
| `render_results_report.py` | Raport opisowy, zestawienie metod, materiał LaTeX i poglądowe wykresy diagnostyczne. |
| `render_detection_heatmap.py` | Macierz reakcji reguł dla 19 sesji i 323 epizodów, PNG/PDF oraz dane CSV i metadane. |
| `render_timing_results.py` | Rzeczywiste odstępy i progi, A04 oraz alarmy poza wymuszeniami, PNG/PDF/CSV i metadane. |

Opcja `-X utf8` zapewnia poprawny wypis polskich znaków i symbolu σ również
w terminalu Windows. Kolejność ma znaczenie: renderery używają tabel wytworzonych wcześniej.
`render_timing_results.py` korzysta również z `method_summary.csv`
utworzonego przez `render_results_report.py`.
Opcje `--output`, `--report-dir` i `--output-dir` (zależnie od skryptu)
pozwalają zapisać oddzielny wariant; szczegóły podaje `--help` tam, gdzie
jest dostępne.

Raporty są specyficzne dla kampanii opisanej w pracy; dodanie innego
eksperymentu wymaga świadomego dostosowania doboru sesji.
Brak nowego alarmu nie jest utożsamiany z brakiem spełnienia warunku reguły.

Dotychczasowy komplet raportów i figur zachowano w
`archive/reports/campaign_2026-09-20_before_reorganization/`.
Stare dokumenty i metadane w archiwum mogą zawierać historyczne ścieżki.
W katalogu aktywnym należy korzystać z raportów odtworzonych po reorganizacji.
