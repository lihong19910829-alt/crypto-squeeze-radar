"""Evaluate and search short-side strategy candidates from local snapshots."""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable


DB_FILE = Path(__file__).resolve().parent / "storage" / "radar_history.sqlite3"
HORIZONS = (4, 6, 12, 24)


def main() -> None:
    rows = load_rows()
    samples = attach_future_returns(rows)
    print_overview(rows)
    evaluate_named_rules(samples)
    search_short_rules(samples)
    print_recent_candidates(samples)


def evaluate_named_rules(samples: list[dict[str, Any]]) -> None:
    rules: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("baseline_all", lambda r: True),
        (
            "current_A_high_oi_4h_short",
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 20
            and n(r, "price_change_1h") > -3
            and "多头拥挤、杠杆过热" not in tag(r),
        ),
        (
            "current_A_plus_24h_up_20",
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 20
            and n(r, "price_change_1h") > -3
            and "多头拥挤、杠杆过热" not in tag(r),
        ),
        (
            "current_B_high_neg_funding",
            lambda r: n(r, "funding_rate") <= -0.001
            and n(r, "price_position_24h") >= 80
            and n(r, "quote_volume_24h") >= 30_000_000
            and n(r, "price_change_1h") > -3,
        ),
        (
            "current_C_short_crowd_high_volume",
            lambda r: "空头拥挤" in tag(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "quote_volume_change_24h") >= 100
            and n(r, "price_change_1h") > -3,
        ),
        (
            "high_pos_oi_weak_or_neutral",
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_1h") > -3
            and not is_strong_market(r),
        ),
        (
            "high_pos_oi_weak_only",
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "price_change_1h") > -3
            and is_weak_market(r),
        ),
        (
            "high_pos_oi_volume_expansion",
            lambda r: has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and n(r, "quote_volume_change_24h") >= 50
            and n(r, "price_change_1h") > -3,
        ),
        (
            "high_pos_neg_funding_or_short_crowd",
            lambda r: n(r, "price_position_24h") >= 80
            and (n(r, "funding_rate") <= -0.0003 or "空头拥挤" in tag(r))
            and n(r, "quote_volume_24h") >= 30_000_000
            and n(r, "price_change_1h") > -3,
        ),
        (
            "failed_breakout_high_pos",
            lambda r: n(r, "price_position_24h") >= 80
            and n(r, "price_change_24h") >= 10
            and -3 < n(r, "price_change_1h") <= 0
            and n(r, "oi_change_1h") >= 0,
        ),
        (
            "crowded_long_high_oi_no_chase",
            lambda r: "多头拥挤" in tag(r)
            and has_oi_pressure(r)
            and n(r, "price_position_24h") >= 80
            and -3 < n(r, "price_change_1h") <= 1,
        ),
    ]

    print("\nNAMED_RULES_SHORT_RETURN")
    print("RULE\tRAW\tDEDUP\tH\tAVG\tMED\tWIN%\tP25\tP75")
    for name, fn in rules:
        hits = [row for row in samples if fn(row)]
        deduped = dedupe(hits, hours=6)
        for horizon in HORIZONS:
            values = [-row[f"return_{horizon}h_pct"] for row in deduped if row.get(f"return_{horizon}h_pct") is not None]
            print_summary(name, len(hits), len(deduped), horizon, values)


def search_short_rules(samples: list[dict[str, Any]]) -> None:
    base = dedupe(samples, hours=6)
    regimes = [
        ("any", lambda r: True),
        ("not_strong", lambda r: not is_strong_market(r)),
        ("weak", is_weak_market),
        ("neutral", lambda r: not is_strong_market(r) and not is_weak_market(r)),
    ]
    positions = [
        ("pos>=70", lambda r: n(r, "price_position_24h") >= 70),
        ("pos>=80", lambda r: n(r, "price_position_24h") >= 80),
        ("pos>=90", lambda r: n(r, "price_position_24h") >= 90),
    ]
    price_filters = [
        ("no_chase", lambda r: n(r, "price_change_1h") > -3),
        ("flat_or_red_1h", lambda r: -3 < n(r, "price_change_1h") <= 0),
        ("up24>=10_no_chase", lambda r: n(r, "price_change_24h") >= 10 and n(r, "price_change_1h") > -3),
        ("up24>=20_no_chase", lambda r: n(r, "price_change_24h") >= 20 and n(r, "price_change_1h") > -3),
    ]
    pressure_filters = [
        ("oi_pressure", has_oi_pressure),
        ("oi1h>=5", lambda r: n(r, "oi_change_1h") >= 5),
        ("oi24h>=25", lambda r: n(r, "oi_change_24h") >= 25),
        ("vol_chg>=50", lambda r: n(r, "quote_volume_change_24h") >= 50),
        ("vol_chg>=100", lambda r: n(r, "quote_volume_change_24h") >= 100),
        ("neg_funding", lambda r: n(r, "funding_rate") <= -0.0003),
        ("hot_pos_funding", lambda r: n(r, "funding_rate") >= 0.0003),
        ("short_crowd", lambda r: "空头拥挤" in tag(r)),
        ("long_crowd", lambda r: "多头拥挤" in tag(r)),
    ]

    scored = []
    for regime_name, regime_fn in regimes:
        for position_name, position_fn in positions:
            for price_name, price_fn in price_filters:
                for pressure_name, pressure_fn in pressure_filters:
                    name = f"{regime_name}+{position_name}+{price_name}+{pressure_name}"
                    rows = [
                        row
                        for row in base
                        if regime_fn(row) and position_fn(row) and price_fn(row) and pressure_fn(row)
                    ]
                    for horizon in (4, 12):
                        values = [
                            -row[f"return_{horizon}h_pct"]
                            for row in rows
                            if row.get(f"return_{horizon}h_pct") is not None
                        ]
                        if len(values) < 40:
                            continue
                        scored.append(score_tuple(name, horizon, values))

    print("\nTOP_SHORT_RULES_BY_MEDIAN_MIN40")
    print("H\tN\tAVG\tMED\tWIN%\tP25\tP75\tRULE")
    for med, win, avg, count, horizon, p25, p75, name in sorted(scored, reverse=True)[:30]:
        print(f"{horizon}\t{count}\t{avg:.2f}\t{med:.2f}\t{win:.1f}\t{p25:.2f}\t{p75:.2f}\t{name}")


def print_recent_candidates(samples: list[dict[str, Any]]) -> None:
    latest = max(row["timestamp_utc"] for row in samples)
    recent = [
        row
        for row in samples
        if row["timestamp_utc"] == latest
        and n(row, "price_position_24h") >= 80
        and n(row, "price_change_1h") > -3
        and (
            has_oi_pressure(row)
            or n(row, "funding_rate") <= -0.0003
            or n(row, "quote_volume_change_24h") >= 100
        )
    ]
    recent = sorted(recent, key=short_score, reverse=True)[:20]
    print("\nLATEST_SHORT_WATCHLIST")
    print("SYMBOL\tSCORE\tPOS\t1H\t24H\tOI1H\tOI24H\tFUNDING\tVOLCHG\tTAG")
    for row in recent:
        print(
            f"{row['symbol']}\t{short_score(row)}\t{n(row,'price_position_24h'):.1f}"
            f"\t{n(row,'price_change_1h'):.2f}\t{n(row,'price_change_24h'):.2f}"
            f"\t{n(row,'oi_change_1h'):.2f}\t{n(row,'oi_change_24h'):.2f}"
            f"\t{n(row,'funding_rate'):.5f}\t{n(row,'quote_volume_change_24h'):.1f}\t{tag(row)}"
        )


def short_score(row: dict[str, Any]) -> int:
    score = 0
    if n(row, "price_position_24h") >= 90:
        score += 25
    elif n(row, "price_position_24h") >= 80:
        score += 20
    if has_oi_pressure(row):
        score += 25
    if n(row, "price_change_24h") >= 20:
        score += 15
    elif n(row, "price_change_24h") >= 10:
        score += 8
    if n(row, "quote_volume_change_24h") >= 100:
        score += 12
    elif n(row, "quote_volume_change_24h") >= 50:
        score += 6
    if n(row, "funding_rate") <= -0.0003:
        score += 10
    if is_weak_market(row):
        score += 10
    if is_strong_market(row):
        score -= 20
    if n(row, "price_change_1h") <= -3:
        score -= 30
    if "多头拥挤、杠杆过热" in tag(row):
        score -= 15
    return max(0, min(100, score))


def load_rows() -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT timestamp_utc, coin, symbol, price, funding_rate, open_interest,
                       oi_change_1h, oi_change_24h, price_change_1h, price_change_4h,
                       price_change_24h, price_position_24h, quote_volume_24h,
                       quote_volume_change_24h, funding_same_sign_count,
                       funding_avg_abs_6, risk_score, anomaly_tag, source
                FROM market_snapshots
                WHERE symbol IS NOT NULL AND price IS NOT NULL
                ORDER BY symbol, timestamp_utc
                """
            ).fetchall()
        ]
    for row in rows:
        row["dt"] = parse_time(row["timestamp_utc"])
    attach_market_context(rows)
    return rows


def attach_market_context(rows: list[dict[str, Any]]) -> None:
    by_time: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_time[row["timestamp_utc"]].append(row)
    context = {}
    for timestamp, items in by_time.items():
        changes = [n(row, "price_change_24h") for row in items if row.get("price_change_24h") is not None]
        context[timestamp] = (
            median(changes) if changes else 0.0,
            sum(1 for value in changes if value > 0) / len(changes) * 100 if changes else 0.0,
        )
    for row in rows:
        row["market_median_24h"], row["market_breadth_up"] = context[row["timestamp_utc"]]


def attach_future_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    samples = []
    for series in by_symbol.values():
        timestamps = [row["dt"] for row in series]
        for index, row in enumerate(series):
            item = dict(row)
            entry = n(row, "price")
            for horizon in HORIZONS:
                future = first_price_at_or_after(series, timestamps, index, row["dt"] + timedelta(hours=horizon))
                item[f"return_{horizon}h_pct"] = None if future is None or entry <= 0 else (future - entry) / entry * 100
            samples.append(item)
    return samples


def first_price_at_or_after(
    series: list[dict[str, Any]], timestamps: list[datetime], start_index: int, target: datetime
) -> float | None:
    index = bisect_left(timestamps, target, lo=start_index)
    if index >= len(series):
        return None
    return n(series[index], "price")


def dedupe(rows: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
    output = []
    last_seen: dict[str, datetime] = {}
    for row in sorted(rows, key=lambda item: (item["dt"], item["symbol"])):
        last = last_seen.get(row["symbol"])
        if last is not None and row["dt"] < last + timedelta(hours=hours):
            continue
        output.append(row)
        last_seen[row["symbol"]] = row["dt"]
    return output


def print_overview(rows: list[dict[str, Any]]) -> None:
    timestamps = [row["timestamp_utc"] for row in rows]
    print(f"DB={DB_FILE}")
    print(f"RANGE={min(timestamps)} -> {max(timestamps)}")
    print(f"ROWS={len(rows)} SYMBOLS={len({row['symbol'] for row in rows})}")


def print_summary(name: str, raw_count: int, dedup_count: int, horizon: int, values: list[float]) -> None:
    if not values:
        print(f"{name}\t{raw_count}\t{dedup_count}\t{horizon}\tNA\tNA\tNA\tNA\tNA")
        return
    med, win, avg, count, _, p25, p75, _ = score_tuple(name, horizon, values)
    print(f"{name}\t{raw_count}\t{count}\t{horizon}\t{avg:.2f}\t{med:.2f}\t{win:.1f}\t{p25:.2f}\t{p75:.2f}")


def score_tuple(name: str, horizon: int, values: list[float]) -> tuple[float, float, float, int, int, float, float, str]:
    clean = sorted(values)
    p25 = clean[int((len(clean) - 1) * 0.25)]
    p75 = clean[int((len(clean) - 1) * 0.75)]
    win = sum(1 for value in clean if value > 0) / len(clean) * 100
    return median(clean), win, mean(clean), len(clean), horizon, p25, p75, name


def has_oi_pressure(row: dict[str, Any]) -> bool:
    return "OI异常增加" in tag(row) or n(row, "oi_change_1h") >= 5


def is_strong_market(row: dict[str, Any]) -> bool:
    return n(row, "market_median_24h") >= 0.5 and n(row, "market_breadth_up") >= 55


def is_weak_market(row: dict[str, Any]) -> bool:
    return n(row, "market_median_24h") <= -2 and n(row, "market_breadth_up") <= 25


def tag(row: dict[str, Any]) -> str:
    return str(row.get("anomaly_tag") or "")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def n(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return 0.0 if value is None else float(value)


if __name__ == "__main__":
    main()
