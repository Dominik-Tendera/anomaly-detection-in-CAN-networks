"""Rysunek 2: odstępy i progi; rysunek 3: alarmy w tle; osobny rysunek A04.

Wyłącznie sesje kind=matrix, bez integracji. Nie zmienia LaTeX ani starych figur.
Punkt wyjścia: render_results_report.py; używa tego samego dekodera śladu.
Wymagania: Python, matplotlib (dekoder w sąsiednim pakiecie rpi_receiver).

Uruchomienie z katalogu głównego repozytorium:
  python -X utf8 results/render_timing_results.py
  python -X utf8 results/render_timing_results.py --figure timing
  python -X utf8 results/render_timing_results.py --figure background --dpi 300

Opcja timing zapisuje dwa osobne obrazy: odstępy oraz zestawienie A04.

Zależności: python -m pip install -r results/requirements.txt.
Etykiety, kolory, wielkość rysunku i margines czasu są w stałych poniżej.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MultipleLocator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from repo_paths import configure_imports
configure_imports()

from rpi_receiver import can_stream_protocol as proto

ROOT = Path(__file__).resolve().parents[1]
# Odczytaj sam format znacznika bez importowania generatora i bibliotek DBC.
_spec = importlib.util.spec_from_file_location('figure_time_reference', ROOT / 'tools/can_generate/time_reference.py')
_marker_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _marker_module
_spec.loader.exec_module(_marker_module)
decode_marker = _marker_module.decode_marker
REPORT = ROOT / 'results/reports/campaign_2026-09-20'
SESSION = 'anomaly_seed86_matrix_01'
PANELS = [('E01', 'Zwiększenie częstotliwości do 1,25×'),
          ('E04', 'Zmniejszenie częstotliwości do 0,8×')]
CONTEXT_SECONDS = 1.0  # Ruch bez wymuszeń, ale już podczas aktywnego scenariusza.
INK, MUTED, BLUE = '#182F41', '#556774', '#236B8E'
RED, ORANGE, TEAL = '#B74735', '#C48128', '#39756B'
SHADE, GRID = '#EAF1F5', '#DCE4E9'
TIMING_SIZE, BACKGROUND_SIZE = (8.4, 7.7), (8.4, 4.9)
BACKGROUND_METHODS = [
    ('three_sigma', 'Odchylenie statystyczne (3σ)'),
    ('frequency', 'Zmiana częstotliwości'),
    ('period', 'Odchylenie okresu'),
    ('timeout', 'Brak oczekiwanej ramki'),
    ('burst', 'Krótka seria ramek'),
    ('dbc', 'Kontrole zgodności z DBC'),
]


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def pl(value, digits=2):
    return f'{value:.{digits}f}'.replace('.', ',')


def save(fig, output, name, dpi):
    for ext in ('png', 'pdf'):
        fig.savefig(output / f'{name}.{ext}', dpi=dpi, facecolor='white')
    plt.close(fig)


def save_metadata(output, name, metadata, sources):
    metadata['sources_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    metadata['matplotlib'] = matplotlib.__version__
    (output / f'{name}_metadane.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def decode_frames(path, channel):
    """Odtwarza czas jak campaign_trace_audit.decode_trace, bez uruchamiania reguł."""
    decoder = proto.StreamDecoder()
    sync = None
    frames, markers = [], []
    previous_seq, gaps = None, 0
    for record in decoder.feed(path.read_bytes()):
        if previous_seq is not None and record.seq != (previous_seq + 1) % 65536:
            gaps += 1
        previous_seq = record.seq
        if record.type == proto.REC_SESSION:
            sync = None
        elif record.type == proto.REC_TIME_SYNC:
            sync = proto.decode_time_sync(record)
        elif record.type == proto.REC_FRAME:
            frame = proto.decode_frame(record)
            if frame and frame.channel == channel:
                ts = proto.restore_time(sync, frame.ts32) if sync is not None else None
                frames.append({'can_id': frame.can_id, 'ts_us': ts, 'seq': frame.seq})
                marker = decode_marker(frame.data) if frame.can_id == 0x7FE else None
                if marker:
                    markers.append({'kind': marker.kind, 'ts_us': ts,
                                    'generator_s': marker.generator_time_us / 1e6})
    counters = decoder.counters.as_dict()
    # Wybrany przykład ma ciągły zapis; po zmianie sesji nie łączymy odstępów przez luki.
    if gaps or any(counters.get(k, 0) for k in ('crc_errors', 'cobs_errors', 'short_records', 'sync_losses')):
        raise ValueError('Wybierz sesję bez luk lub dodaj jawne przerwanie odstępów na lukach.')
    return frames, markers, counters


def timing(report, output, dpi):
    name = '02_odstepy_i_progi'
    sessions = read_csv(report / 'sessions.csv')
    session = next(s for s in sessions if s['session'] == SESSION and s['kind'] == 'matrix')
    episodes = [e for e in read_csv(report / 'episodes.csv')
                if e['session'] == SESSION and e['kind'] == 'matrix']
    events = [e for e in read_csv(report / 'events_audit.csv')
              if e['session'] == SESSION and e['kind'] == 'matrix']
    trace_path = ROOT / session['trace_path']
    folder = trace_path.parent
    profile_path, config_path = folder / 'baseline.json', folder / 'config.json'
    profile, config = read_json(profile_path), read_json(config_path)
    manifest = {r['path'].replace('\\', '/'): r['sha256'] for r in read_csv(report / 'source_manifest.csv')}
    for path in (trace_path, profile_path, config_path):
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest[path.relative_to(ROOT).as_posix()]:
            raise ValueError(f'Zmienione źródło względem audytu: {path}')
    frames, markers, counters = decode_frames(trace_path, config['settings']['channel'])
    start, end = [[m for m in markers if m['kind'] == k] for k in (1, 2)]
    if len(start) != 1 or len(end) != 1:
        raise ValueError('Wymagane jednoznaczne znaczniki początku i końca.')
    a, b = start[0], end[0]
    scale = (b['ts_us'] - a['ts_us']) / 1e6 / (b['generator_s'] - a['generator_s'])
    offset = a['ts_us'] / 1e6 - scale * a['generator_s']
    if not (math.isclose(scale, float(session['marker_scale']), abs_tol=1e-12)
            and math.isclose(offset, float(session['offset_marker_s']), abs_tol=1e-9)):
        raise ValueError('Niezgodne powiązanie czasu generatora i urządzenia.')
    quality = [q for q in read_csv(report / 'quality_audit.csv') if q['session'] == SESSION]
    if quality:
        raise ValueError('Wybrany przykład wymaga ponownej oceny maski jakości.')
    target_ids = {int(e['can_id']) for e in episodes if e['episode'] in {p[0] for p in PANELS}}
    if len(target_ids) != 1:
        raise ValueError('Panele powinny przedstawiać ten sam identyfikator.')
    target = target_ids.pop()
    selected = [f for f in frames if f['can_id'] == target and f['ts_us'] is not None
                and a['ts_us'] <= f['ts_us'] <= b['ts_us']]
    intervals = [{'time_s': (current['ts_us'] / 1e6 - offset) / scale,
                  'previous_s': (previous['ts_us'] / 1e6 - offset) / scale,
                  'interval_ms': (current['ts_us'] - previous['ts_us']) / 1000,
                  'ts_us': current['ts_us']}
                 for previous, current in zip(selected, selected[1:])]
    p = profile['messages'][str(target)]
    mu = p['period_us'] / 1000
    tolerance = config['settings']['period_tolerance']
    low, high = mu * (1 - tolerance), mu * (1 + tolerance)
    sigma_limit = config['settings']['sigma_multiplier'] * p['std_us'] / 1000
    sigma_low, sigma_high = mu - sigma_limit, mu + sigma_limit
    # Odtwórz momenty wejścia w naruszenie z kolejnych odstępów całej aktywnej sesji.
    # Zachowaj stan przed początkiem panelu; nie licz ponownie trwającego naruszenia.
    expected_onsets = {}
    for method, bounds in [('period', (low, high)), ('three_sigma', (sigma_low, sigma_high))]:
        active = False
        onsets = set()
        for interval in intervals:
            bad = interval['interval_ms'] < bounds[0] or interval['interval_ms'] > bounds[1]
            if bad and not active:
                onsets.add(interval['ts_us'])
            active = bad
        expected_onsets[method] = onsets
    profile_row = next(r for r in read_csv(report / 'profile_parameters.csv')
                       if r['seed'] == session['seed'] and int(r['can_id']) == target)
    if not math.isclose(float(profile_row['period_us']), p['period_us'], abs_tol=1e-9):
        raise ValueError('Profil sesji różni się od zestawienia profili.')
    fig, axes = plt.subplots(2, 1, figsize=TIMING_SIZE, sharey=True)
    fig.subplots_adjust(left=.10, right=.955, top=.79, bottom=.20, hspace=.46)
    fig.text(.055, .962, 'Rzeczywiste odstępy między ramkami a progi detekcji',
             fontsize=14, weight='bold', color=INK, va='top')
    fig.text(.055, .916, 'Łagodne zmiany częstotliwości bliskie progu detekcji',
             fontsize=9, color=MUTED)
    handles = [Line2D([], [], marker='o', ls='', color=BLUE, markersize=4, label='Zarejestrowany odstęp'),
               Line2D([], [], marker='o', ls='', color=RED, markersize=4, label='Odstęp poza progami okresu'),
               Line2D([], [], color=TEAL, ls=':', label='Planowany odstęp'),
               Line2D([], [], color=ORANGE, ls='--', label='Progi reguły okresu')]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(.085, .89), ncol=2,
               frameon=False, fontsize=8.5, columnspacing=3.0)
    plotted, summaries = [], []
    for ax, (code, title) in zip(axes, PANELS):
        ep = next(e for e in episodes if e['episode'] == code)
        lo, hi = float(ep['start_s']), float(ep['end_s'])
        params = json.loads(ep['parameters'])
        nominal = params['anomalous_period_s'] * 1000
        reference = params['reference_period_s'] * 1000
        view_start, view_end = max(a['generator_s'], lo - CONTEXT_SECONDS), min(b['generator_s'], hi + CONTEXT_SECONDS)
        points = [r for r in intervals if view_start <= r['time_s'] <= view_end]
        # Statystyki obu końców odstępu wewnątrz wymuszenia, bez odstępów przejściowych.
        internal = [r for r in points if lo <= r['previous_s'] < r['time_s'] < hi]
        internal_by_ts = {r['ts_us']: r for r in internal}
        dts = [r['interval_ms'] for r in internal]
        alarm_counts = {}
        expected_counts, matched_counts = {}, {}
        for method, event_type, bounds in [('period', 'period_violation', (low, high)),
                                          ('three_sigma', 'statistical_deviation', (sigma_low, sigma_high))]:
            internal_alarms = [e for e in events if e['type'] == event_type and int(e['can_id']) == target
                               and int(e['ts64']) in internal_by_ts and e['quality_masked'] == 'False']
            for e in internal_alarms:
                measured = internal_by_ts[int(e['ts64'])]['interval_ms']
                if not (math.isclose(measured, float(e['measured']) / 1000, abs_tol=1e-9)
                        and (measured < bounds[0] or measured > bounds[1])):
                    raise ValueError('Alarm statystyki nie odpowiada przekroczeniu w śladzie.')
            alarm_counts[method] = len(internal_alarms)
            predicted_times = expected_onsets[method] & set(internal_by_ts)
            recorded_times = {int(e['ts64']) for e in internal_alarms}
            if predicted_times != recorded_times or len(recorded_times) != len(internal_alarms):
                raise ValueError(f'{code}: alarmy {method} nie odpowiadają początkom naruszeń w śladzie.')
            expected_counts[method] = len(predicted_times)
            matched_counts[method] = len(predicted_times & recorded_times)
        if any(not 48 <= r['interval_ms'] <= 157 for r in points):
            raise ValueError('Zmień granice osi Y, aby nie obcinać danych.')
        alarm = min((e for e in events if e['type'] == 'period_violation' and int(e['can_id']) == target
                     and lo <= float(e['relative_s']) < hi and e['quality_masked'] == 'False'),
                    key=lambda e: int(e['ts64']))
        expected = json.loads(alarm['expected'])
        if not (math.isclose(expected['min'] / 1000, low) and math.isclose(expected['max'] / 1000, high)):
            raise ValueError('Progi profilu nie zgadzają się z zapisanym alarmem.')
        alarm_point = next(r for r in points if r['ts_us'] == int(alarm['ts64']))
        if not math.isclose(alarm_point['interval_ms'], float(alarm['measured']) / 1000, abs_tol=1e-9):
            raise ValueError('Odstęp przy alarmie nie zgadza się ze śladem.')
        ax.axvspan(lo, hi, color=SHADE, zorder=0)
        ax.axvline(lo, color='#BACAD4', lw=.7)
        ax.axvline(hi, color='#BACAD4', lw=.7)
        for threshold in (low, high):
            ax.axhline(threshold, color=ORANGE, ls=(0, (5, 3)), lw=1.1, zorder=2)
        ax.plot([view_start, lo, lo, hi, hi, view_end],
                [reference, reference, nominal, nominal, reference, reference],
                color=TEAL, ls=':', lw=1.5, zorder=2)
        for r in points:
            outside = r['interval_ms'] < low or r['interval_ms'] > high
            during = lo <= r['time_s'] < hi
            ax.scatter(r['time_s'], r['interval_ms'], s=16,
                       c=RED if outside else BLUE if during else '#8C9CA7',
                       edgecolors='white', linewidths=.25, zorder=3)
            plotted.append({'episode': code, **r, 'during_injection': during,
                            'included_in_statistics': r['ts_us'] in internal_by_ts,
                            'outside_period_limits': outside,
                            'outside_sigma_limits': r['interval_ms'] < sigma_low or r['interval_ms'] > sigma_high,
                            'expected_period_onset': r['ts_us'] in expected_onsets['period'],
                            'expected_sigma_onset': r['ts_us'] in expected_onsets['three_sigma'],
                            'lower_ms': low, 'upper_ms': high, 'sigma_lower_ms': sigma_low, 'sigma_upper_ms': sigma_high})
        alarm_t, alarm_y = float(alarm['relative_s']), float(alarm['measured']) / 1000
        ax.scatter([alarm_t], [alarm_y], s=64, facecolors='none', edgecolors=RED, lw=1.2, zorder=4)
        ax.annotate(f'Pierwszy alarm reguły okresu\npo {pl(alarm_t - lo, 3)} s od początku wymuszenia',
                    xy=(alarm_t, alarm_y), xytext=(lo + 2.9, 114 if code == 'E01' else 91),
                    fontsize=7.5, color=RED, ha='center', va='center',
                    arrowprops={'arrowstyle': '->', 'color': RED, 'lw': .9},
                    bbox={'facecolor':'white', 'edgecolor':'none', 'alpha': .9, 'pad': 2}, zorder=5)
        ax.set_title(f'{code}  {title}   |   planowo: {pl(nominal, 0)} ms', loc='left', fontsize=10, pad=9, color=INK)
        ax.set(xlim=(view_start, view_end), ylim=(48, 157), ylabel='Odstęp między ramkami [ms]',
               xlabel='Czas od początku scenariusza [s]')
        ax.yaxis.set_major_locator(MultipleLocator(20))
        ax.xaxis.set_major_locator(MultipleLocator(1))
        ax.grid(axis='y', color=GRID, lw=.6)
        ax.set_axisbelow(True)
        summaries.append({'episode':code, 'planned_ms':nominal, 'start_s':lo, 'end_s':hi,
                          'displayed_intervals':len(points), 'first_period_alarm_s':alarm_t,
                          'min_displayed_ms':min(r['interval_ms'] for r in points),
                          'max_displayed_ms':max(r['interval_ms'] for r in points),
                          'internal_intervals':len(dts), 'internal_min_ms':min(dts), 'internal_max_ms':max(dts),
                          'internal_mean_ms':statistics.mean(dts), 'internal_std_ms':statistics.stdev(dts),
                          'period_exceedances':sum(x < low or x > high for x in dts),
                          'sigma_exceedances':sum(x < sigma_low or x > sigma_high for x in dts),
                          'period_new_alarms':alarm_counts['period'], 'sigma_new_alarms':alarm_counts['three_sigma'],
                          'period_expected_onsets':expected_counts['period'], 'sigma_expected_onsets':expected_counts['three_sigma'],
                          'period_matched_onsets':matched_counts['period'], 'sigma_matched_onsets':matched_counts['three_sigma']})
    fig.text(.055, .117, f'Profil odniesienia: μ ≈ {pl(mu, 3)} ms, progi okresu: ≈ {pl(low, 3)}–{pl(high, 3)} ms (±30%).',
             fontsize=8.5, color=INK)
    fig.text(.055, .087, 'Cieniowanie: czas zmiany częstotliwości. Po bokach: po 1 s ruchu referencyjnego bez wymuszeń.',
             fontsize=8, color=MUTED)
    fig.text(.055, .058, f'Sesja: anomaly_seed86_matrix_01 · CAN ID 0x{target:03X} ({target}). Wyłącznie czas aktywnego scenariusza.',
             fontsize=8, color=MUTED)
    fig.text(.055, .028, 'Źródło: opracowanie własne na podstawie śladu CAN, profilu odniesienia i zapisu alarmów.',
             fontsize=8, color=MUTED, style='italic')
    save(fig, output, name, dpi)
    with (output / f'{name}_dane.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plotted[0]))
        writer.writeheader(); writer.writerows(plotted)
    with (output / f'{name}_statystyki.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader(); writer.writerows(summaries)
    metadata = {'session':SESSION, 'kind':'matrix', 'can_id':target, 'channel':config['settings']['channel'],
                'active_start_s':a['generator_s'], 'active_end_s':b['generator_s'],
                'offset_s':offset, 'scale':scale, 'period_us':p['period_us'], 'tolerance':tolerance,
                'lower_ms':low, 'upper_ms':high, 'sigma_lower_ms':sigma_low, 'sigma_upper_ms':sigma_high,
                'panels':summaries, 'decoder':counters,
                'statistics_scope':'Oba końce odstępu w [start_s, end_s); alarmy tego samego CAN ID przy tych odstępach.',
                'a04_scope':'Obserwacyjny opis nieregularności w dwóch epizodach A01, bez osobnego wymuszenia A04.',
                'filter':'Oba końce każdego odstępu między znacznikami aktywnego scenariusza; żadnych luk jakości.',
                'time_axis':'Czas generatora z powiązania znacznikami; odstęp z czasu urządzenia, bez skalowania.',
                'interpretation':'Zmienność odebranych odstępów, nie pomiar błędu znacznika czasu.'}
    sources = [trace_path, profile_path, config_path] + [report / n for n in
              ('sessions.csv','episodes.csv','events_audit.csv','profile_parameters.csv','quality_audit.csv','source_manifest.csv')]
    save_metadata(output, name, metadata, sources)
    irregularity(output, dpi, plotted, summaries, metadata, sources)
    print(json.dumps(metadata['panels'], ensure_ascii=False))


def irregularity(output, dpi, plotted, summaries, metadata, sources):
    """Osobny obraz: obserwowana zmienność oraz kontrola czasów nowych alarmów.

    Wynik potwierdza zgodność reakcji z regułami w pokazanym przykładzie.
    Nie stanowi niezależnego pomiaru skuteczności wykrywania rodzaju A04.
    """
    name = '04_nieregularnosc_odstepow_A04'
    fig = plt.figure(figsize=(8.4, 7.0), facecolor='white')
    fig.text(.05, .962, 'A04 — nieregularność odstępów i reakcja detektora',
             fontsize=14, color=INK, weight='bold', va='top')
    fig.text(.05, .914, 'Obserwacja podczas E01 i E04 · CAN ID 0x067 (103) · ziarno 86, próba 01',
             fontsize=9, color=MUTED)
    ax = fig.add_axes([.22, .625, .735, .19])
    ax.axvline(0, color=TEAL, ls=':', lw=1.3, zorder=1)
    for i, row in enumerate(summaries):
        points = [p for p in plotted if p['episode'] == row['episode'] and p['included_in_statistics']]
        residuals = [p['interval_ms'] - row['planned_ms'] for p in points]
        ax.hlines(i, min(residuals), max(residuals), color='#A4B7C1', lw=1, zorder=1)
        ax.scatter(residuals, [i] * len(points), s=24, linewidth=.35, edgecolor='white',
                   c=[RED if p['outside_period_limits'] else BLUE for p in points], alpha=.8, zorder=3)
    ax.set_yticks(range(len(summaries)),
                  [f"{r['episode']} · plan {pl(r['planned_ms'], 0)} ms\nN = {r['internal_intervals']} odstępów" for r in summaries],
                  fontsize=9)
    ax.set_ylim(1.55, -.55)
    ax.set_xlim(-18, 18)
    ax.xaxis.set_major_locator(MultipleLocator(5))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: pl(x, 0)))
    ax.grid(axis='x', color=GRID, lw=.7)
    ax.set_axisbelow(True)
    ax.spines['left'].set_visible(False)
    ax.tick_params(axis='y', length=0, pad=12)
    ax.set_xlabel('Różnica względem planowanego odstępu [ms]', labelpad=8)
    fig.legend(handles=[Line2D([], [], marker='o', ls='', color=BLUE, markersize=4, label='Zarejestrowany odstęp'),
                        Line2D([], [], marker='o', ls='', color=RED, markersize=4, label='Poza progami reguły okresu'),
                        Line2D([], [], color=TEAL, ls=':', label='Wartość planowana')],
               loc='upper left', bbox_to_anchor=(.04, .884), ncol=3, frameon=False, fontsize=8,
               columnspacing=1.4)

    fig.text(.05, .51, 'Zgodność początków naruszeń z zapisanymi alarmami',
             fontsize=10, color=INK, weight='bold')
    tax = fig.add_axes([.05, .30, .90, .19])
    tax.axis('off')
    cells, exported = [], []
    for row in summaries:
        for key, title in [('period', 'Odchylenie okresu'), ('sigma', 'Odchylenie statystyczne (3σ)')]:
            outside = row[f'{key}_exceedances']
            onsets = row[f'{key}_expected_onsets']
            matched = row[f'{key}_matched_onsets']
            cells.append([f"{row['episode']} · {title}", str(outside), str(onsets), f'{matched}/{onsets}'])
            exported.append({'episode':row['episode'], 'rule':key, 'evaluated_intervals':row['internal_intervals'],
                             'intervals_outside_limits':outside, 'expected_new_onsets':onsets,
                             'recorded_new_alarms':row[f'{key}_new_alarms'], 'matched_onsets':matched})
    table = tax.table(cellText=cells,
                      colLabels=['Epizod i reguła', 'Odstępy\npoza progiem', 'Początki\nnaruszenia',
                                 'Potwierdzone alarmy /\npoczątki naruszenia'],
                      colWidths=[.38, .18, .18, .26], cellLoc='center', bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(8)
    for (i, j), cell in table.get_celld().items():
        cell.set_edgecolor('white')
        cell.set_linewidth(2)
        cell.set_facecolor(SHADE if i == 0 else '#F3F6F8' if i in (1, 2) else 'white')
        cell.get_text().set_color(INK)
        if i == 0:
            cell.get_text().set_weight('bold')
            cell.get_text().set_fontsize(7.8)
        elif j == 3:
            cell.set_facecolor('#E9F3ED')
            cell.get_text().set_color('#286147')
            cell.get_text().set_weight('bold')

    fig.text(.05, .266, 'Odstęp poza progiem: jedna zaobserwowana przerwa niespełniająca warunku danej reguły.', fontsize=8, color=MUTED)
    fig.text(.05, .241, 'Początek naruszenia: pierwszy taki odstęp po odstępie prawidłowym; wtedy wymagany jest nowy alarm.', fontsize=7.8, color=MUTED)
    fig.text(.05, .216, 'Kolejne odstępy poza progiem podtrzymują naruszenie i nie wymagają ponawiania alarmu.', fontsize=8, color=MUTED)
    fig.text(.05, .18, 'Potwierdzono zgodność czasów wszystkich wymaganych alarmów z początkami naruszeń w śladzie.',
             fontsize=8.5, color='#286147', weight='bold')
    fig.text(.05, .144, 'Planowane 80 i 125 ms mieszczą się w progach okresu. Jego alarmy potwierdzają reakcję na zmienność.', fontsize=7.8, color=MUTED)
    fig.text(.05, .119, 'Reguła 3σ reaguje także na zmianę średniego odstępu; nie potwierdza wyłącznie nieregularności.', fontsize=7.8, color=MUTED)
    fig.text(.05, .085, 'Zakres: oba końce odstępu wewnątrz E01/E04 jednej sesji. Nie wykonano osobnego wymuszenia A04.', fontsize=7.8, color=MUTED)
    fig.text(.05, .061, f'Sesja: {SESSION}. Wynik potwierdza działanie reguł w tym przykładzie.', fontsize=7.8, color=MUTED)
    fig.text(.05, .026, 'Źródło: opracowanie własne na podstawie śladu CAN, profilu odniesienia i zapisu alarmów.',
             fontsize=7.8, color=MUTED, style='italic')
    save(fig, output, name, dpi)
    with (output / f'{name}_dane.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(exported[0]))
        writer.writeheader(); writer.writerows(exported)
    save_metadata(output, name, {**metadata, 'alarm_verification':exported,
                  'evidence':'Równość zbioru czasów przejść w naruszenie ze śladu i zbioru czasów zapisanych alarmów.',
                  'scope':'Sprawdzenie realizacji warunków reguł; nie niezależny pomiar skuteczności A04.'}, sources)


def background(report, output, dpi):
    name = '03_alarmy_tla_po_wylaczeniach'
    rows = {r['method']:r for r in read_csv(report / 'method_summary.csv')}
    sessions = [s for s in read_csv(report / 'sessions.csv') if s['kind'] == 'matrix']
    if len(sessions) != 19:
        raise ValueError('Oczekiwano 19 sesji macierzy.')
    duration = sum(float(s['background_clean_duration_s']) for s in sessions) / 60
    events = [e for e in read_csv(report / 'events_audit.csv') if e['kind'] == 'matrix'
              and e['category'] == 'background_candidate' and e['quality_masked'] == 'False']
    session_map = {s['session']:s for s in sessions}
    if any(not 0 <= float(e['relative_s']) <= float(session_map[e['session']]['duration_s']) for e in events):
        raise ValueError('Alarm tła poza aktywnym scenariuszem.')
    fig, ax = plt.subplots(figsize=BACKGROUND_SIZE)
    fig.subplots_adjust(left=.36, right=.955, top=.79, bottom=.24)
    fig.text(.045, .954, 'Fałszywe alarmy w ruchu referencyjnym', fontsize=14,
             weight='bold', color=INK, va='top')
    fig.text(.045, .879, f'19 sesji pomiarowych · sumaryczny czas ruchu referencyjnego: {pl(duration)} min', fontsize=10, color=MUTED)
    checked = []
    for i, (method, label) in enumerate(BACKGROUND_METHODS):
        r = rows[method]
        count, rate = int(r['clean_background_events']), float(r['clean_background_events_per_minute'])
        if (count != sum(int(s['background_clean_' + method]) for s in sessions)
                or count != sum(e['method'] == method for e in events)
                or not math.isclose(duration, float(r['clean_background_minutes']))
                or not math.isclose(rate, count / duration)):
            raise ValueError(f'Niezgodny audyt tła: {method}')
        color = BLUE if method == 'three_sigma' else '#81AFC3'
        ax.barh(i, rate, height=.57, color=color, zorder=3)
        if count == 0:
            ax.plot(0, i, 'o', ms=3.5, color='#9DAEB9', clip_on=False, zorder=4)
        ax.text(rate + .10, i, f'{pl(rate)}  (n = {count})', va='center', fontsize=9, color=INK)
        checked.append({'method':method, 'clean_background_events':count,
                        'clean_background_minutes':duration, 'clean_background_events_per_minute':rate})
    if int(rows['three_sigma']['clean_background_events']) != 115:
        raise ValueError('Oczekiwano 115 alarmów 3σ po wyłączeniach.')
    ax.set_yticks(range(len(BACKGROUND_METHODS)), [p[1] for p in BACKGROUND_METHODS], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 6.15)
    ax.set_xlabel('Średnia liczba alarmów na minutę ruchu referencyjnego', labelpad=10, color=INK)
    ax.set_xticks(range(6))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: pl(x, 0)))
    ax.grid(axis='x', color=GRID, lw=.7)
    ax.set_axisbelow(True)
    ax.spines['left'].set_visible(False)
    ax.tick_params(axis='y', length=0, pad=10)
    fig.text(.045, .117, 'n — liczba nowych alarmów', fontsize=8, color=MUTED)
    fig.text(.045, .078, 'Tło: aktywny scenariusz poza wymuszeniami i ich marginesami; po zastosowaniu maski jakości.', fontsize=8, color=MUTED)
    fig.text(.045, .031, 'Źródło: opracowanie własne na podstawie zestawienia kampanii pomiarowej.', fontsize=8, color=MUTED, style='italic')
    save(fig, output, name, dpi)
    with (output / f'{name}_dane.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(checked[0]))
        writer.writeheader(); writer.writerows(checked)
    save_metadata(output, name, {'sessions':[s['session'] for s in sessions], 'values':checked,
                  'filter':'kind=matrix; background_candidate; quality_masked=False',
                  'duration_definition':'[3.05, duration-0.05] s minus epizody, margines 0.05 s przed i 1.05 s po, minus maski jakości.'},
                  [report / n for n in ('method_summary.csv','sessions.csv','events_audit.csv','quality_audit.csv')])
    print(f'Tło po wyłączeniach: {duration:.6f} min; 3σ: 115 alarmów; częstotliwość: {rows["frequency"]["clean_background_events"]}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--report-dir', type=Path, default=REPORT)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--figure', choices=('timing','background','both'), default='both')
    parser.add_argument('--dpi', type=int, default=300)
    args = parser.parse_args()
    output = args.output_dir or args.report_dir / 'figures'
    output.mkdir(exist_ok=True, parents=True)
    plt.rcParams.update({'font.family':'DejaVu Sans', 'font.size':9, 'pdf.fonttype':42,
                         'axes.spines.top':False, 'axes.spines.right':False,
                         'axes.edgecolor':'#B9C8D1', 'xtick.color':MUTED, 'ytick.color':MUTED,
                         'axes.labelcolor':INK})
    if args.figure in ('timing','both'):
        timing(args.report_dir, output, args.dpi)
    if args.figure in ('background','both'):
        background(args.report_dir, output, args.dpi)
    print(f'Zapisano PNG, PDF, CSV i metadane w: {output}')


if __name__ == '__main__':
    main()
