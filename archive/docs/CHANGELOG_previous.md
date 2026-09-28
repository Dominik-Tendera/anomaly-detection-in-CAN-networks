# Dziennik prac

Zapis ustaleń inżynierskich powstających przy budowie loggera magistrali CAN i oprogramowania
odbiorczego na Raspberry Pi. Dziennik jest materiałem źródłowym do rozdziałów 6 do 8 pracy
magisterskiej, dlatego notuje nie tylko treść zmiany, ale też sposób jej rozstrzygnięcia, który
w pracy stanowi część opisu metody.

Każdy wpis ma cztery części:

- **Data i zakres**: kiedy i czego dotyczyła zmiana.
- **Ustalenie**: co zostało odkryte lub rozstrzygnięte.
- **Rozstrzygnięcie**: jakim pomiarem albo testem to wykazano.
- **Skutek**: co z tego wynikło dla firmware, dla oprogramowania odbiorczego albo dla metodyki badań.

Wpis dostaje dodatkowo część **Wniosek metodyczny** wtedy, gdy ustalenie zmienia sposób prowadzenia
dalszych prac, a nie tylko pojedynczy fragment kodu.

Poziomy pewności oznaczane są tak samo jak w spec-ach: `[PEWNE — ze źródła]`, `[INFERENCJA]`,
`[HIPOTEZA]`, `[DO ZWERYFIKOWANIA]`. Odwołania do plików wskazują ścieżki w tym repozytorium.

---

## 2026-09-17, tor odbiorczy STM32 do Raspberry Pi: 29-bitowy identyfikator narzędzia stanowiskowego

Data wpisu wynika z commita `8f5ed52` "add working bit stream from usbccviewer can to rpi decoding",
który wprowadza jednocześnie `CAN_ACCEPT_EXTENDED_AS_STANDARD` i `CAN_SELFTEST_ON_BOOT`
w `Core/Src/App.c` `[PEWNE — z historii repozytorium]`.

### Ustalenie

Narzędzie symulujące ruch na stanowisku, adapter uCCB w protokole SLCAN, nadawało ramki
z identyfikatorem 29-bitowym, mimo że stanowisko było skonfigurowane jako CAN 2.0A. Format rekordu
strumienia obejmuje wyłącznie standardowe ramki danych o identyfikatorze 11-bitowym i DLC od 0 do 8,
więc każda taka ramka ginęła na wejściu ścieżki odbiorczej: `CAN_ReceiveFrame()` sprawdza warunek
`header.IDE != CAN_ID_STD` i przekazuje ramkę do `CAN_DiagnosticsRecordInvalidFrame()` z powodem
`CAN_DIAG_INVALID_EXTENDED`, co w `Core/Src/can_diagnostics.c` inkrementuje licznik `extended_frames`
`[PEWNE — z kodu]`.

Objaw był nieodróżnialny od usterki odbioru w firmware, ponieważ kontroler rejestrował ramki, a na
wyjściu nie pojawiał się ani jeden rekord ramki. Diagnostyka zajęła cztery rundy sprawdzania, zanim
przyczyna została nazwana `[DO ZWERYFIKOWANIA — liczba rund pochodzi z relacji z sesji na stanowisku
i nie ma śladu w repozytorium]`.

### Rozstrzygnięcie

Test w pętli zwrotnej kontrolera, `CANSelfTest()` w `Core/Src/caninit.c`, nadaje dziewięć ramek
o identyfikatorach od `0x600` do `0x608` i DLC narastającym od 0 do 8, w trybie silent loopback, czyli
bez sterowania liniami magistrali `[PEWNE — z kodu]`. Test nie zależy od zewnętrznego węzła, od
prędkości bitowej obecnej na przewodzie ani od terminacji, a ramki wracają pełną ścieżką odbiorczą:
przerwanie odbiorcze, bufor pierścieniowy, strumień rekordów oraz plik na pamięci USB. Dziewięć ramek
zdekodowanych na Raspberry Pi wykluczyło firmware jako źródło problemu.

Przyczynę wskazał układ liczników: `rx_frames` rosło, `valid_frames` pozostawało zerowe,
a `extended_frames` rosło razem z `rx_frames`. Tej kombinacji odpowiada w tabeli "Reading a silent
receiver" w `CAN_STREAM_PROTOCOL.md` jeden wiersz: ramki docierają poprawnie i są odrzucane jako
rozszerzone, ponieważ nadawca jest skonfigurowany na identyfikatory 29-bitowe
`[PEWNE — z kodu i z dokumentacji formatu]`.

### Skutek

1. Firmware przenosi ramkę rozszerzoną jako standardową, gdy jej identyfikator mieści się w 11 bitach:
   pod `CAN_ACCEPT_EXTENDED_AS_STANDARD` wykonywane jest przypisanie `header.StdId = header.ExtId`
   dla wartości nie większych od `0x7FF`. Wartość powyżej tej granicy nadal jest odrzucana
   i raportowana zdarzeniem `CAN_STREAM_EVENT_EXTENDED_REJECTED`, które niesie pełny identyfikator
   29-bitowy `[PEWNE — z kodu]`.
2. Samo rzutowanie jest raportowane raz na sesję zdarzeniem `CAN_STREAM_EVENT_EXTENDED_REMAPPED`
   o kodzie 11, a jego skala pozostaje widoczna w liczniku `extended_frames`, więc ślad nie ukrywa
   faktu, że ruch na przewodzie był rozszerzony `[PEWNE — z kodu]`.
3. Ślad z takiego przebiegu ma ograniczenie, które oprogramowanie odbiorcze musi odnotować:
   `extended_frames` równa się wtedy `valid_frames`, a ramka rozszerzona i standardowa o tym samym
   numerze są nierozróżnialne. Stąd wymaganie 2.7 w spec-u `rpi-detekcja-anomalii`, nakazujące
   adnotację w raporcie sesji `[PEWNE — z formatu]`.
4. Test w pętli zwrotnej jest domyślnie wyłączony przy starcie, `CAN_SELFTEST_ON_BOOT` ma wartość 0,
   ponieważ wstrzykuje dziewięć syntetycznych ramek do śladu i do pliku na pamięci USB. Pozostaje
   dostępny na żądanie komendą `can selftest [1|2]` na kanale diagnostycznym `[PEWNE — z kodu]`.
5. Przebieg kontrolny toru: 366 ramek odebranych, zero brakujących rekordów na łączu szeregowym, zero
   błędów sumy kontrolnej, zero odrzuceń w buforze pierścieniowym `[PEWNE — z pomiaru na sprzęcie]`.
   Potwierdza to poprawność toru, nie jego przepustowość przy obciążeniu docelowym.

### Wniosek metodyczny

Licznik nazywający przyczynę odrzucenia ramki skrócił diagnostykę z godzin do jednego pomiaru.
Ogólniejsza reguła, przyjęta dla dalszych prac: każda ścieżka, która może odrzucić dane, prowadzi
osobny licznik dla każdej przyczyny odrzucenia, a nie jeden licznik zbiorczy. Bez rozdzielenia
przyczyn brak rekordu ramki wskazuje jednocześnie na okablowanie, prędkość bitową, terminację,
konfigurację nadawcy i usterkę oprogramowania, i każdą z tych hipotez trzeba wykluczać osobno.
Z tej samej reguły wynika rozdzielenie strat na łączu szeregowym i odrzuceń wewnątrz urządzenia przez
numerację sekwencyjną rekordów, na której opiera się argument o bezstratności zapisu.

---

## 2026-09-20, generator DBC, scenariusze anomalii i odporna nauka profilu czasowego

### Ustalenie

Powstał kompletny przepływ pomiarowy: generator uruchomiony na komputerze steruje adapterem uCCB
przez USB i wytwarza ruch na podstawie DBC, logger STM32 odbiera magistralę i przesyła rekordy przez
UART, a Raspberry Pi jednocześnie zapisuje surowy `trace.canbin`, zdarzenia detektora, przedziały
jakości oraz raport sesji. Detekcja reguł DBC działa od początku sesji, natomiast reguły czasowe są
uruchamiane po zamrożeniu profilu ruchu referencyjnego `[PEWNE — z kodu i pomiarów na stanowisku]`.

Generator korzysta z okresów wiadomości zapisanych w DBC, a ograniczenie `--max-fps` wybiera taki
podzbiór wiadomości, który mieści się w budżecie przepustowości. Ziarno `--seed` zapewnia powtarzalny
wybór ID pomiędzy sesją referencyjną i testową; wcześniejsze deterministyczne losowanie niezależne od
podanego ziarna zostało usunięte `[PEWNE — z kodu]`. Dla `--seed 42 --max-fps 80` wybrano 17 ze 124
wiadomości i uzyskano około 79,4 ramki/s. W przebiegu 210 s adapter potwierdził 16 672 polecenia,
a dwa terminy zostały pominięte jako spóźnione `[PEWNE — z raportu generatora]`.

Minuta nauki zawiera około 600 odstępów dla wiadomości o okresie 100 ms i zwykle wystarcza do
stabilnego oszacowania jej średniego okresu. Do pomiarów właściwych przyjęto 180 s, aby lepiej
oszacować jitter i wiadomości wolniejsze. Przy `--min-intervals 100` wiadomość o okresie 10 s nie
uzyska jednak profilu czasowego nawet po 180 s; pozostają dla niej reguły wynikające bezpośrednio
z DBC. Włączenie jej do analizy czasowej wymaga dłuższej nauki albo jawnie uzasadnionej niższej
wartości `--min-intervals` `[PEWNE — z parametrów i liczby możliwych obserwacji]`.

Pierwotna obsługa jakości była zbyt restrykcyjna. Każdy błąd CRC, COBS lub brak numeru sekwencyjnego
wywoływał wspólną funkcję zerującą nie tylko stan odstępów wokół uszkodzenia, ale także cały profil
zbierany w fazie nauki. Pojedynczy błąd pod koniec 180 s powodował więc rozpoczęcie nauki od nowa,
przez co `baseline.json` pozostawał niemal pusty i miał `ready: false`, mimo odebrania tysięcy
poprawnych ramek `[PEWNE — z kodu i dwóch nieudanych pomiarów]`.

Rozróżniono dwa rodzaje problemów jakościowych. Uszkodzony rekord strumienia, luka sekwencji lub
niepoprawny rekord ramki powoduje teraz wyczyszczenie historii odstępów, okien częstotliwości i stanu
burst, ale zachowuje wcześniejsze statystyki nauki. Dzięki temu żaden odstęp nie jest liczony przez
lukę w danych, a poprawna część 180-sekundowej próby nie jest tracona. Restart urządzenia, granica
sesji, utrata czasu urządzenia lub potwierdzona utrata odbioru CAN nadal zerują naukę, ponieważ nie
ma wtedy podstaw do łączenia obu fragmentów `[PEWNE — z kodu i testów automatycznych]`.

Licznik zdarzeń detektora nie jest licznikiem wszystkich anomalnych ramek. Detektor zgłasza przejście
w stan anomalii, utrzymuje ten stan bez powielania alarmu dla każdej następnej ramki i uzbraja alarm
ponownie po powrocie do normy. Dlatego np. 120 albo 320 ramek wygenerowanych podczas zwiększenia
częstotliwości może dać jedno lub kilka zdarzeń `frequency_increase`, a nie 120 albo 320 zdarzeń.
Skuteczność należy liczyć na poziomie epizodów oraz ich nakładania się w czasie, natomiast liczbę
faktycznie nadanych ramek brać z raportu generatora `[PEWNE — z kodu detektora]`.

Burst 30 ramek z odstępem 5 ms okazał się zbyt agresywny dla całego toru sterowania. W pomiarze
adapter potwierdził 22 z 30 zaplanowanych ramek, a osiem terminów zostało pominiętych przez spóźnienie
harmonogramu. Nie dowodzi to ograniczenia samej magistrali CAN; obejmuje opóźnienia systemu Windows,
komunikacji USB, protokołu poleceń adaptera i planisty generatora. Dla dalszych scenariuszy gęsty burst
został ustawiony na 10 ms, a raport generatora pozostaje źródłem prawdy o liczbie faktycznie
potwierdzonych transmisji `[PEWNE — z raportu generatora; przyczyna w konkretnym elemencie toru
pozostaje DO ZWERYFIKOWANIA]`.

Różny SHA-256 pliku DBC może wynikać wyłącznie z reprezentacji bajtowej, np. zamiany zakończeń linii
LF na CRLF. Parser może wtedy zbudować identyczny model wiadomości, ale suma pliku będzie inna.
Różnica SHA nie jest zatem dowodem różnicy semantycznej DBC, jednak kontrola sumy celowo nie pozwala
bez dodatkowego potwierdzenia użyć profilu z plikiem o innych bajtach. W pomiarach należy kopiować
ten sam artefakt i zapisywać jego sumę, zamiast opierać identyczność wyłącznie na nazwie pliku
`[PEWNE — z własności SHA-256; semantyczna zgodność konkretnych kopii wymaga porównania parserem]`.

### Rozstrzygnięcie

Po zmianie polityki jakości powtórzono naukę z `--learn-seconds 180 --min-intervals 100`. Detektor
wyświetlił `Baseline ready; timing rules active. Saved baseline.json.` i zapisał gotowy profil.
Sesja trwała 199,16 s, odebrała 855 670 bajtów oraz 21 761 poprawnych rekordów. Wystąpiły cztery
błędy CRC, jeden błąd COBS i łącznie sześć przedziałów jakości. Mimo tych błędów profil został
poprawnie ukończony, co potwierdza, że faza nauki nie wymaga już idealnego strumienia
`[PEWNE — z pomiaru baseline_dbc_seed42 po poprawce]`.

Status `interrupted` w raporcie tej sesji wynika z zakończenia programu przez `Ctrl+C` już po
komunikacie o zapisaniu profilu. Nie unieważnia to `baseline.json`; o jego przydatności rozstrzygają
`ready: true`, zgodność DBC i parametry profilu `[PEWNE — z przebiegu programu]`.

W próbie `anomalies_smoke_01` analiza zajęła około 7,31 s przy czasie rejestracji 255,86 s, czyli
około 2,86% czasu sesji. Przetwarzano około 2412 rekordów/s czasu procesora, szczyt pamięci wyniósł
około 32,6 MB, a kolejka osiągnęła 14 ze 128 fragmentów. Dla zmierzonego obciążenia detekcja na żywo
na Raspberry Pi ma duży zapas obliczeniowy `[PEWNE — z report.json tej sesji]`.

### Skutek

1. Detektor może ukończyć profil mimo pojedynczych błędów transmisji, ale zachowuje je w
   `quality.jsonl` i `report.json`; błędów nie wolno ukrywać ani traktować jak poprawnych danych.
2. Analiza wyników macierzy będzie prowadzona na poziomie zaplanowanych i wykrytych epizodów, z
   wyłączeniem epizodów nakładających się na istotne przedziały jakości. Surowa liczba alarmów nie
   będzie porównywana bezpośrednio z liczbą wstrzykniętych ramek.
3. Każda sesja zachowuje `trace.canbin`, dzięki czemu detekcję można odtworzyć, sprawdzić po pomiarze
   i oddzielić błąd algorytmu od błędu akwizycji.
4. Scenariusze burst używają odstępu co najmniej 10 ms dla wariantu gęstego. Pole
   `slots_skipped_late` i liczba potwierdzonych poleceń muszą być sprawdzone przed uznaniem scenariusza
   za wykonany zgodnie z planem.
5. Detektor należy uruchamiać przed generatorem, aby zarejestrować początek ruchu i nie rozpocząć
   nauki od niepełnej sesji. Po zakończeniu generatora detektor można zatrzymać przez `Ctrl+C`, gdy
   zapisał już profil albo zarejestrował cały scenariusz.

### Hipoteza temperaturowa i plan weryfikacji

Długotrwała praca i nagrzanie adaptera, loggera albo Raspberry Pi mogły pogorszyć jakość transmisji
i zwiększyć liczbę błędów CRC/COBS `[HIPOTEZA]`. Obecne dane nie pokazują temperatury, napięcia,
poziomu sygnału ani miejsca powstania uszkodzenia, więc nie pozwalają przypisać błędów temperaturze.
Równie możliwe są zakłócenia zasilania, poziomy logiczne UART, przewody, taktowanie, buforowanie lub
chwilowe opóźnienia odbioru `[DO ZWERYFIKOWANIA]`.

Weryfikacja wymaga serii powtarzalnych pomiarów zimnego i rozgrzanego układu przy niezmienionym
ruchu, kablach i zasilaniu. Dla każdej sesji należy zapisać czas pracy od zimnego startu, temperaturę
Raspberry Pi i — jeśli jest dostępna — temperaturę pozostałych urządzeń, a następnie porównać liczbę
błędów CRC/COBS na milion rekordów. Dopiero powtarzalny wzrost współczynnika błędów wraz z temperaturą
uzasadni wniosek o zależności termicznej.

### Wniosek metodyczny

Warunkiem użyteczności pomiaru nie może być bezwzględne `quality_intervals == 0`, ponieważ sporadyczne
uszkodzenie rekordu przy dziesiątkach tysięcy poprawnych rekordów nie przekreśla całej sesji.
Jednocześnie błędu nie wolno ignorować. Przyjęta zasada brzmi: zachować poprawne dane, przerwać
ciągłość obliczeń na granicy luki, oznaczyć przedział jakości i wykluczyć z oceny tylko tę część
wyniku, której luka mogła dotyczyć. Całą naukę należy powtarzać dopiero po zdarzeniu podważającym
ciągłość sesji lub czasu urządzenia albo gdy skala strat uniemożliwia reprezentatywne oszacowanie
profilu.

## 2026-09-21 — analiza kampanii i raport do planu rozdziału wynikowego

Przygotowano analizę pomiarów dla ziaren 42, 86, 829730, 550994, 805238 i 159346,
sesji smoke_03 oraz dostępnych pomiarów odniesienia. Do weryfikacji wykorzystano
surowe zapisy `trace.canbin`, dzienniki generatora, profile i alarmy odbiornika.
Wyniki i materiały pomocnicze zapisano w `reports/campaign_2026-09-20/`.

Porównanie 19 pomiarów macierzowych wykazało 244128 ramek nadanych i 244126
odebranych; wszystkie 11110 potwierdzonych ramek wstrzykujących anomalie zostały
odnalezione po stronie odbiornika. Odtworzenie zapisów potwierdziło zgodność
alarmów w 20 sesjach testowych oraz alarmów i profili w sześciu sesjach odniesienia.
Oddzielono realizację scenariusza, odbiór ramek i reakcję metod; liczby nowych
alarmów nie utożsamiono z czułością klasyfikacji pojedynczych ramek.

Na prośbę użytkownika opracowano tekstowy materiał przekazania
`reports/campaign_2026-09-20/RAPORT_WSTEPNY_DO_PLANU_ROZDZIALU.md`.
Opis eksponuje potwierdzone osiągnięcia: działające stanowisko, detekcję podczas
odbioru, odtwarzalność analizy i komplementarność reguł. Wyjaśnia także ograniczenia
generatora, progi graniczne, podtrzymywanie alarmów, jakość transmisji oraz krótki
niezależny odcinek ruchu odniesienia. Zawiera instrukcję dla kolejnego czatu,
który ma przygotować plan rozdziału. Nie zmieniano algorytmów w celu poprawienia
uzyskanych statystyk ani nie przypisywano błędów temperaturze bez pomiarów.
