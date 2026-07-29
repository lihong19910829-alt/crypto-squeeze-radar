"""Explore long-side strategy candidates from accumulated market snapshots."""

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
    samples = attach_future_returns(rows)
    print_overview(rows)

    rules: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("baseline_all", lambda r: True),
        ("low_position_only", lambda r: n(r, "price_position_24h") <= 20),
        (
            "low_position_not_dumping",
            lambda r: n(r, "price_position_24h") <= 20 and n(r, "price_change_1h") > -1,
        ),
        (
            "low_position_1h_green",
            lambda r: n(r, "price_position_24h") <= 20 and n(r, "price_change_1h") >= 0,
        ),
        (
            "low_position_4h_green",
            lambda r: n(r, "price_position_24h") <= 20 and n(r, "price_change_4h") >= 0,
        ),
        (
            "low_position_1h_green_oi_up",
            lambda r: n(r, "price_position_24h") <= 20
            and n(r, "price_change_1h") >= 0
            and n(r, "oi_change_1h") >= 0,
        ),
        (
            "low_position_1h_green_oi_1h_2_12",
            lambda r: n(r, "price_position_24h") <= 20
            and n(r, "price_change_1h") >= 0
            and 2 <= n(r, "oi_change_1h") <= 12,
        ),
        (
            "low_position_1h_green_oi_24h_up",
            lambda r: n(r, "price_position_24h") <= 20
            and n(r, "price_change_1h") >= 0
            and n(r, "oi_change_24h") >= 10,
        ),
        (
            "low_position_volume_expansion",
            lambda r: n(r, "price_position_24h") <= 20
            and n(r, "price_change_1h") >= 0
            and n(r, "quote_volume_change_24h") >= 100,
        ),
        (
            "mid_score_oi_24h_extreme",
            lambda r: 40 <= n(r, "risk_score") < 70 and n(r, "oi_change_24h") >= 100,
        ),
        (
            "mid_score_oi_24h_extreme_not_dumping",
            lambda r: 40 <= n(r, "risk_score") < 70
            and n(r, "oi_change_24h") >= 100
            and n(r, "price_change_1h") > -1,
        ),
        (
            "positive_funding_low_position",
            lambda r: n(r, "price_position_24h") <= 20
            and n(r, "funding_rate") >= 0.0003
            and n(r, "price_change_1h") >= 0,
        ),
        (
            "short_crowd_low_position_1h_green",
            lambda r: n(r, "price_position_24h") <= 20
            and "空头拥挤" in str(r.get("anomaly_tag") or "")
            and n(r, "price_change_1h") >= 0,
        ),
        (
            "reclaim_from_low_volume_confirm",
            lambda r: n(r, "price_position_24h") <= 35
            and n(r, "price_change_1h") >= 0.5
            and n(r, "price_change_4h") > -4
            and n(r, "quote_volume_24h") >= 30_000_000,
        ),
        (
            "strong_market_low_pullback",
            lambda r: n(r, "market_median_24h") >= 0.5
            and n(r, "market_breadth_up") >= 55
            and n(r, "price_position_24h") <= 35
            and n(r, "price_change_1h") >= 0,
        ),
        (
            "not_weak_market_low_reclaim_oi",
            lambda r: not (
                n(r, "market_median_24h") <= -2 and n(r, "market_breadth_up") <= 25
            )
            and n(r, "price_position_24h") <= 30
            and n(r, "price_change_1h") >= 0
            and n(r, "oi_change_1h") >= 0,
        ),
    ]

    print("\nRULE\tN_RAW\tN_DEDUP\tH\tAVG\tMED\tUP%\tP25\tP75")
    for name, fn in rules:
        hits = [row for row in samples if fn(row)]
        deduped = dedupe(hits, hours=6)
        for horizon in HORIZONS:
            values = [row.get(f"return_{horizon}h_pct") for row in deduped]
            print_summary(name, len(hits), values, horizon)

    print("\nRECENT_EXAMPLES")
    recent = [
        row
        for row in samples
        if row["timestamp_utc"] >= "2026-07-20"
        and n(row, "price_position_24h") <= 30
        and n(row, "price_change_1h") >= 0
        and n(row, "oi_change_1h") >= 0
    ]
    recent = dedupe(recent, hours=6)[-20:]
    for row in recent:
        print(
            f"{row['timestamp_utc']}\t{row['symbol']}\tpos={n(row,'price_position_24h'):.1f}"
            f"\t1h={n(row,'price_change_1h'):.2f}\toi1h={n(row,'oi_change_1h'):.2f}"
            f"\t4h={fmt(row.get('return_4h_pct'))}\t12h={fmt(row.get('return_12h_pct'))}"
        )

    search_rules(samples)


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
    context: dict[str, tuple[float, float]] = {}
    for timestamp, items in by_time.items():
        changes = [n(row, "price_change_24h") for row in items if row.get("price_change_24h") is not None]
        if not changes:
            context[timestamp] = (0.0, 0.0)
            continue
        context[timestamp] = (median(changes), sum(1 for value in changes if value > 0) / len(changes) * 100)
    for row in rows:
        row["market_median_24h"], row["market_breadth_up"] = context[row["timestamp_utc"]]


def attach_future_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)
    samples: list[dict[str, Any]] = []
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
    series: list[dict[str, Any]],
    timestamps: list[datetime],
    start_index: int,
    target: datetime,
) -> float | None:
    index = bisect_left(timestamps, target, lo=start_index)
    if index >= len(series):
        return None
    return n(series[index], "price")


def dedupe(rows: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
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
    symbols = {row["symbol"] for row in rows}
    print(f"DB={DB_FILE}")
    print(f"RANGE={min(timestamps)} -> {max(timestamps)}")
    print(f"ROWS={len(rows)} SYMBOLS={len(symbols)}")


def print_summary(name: str, raw_count: int, values: list[Any], horizon: int) -> None:
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        print(f"{name}\t{raw_count}\t0\t{horizon}\tNA\tNA\tNA\tNA\tNA")
        return
    p25 = clean[int((len(clean) - 1) * 0.25)]
    p75 = clean[int((len(clean) - 1) * 0.75)]
    up_pct = sum(1 for value in clean if value > 0) / len(clean) * 100
    print(
        f"{name}\t{raw_count}\t{len(clean)}\t{horizon}"
        f"\t{mean(clean):.2f}\t{median(clean):.2f}\t{up_pct:.1f}\t{p25:.2f}\t{p75:.2f}"
    )


def search_rules(samples: list[dict[str, Any]]) -> None:
    samples = dedupe(samples, hours=6)
    candidates: list[tuple[str, Callable[[dict[str, Any]], bool]]] = []
    regimes = [
        ("any", lambda r: True),
        ("not_weak", lambda r: not (n(r, "market_median_24h") <= -2 and n(r, "market_breadth_up") <= 25)),
        ("strong", lambda r: n(r, "market_median_24h") >= 0.5 and n(r, "market_breadth_up") >= 55),
        ("weak", lambda r: n(r, "market_median_24h") <= -2 and n(r, "market_breadth_up") <= 25),
    ]
    position_bands = [
        ("pos<=20", lambda r: n(r, "price_position_24h") <= 20),
        ("pos20_50", lambda r: 20 < n(r, "price_position_24h") <= 50),
        ("pos50_80", lambda r: 50 < n(r, "price_position_24h") <= 80),
        ("pos>=80", lambda r: n(r, "price_position_24h") >= 80),
    ]
    price_filters = [
        ("1h>=0", lambda r: n(r, "price_change_1h") >= 0),
        ("1h>=0.5", lambda r: n(r, "price_change_1h") >= 0.5),
        ("4h>=0", lambda r: n(r, "price_change_4h") >= 0),
        ("24h>=5", lambda r: n(r, "price_change_24h") >= 5),
        ("24h>=10", lambda r: n(r, "price_change_24h") >= 10),
        ("not_chase_1h<3", lambda r: n(r, "price_change_1h") < 3),
    ]
    oi_filters = [
        ("oi1h>=0", lambda r: n(r, "oi_change_1h") >= 0),
        ("oi1h>=2", lambda r: n(r, "oi_change_1h") >= 2),
        ("oi1h0_5", lambda r: 0 <= n(r, "oi_change_1h") <= 5),
        ("oi24h>=10", lambda r: n(r, "oi_change_24h") >= 10),
        ("oi24h>=25", lambda r: n(r, "oi_change_24h") >= 25),
    ]
    funding_filters = [
        ("funding>=0", lambda r: n(r, "funding_rate") >= 0),
        ("funding>=0.0003", lambda r: n(r, "funding_rate") >= 0.0003),
        ("funding>-0.0003", lambda r: n(r, "funding_rate") > -0.0003),
        ("funding<0", lambda r: n(r, "funding_rate") < 0),
    ]

    for regime_name, regime_fn in regimes:
        for position_name, position_fn in position_bands:
            for price_name, price_fn in price_filters:
                candidates.append(
                    (
                        f"{regime_name}+{position_name}+{price_name}",
                        combine(regime_fn, position_fn, price_fn),
                    )
                )
                for oi_name, oi_fn in oi_filters:
                    candidates.append(
                        (
                            f"{regime_name}+{position_name}+{price_name}+{oi_name}",
                            combine(regime_fn, position_fn, price_fn, oi_fn),
                        )
                    )
                for funding_name, funding_fn in funding_filters:
                    candidates.append(
                        (
                            f"{regime_name}+{position_name}+{price_name}+{funding_name}",
                            combine(regime_fn, position_fn, price_fn, funding_fn),
                        )
                    )

    scored = []
    for name, fn in candidates:
        rows = [row for row in samples if fn(row)]
        for horizon in (4, 12):
            values = sorted(
                float(row[f"return_{horizon}h_pct"])
                for row in rows
                if row.get(f"return_{horizon}h_pct") is not None
            )
            if len(values) < 100:
                continue
            up_pct = sum(1 for value in values if value > 0) / len(values) * 100
            med = median(values)
            avg = mean(values)
            scored.append((med, up_pct, avg, len(values), horizon, name))

    print("\nTOP_BY_MEDIAN_MIN100")
    print("H\tN\tAVG\tMED\tUP%\tRULE")
    for med, up_pct, avg, count, horizon, name in sorted(scored, reverse=True)[:25]:
        print(f"{horizon}\t{count}\t{avg:.2f}\t{med:.2f}\t{up_pct:.1f}\t{name}")


def combine(*fns: Callable[[dict[str, Any]], bool]) -> Callable[[dict[str, Any]], bool]:
    return lambda row: all(fn(row) for fn in fns)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def n(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return 0.0 if value is None else float(value)


def fmt(value: Any) -> str:
    return "NA" if value is None else f"{float(value):.2f}"


if __name__ == "__main__":
    main()
