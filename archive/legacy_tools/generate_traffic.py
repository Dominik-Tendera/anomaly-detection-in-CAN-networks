#!/usr/bin/env python3
"""CLI do generowania ruchu CAN przez USB-CAN Viewer (SLCAN)."""

from __future__ import annotations
import argparse, sys, time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.dbc import load_dbc
from can_generate.slcan import SLCANClient
from can_generate.traffic import ReferenceMessage, ReferenceTrafficGenerator

def calculate_representable_value(signal) -> float:
    """Oblicz wartość sygnału możliwą do zakodowania w jego skali DBC.

    Dla sygnału ze skalą i offsetem, wartość fizyczna value musi spełniać:
    raw = (value - offset) / scale
    gdzie raw musi być liczbą całkowitą.
    """
    if not signal.has_reference_range:
        return 0.0

    # Użyj środka zakresu
    middle = (signal.minimum + signal.maximum) / 2

    # Sprawdź czy scale != 0 (ochrona przed dzieleniem przez 0)
    if signal.scale == 0:
        return signal.minimum  # Fallback do minimum

    # Oblicz wartość raw dla środka
    raw_float = (middle - signal.offset) / signal.scale
    # Zaokrąglij do najbliższej liczby całkowitej
    raw_int = round(raw_float)
    # Przelicz z powrotem na wartość fizyczną
    representable = raw_int * signal.scale + signal.offset

    # Upewnij się że wartość mieści się w zakresie
    return max(signal.minimum, min(signal.maximum, representable))

def build_reference_traffic(database) -> list[ReferenceMessage]:
    """Zbuduj ruch referencyjny na podstawie załadowanej bazy DBC."""
    messages = []
    for msg in database.messages.values():
        period = 0.1  # Domyślny okres 100ms
        signal_values = {}
        for signal in msg.signals:
            signal_values[signal.name] = calculate_representable_value(signal)

        messages.append(ReferenceMessage(
            can_id=msg.can_id,
            period=period,
            signal_values=signal_values
        ))

    if not messages:
        raise ValueError("Plik DBC nie zawiera żadnych wiadomości")
    return messages

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dbc", required=True, type=Path)
    parser.add_argument("--port", required=True)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--session", default=None)
    parser.add_argument("--output", type=Path, default=Path("captures"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--bitrate", type=int, default=1000000)
    args = parser.parse_args()

    if not args.dbc.exists():
        print(f"BŁĄD: Plik DBC nie istnieje: {args.dbc}", file=sys.stderr)
        return 1

    print(f"Wczytuję DBC: {args.dbc}")
    try:
        database = load_dbc(str(args.dbc))
        messages = build_reference_traffic(database)
        print(f"Znaleziono {len(messages)} wiadomości w DBC")
    except Exception as e:
        print(f"BŁĄD podczas budowania ruchu: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    session_id = args.session or time.strftime("%Y%m%d_%H%M%S")
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"\nŁączę z USB-CAN Viewer: {args.port} @ {args.bitrate} bit/s")
    print("(USB-CAN Viewer musi być już uruchomiony!)")

    try:
        slcan = SLCANClient(port=args.port)
        slcan.connect()
        slcan.open(bitrate=args.bitrate)

        try:
            print("✓ Połączono!")
            generator = ReferenceTrafficGenerator(database, messages, seed=args.seed)
            print(f"\n→ Rozpoczynam sesję ({args.duration}s)...")
            print("  (Naciśnij Ctrl+C aby przerwać)\n")

            run = generator.run_session(
                duration=args.duration,
                sender=slcan,
                session_id=session_id
            )

            print("\n" + "="*60)
            print("PODSUMOWANIE")
            print("="*60)
            print(f"Identyfikator sesji:    {session_id}")
            print(f"Czas trwania:           {run.duration:.1f}s")
            print(f"Ramek zaplanowanych:    {run.frames_requested}")
            print(f"Ramek wysłanych:        {run.frames_sent}")
            print(f"Ramek niewysłanych:     {run.frames_not_sent}")
            print(f"Osiągnięta częstość:    {run.achieved_rate:.1f} ramek/s")

            print("\nRamek per CAN ID:")
            for can_id, count in sorted(run.counts_by_id.items()):
                print(f"  0x{can_id:03X}: {count:4d}")

            if run.warnings:
                print("\n⚠ OSTRZEŻENIA:")
                for w in run.warnings:
                    print(f"  {w}")

            print("="*60)

            if run.frames_not_sent > 0:
                print("\n⚠ UWAGA: Nie wszystkie ramki zostały wysłane!")
                return 1

            print("\n✓ Sesja zakończona pomyślnie!")
            return 0
        finally:
            slcan.close()

    except KeyboardInterrupt:
        print("\n\n⚠ Przerwano przez użytkownika")
        return 130
    except Exception as e:
        print(f"\n✗ BŁĄD: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
