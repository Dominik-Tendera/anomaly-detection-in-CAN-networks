# Archiwum

Materiały pozostawione dla odtwarzalności badań i możliwości odzyskania starszych
rozwiązań. Nie są domyślnymi punktami wejścia. Ich historycznych ścieżek,
konfiguracji sesji i sum kontrolnych nie poprawiano.

| Dawna lokalizacja | Nowa lokalizacja / przeznaczenie |
| --- | --- |
| `tools/generate_can_traffic.py`, `generate_anomaly_traffic.py` | Aktywne: `generator/`. |
| `tools/live_detect.py`, `benchmark_live_detect.py`, `rpi_receiver/` | Aktywne: `raspberry_pi/`. |
| Skrypty analizy i rysowania w `tools/` | Aktywne: `results/`. |
| `captures/` — katalogi sesji | `generator/captures/`. |
| `captures/` — płaskie logi i ślady z wcześniejszych prób | `archive/captures_pc_legacy/`. |
| `deployment/can-live-rpi/captures_live/` | `raspberry_pi/captures/`. |
| `deployment/` — kod i ZIP | `archive/deployment/`: historyczny kod użyty w pomiarach; potrzebny do kontroli sum. |
| `CAN_PARSER/` | `archive/CAN_PARSER/`: parser tekstowych logów, EXE, konfiguracja i oryginalne DBC. |
| `tools/generate_traffic.py` | `archive/legacy_tools/`: starszy generator, zastąpiony aktualnymi skryptami. |
| `generator_checks/`, `live_checks/` | `archive/checks/`: wcześniejsze testy i benchmarki. |
| Dotychczasowy README, changelog, instrukcje i plan eksperymentu | `archive/docs/`; aktualna instrukcja zaczyna się w głównym README. |
| Dotychczasowe raporty kampanii | `archive/reports/campaign_2026-09-20_before_reorganization/`. |

Nie usuwać `archive/deployment/can-live-rpi/tools/` bez zmiany sposobu audytu:
sumy kontrolne w historycznych konfiguracjach odnoszą się właśnie do tej
wersji programu. Nie oznacza to, że należy uruchamiać stare wdrożenie do
nowych pomiarów.

Usuwane są tylko odtwarzalne produkty kompilacji (`Debug/`, `Release/`)
i cache Pythona. Nie usuwano pomiarów ani źródeł firmware. Nowa kompilacja
STM32CubeIDE odtworzy produkty kompilacji.

## Kontrola reorganizacji (2026-09-28)

- Sumy SHA-256 potwierdziły niezmienność 637 chronionych plików: pomiarów,
  historycznego wdrożenia, źródeł firmware, DBC i zarchiwizowanych raportów.
- Odtworzono analizę kampanii i wszystkie trzy skrypty raportów/wykresów.
  Wynik pozostał zgodny: 311/323 epizodów z nowym alarmem, 256/323 dla reguły
  podstawowej; kontrola kampanii nie zgłosiła błędów.
- Dziewięć tabel CSV z wynikami i walidacją pozostało identycznych z wersją
  sprzed reorganizacji. W pozostałych zestawieniach zaktualizowano ścieżki.
- Testy Python: przed zmianą 241/242, po zmianie 246/247. Jedyna nieudana
  próba to ten sam, wcześniej istniejący test starszego odbiornika opisany
  w głównym README. Pięć nowych testów ścieżek i uruchomienia przechodzi.
- Testy kodera C i zgodności C/Python zakończyły się bez błędów.
- Usunięto 592 odtwarzalne pliki kompilacji i cache (około 61,86 MiB).
  Nie wykonywano ponownej kompilacji całego firmware STM32 ani prób sprzętowych.
