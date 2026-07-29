"""Measure adverse/favorable excursion for short strategy candidates."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable


DB_FILE = Path(__file__).resolve().parent / "storage" / "radar_history.sqlite3"
HORIZONS = (4, 12, 24)


def main() -> None:
    rows = load_rows()
    samples = attach_paths(rows)
    print_overview(rows)
    rules: list[tuple[str, int, Callable[[dict[str, Any]], bool]]] = [
        (
            "A_high_oi_4h",
            4,
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 20
            and n(r, "price_change_1h") > -3,
        ),
        (
            "A_strong_up20_12h",
            12,
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 20
            and n(r, "price_change_1h") > -3,
        ),
        (
            "B_high_neg_funding_12h",
            12,
            lambda r: n(r, "funding_rate") <= -0.001
            and n(r, "price_position_24h") >= 80
            and n(r, "quote_volume_24h") >= 30_000_000
            and n(r, "price_change_1h") > -3,
        ),
        (
            "New_high_up10_neg_funding_12h",
            12,
            lambda r: n(r, "funding_rate") <= -0.0003
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 10
            and n(r, "price_change_1h") > -3,
        ),
        (
            "High_up20_vol50_12h",
            12,
            lambda r: n(r, "price_position_24h") >= 90
            and n(r, "price_change_24h") >= 20
            and n(r, "quote_volume_change_24h") >= 50
            and n(r, "price_change_1h") > -3,
        ),
    ]

    print("\nRULE\tH\tN\tSHORT_AVG\tSHORT_MED\tWIN%\tMAE_MED\tMAE_P75\tMAE_P90\tMFE_MED\tMFE_P75\tMFE_P90")
    for name, horizon, fn in rules:
        hits = dedupe([row for row in samples if fn(row)], hours=6)
        values = [row for row in hits if row.get(f"return_{horizon}h_pct") is not None]
        if not values:
            continue
        short_returns = [-row[f"return_{horizon}h_pct"] for row in values]
        maes = [row[f"short_mae_{horizon}h_pct"] for row in values]
        mfes = [row[f"short_mfe_{horizon}h_pct"] for row in values]
        win = sum(1 for value in short_returns if value > 0) / len(short_returns) * 100
        print(
            f"{name}\t{horizon}\t{len(values)}"
            f"\t{mean(short_returns):.2f}\t{median(short_returns):.2f}\t{win:.1f}"
            f"\t{median(maes):.2f}\t{pct(maes, 75):.2f}\t{pct(maes, 90):.2f}"
            f"\t{median(mfes):.2f}\t{pct(mfes, 75):.2f}\t{pct(mfes, 90):.2f}"
        )

    print("\nSTOP_SURVIVAL")
    print("RULE\tH\tSTOP\tNOT_STOPPED%\tFINAL_WIN_IF_NOT_STOPPED%\tAVG_IF_NOT_STOPPED")
    for name, horizon, fn in rules:
        hits = dedupe([row for row in samples if fn(row)], hours=6)
        values = [row for row in hits if row.get(f"return_{horizon}h_pct") is not None]
        for stop in (4, 6, 8, 10, 12):
            kept = [row for row in values if row[f"short_mae_{horizon}h_pct"] <= stop]
            if not values or not kept:
                continue
            short_returns = [-row[f"return_{horizon}h_pct"] for row in kept]
            final_win = sum(1 for value in short_returns if value > 0) / len(short_returns) * 100
            print(
                f"{name}\t{horizon}\t{stop}%\t{len(kept) / len(values) * 100:.1f}"
                f"\t{final_win:.1f}\t{mean(short_returns):.2f}"
            )


def load_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT timestamp_utc, symbol, price, funding_rate, oi_change_1h,
                       oi_change_24h, price_change_1h, price_change_24h,
                       price_position_24h, quote_volume_24h, quote_volume_change_24h
                FROM market_snapshots
                WHERE symbol IS NOT NULL AND price IS NOT NULL
                ORDER BY symbol, timestamp_utc
                """
            ).fetchall()
        ]
    for row in rows:
        row["dt"] = parse_time(row["timestamp_utc"])
    return rows


def attach_paths(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    out = []
    for series in by_symbol.values():
        timestamps = [row["dt"] for row in series]
        for index, row in enumerate(series):
            item = dict(row)
            entry = n(row, "price")
            for horizon in HORIZONS:
                target = row["dt"] + timedelta(hours=horizon)
                future = first_price_at_or_after(series, timestamps, index, target)
                high = highest_price_until(series, timestamps, index, target)
                low = lowest_price_until(series, timestamps, index, target)
                item[f"return_{horizon}h_pct"] = None if future is None or entry <= 0 else (future - entry) / entry * 100
                item[f"short_mae_{horizon}h_pct"] = None if high is None or entry <= 0 else (high - entry) / entry * 100
                item[f"short_mfe_{horizon}h_pct"] = None if low is None or entry <= 0 else (entry - low) / entry * 100
            out.append(item)
    return out


def first_price_at_or_after(
    series: list[dict[str, Any]], timestamps: list[datetime], start_index: int, target: datetime
) -> float | None:
    index = bisect_left(timestamps, target, lo=start_index)
    return None if index >= len(series) else n(series[index], "price")


def highest_price_until(
    series: list[dict[str, Any]], timestamps: list[datetime], start_index: int, target: datetime
) -> float | None:
    end = bisect_left(timestamps, target, lo=start_index)
    if end >= len(series):
        return None
    values = [n(row, "price") for row in series[start_index : end + 1] if row.get("price") is not None]
    return max(values) if values else None


def lowest_price_until(
    series: list[dict[str, Any]], timestamps: list[datetime], start_index: int, target: datetime
) -> float | None:
    end = bisect_left(timestamps, target, lo=start_index)
    if end >= len(series):
        return None
    values = [n(row, "price") for row in series[start_index : end + 1] if row.get("price") is not None]
    return min(values) if values else None


def dedupe(rows: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
    out = []
    last_seen: dict[str, datetime] = {}
    for row in sorted(rows, key=lambda item: (item["dt"], item["symbol"])):
        last = last_seen.get(row["symbol"])
        if last is not None and row["dt"] < last + timedelta(hours=hours):
            continue
        out.append(row)
        last_seen[row["symbol"]] = row["dt"]
    return out


def has_oi_pressure(row: dict[str, Any]) -> bool:
    return n(row, "oi_change_1h") >= 5 or n(row, "oi_change_24h") >= 25


def print_overview(rows: list[dict[str, Any]]) -> None:
    timestamps = [row["timestamp_utc"] for row in rows]
    print(f"RANGE={min(timestamps)} -> {max(timestamps)}")
    print(f"ROWS={len(rows)} SYMBOLS={len({row['symbol'] for row in rows})}")


def pct(values: list[float], percentile: int) -> float:
    clean = sorted(values)
    return clean[int((len(clean) - 1) * percentile / 100)] if clean else 0.0


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def n(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return 0.0 if value is None else float(value)


if __name__ == "__main__":
    main()
