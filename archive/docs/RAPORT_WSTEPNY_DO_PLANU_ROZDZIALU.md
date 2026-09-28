# Raport wstępny dla autora planu rozdziału wynikowego

Data opracowania: 21.09.2026. Materiał dotyczy kampanii pomiarowej z 20.09.2026.

## 1. Przeznaczenie i główna myśl

Dokument przekazuje kontekst, potwierdzone osiągnięcia i interpretację badań do innego czatu, który ma przygotować plan rozdziału pracy magisterskiej. Jest materiałem redakcyjnym opartym na zakończonej analizie danych, a nie gotowym rozdziałem ani nowym zestawem wyników. Liczby pochodzą z raportu technicznego i tabel wskazanych na końcu dokumentu.

**Głównym osiągnięciem jest uruchomienie i eksperymentalne sprawdzenie kompletnego przepływu: przygotowanie ruchu CAN z DBC, fizyczna transmisja na magistrali, rejestracja przez logger, detekcja na Raspberry Pi podczas odbioru oraz zapis umożliwiający późniejszą kontrolę wyników.** Badania wykazały przydatność prostych, interpretowalnych reguł do wykrywania różnych rodzajów nieprawidłowości. Pozwoliły również ustalić, w jakich warunkach poszczególne reguły reagują i jak poprawnie odczytywać ich alarmy.

Na tym osiągnięciu należy oprzeć narrację. Zestaw wyników nie sprowadza się do licznika alarmów ani do rankingu metod. Obejmuje działające stanowisko, powtarzalną procedurę, zweryfikowany odbiór danych, demonstrację detekcji oraz rozpoznanie ograniczeń. Wartość pracy polega także na tym, że wynik można odtworzyć i wyjaśnić na podstawie zarejestrowanego przebiegu.

## 2. Co rzeczywiście zostało wykonane

### Stanowisko pozwalające sprawdzić cyfrową część koncepcji

Wykorzystano komputer z generatorem ruchu, adapter uCCB sterowany przez USB, magistralę CAN, starszy logger i Raspberry Pi. Komputer przygotowuje ramki na podstawie pliku DBC. Adapter wprowadza je na fizyczną magistralę, logger rejestruje ich wystąpienia i przekazuje strumień binarny przez UART, a Raspberry Pi analizuje napływające dane i zapisuje wyniki.

Jest to pomiar sprzętowy z syntetycznie przygotowanym ruchem. Dzięki takiemu połączeniu można kontrolować moment i rodzaj wprowadzanej nieprawidłowości, a jednocześnie obserwować rzeczywiste skutki transmisji, opóźnień i nieregularności nadawania. Wyniki odnoszą się do uruchomionego stanowiska zastępczego. Nie należy przypisywać ich nieuruchomionej docelowej płytce ani traktować jako pomiarów analogowej warstwy fizycznej CAN.

### Generator ruchu o kontrolowanych warunkach

Generator korzysta z definicji wiadomości, sygnałów i okresów zawartych w DBC. Wybrano podzbiór wiadomości, aby zmieścić się w możliwościach stanowiska. W badanych konfiguracjach obejmował on 16 lub 17 ID i około 79,4 ramki/s ruchu odniesienia. Nie wymagano jednoczesnego nadawania całej bazy DBC.

Ziarno pozwala odtworzyć wybór wiadomości i fazy ruchu. Dzięki temu profil odniesienia i pomiar anomalii mogą dotyczyć tej samej konfiguracji. Zmiana ziarna daje inną konfigurację testową, a powtórzenia dla danego ziarna pozwalają ocenić powtarzalność odpowiedzi. Generator zapisuje zarówno plan epizodów, jak i faktycznie potwierdzone komendy oraz pominięte terminy. Możliwe jest więc odróżnienie zaplanowanej intensywności od intensywności rzeczywiście uzyskanej.

### Detekcja działająca podczas odbioru

Na Raspberry Pi uruchomiono kontrole zgodności z DBC oraz reguły analizujące zanik wiadomości, odstępy między ramkami, liczność w oknie czasowym, odchylenie od wyuczonego rozkładu odstępów i krótkie serie ramek. Wyniki są zapisywane na bieżąco. Przygotowano również obsługę diagnostyki kontrolera, jednak skuteczności tej części nie oceniano przez kontrolowane wstrzykiwanie błędów kontrolera.

Uczenie poprzedza test właściwy. Po około 180 s ruchu referencyjnego profil zostaje zamrożony i jest wczytywany do kolejnych pomiarów. Pozwala to oceniać zmiany względem wcześniej ustalonego odniesienia. Wstrzykiwane anomalie nie są wówczas automatycznie włączane do wzorca normalnego ruchu.

### Zapis umożliwiający sprawdzenie wyniku

Po stronie generatora zachowano konfiguracje, plany epizodów i dzienniki nadawania. Po stronie odbiornika zapisano surowy strumień `trace.canbin`, alarmy, informacje o jakości, profil oraz raport wykonania. Znaczniki początku i końca sesji pozwoliły połączyć czas generatora z czasem loggera.

Ten zapis ma bezpośrednie znaczenie badawcze. Umożliwia odpowiedź na trzy odrębne pytania: czy generator wykonał zamierzony scenariusz, czy ramki dotarły do odbiornika oraz jak zareagował algorytm. Bez tego rozdzielenia pominięcie terminu przez generator mogłoby zostać błędnie uznane za nieskuteczność detektora.

## 3. Najmocniejsze potwierdzone wyniki

### Komunikacja i rejestracja były bardzo kompletne

W 19 sesjach macierzowych generator potwierdził 244 128 komend nadania, a ślady RPi zawierają 244 126 poprawnie zdekodowanych ramek. W 18 sesjach potwierdzono pełną zgodność kolejności, identyfikatorów, DLC i danych. W pozostałej próbie, `seed805238/03`, brakuje dwóch konkretnych ramek ruchu referencyjnego. Nie stwierdzono dodatkowych ramek w porównywanym ciągu.

**Wszystkie 11 110 potwierdzonych ramek przypisanych przez generator do wstrzykiwanych anomalii odnaleziono w zapisach odbiornika.** To mocna podstawa do oceny reakcji detektora: dla tych ramek wiadomo, że bodziec testowy rzeczywiście dotarł do loggera. Zaniki wiadomości oceniano osobno, ponieważ polegają na celowym braku transmisji.

Proporcja RX/TX wynosi około 99,9992%. Jest to wynik kompletności badanego zapisu. Nie jest miarą skuteczności wykrywania anomalii i nie powinien być tak podpisany.

### Analiza jest odtwarzalna

Ponowne przetworzenie śladów daje dokładnie te same alarmy co analiza podczas odbioru dla wszystkich 20 testów, obejmujących 19 macierzy i smoke_03. Odtworzono również alarmy oraz profile sześciu kompletnych baseline’ów. W audycie przeszło 887 kontroli spójności danych i konfiguracji.

W pracy można na tej podstawie stwierdzić, że wynik detekcji jest powtarzalny dla zachowanego wejścia i konfiguracji. Pozwala to analizować przyczyny alarmu po zakończeniu eksperymentu oraz porównywać późniejsze warianty algorytmu na tym samym materiale. Sam replay nie stanowi niezależnego dowodu poprawności definicji reguły, ale potwierdza zgodność wykonania na żywo z analizą zapisu.

### Wykryto różne klasy nieprawidłowości

Test smoke_03 obejmował dziewięć epizodów. Każdy wywołał reakcję co najmniej jednej adekwatnej reguły. Wszystkie 9751 ramek tej sesji są zgodne z dziennikiem nadawania. Jest to czytelna demonstracja funkcjonalna całego przepływu od wstrzyknięcia nieprawidłowości do zapisu alarmu.

Macierze rozszerzyły tę demonstrację do 17 wariantów intensywności i rodzaju anomalii, sześciu ziaren oraz powtórzeń. Nowy alarm co najmniej jednej metody wystąpił w 311 z 323 epizodów. Pozostałe dwanaście przypadków dotyczyło utrzymywania aktywnego warunku DBC między kolejnymi wstrzyknięciami, bez poprawnej ramki kasującej ten warunek. Znaczenie tej liczby wyjaśniono w punkcie 5.

### Raspberry Pi poradziło sobie z analizowanym obciążeniem

W 19 sesjach macierzowych analiza była kompletna i nie nastąpiło przepełnienie kolejki. Jej maksymalne zapełnienie wynosiło 2–4 fragmenty przy pojemności 128. Pamięć rezydentna procesu wynosiła około 31 MiB. Zmierzony czas przetwarzania analizy stanowił około 5,05–5,85% czasu rejestracji.

Wniosek dla celu pracy jest konkretny: proste reguły dało się wykonywać na żywo na użytym Raspberry Pi przy badanym ruchu i jednoczesnym zapisie wyników. Nie pojawiła się konieczność przeniesienia całej detekcji do analizy po pomiarze. Wskaźnik czasu przetwarzania nie jest jednak pomiarem całkowitego wykorzystania CPU ani testem maksymalnej przepustowości CAN.

## 4. Co pokazały poszczególne metody

### DBC: jednoznaczne wskazanie niezgodności treści lub formatu

Kontrola DBC zareagowała na wszystkie 38 epizodów nieznanego identyfikatora, wszystkie 19 epizodów skróconego DLC oraz wszystkie 19 pierwszych epizodów przekroczenia górnej granicy sygnału. Znaczenie praktyczne tej metody polega na możliwości podania konkretnej przyczyny alarmu: nieznanego ID, niewłaściwej długości albo wartości wykraczającej poza zdefiniowany zakres.

Kontrole te mogą działać również dla rzadkich wiadomości bez wyuczonego okresu. To ważne uzupełnienie metod czasowych. Ograniczeniem jest jakość i zakres definicji DBC: reguła nie zweryfikuje informacji, której baza nie opisuje.

### Timeout: reakcja w trakcie zaniku

Reguła zaniku zareagowała we wszystkich 38 próbach ciszy trwającej 1 s lub 5 s. Odpowiedź następowała podczas braku wiadomości. Mediana czasu alarmu względem początku zaplanowanej ciszy wyniosła około 0,26 s w tej konfiguracji.

Jest to szczególnie czytelny wynik funkcjonalny. Reguła pozwala wykryć brak oczekiwanej komunikacji bez oczekiwania na powrót wiadomości. Uzupełnia kontrolę odstępu, która może ocenić długość przerwy dopiero po odebraniu następnej ramki. Podany czas jest wyznaczony na osi urządzenia; nie zmierzono osobno opóźnienia prezentacji alarmu użytkownikowi.

### Okres: prosta i skuteczna obserwacja zmian odstępów

Reguła okresu zgłosiła nowe alarmy we wszystkich 114 epizodach zwiększenia lub zmniejszenia częstotliwości. W kontrolowanych przedziałach tła przyjętych w analizie nie zarejestrowano jej alarmów. Wynik przemawia za przydatnością prostego porównania odstępu z wyuczonym zakresem w badanym stanowisku.

W łagodnych wariantach część odpowiedzi wynikała z rzeczywistej nieregularności nadawania. Idealne odstępy 80 ms i 125 ms mieszczą się w zakresie około 70–130 ms, ale zmierzone odstępy czasem go przekraczały. Należy więc pisać o wykryciu rzeczywiście zarejestrowanej zmiany ruchu. Nie należy twierdzić, że tolerancja ±30% gwarantuje wykrywanie idealnie równych odstępów zmienionych tylko o −20% lub +25%.

### Częstotliwość: ocena zmiany utrzymującej się w oknie

Reguła częstotliwości zareagowała we wszystkich 57 próbach wzrostu 2×, wzrostu 4× i spadku do 0,25×. Pokazuje to przydatność liczenia ramek w oknie do wykrywania wyraźnej zmiany intensywności komunikacji.

Łagodne zmiany 1,25× i 0,8× nie przekraczały przyjętej tolerancji tej reguły. Dla wariantu 0,5× wynik 13/19 ujawnił wrażliwość decyzji na granicę pięciu ramek w oknie. Niewielka różnica wyuczonego okresu względem 100 ms przesuwała próg nieco powyżej albo poniżej pięciu. Jest to zidentyfikowana własność implementacji progu, którą można uwzględnić przy dalszym dopracowaniu metody. Silniejsze zmiany pozostawały wykrywalne mimo tej granicznej własności.

### Burst: wykrycie krótkiego, silnego zagęszczenia

Reguła burst zareagowała w każdej z 19 prób gęstego wstrzyknięcia z planowanym odstępem 10 ms. Rzeczywista liczba ramek wynosiła 16–20 na próbę. Demonstracja potwierdza wykrywanie takiego krótkiego zagęszczenia komunikacji w badanym układzie.

Dla granicznego wariantu czterech ramek co 20 ms reakcja wystąpiła w 8/19 prób, mimo odbioru wszystkich wstrzykniętych ramek. Ślad pokazał, że rzeczywiste odstępy dochodziły do około 30 ms, podczas gdy reguła wymagała kolejnych odstępów poniżej około 25 ms. Ten wynik wyznacza praktyczną granicę działania przy jitterze stanowiska. W pozostałych próbach tego wariantu występowały reakcje kontroli okresu i 3σ, co pokazuje uzupełnianie się metod.

### 3σ: większa wrażliwość wymagająca świadomego doboru progu

Reguła 3σ reagowała na zmiany rozkładu odstępów i pozwoliła porównać prostą metodę statystyczną z regułami o stałej tolerancji. W badanym tle zgłaszała również więcej dodatkowych alarmów. Po zastosowaniu maski jakości wystąpiło 115 takich alarmów w około 24,68 min ocenianego tła, czyli około 4,66 alarmu/min. Reguła częstotliwości zgłosiła w tym samym tle 13 alarmów, a DBC, timeout, okres i burst nie zgłosiły ich.

Wniosek jest użyteczny dla doboru metody: zwiększona wrażliwość na odchylenia wymaga uwzględnienia naturalnego jitteru i kosztu dodatkowych alarmów. W tych pomiarach nie wykazano przewagi 3σ nad pozostałymi regułami. Wykazano natomiast, dlaczego porównanie powinno obejmować zarówno reakcję na anomalię, jak i zachowanie przy ruchu tła. Nie nazywać alarmów/min współczynnikiem FPR.

## 5. Jak rozumieć alarmy i zakres oceny

Detektor zapisuje wejście w stan naruszenia. Jeżeli kolejne ramki nadal naruszają ten sam warunek, nie musi zapisywać nowego alarmu dla każdej z nich. Liczba zdarzeń jest zatem różna od liczby ramek nieprawidłowych.

Ta zasada jest istotna dla epizodów DBC. Wykorzystywane ID 6 pojawia się w ruchu referencyjnym co 10 s, a początki kolejnych testów wartości poza zakresem oddzielono o 8 s. Zależnie od fazy może zabraknąć poprawnej wiadomości pomiędzy wstrzyknięciami. Warunek pozostaje wtedy aktywny i kolejna nieprawidłowa wartość nie tworzy nowego zdarzenia. Analogiczna sytuacja dotyczy części testów DLC.

**Wynik 311/323 należy przedstawiać jako liczbę epizodów z nowym alarmem.** Nie należy podpisywać go jako „96,28% poprawnie wykrytych ramek” ani zmieniać na „100% skuteczności” po uznaniowym doliczeniu utrzymanych stanów. Można natomiast wyjaśnić, że dwanaście braków nowego zdarzenia ma rozpoznaną przyczynę w mechanizmie raportowania i separacji epizodów. Nie wskazuje to na brak odbioru ich nieprawidłowych ramek.

Metody mają różne zadania. Kontrola okresu nie służy do rozpoznawania obcego ID, a kontrola DBC nie służy do wykrywania zaniku poprawnie zdefiniowanej wiadomości. Zera w pełnej mapie metod i scenariuszy nie mogą być czytane jako porażki każdej metody we wszystkich klasach. Porównanie powinno najpierw określić własność, którą dana reguła kontroluje, a dopiero potem ocenić jej odpowiedź.

## 6. Ograniczenia, które określają zakres wniosków

**Przepustowość i punktualność generatora.** W macierzach harmonogram PC pominął 43 terminy, w tym 24 ramki gęstych burstów. Odebrano wszystkie 356 ramek burstów potwierdzonych przez adapter, przy planie 380. Ograniczenie dotyczy realizacji części bodźców. Nie ma podstaw do przypisania go wyłącznie USB; w danych potwierdzono przekroczenie terminów harmonogramu. Do interpretacji należy używać rzeczywiście otrzymanej intensywności.

**Sporadyczne problemy jakości strumienia.** Dodanie pełnych śladów umożliwiło sprawdzenie, czy uszkodzony rekord dotyczył ramki CAN, czy informacji diagnostycznej, oraz ograniczenie miejsca wystąpienia luki. Niewielka liczba błędów nie wymaga odrzucania całego pomiaru. Problemy jakości pozostają jawne, a wynik można ocenić również po wyłączeniu ich otoczenia. CRC strumienia UART nie oznacza automatycznie błędu CRC na magistrali CAN.

**Długość uczenia i rzadkie ID.** Powstało sześć gotowych profili. Każdy obejmuje 12 lub 13 ID z wyuczonym okresem oraz cztery rzadkie ID bez wystarczającej liczby odstępów. Profil nie przypisuje więc wiarygodności czasowej tam, gdzie brakuje danych. Kontrole DBC tych wiadomości nadal działają. Dłuższe uczenie lub osobna procedura byłyby potrzebne do oceny ich regularności.

**Czas niezależnej obserwacji ruchu poprawnego.** W sześciu baseline’ach łączny czas ruchu po zamrożeniu profilu wynosi około 69,87 s. Poza tym oceniono tło między epizodami w macierzach. Materiał pozwala porównać zachowanie reguł, ale nie zastępuje długotrwałego testu częstości fałszywych alarmów w pojeździe.

**Zakres ruchu i sprzętu.** Ruch jest przygotowany na podstawie DBC i generowany w kontrolowany sposób. Nie reprezentuje pełnej różnorodności jazdy, zależności między rzeczywistymi ECU ani wszystkich obciążeń CAN. Nie wykonano w tej kampanii walidacji metod ML, analizy analogowych przebiegów CANH/CANL, pomiarów oscyloskopowych ani skuteczności wykrywania błędów warstwy fizycznej. Publiczne datasety nie są źródłem przedstawionych tutaj wyników.

**Granice dokumentacji.** Brakuje dziennika generatora odpowiadającego baseline seed829730, choć jego ślad, profil i alarmy RPi są spójne. Dodatkowy baseline seed222720 ma tylko ślad i nie wchodzi do głównej oceny. Hipotezy o wpływie temperatury nie zweryfikowano, ponieważ nie zarejestrowano zsynchronizowanych pomiarów temperatury.

Te ograniczenia nie unieważniają demonstracji i porównań wykonanych na stanowisku. Wyznaczają, do jakich warunków można odnieść wyniki oraz jakie pytania pozostają do dalszych badań.

## 7. Czego nauczyło uruchamianie i sprawdzanie systemu

Prace pokazały, że do poprawnej oceny metod potrzebny jest spójny przepływ danych. Samo przygotowanie algorytmu nie wystarcza, jeżeli nie wiadomo, czy generator wykonał plan i czy odbiornik zachował przebieg. Wprowadzenie dzienników TX, surowego zapisu, numeracji rekordów, znaczników czasu i informacji o jakości umożliwiło rozdzielenie tych problemów.

Obsługę uczenia dopracowano tak, aby pojedyncze uszkodzenia transportu nie usuwały całego zgromadzonego materiału referencyjnego. Zachowano oznaczanie luk i przerywanie niepewnych porównań czasowych. Rozwiązanie to pozwoliło ukończyć tworzenie profilu także w obecności sporadycznych problemów strumienia.

Analiza baseline’ów wyjaśniła również pozorne straty wynikające z różnych momentów kończenia programów. Dla ziaren 86, 159346, 550994 i 805238 zapis RX jest dokładnym prefiksem TX; różnica sum ramek przypada na końcówkę nadawania po zamknięciu odbiornika. W baseline42 wykazano jeden brak wewnętrzny oraz 86 ramek na końcu poza zapisem RX. Pozwoliło to zastąpić przypuszczenia o przyczynach różnicy konkretną lokalizacją braków.

Rozbieżności sum kontrolnych profili wyjaśniono zmianą zakończeń linii CRLF/LF. Treść profili jest zgodna. To kolejny przykład, dlaczego kontrola zgodności powinna obejmować zarówno identyfikację pliku, jak i znaczenie przechowywanych danych.

## 8. Wskazówki dla czatu przygotowującego plan

Rozdział powinien prowadzić czytelnika od pytania badawczego do potwierdzonego wniosku. Najpierw należy pokazać, że stanowisko działa i dostarcza wiarygodny zapis. Następnie warto omówić udane demonstracje poszczególnych własności detekcji. Dopiero na tym tle czytelne stają się różnice czułości, granice progów i ograniczenia realizacji scenariuszy.

Główne pytania, na które materiał daje odpowiedź:

1. Czy przygotowany przepływ umożliwia generację, odbiór i detekcję anomalii na żywo? Tak, potwierdzają to pomiary sprzętowe, zapis wyników i kompletność analizy.
2. Czy dane wystarczają do powiązania bodźca z reakcją? Tak dla wskazanych testów, dzięki dziennikom TX, znacznikom i dopasowaniu ramek RX.
3. Jakie własności ruchu można wykryć poszczególnymi metodami? Wyniki pokazują różne role DBC, timeoutu, okresu, częstotliwości, 3σ i burstu.
4. Czy wynik jest powtarzalny? Potwierdzono identyczne odtworzenie zapisanych alarmów i profili. Powtórzenia sprzętowe pozwoliły też zaobserwować wpływ jitteru i konfiguracji na reakcje graniczne.
5. Co ogranicza zakres wniosków? Przede wszystkim syntetyczny ruch, charakter stanowiska, punktualność generatora, definicja epizodu i czas niezależnej obserwacji tła.

**Preferowany sposób prezentacji jest tekstowy.** Tabela lub rysunek powinny odpowiadać na jedno pytanie i być poprzedzone krótkim wyjaśnieniem, co czytelnik ma w nich zobaczyć. Istniejące wykresy są materiałem pomocniczym i nie muszą zostać użyte w rozdziale.

Jeżeli potrzebne będą ilustracje, najbardziej czytelne są: schemat przepływu z zaznaczonymi punktami zapisu, pojedynczy przykład „wstrzyknięcie → obserwacja → alarm” oraz mała tabela „własność → odpowiednia metoda → potwierdzony wynik → ograniczenie”. Zbiorczą mapę wszystkich metod należy stosować tylko z wyjaśnieniem, które pola są właściwe dla ich przeznaczenia. Nie zaczynać rozdziału od dużej tabeli alarmów ani od wykresu problemów generatora.

Do porównania z równą liczbą powtórzeń dostępne jest 18 sesji po 160 s: seed42/02–04 i po trzy próby pozostałych ziaren. Dodatkowa seed42/01 trwa 155 s, ale obejmuje komplet 17 epizodów. Raport techniczny uwzględnia wszystkie 19. W planie należy jawnie określić zbiór, aby nie mieszać mianowników. Próby współdzielące ziarno i profil nie są niezależnymi losowymi obserwacjami całego możliwego ruchu CAN.

Przed zaproponowaniem struktury trzeba przeczytać aktualne pliki pracy i `.kiro/steering`. Obecny plik `06implementacja-realizacja.tex` zawiera również treści metodologiczne. Nie należy powielać ich w rozdziale wynikowym ani opierać planu wyłącznie na historycznych nazwach rozdziałów z rozmowy. W części wynikowej wystarczy przypomnieć warunek eksperymentu potrzebny do zrozumienia konkretnego rezultatu.

## 9. Gdzie znajdują się dowody i materiały

Repozytorium oprogramowania:

`K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023`

Dane generatora: `captures`. Dane RPi: `deployment\can-live-rpi\captures_live`.

Raport i tabele:

`K:\Praca Magisterska\oprogramowanie\RTE_3_0_CAN_LOGGER_2023\reports\campaign_2026-09-20`

- `RAPORT_ANALIZY.md`: szczegółowy raport techniczny, pełne tabele scenariuszy, metody oceny i zastrzeżenia.
- `sessions.csv`, `episodes.csv`, `scenario_summary.csv`: wyniki sesji i każdego epizodu.
- `method_summary.csv`, `events_audit.csv`: porównanie metod i klasyfikacja alarmów.
- `baselines.csv`, `profile_parameters.csv`: profile odniesienia i ich weryfikacja.
- `quality_audit.csv`, `missing_rx_frames.csv`: lokalizacja problemów jakości i brakujących ramek.
- `validation.csv`, `source_manifest.csv`: kontrole spójności i sumy danych źródłowych.
- `figures`: wykresy pomocnicze PNG/PDF. Ich wykorzystanie jest opcjonalne.
- `fragment_wyniki.tex`: wcześniejszy szkic liczbowy; nie jest obowiązującym planem rozdziału.

Repozytorium pracy:

`K:\Praca Magisterska\Praca-Magisterska-Dominik-Tendera`

Wyników nie należy zastępować korzystniejszymi liczbami ani traktować wszystkich ograniczeń jako niepowodzeń. Celem planu jest pokazanie osiągniętej funkcjonalności, przedstawienie dowodów i wyprowadzenie wniosków w zakresie rzeczywiście wykonanego eksperymentu.

## 10. Polecenie do wykorzystania w następnym czacie

Na podstawie tego raportu i aktualnej treści pracy przygotuj plan rozdziału przedstawiającego wyniki badań oraz ocenę metod detekcji anomalii CAN. Najpierw zapoznaj się z `.kiro/steering` i istniejącymi rozdziałami, szczególnie opisem implementacji oraz metodologii. Oprzyj narrację na osiągnięciach: działającym stanowisku, zweryfikowanym przepływie danych, detekcji na żywo, odtwarzalności wyników i uzupełniających się rolach metod. Dla każdego proponowanego podrozdziału podaj pytanie, główny wniosek, dowód z pomiarów i konieczne ograniczenie. Preferuj klarowny opis tekstowy; proponuj tabele i rysunki tylko wtedy, gdy pomagają udowodnić konkretny wniosek. Zachowaj rozróżnienie między kompletnością odbioru, nowymi alarmami, ciągłością stanu naruszenia i metrykami klasyfikacji. Na tym etapie przygotuj plan, bez pisania pełnego rozdziału i bez zmieniania wyników eksperymentu.
