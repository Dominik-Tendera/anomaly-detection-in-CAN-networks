"""Mapa reakcji reguł na kontrolowane wymuszenia anomalii (bez testu integracyjnego).

Źródło: episodes.csv, zweryfikowane względem scenario_summary.csv.
Punkt wyjścia: render_results_report.py. Nie uruchamia analizy kampanii ani
nie modyfikuje plików LaTeX lub poprzednich rysunków.

Uruchomienie (Python z biblioteką matplotlib):
    python -X utf8 results/render_detection_heatmap.py
    python -X utf8 results/render_detection_heatmap.py --dpi 300 --output-dir WYBRANY_KATALOG

Nazwy, kolory i rozmiary można zmieniać w sekcjach poniżej.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.patches import Rectangle


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "results" / "reports" / "campaign_2026-09-20"
OUTPUT_NAME = "01_macierz_reakcji_regul"

# Kolejność kolumn: pięć reguł czasowych, trzy kontrole DBC, wynik łączny.
COLUMNS = [
    ("timeout", "Brak oczekiwanej\nramki", ("missing_frame",)),
    ("period", "Odchylenie\nokresu", ("period_violation",)),
    ("three_sigma", "Odchylenie\nstatystyczne (3σ)", ("statistical_deviation",)),
    ("frequency", "Zmiana\nczęstotliwości", ("frequency_increase", "frequency_decrease")),
    ("burst", "Krótka seria\nramek", ("burst",)),
    ("unknown_id", "Nieznany\nidentyfikator", ("unknown_id",)),
    ("dlc_mismatch", "Niezgodne\nDLC", ("dlc_mismatch",)),
    ("signal_out_of_range", "Przekroczenie\nzakresu", ("signal_out_of_range",)),
    ("any", "Co najmniej\njedna\nreguła", ()),
]

# Nazwy rodzajów są zgodne z tabelą A w pracy. Brak A04 i A08–A11
# oznacza brak dedykowanych wymuszeń w tej macierzy, a nie wynik zerowy.
GROUPS = [
    ("A01", "Zmiana częstotliwości\nwystępowania ramek", 0, 6),
    ("A02", "Zanik ramek\ndanego identyfikatora", 6, 8),
    ("A03", "Krótka seria ramek\n(burst)", 8, 10),
    ("A05", "Nieznany\nidentyfikator", 10, 12),
    ("A06", "Niezgodna długość\npola danych", 12, 14),
    ("A07", "Wartość sygnału\npoza zakresem", 14, 17),
]

PRIMARY_COLUMN = {
    **{f"E{i:02}": 3 for i in range(1, 7)},
    "E07": 0, "E08": 0, "E09": 4, "E10": 4,
    "E11": 5, "E12": 5, "E13": 6, "E14": 6,
    "E15": 7, "E16": 7, "E17": 7,
}

# Styl: jedna skala kolorystyczna we wszystkich kolumnach.
INK = "#182F41"
MUTED = "#556774"
SEPARATOR = "#B9C8D1"
OUTLINE = "#D78622"
RETURN_OUTLINE = "#78669A"
COLORS = ["#F5F7F9", "#D1E4EC", "#8CB9CC", "#427D9C", "#174B6B"]
FIGSIZE = (8.4, 9.9)
CELL_FONT = 9.0
ROW_FONT = 8.6
HEADER_FONT = 8.1
GROUP_FONT = 8.3


def truth(value: str) -> bool:
    if value not in ("True", "False"):
        raise ValueError(f"Nieprawidłowa wartość logiczna: {value!r}")
    return value == "True"


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def decimal(value: float) -> str:
    return f"{value:g}".replace(".", ",").replace("-", "−")


def variant_label(row: dict) -> str:
    """Parametry podpisu pochodzą z danych, a nie z wcześniejszego obrazu."""
    kind = row["type"]
    p = row["parameters"]
    if kind == "frequency_increase":
        return f"wzrost {decimal(p['frequency_factor'])}×"
    if kind == "frequency_decrease":
        return f"spadek do {decimal(p['frequency_factor'])}×"
    if kind == "disappearance":
        return f"zanik na {decimal(float(row['end_s']) - float(row['start_s']))} s"
    if kind == "burst":
        noun = "ramki" if p["count"] == 4 else "ramek"
        return f"{p['count']} {noun} co {decimal(p['interval_s'] * 1000)} ms"
    if kind == "unknown_dbc_id":
        return "pojedyncza ramka" if p["count"] == 1 else f"{p['count']} ramek co {decimal(p['interval_s'] * 1000)} ms"
    if kind == "dlc_mismatch":
        difference = p["injected_dlc"] - p["dbc_dlc"]
        if abs(difference) != 1:
            raise ValueError("Oczekiwano zmiany długości danych o jeden bajt.")
        return "krótsze o 1 bajt" if difference < 0 else "dłuższe o 1 bajt"
    if kind == "out_of_range_signal":
        value = p["physical_value"]
        boundary = p["maximum"] if p["direction"] == "above" else p["minimum"]
        limit = "max" if p["direction"] == "above" else "min"
        difference = value - boundary
        sign = "+" if difference >= 0 else "−"
        return f"{limit} {sign} {decimal(abs(difference))}"
    raise ValueError(f"Nieznany rodzaj wymuszenia: {kind}")


def prepare(report: Path) -> tuple[list[dict], dict]:
    all_rows = read_csv(report / "episodes.csv")
    # Jedyny zbiór wynikowy: 19 pomiarów macierzy. Smoke/integracja są wykluczone.
    rows = [r for r in all_rows if r["kind"] == "matrix"]
    if len(rows) != 323 or len({r["session"] for r in rows}) != 19:
        raise ValueError("Oczekiwano 323 epizodów z 19 sesji macierzy.")
    if len({(r["session"], r["episode"]) for r in rows}) != len(rows):
        raise ValueError("Powtórzone pary sesja–epizod.")
    if set(Counter(r["session"] for r in rows).values()) != {17}:
        raise ValueError("Każda sesja musi obejmować 17 epizodów.")
    for row in rows:
        row["event_types"] = json.loads(row["event_types"])
        row["parameters"] = json.loads(row["parameters"])
        row["delayed_methods"] = json.loads(row["delayed_methods"])
        for name in ("primary_onset", "any_onset"):
            row[name] = truth(row[name])
    summary = {r["episode"]: r for r in read_csv(report / "scenario_summary.csv")}
    prepared = []
    for i in range(1, 18):
        episode = f"E{i:02}"
        subset = [r for r in rows if r["episode"] == episode]
        if len(subset) != 19:
            raise ValueError(f"{episode}: nieprawidłowy mianownik")
        labels = {variant_label(r) for r in subset}
        if len(labels) != 1:
            raise ValueError(f"{episode}: różne parametry wymuszenia: {labels}")
        counts = []
        for key, _, event_types in COLUMNS:
            if key == "any":
                value = sum(r["any_onset"] for r in subset)
            else:
                value = sum(any(r["event_types"].get(t, 0) > 0 for t in event_types) for r in subset)
            counts.append(value)
        # Zgodność rozbicia DBC i pozostałych kolumn z istniejącym audytem.
        s = summary[episode]
        for column in range(5):
            if counts[column] != int(s[COLUMNS[column][0]]):
                raise ValueError(f"{episode}: rozbieżność {COLUMNS[column][0]}")
        dbc_union = sum(any(r["event_types"].get(t, 0) for t in ("unknown_id", "dlc_mismatch", "signal_out_of_range")) for r in subset)
        primary = sum(r["primary_onset"] for r in subset)
        if (dbc_union != int(s["dbc"]) or counts[-1] != int(s["any_onsets"])
                or primary != int(s["primary_onsets"]) or int(s["runs"]) != 19):
            raise ValueError(f"{episode}: rozbieżność z scenario_summary.csv")
        # W A01 alarm reguły podstawowej musi mieć kierunek zgodny z wymuszeniem.
        # Dla tej kampanii oba sposoby zliczenia są zgodne; przerwij po zmianie danych.
        if counts[PRIMARY_COLUMN[episode]] != primary:
            raise ValueError(f"{episode}: kolumna reguły podstawowej obejmuje także inne reakcje.")
        if any(bool(r["event_types"]) != r["any_onset"] for r in subset):
            raise ValueError(f"{episode}: niespójny alarm łączny")
        delayed = {method: sum(r["delayed_methods"].get(method, 0) > 0 for r in subset)
                   for method in ("period", "three_sigma")} if episode in ("E07", "E08") else {}
        if delayed and (counts[1:3] != [0, 0] or set(delayed.values()) != {19}):
            raise ValueError("Zmień opis reakcji po zaniku: dane różnią się od 0/19 i 19/19.")
        prepared.append({"episode": episode, "label": labels.pop(), "counts": counts, "runs": 19,
                         "primary": primary, "after_return": delayed})
    any_total = sum(r["counts"][-1] for r in prepared)
    primary_total = sum(r["primary"] for r in prepared)
    if (any_total, primary_total) != (311, 256):
        raise ValueError(f"Nieoczekiwane sumy: {any_total}, {primary_total}")
    metadata = {
        "sources": {name: hashlib.sha256((report / name).read_bytes()).hexdigest()
                    for name in ("episodes.csv", "scenario_summary.csv")},
        "filter": "kind == matrix", "sessions": sorted({r["session"] for r in rows}),
        "episodes": len(rows), "excluded_integration_episodes": sum(r["kind"] == "smoke" for r in all_rows),
        "new_alarm_any_rule": any_total, "new_alarm_primary_rule": primary_total,
        "after_return_episodes": {r["episode"]: r["after_return"] for r in prepared if r["after_return"]},
        "after_return_note": "Dodatkowe oznaczenie; nie zmienia komórek, kolorów ani sum głównej oceny.",
        "assignment": "event_types / any_onset; bez core_*, bez ponownego przypisywania czasów",
        "quality": "pełny zbiór 323 epizodów, bez wykluczania epizodów nakładających się na problemy jakości",
        "unit": "epizody z nowym alarmem / liczba sesji pomiarowych",
        "software": {"matplotlib": matplotlib.__version__},
    }
    return prepared, metadata


def render(rows: list[dict], output: Path, dpi: int) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "pdf.fonttype": 42, "axes.unicode_minus": True})
    fig = plt.figure(figsize=FIGSIZE, facecolor="white")
    # Własny układ umożliwia czytelne grupowanie A oraz E bez długich etykiet osi.
    ax = fig.add_axes([0.025, 0.26, 0.955, 0.63])
    ax.set_xlim(-5.5, 9.2)
    ax.set_ylim(17.15, -4.9)
    ax.axis("off")
    cmap = LinearSegmentedColormap.from_list("can_blue", COLORS)
    norm = Normalize(0, 1)

    fig.text(0.035, 0.958, "Zestawienie wyników działania reguł detekcji", fontsize=15,
             color=INK, weight="bold", va="top")
    fig.text(0.035, 0.924, "19 sesji pomiarowych · 17 wariantów · 323 epizody", fontsize=10, color=MUTED)

    # Nagłówki grup tuż nad komórkami, ukośne nazwy reguł ponad nimi.
    ax.text(-5.35, -.4, "Rodzaj anomalii", fontsize=8.5, color=MUTED, weight="bold", va="center")
    ax.text(-2.67, -.4, "Wariant wymuszenia", fontsize=8.5, color=MUTED, weight="bold", va="center")
    for x, width, title in [(0, 5, "REGUŁY CZASOWE"), (5, 3, "ZGODNOŚĆ Z DBC"), (8, 1, "ŁĄCZNIE")]:
        ax.add_patch(Rectangle((x, -.8), width, 0.8, facecolor="#EAF0F4", edgecolor="white", lw=2))
        ax.text(x + width / 2, -.4, title, ha="center", va="center", color=INK,
                fontsize=7.3 if width == 1 else 8, weight="bold")
    for j, (_, label, _) in enumerate(COLUMNS):
        ax.text(j + 0.15, -1.07, label, rotation=48, ha="left", va="bottom",
                rotation_mode="anchor", fontsize=HEADER_FONT, color=INK, linespacing=1.08)

    for group_index, (code, title, start, end) in enumerate(GROUPS):
        if group_index % 2 == 0:
            ax.add_patch(Rectangle((-5.45, start), 5.42, end - start, facecolor="#F4F6F8", edgecolor="none"))
        middle = (start + end) / 2
        ax.text(-5.3, middle - 0.43, code, color=INK, weight="bold", fontsize=10, va="bottom")
        ax.text(-5.3, middle - 0.31, title, color=INK, fontsize=GROUP_FONT, va="top", linespacing=1.22)
        ax.plot([-5.45, 9.0], [start, start], color=SEPARATOR, lw=0.8, zorder=5)

    variant_texts = []
    for i, row in enumerate(rows):
        ax.text(-2.67, i + 0.5, row["episode"], color=MUTED, weight="bold", fontsize=8.3, va="center")
        variant_texts.append(ax.text(-1.98, i + 0.5, row["label"], color=INK, fontsize=ROW_FONT, va="center"))
        for j, count in enumerate(row["counts"]):
            fraction = count / row["runs"]
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=cmap(norm(fraction)), edgecolor="white", lw=1.25))
            color = "white" if fraction >= 0.65 else (MUTED if count == 0 else INK)
            ax.text(j + 0.5, i + 0.5, f"{count}/{row['runs']}", ha="center", va="center",
                    color=color, fontsize=CELL_FONT, weight="bold" if j == 8 else "normal")
        primary = PRIMARY_COLUMN[row["episode"]]
        ax.add_patch(Rectangle((primary + 0.045, i + 0.065), 0.91, 0.87,
                               fill=False, edgecolor=OUTLINE, lw=1.5, zorder=6))
        if row["after_return"]:
            for column in (1, 2):
                ax.add_patch(Rectangle((column + .045, i + .065), .91, .87,
                                      fill=False, edgecolor=RETURN_OUTLINE,
                                      lw=1.3, linestyle=(0, (3, 2)), zorder=6))
    ax.plot([-5.45, 9], [17, 17], color=SEPARATOR, lw=0.8)
    for boundary in (5, 8):
        ax.plot([boundary, boundary], [0, 17], color="white", lw=3.5, zorder=5)

    # Jeden rozmiar wszystkich podpisów, dopasowany do najdłuższego wariantu.
    # Zachowaj odstęp przed pierwszą komórką również po zmianie etykiet.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    available = ax.transData.transform((-0.13, 0))[0] - ax.transData.transform((-1.98, 0))[0]
    widest = max(text.get_window_extent(renderer).width for text in variant_texts)
    for text in variant_texts:
        text.set_fontsize(ROW_FONT * min(1, available / widest))

    # Legenda i skala z jednoznaczną jednostką; tekst również dla komórek zerowych.
    cax = fig.add_axes([0.035, 0.196, 0.37, 0.014])
    cb = fig.colorbar(matplotlib.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, orientation="horizontal")
    cb.set_ticks([0, .25, .5, .75, 1], labels=["0%", "25%", "50%", "75%", "100%"])
    cb.ax.tick_params(labelsize=8, length=0, pad=4, colors=MUTED)
    cb.outline.set_visible(False)
    fig.text(0.035, 0.226, "Udział epizodów z nowym alarmem", fontsize=8.5, color=INK)
    fig.add_artist(Rectangle((0.51, 0.193), .025, .02, transform=fig.transFigure,
                            fill=False, edgecolor=OUTLINE, lw=1.5))
    fig.text(0.548, 0.203, "Reguła podstawowa przyjęta do oceny", fontsize=8, va="center", color=INK)
    fig.add_artist(Rectangle((.035, .151), .025, .02, transform=fig.transFigure,
                            fill=False, edgecolor=RETURN_OUTLINE, lw=1.3, linestyle=(0, (3, 2))))
    fig.text(.075, .161, "E07 i E08: po powrocie ramek alarm w 19/19 prób każdej reguły i wariantu.",
             fontsize=8, color=INK, va="center")
    fig.text(.035, .131, "Komórka i kolor: epizody z nowym alarmem / 19 prób; dla A02 — alarm podczas zaniku.", fontsize=8, color=MUTED)
    fig.text(.035, .109, "Reguła braku oczekiwanej ramki reaguje już podczas ciszy; reguły odstępów wymagają powrotu ramki.", fontsize=7.8, color=MUTED)
    fig.text(.035, .087, "A04: obserwowaną nieregularność odstępów i reakcje reguł przedstawiono na wykresie odstępów.", fontsize=7.8, color=MUTED)
    fig.text(.035, .065, "Brak osobnych wymuszeń A04 i A08–A11. Pełny zbiór: 323 epizody. Test integracyjny wyłączono.", fontsize=7.8, color=MUTED)
    fig.text(0.035, 0.035, "Źródło: opracowanie własne na podstawie danych kampanii pomiarowej.", fontsize=8, color=MUTED, style="italic")
    for suffix in ("png", "pdf"):
        fig.savefig(output / f"{OUTPUT_NAME}.{suffix}", dpi=dpi, facecolor="white")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args()
    output = args.output_dir or args.report_dir / "figures"
    rows, metadata = prepare(args.report_dir)
    output.mkdir(parents=True, exist_ok=True)
    render(rows, output, args.dpi)
    # Mała tabela kontrolna ułatwia sprawdzenie i późniejsze zmiany rysunku.
    with (output / f"{OUTPUT_NAME}_dane.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["wariant", "opis", "liczba_realizacji"] + [c[0] for c in COLUMNS]
                        + ["regula_podstawowa", "period_po_powrocie", "three_sigma_po_powrocie"])
        for row in rows:
            writer.writerow([row["episode"], row["label"], row["runs"]] + row["counts"]
                            + [COLUMNS[PRIMARY_COLUMN[row["episode"]]][0], row["after_return"].get("period", ""),
                               row["after_return"].get("three_sigma", "")])
    (output / f"{OUTPUT_NAME}_metadane.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Zweryfikowano 19 sesji, 323 epizody; nowy alarm: {metadata['new_alarm_any_rule']}/323; reguła podstawowa: {metadata['new_alarm_primary_rule']}/323.")
    print(f"Wyniki: {output / OUTPUT_NAME}.png oraz .pdf")


if __name__ == "__main__":
    main()
