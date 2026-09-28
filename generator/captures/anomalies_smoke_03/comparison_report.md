# Porównanie baseline_dbc_seed42 i anomalies_smoke_03

Data analizy: 2026-09-20.

## Zakres i ograniczenia

Porównano raporty generatora oraz pliki `truth.json` i `tx.jsonl` z raportami, zdarzeniami i
przedziałami jakości detektora na Raspberry Pi. Lokalne kopie wyników RPi nie zawierają plików
`trace.canbin`, mimo że raporty potwierdzają ich zapis na Raspberry Pi. Możliwe jest więc dokładne
porównanie sum oraz zdarzeń, ale nie identyfikacja każdej odebranej ramki względem każdej pozycji
`tx.jsonl`.

Oś czasu smoke została odtworzona z alarmu dla pierwszej ramki E07, której zaplanowany czas wynosił
dokładnie 79 s. Zgodność potwierdzają początki E08 i E09 z dokładnością kilku milisekund. Do
ostatecznego automatycznego wyliczenia metryk należy skopiować `trace.canbin` i użyć ramek markerów
start/end.

## Zgodność konfiguracji

- Generator baseline, `truth.json` smoke i nauczony profil mają ten sam SHA-256 DBC:
  `8247ff6cef6e49ecc2c3680a4de5ef626a0efdc049910536efd62ccf8294c46a`.
- Oba przebiegi generatora użyły ziarna 42 i tego samego zestawu 17 wiadomości.
- Profil jest gotowy (`ready: true`) i zawiera 13 okresowych ID. Cztery ID o okresie 10 s nie
  osiągnęły wymaganych 100 odstępów i prawidłowo pozostały bez profilu czasowego.
- Marker `0x7FE` jest ignorowany przez detektor, a anomalia nieznanego ID używa `0x7FD`.

## Porównanie transmisji i odbioru

| Sesja | Generator: potwierdzone | RPi: odebrane ramki | Różnica | Udział odebranych | Jakość strumienia |
|---|---:|---:|---:|---:|---|
| baseline_dbc_seed42 | 15 723 | 15 636 | -87 | 99,45% | 4 CRC, 1 COBS, 3 brakujące rekordy, 6 wpisów jakości |
| anomalies_smoke_03 | 9 751 | 9 751 | 0 | 100,00% sumarycznie | 2 CRC, 1 brakujący rekord, 2 wpisy jakości |

W baseline generator został zatrzymany po 198,03 s mimo zadanych 210 s, a detektor po 199,16 s.
Różnica 87 ramek stanowi 0,55% potwierdzonych transmisji. Nie można przypisać jej bezpośrednio
błędom CRC/COBS: potwierdzenie SLCAN oznacza przyjęcie polecenia przez adapter, nie pomiar ramki na
przewodzie, a granice czasu generatora i odbiornika nie są identyczne. Profil 180 s mimo tego powstał
poprawnie i nie zgłosił żadnych anomalii.

W smoke generator zgłosił jeden pominięty termin, ale nie należał on do żadnego epizodu anomalii;
wszystkie anomalie zostały wysłane w zaplanowanej liczbie. Dokładna równość 9751 potwierdzonych
transmisji i 9751 ramek RPi jest bardzo dobrym wynikiem agregatowym. Nie dowodzi jeszcze zgodności
ramka po ramce, ponieważ uszkodzone rekordy mogły dotyczyć rekordów diagnostycznych albo zostać
liczbowo skompensowane przez inną ramkę.

Oba problemy jakości smoke wystąpiły około 121,204 s od początku generatora, czyli już po markerze
końcowym w 120 s. Nie nakładają się na żaden z dziewięciu epizodów i nie obniżają ich wiarygodności.

## Zaplanowane, wysłane i wykryte anomalie

| Epizod | Plan | Potwierdzone przez adapter | Wykrycie w przedziale epizodu | Ocena |
|---|---|---:|---|---|
| E01 | wzrost częstotliwości 1,5×, ID 13, 120 ramek | 120/120 | 40 `period_violation` z uwzględnieniem alarmu 2 ms przed nominalną granicą; brak `frequency_increase` | epizod wykryty przez okres; metoda częstotliwości nie przekroczyła progu |
| E02 | wzrost częstotliwości 4×, ID 13, 320 ramek | 320/320 | 1 `frequency_increase`, 2 `burst`; alarm okresu/statystyki na granicy wejścia | wykryty, dodatkowa reakcja burst |
| E03 | spadek częstotliwości 0,5×, ID 13, 40 ramek | 40/40 | 1 `frequency_decrease`, 1 `period_violation`, 1 `statistical_deviation` | wykryty prawidłowo |
| E04 | zanik ID 13 przez 8 s | 0 ramek zgodnie z planem | 1 `missing_frame`, 1 `frequency_decrease`; alarm okresu/statystyki przy powrocie | wykryty prawidłowo |
| E05 | burst ID 26, 8 ramek co 10 ms | 8/8 | 1 `burst`, 1 `frequency_increase`; alarm okresu/statystyki na granicy | wykryty prawidłowo |
| E06 | burst ID 26, 30 ramek co 10 ms | 30/30 | 1 `burst`, 1 `frequency_increase`; alarm okresu/statystyki na granicy | wykryty prawidłowo; problem wcześniejszego burstu 5 ms usunięty |
| E07 | nieznane ID `0x7FD`, 8 ramek | 8/8 | 1 `unknown_id` | wykryty prawidłowo; jedno zdarzenie reprezentuje cały stan anomalii |
| E08 | błędne DLC ID 13, 8 ramek | 8/8 | 4 `dlc_mismatch`, 1 `frequency_increase`; alarm okresu/statystyki na granicy | epizod wykryty; liczba alarmów nie jest liczbą ramek |
| E09 | sygnał poza zakresem ID 6, 8 ramek | 8/8 | 1 `signal_out_of_range` | wykryty prawidłowo; jedno zdarzenie reprezentuje cały stan anomalii |

Każdy z 9 epizodów został wykryty przez co najmniej jedną adekwatną regułę: pokrycie epizodowe wynosi
9/9. Jeśli wymagać alarmu dokładnie od metody podstawowej przypisanej do typu, wynik wynosi 8/9,
ponieważ E01 został wykryty przez regułę okresu, ale nie przez regułę częstotliwości.

E01 leży dokładnie na granicy konfiguracji. Wzrost 1,5× daje około 15 ramek w oknie zamiast 10,
a tolerancja częstotliwości wynosi 50%. Warunek alarmu jest ostry (`count > expected + margin`), więc
wartość równa granicy nie wywołuje `frequency_increase`. Nie jest to utrata danych ani awaria
detektora, lecz skutek przyjętej definicji progu. Epizod bardzo wyraźnie wykryła reguła odstępu.

## Alarmy poza epizodami

Przed markerem startu i po markerze końca zapisano 31 alarmów `missing_frame` oraz 33 alarmy
`frequency_decrease`. Wynikają one z pracy detektora przy braku ruchu przed uruchomieniem i po
zatrzymaniu generatora. Nie są fałszywymi alarmami podczas scenariusza i należy odciąć je markerami
start/end przed obliczaniem precision.

W aktywnym przedziale 0–120 s wystąpiło osiem alarmów `statistical_deviation` niezwiązanych z
wstrzykniętym epizodem, dla ID 1324, 1326 i 2026. Są to wiadomości o okresie około 1 s, dla których
profil ma małe odchylenie standardowe. Pojedynczy większy jitter przekracza próg 3 sigma. Dla obecnych
ustawień należy je traktować jako fałszywe alarmy metody statystycznej. Pozostałe alarmy okresu na
ID 13 i 26 dają się przypisać początkom, końcom albo skutkom zaplanowanych epizodów.

Niektóre metody reagują krzyżowo: silny wzrost częstotliwości E02 spełnił też warunek burst, a bursty
E05/E06 oraz dodatkowe ramki złego DLC E08 zwiększyły liczbę ramek w oknie częstotliwości. Takie
alarmy są technicznie zgodne z obserwowanym ruchem, ale przy metrykach per typ powinny być raportowane
osobno jako reakcje metod dodatkowych.

## Wniosek

Smoke 03 nadaje się jako poprawny test funkcjonalny wszystkich dziewięciu przypadków. Wszystkie
zaplanowane ramki anomalii zostały potwierdzone, gęsty burst 10 ms został wykonany w całości, a żaden
problem jakości nie nałożył się na epizody. Przed pomiarami macierzy trzeba jeszcze przyjąć dwie
zasady analizy: zakres oceniany jest wyłącznie pomiędzy markerami, a skuteczność liczona jest osobno
dla epizodów i dla metod. Dla łagodnego wzrostu 1,5× należy świadomie pozostawić obecną granicę jako
test wykrywalności przez metodę okresu albo zmienić wariant na wartość większą niż 1,5×, jeśli ma
sprawdzać wyłącznie detektor częstotliwości.
