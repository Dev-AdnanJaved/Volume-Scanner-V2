"""
Flatten all signal data (active + archived) into a single CSV file.

Each signal becomes one row with all fields as columns, including:
  - Root-level signal fields (symbol, entry_price, etc.)
  - All additional_data fields (prefixed with add_)
  - All outcome fields (prefixed with out_)
  - All TP snapshot fields (prefixed with tp{level}_ e.g. tp10_oi_change_pct)
  - Price journey summary stats (journey_count, journey_events)

Usage:
  python export_csv.py                    # exports to data/signals_export.csv
  python export_csv.py --out my_file.csv  # custom output path
  python export_csv.py --active-only      # only active signals
  python export_csv.py --history-only     # only archived signals
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")

SKIP_FIELDS = {
    "_prev_highest", "_prev_lowest", "last_update_ts",
}

LIST_AS_STRING_FIELDS = {
    "candle_colors", "tp_sent", "candle_colors_at_hit",
}


def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def _load_gzip_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError, OSError):
        return []


def load_all_signals(active: bool = True, history: bool = True) -> list:
    signals = []

    if active:
        signals.extend(_load_json(DATA_DIR / "signals.json"))

    if history:
        signals.extend(_load_json(DATA_DIR / "history.json"))

        for gz_file in sorted(DATA_DIR.glob("signals_*.json.gz")):
            signals.extend(_load_gzip_json(gz_file))

    return signals


def _flatten_value(val: Any) -> Any:
    if val is None:
        return ""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, list):
        return "|".join(str(v) for v in val)
    if isinstance(val, dict):
        return json.dumps(val, separators=(",", ":"))
    return str(val)


def flatten_signal(sig: dict) -> dict:
    row: dict = {}

    for key, val in sig.items():
        if key in SKIP_FIELDS:
            continue
        if key == "additional_data" and isinstance(val, dict):
            for ak, av in val.items():
                row[f"add_{ak}"] = _flatten_value(av)
            continue
        if key == "outcome" and isinstance(val, dict):
            for ok, ov in val.items():
                row[f"out_{ok}"] = _flatten_value(ov)
            continue
        if key.endswith("_snapshot") and key.startswith("tp") and isinstance(val, dict):
            prefix = key.replace("_snapshot", "_")
            for sk, sv in val.items():
                row[f"{prefix}{sk}"] = _flatten_value(sv)
            continue
        if key == "price_journey" and isinstance(val, list):
            row["journey_count"] = len(val)
            events = []
            for snap in val:
                ev = snap.get("event", "")
                if ev:
                    events.append(ev)
            row["journey_events"] = "|".join(events)
            row["price_journey_json"] = json.dumps(val, separators=(",", ":"))
            continue
        if key in LIST_AS_STRING_FIELDS and isinstance(val, list):
            row[key] = "|".join(str(v) for v in val)
            continue
        row[key] = _flatten_value(val)

    return row


def compute_fieldnames(signals: list) -> list[str]:
    rows = [flatten_signal(sig) for sig in signals]

    all_columns: list[str] = []
    seen: set = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                all_columns.append(k)

    priority_cols = [
        "symbol", "alert_time", "alert_time_ts", "timeframe",
        "entry_price", "current_price", "highest_price", "lowest_price",
        "peak_pct", "lowest_pct", "exit_pct", "exit_price",
        "breakout_margin_pct", "high_breakout_warning", "high_24h",
        "price_change_24h",
        "vol_candle_1", "vol_candle_2", "vol_candle_3",
        "vol_candle_1_fmt", "vol_candle_2_fmt", "vol_candle_3_fmt",
        "vol_candle_1_base", "vol_candle_2_base", "vol_candle_3_base",
        "vol_candle_1_base_fmt", "vol_candle_2_base_fmt", "vol_candle_3_base_fmt",
        "vol_ratio", "rvol", "candle_colors",
        "btc_price", "candle_time",
    ]

    ordered: list[str] = []
    for c in priority_cols:
        if c in seen:
            ordered.append(c)

    add_cols = sorted(c for c in all_columns if c.startswith("add_") and c not in ordered)
    ordered.extend(add_cols)

    out_cols = sorted(c for c in all_columns if c.startswith("out_") and c not in ordered)
    ordered.extend(out_cols)

    tp_snap_cols = sorted(
        (c for c in all_columns if any(c.startswith(f"tp{t}_") for t in [5,10,20,30,50,75,100]) and c not in ordered),
        key=lambda x: (int(x.split("_")[0][2:]), x),
    )
    ordered.extend(tp_snap_cols)

    remaining = [c for c in all_columns if c not in set(ordered)]
    ordered.extend(sorted(remaining))

    return ordered


def build_csv(signals: list, output_path: str, fieldnames: list[str] | None = None) -> int:
    if not signals:
        logger.warning("No signals to export")
        return 0

    rows = [flatten_signal(sig) for sig in signals]

    if fieldnames is None:
        fieldnames = compute_fieldnames(signals)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    logger.info("Exported %d signals → %s (%d columns)", len(rows), output_path, len(fieldnames))
    return len(rows)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Export signals to flat CSV")
    parser.add_argument("--out", default="data/signals_export.csv", help="Output CSV path")
    parser.add_argument("--active-only", action="store_true", help="Only active signals")
    parser.add_argument("--history-only", action="store_true", help="Only archived signals")
    args = parser.parse_args()

    active = not args.history_only
    history = not args.active_only

    signals = load_all_signals(active=active, history=history)
    if not signals:
        print("No signals found.")
        sys.exit(0)

    count = build_csv(signals, args.out)
    print(f"Done: {count} signals exported to {args.out}")


if __name__ == "__main__":
    main()
