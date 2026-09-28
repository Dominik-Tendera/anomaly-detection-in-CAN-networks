"""Publication figures and Polish report from the audited campaign JSON/CSV."""
from pathlib import Path
import csv
import json
import statistics as st
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from analyze_measurement_campaign import ROOT, METHODS, read_lines, write_csv
from campaign_trace_audit import decode_trace, locate_trace, align_markers, match_frames

OUT = ROOT / 'results/reports/campaign_2026-09-20'
LABELS = ['E01  Wzrost 1,25×', 'E02  Wzrost 2×', 'E03  Wzrost 4×',
          'E04  Spadek do 0,8×', 'E05  Spadek do 0,5×', 'E06  Spadek do 0,25×',
          'E07  Zanik 1 s', 'E08  Zanik 5 s', 'E09  Burst 4 ramki / 20 ms',
          'E10  Burst 20 ramek / 10 ms', 'E11  Nieznane ID, 1 ramka',
          'E12  Nieznane ID, 8 ramek', 'E13  DLC krótsze', 'E14  DLC dłuższe',
          'E15  Wartość 101 (maks. 100)', 'E16  Wartość −101 (min. −100)',
          'E17  Wartość 110 (maks. 100)']
MNAMES = {'dbc':'DBC','timeout':'Timeout','period':'Okres','frequency':'Częstotliwość',
          'three_sigma':'3σ','burst':'Burst','controller':'Kontroler'}
SEEDS = [42,86,829730,550994,805238,159346]


def table(headers, rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join('---' for _ in headers)+'|']+
                     ['| '+' | '.join(str(v) for v in row)+' |' for row in rows])


def save(fig, name):
    for ext in ('png','pdf'):
        fig.savefig(OUT/'figures'/f'{name}.{ext}', dpi=180, bbox_inches='tight')
    plt.close(fig)


def main():
    (OUT/'figures').mkdir(exist_ok=True)
    data = json.loads((OUT/'analysis.json').read_text(encoding='utf-8'))
    sessions = [s for s in data['sessions'] if s['kind']=='matrix']
    eps = [e for e in data['episodes'] if e['kind']=='matrix']
    bases = [b for b in data['baselines'] if b.get('ready') is not None]
    events = list(csv.DictReader((OUT/'events_audit.csv').open(encoding='utf-8-sig')))
    scenarios = [{**{'episode':f'E{i:02d}'},'items':[e for e in eps if e['episode']==f'E{i:02d}']} for i in range(1,18)]
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
                         'axes.spines.right':False,'pdf.fonttype':42,'axes.titleweight':'bold'})
    shown = ['dbc','timeout','period','frequency','three_sigma','burst']
    fig,ax = plt.subplots(figsize=(10.2,8.6),layout='constrained')
    values = np.array([[sum(e['hit_'+m] for e in s['items']) for m in shown] for s in scenarios])
    ax.imshow(values, cmap='Blues', vmin=0,vmax=19,aspect='auto')
    ax.set_xticks(range(6),[MNAMES[m] for m in shown])
    ax.set_yticks(range(17),LABELS)
    for (i,j),v in np.ndenumerate(values):
        ax.text(j,i,f'{v}/19',ha='center',va='center',color='white' if v>=11 else '#243746',fontsize=10)
    ax.set_title('Reakcje metod na 17 wariantów anomalii',loc='left',pad=20)
    fig.supxlabel('Sesje z nowym alarmem podczas epizodu. Zanik: alarm przy powrocie ramki liczony osobno.',fontsize=9)
    save(fig,'01_macierz_metod')

    ordered = sorted(sessions,key=lambda s:(SEEDS.index(s['seed']),s['session']))
    burst = [next(e for e in eps if e['session']==s['session'] and e['episode']=='E10') for s in ordered]
    labels = [str(s['seed'])+'/'+s['session'][-2:] for s in ordered]
    fig,ax = plt.subplots(figsize=(11,4.7),layout='constrained')
    x=np.arange(len(burst)); rx=np.array([e['rx_frames_per_episode'] for e in burst])
    ax.bar(x,rx,color='#287d8e',label='Odebrane, zgodne z TX')
    ax.bar(x,20-rx,bottom=rx,color='#e6a157',label='Pominięte przez harmonogram PC')
    for i,v in enumerate(rx): ax.text(i,v-.8,str(v),ha='center',color='white',fontsize=9)
    ax.set_xticks(x,labels,rotation=55,ha='right');ax.set_ylim(0,23);ax.set_ylabel('Ramki w epizodzie E10')
    ax.set_title('Burst 10 ms: plan 20 ramek, odbiór od 16 do 20',loc='left');ax.legend(ncol=2,loc='upper right',fontsize=9)
    save(fig,'02_realizacja_burstu')

    denom=sum(s['background_clean_duration_s'] for s in sessions)/60
    rates=[sum(s['background_clean_'+m] for s in sessions)/denom for m in shown]
    counts=[sum(s['background_clean_'+m] for s in sessions) for m in shown]
    fig,ax=plt.subplots(figsize=(8.2,4.5),layout='constrained')
    ax.barh([MNAMES[m] for m in shown],rates,color=['#b4c9d0']*4+['#b55746','#b4c9d0'])
    for i,(v,n) in enumerate(zip(rates,counts)):ax.text(v+.04,i,f'{v:.2f}/min  (n={n})',va='center')
    ax.set_xlim(0,max(rates)*1.5);ax.set_xlabel('Nowe alarmy / min ruchu tła po wyłączeniu problemów jakości')
    ax.set_title('Koszt czułości metody 3σ',loc='left');ax.invert_yaxis()
    save(fig,'03_alarmy_tla')

    fig,(ax,ax2)=plt.subplots(1,2,figsize=(10.2,4.5),layout='constrained')
    sample=[next(e for e in eps if e['seed']==s and e['episode']=='E05') for s in SEEDS]
    delta=[e['period_us']-100000 for e in sample]
    ax.barh([str(s) for s in SEEDS],delta,color=['#287d8e' if x<0 else '#e6a157' for x in delta]);ax.axvline(0,color='#35495c',lw=1)
    ax.set_xlabel('Wyuczony okres minus 100 ms [µs]');ax.set_ylabel('Ziarno');ax.invert_yaxis()
    detections=[sum(e['hit_frequency'] for e in eps if e['seed']==s and e['episode']=='E05') for s in SEEDS]
    n=[sum(e['seed']==s and e['episode']=='E05' for e in eps) for s in SEEDS]
    ax2.barh([str(s) for s in SEEDS],np.array(detections)/n,color='#287d8e');ax2.set_xlim(0,1.25);ax2.invert_yaxis()
    for i,(v,k) in enumerate(zip(detections,n)):ax2.text(v/k+.03,i,f'{v}/{k}',va='center')
    ax2.set_xlabel('Udział sesji z alarmem częstotliwości')
    fig.suptitle('E05: o reakcji na 5 ramek/s decyduje strona granicy progu',fontsize=12,fontweight='bold')
    save(fig,'04_granica_czestotliwosci')

    # Actual interarrival times for one quality-clean session. No fabricated trace.
    selected=next(s for s in sessions if s['session']=='anomaly_seed86_matrix_01')
    trace=decode_trace(ROOT/selected['trace_path'])
    target=next(e['can_id'] for e in eps if e['session']==selected['session'] and e['episode']=='E01')
    frames=[f for f in trace['frames'] if f['can_id']==target and f['ts_us'] is not None]
    t=np.array([(f['ts_us']/1e6-selected['offset_marker_s'])/selected['marker_scale'] for f in frames])
    dt=np.diff([f['ts_us'] for f in frames])/1000
    fig,axes=plt.subplots(1,2,figsize=(10.4,4.2),sharey=True,layout='constrained')
    for ax,(lo,hi,title,nominal) in zip(axes,[(14,22,'E01: częstotliwość 1,25×',80),(44,52,'E04: częstotliwość 0,8×',125)]):
        take=(t[1:]>=lo)&(t[1:]<=hi);ax.scatter(t[1:][take],dt[take],s=12,color='#287d8e')
        ax.axhline(70,color='#b55746',ls='--',lw=1);ax.axhline(130,color='#b55746',ls='--',lw=1)
        ax.axhline(nominal,color='#6b7785',ls=':',lw=1);ax.set_xlim(lo,hi);ax.set_ylim(45,155)
        ax.set_title(title);ax.set_xlabel('Czas od znacznika początku [s]')
    axes[0].set_ylabel('Odstęp między ramkami [ms]')
    fig.suptitle('Rzeczywisty jitter przekracza progi okresu 70–130 ms',fontweight='bold')
    save(fig,'05_jitter_z_pomiaru')

    # Per-run and per-method tables use the complete audit rather than selected examples.
    methods=[]
    for m in shown:
        methods.append({'method':m,'new_onset_episodes':sum(e['hit_'+m] for e in eps),
                        'background_events':sum(s['background_'+m] for s in sessions),
                        'clean_background_events':sum(s['background_clean_'+m] for s in sessions),
                        'clean_background_minutes':denom,
                        'clean_background_events_per_minute':sum(s['background_clean_'+m] for s in sessions)/denom})
    write_csv(OUT/'method_summary.csv',methods)
    run_table=table(['Ziarno / próba','TX ACK / RX','E10 RX / plan','Epizody z nowym alarmem','CRC / COBS'],[
        [f"{s['seed']} / {s['session'][-2:]}",f"{s['tx_ack']} / {s['rx_frames']}",
         f"{b['rx_frames_per_episode']}/20",f"{sum(e['any_onset'] for e in eps if e['session']==s['session'])}/17",f"{s['crc_errors']} / {s['cobs_errors']}"] for s,b in zip(ordered,burst)])
    scenario_table=table(['Epizod / wariant','Plan / ACK / RX','Metoda podstawowa','Dowolna metoda','Stan aktywny przed epizodem¹'],[
        [LABELS[i],f"{sum(e['planned_frames'] for e in s['items'])} / {sum(e['acknowledged_frames'] for e in s['items'])} / {sum(e['rx_frames_per_episode'] for e in s['items'])}",
         f"{sum(e['primary_onset'] for e in s['items'])}/19",f"{sum(e['any_onset'] for e in s['items'])}/19",
         sum(e.get('no_new_onset_expected_due_to_latch',False) for e in s['items']) or '—'] for i,s in enumerate(scenarios)])
    baseline_table=table(['Ziarno','ACK / RX','ID / okresowe','Uczenie [s]','Ruch po uczeniu [s]','Alarmy przed / po końcu ruchu'],[
        [b['seed'],f"{b.get('commands_ack','brak') or 'brak'} / {b['frames_rx']}",f"{b['ids']} / {b['periodic_ids']}",f"{b['learning_s']:.3f}",f"{b['post_learning_with_traffic_s']:.3f}",f"{b['events_after_learning_before_last_frame']} / {b['events_after_last_frame']}"] for b in sorted(bases,key=lambda b:SEEDS.index(b['seed']))])
    bg_table=table(['Metoda','Alarmy tła, wszystkie','Alarmy tła, po masce jakości','Alarmy/min po masce'],[
        [MNAMES[m['method']],m['background_events'],m['clean_background_events'],f"{m['clean_background_events_per_minute']:.3f}"] for m in methods])
    any_count=sum(e['any_onset'] for e in eps);primary=sum(e['primary_onset'] for e in eps)
    clean_eps=[e for e in eps if not e['quality_mask_overlap']]
    standardized=[e for e in eps if e['session']!='anomaly_seed42_matrix_01']
    mtx=sum(s['tx_ack'] for s in sessions);mrx=sum(s['rx_frames'] for s in sessions)
    quality_episodes='; '.join(f"{e['seed']}/{e['session'][-2:]}: {e['episode']}" for e in eps if e['quality_mask_overlap'])
    smoke=[e for e in data['episodes'] if e['kind']=='smoke']
    methods_table=table(['Metoda','Wynik charakterystyczny','Ograniczenie interpretacji'],[
        ['DBC','ID obce 38/38; krótsze DLC 19/19; zakres 101: 19/19','Nie rejestruje ponownego wejścia, jeśli stan nie został skasowany poprawną ramką'],
        ['Timeout','Zanik 1 s i 5 s: 38/38','Próg 3T; zgłasza również odstępy 4T przy częstotliwości 0,25×'],
        ['Okres','Wzrost/spadek: 114/114','Łagodne warianty przekraczają próg dzięki jitterowi; nie są dowodem wykrycia idealnie równych odstępów 80/125 ms'],
        ['Częstotliwość','Wzrost 2×/4× i spadek 0,25×: 57/57','Łagodne 1,25×/0,8×: 0/38; wariant 0,5×: 13/19 zależnie od profilu'],
        ['3σ','Reakcja na warianty czasowe','Więcej alarmów tła; silna zależność od wyuczonego odchylenia'],
        ['Burst','Gęsty E10: 19/19; graniczny E09: 8/19','Rzeczywiste odstępy 20 ms przekraczają czasem limit około 25 ms'],
        ['Kontroler','0 alarmów w macierzy','Nie wstrzykiwano błędów kontrolera/warstwy fizycznej; skuteczność nieoceniona']])
    perf=lambda k: (min(s[k] for s in sessions),st.median(s[k] for s in sessions),max(s[k] for s in sessions))
    pf=perf('processing_fraction_of_capture_time'); mem=perf('peak_resident_memory_bytes')
    latency=[]
    for m,ids in [('timeout',['E07','E08']),('frequency',['E02','E03','E06']),('burst',['E10'])]:
        vals=[e['latency_'+m+'_s'] for e in eps if e['episode'] in ids and e['latency_'+m+'_s'] is not None]
        latency.append([MNAMES[m],len(vals),f'{min(vals):.3f}',f'{st.median(vals):.3f}',f'{max(vals):.3f}'])
    latency_table=table(['Reguła / zakres','n','Min [s]','Mediana [s]','Max [s]'],latency)
    report=f'''# Weryfikacja pomiarów CAN i materiał do rozdziału wynikowego

Analiza danych pomiarowych z 20.09.2026; wersja oparta na dodanych śladach binarnych.
Źródło liczb: dołączone CSV/JSON oraz manifest SHA-256 każdego użytego pliku. Analiza nie zmienia danych ani ustawień detektora.

## 1. Najważniejsze ustalenia

Zweryfikowano 19 macierzy (323 epizody), smoke_03 (9 epizodów) i sześć kompletnych pomiarów baseline.
Dla ziaren 86, 829730, 550994, 805238 i 159346 są po trzy macierze; dla 42 są cztery.
W dodatkowym katalogu baseline_matrix_seed222720 znajduje się tylko trace.canbin: 14 937 ramek,
bez błędów dekodera, lecz bez profilu i konfiguracji. Został zinwentaryzowany, nie włączony do porównań metod.
Także surowa próba anomaly_seed222720_matrix_01 nie ma pary generatora ani raportów i leży poza sześcioma wskazanymi ziarnami.

W macierzach potwierdzono **{format(mtx, ',').replace(',', ' ')} komend TX i {format(mrx, ',').replace(',', ' ')} poprawnie zdekodowanych ramek RX**.
18 macierzy ma pełną zgodność kolejności, ID, DLC i bajtów danych. W seed805238/03 brakuje
dwóch ramek referencyjnych: ID 1317 (0x525) oraz 455 (0x1C7), zaplanowanych na 70,538701 s
i 70,564201 s. Występują podczas E06, ale dotyczą innych ID niż jego cel. Nie znaleziono dodatkowych ramek.
Wszystkie **11 110 potwierdzonych ramek przypisanych przez generator do anomalii** zostały odnalezione w RX.
Zaniki oceniono oddzielnie, ponieważ ich celem jest brak ramek.

**Replay odtwarza dokładnie zapisane alarmy dla 20 testów oraz sześciu kompletnych baseline’ów**.
Odtwarza również sześć profili baseline. To potwierdza powtarzalność analizy zapisanych danych;
nie jest niezależnym dowodem poprawności założeń algorytmu.

Nowy alarm co najmniej jednej metody wystąpił w **{any_count}/323 epizodach ({100*any_count/323:.2f}%)**.
Metoda podstawowa dla danego typu zareagowała w {primary}/323. Są to udziały epizodów z nowym
alarmem, nie recall ramek ani ogólna skuteczność IDS. Dwanaście braków nowego alarmu wynika
z ciągłości stanu reguły DBC między zaplanowanymi epizodami. Nie należy przeliczać ich automatycznie
na dwanaście niewykrytych naruszeń sygnału/DLC.

## 2. Jak zweryfikowano dane

1. Sesje połączono według ziaren i numerów prób. Dla 42 raporty są w anomaly_seed42_…,
   a ślady w anomalies_seed42_…; powiązanie potwierdził hash trace.canbin zapisany w raporcie.
2. Sprawdzono SHA-256 śladów, liczbę bajtów/ramek, liczniki zdarzeń i bilans plan = ACK + pominięte.
   Wyniki szczegółowe zawiera validation.csv ({len(data['validation'])} kontroli, {sum(not v['passed'] for v in data['validation'])} niezgodności).
3. Sprawdzono zgodność DBC, kanału, zbiorów ID i profili. Wszystkie pary macierzy mają zgodne
   ID i profile. Sumy profili zgadzają się po normalizacji CRLF do LF; ich treść JSON jest identyczna.
   Hash kodu detektora odpowiada lokalnej wersji po normalizacji LF. Nie odtwarzano planów nowym
   generatorem: źródłem prawdy są zapisane config.json, truth.json i tx.jsonl.
4. Oś czasu wyznaczono z poprawnych ramek znacznika 0x7FE na początku i końcu każdej sesji.
   Zastosowano liniowe przeliczenie pomiędzy dwoma znacznikami. Wykrycia anomalii nie służą
   już do synchronizacji, więc ocena nie zakłada z góry poprawności detekcji obcego ID.
5. Dopasowano ciąg TX do RX po kolejności i dokładnej treści. Przy dwóch brakach użyto
   ograniczonego wyszukiwania sąsiednich pozycji i zgodności czasu. Różnica czasu wywołania
   komendy PC i czasu RX po synchronizacji dochodzi do około 16 ms. Obejmuje rozdzielczość
   zegara i planowanie PC, transport, adapter oraz odbiór; nie jest pomiarem opóźnienia samego USB.
6. Zdarzenia przypisano do tego samego ID i rzeczywistego początku wstrzyknięcia odczytanego
   ze śladu. Przy zmianach częstotliwości koniec wyznacza powrót pierwszej ramki referencyjnej.
   Reguły okresu i 3σ po powrocie z zaniku są reakcją opóźnioną, a nie wykryciem podczas ciszy.

Wcześniejsze pliki tej analizy bazowały na oszacowaniu czasu z alarmów; zastępuje je bieżąca
wersja. Starszy comparison_report.md w generator/captures/anomalies_smoke_03 pozostaje historycznym
raportem z etapu, gdy trace.canbin nie był dostępny lokalnie.

## 3. Kompletność i jakość macierzy

{run_table}

TX oznacza komendy potwierdzone przez adapter; dopiero dopasowany zapis RX potwierdza obecność
ramki na wejściu loggera. Proporcja {mrx}/{mtx} wynosi {100*mrx/mtx:.6f}% i opisuje kompletność
tego zapisu, nie skuteczność detekcji. Dwa znaczniki sesji wliczają się do liczby ramek w każdej próbie.

Harmonogram PC pominął łącznie 43 terminy: **24 ramki gęstych burstów E10 oraz 19 ramek
referencyjnych**. E10 zrealizowano w liczbie 356/380 (93,68%). Wszystkie 356 dotarły do loggera.
Zatem wydłużenie interwału do 10 ms poprawiło realizację, ale nie usunęło pominięć.
Na podstawie tych danych nie można przypisać pominięć wyłącznie ograniczeniu przepustowości USB;
potwierdzone jest przekroczenie terminów harmonogramu hosta. Nominalny ruch tła wynosi około
79,4 ramki/s, a w krótkim burście dochodzi około 100 dodatkowych ramek/s.

W macierzach raporty podają 18 błędów CRC strumienia, 5 COBS, jeden zbyt krótki rekord i 13 brakujących rekordów
sekwencji. Rekordy diagnostyczne także należą do strumienia, więc te liczby nie są liczbą
utraconych ramek CAN. To CRC transportu logger–RPi, nie dowód błędów CRC na magistrali CAN.

Luki UART zawężono do sąsiednich poprawnych rekordów śladu. Do analizy wrażliwości dodano
3,05 s po luce lub resecie stanu, aby objąć ponowne napełnienie okien i obserwację okresowych ID.
Maska dotyka {len(eps)-len(clean_eps)} epizodów: {quality_episodes}.
Po ich wyłączeniu nowy alarm występuje w {sum(e['any_onset'] for e in clean_eps)}/{len(clean_eps)} epizodach.
Maska jest konserwatywną zasadą oceny, nie deklaracją, że przez całe 3,05 s występowały błędy.
Szczegóły granic są w quality_audit.csv. Niewielkie błędy nie dyskwalifikują całej sesji.

![Realizacja burstów](figures/02_realizacja_burstu.png)

## 4. Wyniki per scenariusz

Macierz oznacza zestaw kombinacji **typu anomalii i jej intensywności**, powtarzany dla różnych
zestawów ID i faz ruchu. Nie jest to macierz pomyłek TP/FP/TN/FN.
Reguła podstawowa oznacza odpowiednio: częstotliwość dla E01–E06, timeout dla E07–E08,
burst dla E09–E10 oraz właściwy alarm DBC dla E11–E17.

{scenario_table}

¹ Liczba prób, w których model przejść reguły DBC z potwierdzonych ramek TX przewiduje
utrzymanie stanu naruszenia bez nowego wejścia. Zgodność z RX potwierdzono porównaniem ramek.
W E16 liczba 4 jest większa niż liczba braków nowego alarmu (3), ponieważ w seed42/02 błąd
transportu resetuje stan tuż przed E16 i umożliwia ponowny alarm. To efekt resetu, nie wzrost
czułości. Dla E14 brak nowego alarmu dotyczy trzech prób seed86; dla E17 trzech prób seed805238
oraz trzech seed159346. W E16 bez nowego alarmu pozostają seed42/01, /03 i /04.

Przy E13/E14 prawidłowa ramka tego ID musi skasować warunek DLC. W E15–E17 warunek zakresu
kasuje dopiero poprawny sygnał w ramce o właściwym DLC. ID 6 ma okres 10 s, podczas gdy
początki E15/E16/E17 dzieli 8 s. Zależnie od fazy wiadomości poprawna próbka nie występuje
pomiędzy dwoma epizodami. Zmiana wartości 101 na −101 lub 110 wciąż pozostawia aktywny
ten sam warunek naruszenia zakresu. Definicja epizodów w generatorze i definicja wejścia
w stan alarmowy nie są więc tożsame.

Pełne wyniki każdej z 323 realizacji zawiera episodes.csv, w tym ID, plan, ACK, RX, metody,
czasy reakcji, parametry profilu, problemy jakości i przewidywane przejścia DBC.

## 5. Porównanie i interpretacja metod

![Macierz reakcji metod](figures/01_macierz_metod.png)

{methods_table}

Mapa pokazuje również reakcje dodatkowe. Zero dla metody kontrolującej inną własność nie
oznacza samo w sobie jej nieskuteczności. Trzy reakcje timeout w E05 pojawiają się przy
przejściu z ruchu rzadszego do referencyjnego, gdy ostatni odstęp może przekroczyć 3T;
nie należy interpretować ich jako wykrywania ustalonego okresu 2T przez timeout 3T.

### Łagodne zmiany i jitter

Nominalne odstępy E01 i E04 wynoszą odpowiednio 80 ms i 125 ms. Oba leżą w zakresie
reguły okresu około 70–130 ms. Mimo tego reguła okresu reaguje w 19/19 prób obu wariantów:
zarejestrowane odstępy przekraczają granice wskutek jitteru realizacji ruchu. Dla seed86/01
w E01 występują odstępy około 66–70 ms, a w E04 około 135–140 ms. To potwierdzona detekcja
rzeczywiście otrzymanego ruchu, ale nie dowód, że próg ±30% wykrywa idealną zmianę okresu o −20% lub +25%.

![Odstępy z rzeczywistego śladu](figures/05_jitter_z_pomiaru.png)

### Próg częstotliwości przy 0,5×

W oknie 1 s przy okresie 100 ms oczekuje się około 10 ramek. Dolny próg przy tolerancji 50%
wynosi około 5, a kod sprawdza ostrą nierówność count < threshold. Wyuczony okres jest
nieznacznie mniejszy od 100 ms dla ziaren 42, 829730, 805238, 159346, więc dolny próg jest
nieznacznie większy od 5 i pięć ramek uruchamia alarm. Dla 86 i 550994 okres jest nieznacznie
większy od 100 ms, próg jest mniejszy od 5 i alarm nie występuje. Odchylenia okresu wynoszą
zaledwie pojedyncze mikrosekundy. To wyjaśnia wynik 13/19, bez odwoływania się do strat ramek.
Podobna wrażliwość na granicę całkowitoliczbową występuje dla ID o okresie 1 s.

![Granica progu](figures/04_granica_czestotliwosci.png)

### Bursty graniczne

W E09 odebrano wszystkie 76/76 wstrzykniętych ramek. Burst zareagował tylko w 8/19 prób.
Przy czterech ramkach potrzeba trzech kolejnych odstępów poniżej około 25 ms. Mimo planu
20 ms maksymalne odstępy pomiędzy wstrzykniętymi ramkami w poszczególnych próbach dochodzą
do około 30 ms. Niepełna wykrywalność E09 jest więc zgodna z warunkiem reguły i rzeczywistym
rozkładem odstępów. Okres i 3σ reagują w każdej próbie. Dla E10 odebrano 16–20 ramek
na próbę i burst zareagował w 19/19; potwierdza to wykrywanie silniejszego skupienia, choć
nie realizację każdej zaplanowanej intensywności w pełnej liczbie ramek.

## 6. Alarmy tła i czas reakcji

Do tła zaliczono czas od 3,05 s do końca sesji minus 0,05 s, po usunięciu epizodów,
50 ms marginesu przed nimi oraz 1,05 s po nich. Ten margines obejmuje powrót wiadomości
i domknięcie okna częstotliwości. Alarmy innych ID podczas aktywnego wstrzyknięcia są
osobną kategorią w events_audit.csv, bo mogą wynikać z zakłócenia harmonogramu wspólnego generatora.
Po masce jakości pozostaje {denom:.3f} min ruchu tła.

{bg_table}

W analizowanym tle 3σ generuje najwięcej alarmów bez zaplanowanej anomalii. Brak alarmów
innych metod w tych przedziałach nie dowodzi zerowego FPR w dowolnych warunkach. Wskaźnik
alarmów/min nie jest FPR: nie zdefiniowano jednostki negatywnej ani liczby TN. Nie podano
precision/F1 z prostego dzielenia liczby zdarzeń przez liczbę wstrzykniętych ramek.
Jedenaście z 13 alarmów częstotliwości w tle pochodzi z seed42/02 i dotyczy ID 1327.
W oknie pojawiają się dwie ramki, a górny próg wynosi około 1,999947. Pozostałe dwa
dotyczą pustych okien ID 1329 w seed550994/02 przy dolnym progu około 0,000037.
To praktyczny skutek granicy progu bliskiej liczbie całkowitej. Reset po błędzie UART
może ponadto przesunąć fazę okien; sam upływ czasu maski jakości nie przywraca ich
poprzedniej fazy. Alarmy tła 3σ występują także w sesjach bez takich błędów.

![Alarmy tła](figures/03_alarmy_tla.png)

Przed/po właściwym ruchem występują alarmy braku ramek i spadku częstotliwości. Znaczniki
pozwalają je odciąć; nie należy interpretować ich jako błędów podczas aktywnego scenariusza.
W samych baseline’ach po zamrożeniu profilu i przed końcem ruchu wystąpił jeden alarm 3σ.
Łączny czas takiej obserwacji wynosi tylko {sum(b['post_learning_with_traffic_s'] for b in bases):.3f} s.
Nie jest to długi, niezależny zbiór testowy ruchu poprawnego.

{latency_table}

Czas reakcji burstu jest liczony od pierwszej rzeczywiście odebranej wstrzykniętej ramki;
dla zmian częstotliwości analogicznie, a dla zaniku od początku zaplanowanej ciszy
odniesionego do znaczników. Czas znacznika alarmu pochodzi z urządzenia. Nie zmierzono
osobno czasu dostarczenia alarmu do użytkownika ani opóźnienia zapisu pliku na RPi.
Timeout reaguje przed końcem ciszy; okres i 3σ wymagają następnej ramki i reagują po powrocie.

## 7. Baseline’y

{baseline_table}

Wszystkie sześć profili ma ready=true, około 180 s uczenia i poprawne odtworzenie z trace.canbin.
W każdym cztery ID o okresie 10 s nie spełniają minimum 100 odstępów. Są obecne w profilu,
lecz bez modelu czasowego; DBC nadal pozwala kontrolować ich DLC i wartości sygnałów.

W seed86, 159346, 550994 i 805238 cały ciąg RX jest dokładnym prefiksem dziennika TX.
Różnice 73, 45, 60 i 210 ramek występują na końcu, po zakończeniu zapisu odbiornika.
Nie są dowodem strat w środku tych pomiarów. Dla seed42 występuje jeden brak wewnętrzny
(ID 1553, około 159,014 s planu) i 86 ramek nadanych po końcu zapisu RX.
Brak wewnętrzny odpowiada luce sekwencji i błędowi dekodera w tym miejscu; pozostałe błędy
tego baseline’u nie usunęły ramek CAN z dopasowanego ciągu.

Dla baseline seed829730 nadal brakuje dziennika generatora w przekazanym zbiorze. Ślad,
profil i alarmy RPi są spójne, ale nie można zweryfikować planu/ACK tego pomiaru. Jego
18 alarmów (8 timeout, 10 częstotliwości) występuje po ostatniej odebranej ramce,
co potwierdza związek z końcem ruchu. Dodatkowy baseline seed222720 pozostaje niepełny
dokumentacyjnie i wymaga config.json, report.json oraz baseline.json, jeśli miałby wejść do badań.

## 8. Smoke_03 i wydajność RPi

Smoke_03: 9751/9751 ramek zgodnych z TX, 9/9 epizodów z alarmem dowolnej metody i 8/9
z alarmem metody podstawowej. Łagodny wzrost 1,5× wykrywa reguła okresu, a nie częstotliwości.
Gęsty burst zawiera 30/30 ramek. Jeden pominięty termin należy do ruchu referencyjnego.
Problemy jakości występują już po końcu sesji. To poprawny test funkcjonalny, ale ma inne
warianty i intensywności niż macierz, dlatego nie został dodany do jej mianowników.

W 19 macierzach analiza była kompletna, bez przepełnienia kolejki. Udział zmierzonego czasu
przetwarzania analizy w czasie rejestracji wynosi {pf[0]*100:.2f}–{pf[2]*100:.2f}%
(mediana {pf[1]*100:.2f}%). Szczyt kolejki to 2–4 z 128 fragmentów, a pamięć rezydentna
{mem[0]/1024**2:.2f}–{mem[2]/1024**2:.2f} MiB. To wskazuje na zapas dla badanego ruchu.
Wskaźnik czasu analizy nie jest pomiarem procentowego obciążenia całego CPU ani dowodem
wydajności dla pełnej przepustowości CAN. Ograniczenia realizacji gęstych burstów w tej serii
obserwuje się po stronie harmonogramu generatora.

## 9. Jak przedstawić wyniki w pracy

1. **Tabela stanowiska i kompletności:** sześć baseline’ów oraz zestawienie TX/ACK/RX,
   pominięć harmonogramu i błędów dekodera. Dwa rodzaje strat powinny mieć osobne kolumny.
2. **Mapa reakcji metod (rys. 01):** scenariusze w wierszach, metody w kolumnach,
   w komórkach licznik/mianownik. Podpis wyjaśnia, że to nowe alarmy w epizodach.
3. **Wykres realizacji burstów (rys. 02):** pokazuje oddzielnie to, co zaplanowano,
   pominięto na PC i rzeczywiście odebrano. Uzasadnia użycie rzeczywistej intensywności w analizie.
4. **Wykres alarmów tła (rys. 03):** alarmy na minutę i jawny czas obserwacji,
   jako koszt zwiększonej czułości 3σ. Nie podpisywać go jako FPR.
5. **Dwa wykresy mechanizmów (rys. 04–05):** próg całkowitoliczbowej częstotliwości
   oraz odstępy z rzeczywistego trace.canbin. Wyjaśniają wyniki zamiast tylko je ilustrować.

Do zasadniczego porównania powtarzalności można użyć **18 sesji po 160 s**, po trzy na ziarno,
czyli seed42/02–04 oraz wszystkie próby pozostałych ziaren. Seed42/01 trwa 155 s, choć
obejmuje komplet 17 epizodów; należy pokazać go jako dodatkowy pomiar, a nie usuwać bez opisu.
Dla zestawu 18 sesji wynik nowych alarmów dowolnej metody wynosi
{sum(e['any_onset'] for e in standardized)}/{len(standardized)}.
W niniejszym raporcie zachowano wszystkie 19, aby nie ukrywać żadnej dostarczonej realizacji.

Próby tego samego ziarna współdzielą profil i układ ruchu. Nie traktować 323 epizodów jako
323 niezależnych losowych obserwacji przy wyznaczaniu przedziałów ufności. Zmienność między
ziarnami i między powtórzeniami należy raportować osobno. Dołączony fragment LaTeX jest
materiałem do adaptacji, nie zastępuje istniejącego rozdziału pracy.

## 10. Wnioski i dalsze pomiary

Badane reguły wykrywają różne własności komunikacji. DBC kontroluje strukturę i semantykę,
timeout pozwala reagować podczas ciszy, okres reaguje na pojedyncze odstępy, częstotliwość
na liczność okna, a burst na krótkie serie. Ich odpowiedzi uzupełniają się; jedna wspólna
liczba „skuteczności” ukrywa tę różnicę. 3σ zwiększa wrażliwość na jitter kosztem alarmów tła.

Obecny zbiór nadaje się do opisania demonstracji i ograniczeń tych metod. Przed finalnymi
metrykami klasyfikacji warto wykonać oddzielny, kilkuminutowy test poprawnego ruchu z już
zamrożonym profilem dla każdego ziarna. Epizody DBC powinny być rozdzielane potwierdzoną
poprawną ramką danego ID lub testowane w osobnych sesjach. Każdy graniczny burst należy
opisywać rzeczywistą liczbą ramek i odstępami z loggera.

Zmianę zasad progowania częstotliwości (kwantyzacja liczności, jawne traktowanie granicy,
ewentualna histereza) oraz zmianę progu 3σ trzeba oceniać jako nową wersję metod, najlepiej
na oddzielnym zbiorze lub w jawnie oznaczonym replay. Nie zmieniono algorytmów po obejrzeniu
wyników i nie podmieniono wyników tej kampanii. Wpływ temperatury pozostaje hipotezą:
w tych danych nie ma zsynchronizowanego pomiaru temperatury, który pozwalałby ją potwierdzić.

## Pliki i odtworzenie

- `analysis.json`, `sessions.csv`, `episodes.csv`: pełne wyniki, parametry i identyfikatory prób.
- `scenario_summary.csv`, `method_summary.csv`: agregaty do tabel i wykresów.
- `events_audit.csv`, `quality_audit.csv`, `missing_rx_frames.csv`: klasyfikacja każdego alarmu, maski jakości i brakujące ramki.
- `baselines.csv`, `profile_parameters.csv`: audyt odniesienia i parametry ID.
- `validation.csv`, `source_manifest.csv`: kontrole spójności i identyfikacja danych źródłowych.
- `figures/`: pięć rysunków w PNG i wektorowym PDF; do LaTeX zalecany PDF.
- `fragment_wyniki.tex`: proponowany fragment tekstu i tabela do adaptacji.

Odtworzenie z katalogu repozytorium: uruchomić `results/analyze_measurement_campaign.py`,
a następnie `results/render_results_report.py` w Pythonie z cantools i matplotlib.
Programy tylko czytają generator/captures i raspberry_pi/captures; wyniki zapisują w results/reports/campaign_2026-09-20.
'''
    (OUT/'RAPORT_ANALIZY.md').write_text(report,encoding='utf-8')
    texrows=[]
    for i,s in enumerate(scenarios):
        label=LABELS[i].replace('−','-').replace('×',r'$\times$').replace('≥',r'$\geq$')
        label=label.replace('E'+f'{i+1:02d}'+'  ','')
        texrows.append(f"E{i+1:02d} & {label} & {sum(e['primary_onset'] for e in s['items'])}/19 & {sum(e['any_onset'] for e in s['items'])}/19 "+r'\\')
    tex=r'''% Materiał do adaptacji do rozdziału 8. Źródło: pomiary własne,
% results/reports/campaign_2026-09-20; identyfikacja źródeł: source_manifest.csv.
% Tabele nie wymagają dodatkowych pakietów. Rysunki: pakiet graphicx,
% skopiować PDF do katalogu grafik pracy i dostosować ścieżki includegraphics.
\section{Weryfikacja i wyniki pomiarów stanowiskowych}

Badania objęły 19 realizacji macierzy scenariuszy dla sześciu ziaren generatora,
łącznie 323 zaplanowane epizody. Osobno oceniono test funkcjonalny smoke\_03
oraz sześć kompletnych sesji uczenia profilu. Wyniki w tej części pochodzą
z pomiarów własnych. Zestawienie danych źródłowych i sum kontrolnych zapisano
w pliku \texttt{source\_manifest.csv}, a wyniki pojedynczych epizodów
w pliku \texttt{episodes.csv} towarzyszącym analizie.

Znaczniki początku i końca przesyłane na magistrali umożliwiły powiązanie
planu generatora z czasem urządzenia. W 18 z 19 macierzy uzyskano zgodność
kolejności, identyfikatorów, długości i danych każdej potwierdzonej ramki.
W pozostałej próbie nie odnaleziono dwóch ramek ruchu referencyjnego.
W śladach odbiornika potwierdzono wszystkie 11\,110 ramek przypisanych
do wstrzykniętych anomalii i potwierdzonych przez adapter.

\begin{table}[htbp]
\centering
\small
\caption{Liczba realizacji z nowym alarmem w epizodzie. Źródło: pomiary własne.
Brak ponownego alarmu DBC może oznaczać utrzymanie stanu naruszenia.}
\label{tab:wyniki-macierz-alarmy}
\setlength{\tabcolsep}{3pt}
\begin{tabular}{p{0.07\linewidth}p{0.47\linewidth}p{0.20\linewidth}p{0.16\linewidth}}
\hline
Epizod & Wariant & Reguła podstawowa & Dowolna reguła \\
\hline
'''+ '\n'.join(texrows)+r'''
\hline
\end{tabular}
\end{table}

Nowy alarm co najmniej jednej metody wystąpił w 311 z 323 epizodów.
Wskaźnik ten opisuje rejestrację nowych zdarzeń, a nie czułość klasyfikacji
pojedynczych ramek. Reguły zapisują wejście w stan naruszenia i nie powtarzają
alarmu przed jego skasowaniem poprawną obserwacją. Dwanaście epizodów bez
nowego alarmu DBC było związanych z utrzymaniem takiego stanu pomiędzy
wstrzyknięciami. Dla wiadomości o okresie 10~s odstęp 8~s między początkami
epizodów nie gwarantował wystąpienia poprawnej ramki przywracającej stan normalny.

Detektor zaniku zareagował we wszystkich 38 próbach ciszy o długości 1~s
lub 5~s. Reguły odstępu mogły wykryć wydłużenie przerwy dopiero po powrocie
wiadomości. Reguła częstotliwości wykryła silniejsze zmiany w 57 z 57 prób
wzrostu dwukrotnego, czterokrotnego lub spadku do jednej czwartej wartości
odniesienia. Dla spadku do połowy częstotliwości wynik wynosił 13 z 19.
Ostra nierówność względem progu bliskiego pięciu ramkom powodowała,
że niewielka różnica wyuczonego okresu zmieniała decyzję na granicy zakresu.

W granicznym burście czterech ramek co 20~ms reguła burst zareagowała
w 8 z 19 prób, mimo potwierdzenia odbioru wszystkich wstrzykniętych ramek.
Rzeczywiste odstępy dochodziły do około 30~ms, przekraczając próg około 25~ms.
W gęstym burście co 10~ms uzyskano reakcję w 19 z 19 prób, lecz odebrano
356 z 380 zaplanowanych ramek, ponieważ harmonogram generatora pominął
24 terminy. W ocenie intensywności wykorzystano zatem rzeczywisty zapis odbioru.

% Proponowane rysunki: 01_macierz_metod.pdf, 02_realizacja_burstu.pdf,
% 03_alarmy_tla.pdf, 04_granica_czestotliwosci.pdf, 05_jitter_z_pomiaru.pdf.
% Dokładne wskaźniki alarmów tła i parametry wydajności: RAPORT_ANALIZY.md.
% Nie utożsamiać nowych alarmów/epizod z recall ramek ani alarmów/min z FPR.
'''
    (OUT/'fragment_wyniki.tex').write_text(tex,encoding='utf-8')
    print('Report and five PDF/PNG figures:',OUT)


if __name__=='__main__':
    main()
