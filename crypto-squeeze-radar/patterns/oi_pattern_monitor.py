"""独立监控高质量做空模式，并写入单独的数据文件。"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from config import PATTERN_SIGNALS_JSON_FILE, PATTERN_SQLITE_DB_FILE, SQLITE_DB_FILE


HORIZONS = [1, 4, 6, 12, 24]
PATTERN_VERSION = "oi-patterns-v2"
SHORT_STOP_LOSS_PCT = 2.0
SHORT_FIRST_TAKE_PROFIT_PCT = 2.0
SHORT_FINAL_TAKE_PROFIT_PCT = 4.0
SHORT_MAX_HOLD_HOURS = 4
LONG_STOP_LOSS_PCT = 2.0
LONG_FIRST_TAKE_PROFIT_PCT = 2.0
LONG_FINAL_TAKE_PROFIT_PCT = 4.0
LONG_MAX_HOLD_HOURS = 4
HIGH_POSITION_THRESHOLD = 80
CHASE_DOWN_1H_LIMIT = -3
STRONG_MARKET_MEDIAN_24H = 0.5
STRONG_MARKET_BREADTH = 55
WEAK_MARKET_MEDIAN_24H = -2
WEAK_MARKET_BREADTH = 25

PATTERNS = {
    "oi_4h_short_reversal": {
        "name": "高位OI异常后4H反向空",
        "direction": "做空候选",
        "horizon": 4,
        "description": "24h高位叠加短时OI快速堆积，不追多，优先观察未来4小时去杠杆/回落机会；默认2%止损、2%减半、4%全止盈、4小时退出。",
    },
    "high_neg_funding_12h_short": {
        "name": "高位负Funding弱币延续空",
        "direction": "做空候选",
        "horizon": 12,
        "description": "24h高位、Funding显著为负且成交额充足，历史更像弱币继续回落，而不是立刻轧空。",
    },
    "short_crowd_high_volume_12h_short": {
        "name": "空头拥挤高位放量12H空",
        "direction": "做空候选",
        "horizon": 12,
        "description": "空头拥挤叠加24h高位和成交额爆发，优先观察拉高出货后的12小时回落。",
    },
    "strict_momentum_4h_long": {
        "name": "强势延续4H多",
        "direction": "做多候选",
        "horizon": 4,
        "description": "多头拥挤、杠杆升温且价格保持强势时，只做短线延续；弱市不启用。",
    },
}


def run_pattern_monitor(items: list[dict[str, Any]]) -> dict[str, Any]:
    """识别当前命中的模式，单独入库，并导出仪表盘 JSON。"""
    init_pattern_db()
    history = load_history()
    stats = build_pattern_stats(history)
    signals = detect_current_signals(items, stats)
    try:
        save_pattern_signals(signals)
    except sqlite3.Error as error:
        print(f"模式信号入库失败，已跳过 SQLite 写入：{error}")
    payload = build_payload(signals, stats, history)
    write_pattern_json(payload)
    return payload


def init_pattern_db(db_file: Path = PATTERN_SQLITE_DB_FILE) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pattern_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_version TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                pattern_key TEXT NOT NULL,
                pattern_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence TEXT NOT NULL,
                symbol TEXT NOT NULL,
                coin TEXT,
                price REAL,
                original_risk_score INTEGER,
                pattern_score INTEGER,
                context_score INTEGER,
                funding_rate REAL,
                oi_change_1h REAL,
                oi_change_24h REAL,
                price_change_1h REAL,
                price_change_4h REAL,
                price_change_24h REAL,
                price_position_24h REAL,
                quote_volume_24h REAL,
                quote_volume_change_24h REAL,
                funding_same_sign_count INTEGER,
                funding_avg_abs_6 REAL,
                evidence_horizon TEXT,
                evidence_sample_count INTEGER,
                up_probability_pct REAL,
                down_probability_pct REAL,
                avg_return_pct REAL,
                median_return_pct REAL,
                entry_side TEXT,
                entry_price REAL,
                stop_loss_pct REAL,
                stop_loss_price REAL,
                first_take_profit_pct REAL,
                first_take_profit_price REAL,
                final_take_profit_pct REAL,
                final_take_profit_price REAL,
                max_hold_hours INTEGER,
                short_setup_score INTEGER,
                short_setup_reasons TEXT,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        _ensure_columns(
            conn,
            "pattern_signals",
            {
                "context_score": "INTEGER",
                "price_change_1h": "REAL",
                "price_change_4h": "REAL",
                "price_change_24h": "REAL",
                "price_position_24h": "REAL",
                "quote_volume_24h": "REAL",
                "quote_volume_change_24h": "REAL",
                "funding_same_sign_count": "INTEGER",
                "funding_avg_abs_6": "REAL",
                "entry_side": "TEXT",
                "entry_price": "REAL",
                "stop_loss_pct": "REAL",
                "stop_loss_price": "REAL",
                "first_take_profit_pct": "REAL",
                "first_take_profit_price": "REAL",
                "final_take_profit_pct": "REAL",
                "final_take_profit_price": "REAL",
                "max_hold_hours": "INTEGER",
                "short_setup_score": "INTEGER",
                "short_setup_reasons": "TEXT",
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pattern_signals_time
            ON pattern_signals (timestamp_utc, pattern_key, symbol)
            """
        )


def load_history() -> list[dict[str, Any]]:
    if not SQLITE_DB_FILE.exists():
        return []
    with sqlite3.connect(f"file:{SQLITE_DB_FILE}?mode=ro", uri=True) as conn:
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
    return rows


def build_pattern_stats(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    samples = attach_future_returns(history)
    buckets = {
        "oi_4h_short_reversal": [row for row in samples if pattern_oi_4h_short_reversal(row)],
        "high_neg_funding_12h_short": [
            row for row in samples if pattern_high_neg_funding_12h_short(row)
        ],
        "short_crowd_high_volume_12h_short": [
            row for row in samples if pattern_short_crowd_high_volume_12h_short(row)
        ],
        "strict_momentum_4h_long": [
            row for row in samples if pattern_strict_momentum_4h_long(row)
        ],
    }
    stats: dict[str, dict[str, Any]] = {}
    for key, rows in buckets.items():
        stats[key] = {
            **PATTERNS[key],
            "total_matches": len(rows),
            "horizons": {
                str(horizon): summarize_returns([row[f"return_{horizon}h_pct"] for row in rows])
                for horizon in HORIZONS
            },
            "drawdown_horizons": {
                str(horizon): summarize_drawdowns([row[f"drawdown_{horizon}h_pct"] for row in rows])
                for horizon in HORIZONS
            },
        }
    return stats


def attach_future_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)

    samples: list[dict[str, Any]] = []
    for series in by_symbol.values():
        timestamps = [row["dt"] for row in series]
        for index, row in enumerate(series):
            item = dict(row)
            entry_price = number(row.get("price"))
            for horizon in HORIZONS:
                future = first_price_at_or_after(series, timestamps, index, row["dt"] + timedelta(hours=horizon))
                period_low = lowest_price_until(series, timestamps, index, row["dt"] + timedelta(hours=horizon))
                item[f"return_{horizon}h_pct"] = (
                    None if future is None or entry_price <= 0 else (future - entry_price) / entry_price * 100
                )
                item[f"drawdown_{horizon}h_pct"] = (
                    None
                    if period_low is None or entry_price <= 0
                    else (entry_price - period_low) / entry_price * 100
                )
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
    return number(series[index].get("price"))


def lowest_price_until(
    series: list[dict[str, Any]],
    timestamps: list[datetime],
    start_index: int,
    target: datetime,
) -> float | None:
    end_index = bisect_left(timestamps, target, lo=start_index)
    if end_index >= len(series):
        return None
    prices = [
        number(item.get("price"))
        for item in series[start_index : end_index + 1]
        if item.get("price") is not None
    ]
    return min(prices) if prices else None


def detect_current_signals(
    items: list[dict[str, Any]], stats: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    signals = {
        "oi_4h_short_reversal": [],
        "high_neg_funding_12h_short": [],
        "short_crowd_high_volume_12h_short": [],
        "strict_momentum_4h_long": [],
    }
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    market_regime = classify_market_regime_from_items(items)

    for item in items:
        row = normalize_current_item(item, timestamp_utc)
        row["market_regime"] = market_regime["regime"]
        row["market_median_24h"] = market_regime["median_24h_change_pct"]
        row["market_breadth"] = market_regime["up_breadth_pct"]
        if not row["symbol"] or row["price"] is None:
            continue
        if pattern_oi_4h_short_reversal(row):
            signals["oi_4h_short_reversal"].append(build_signal(row, "oi_4h_short_reversal", stats))
        if pattern_high_neg_funding_12h_short(row):
            signals["high_neg_funding_12h_short"].append(
                build_signal(row, "high_neg_funding_12h_short", stats)
            )
        if pattern_short_crowd_high_volume_12h_short(row):
            signals["short_crowd_high_volume_12h_short"].append(
                build_signal(row, "short_crowd_high_volume_12h_short", stats)
            )
        if pattern_strict_momentum_4h_long(row):
            signals["strict_momentum_4h_long"].append(
                build_signal(row, "strict_momentum_4h_long", stats)
            )

    signals["oi_4h_short_reversal"].sort(
        key=lambda row: (row["short_setup_score"], row["original_risk_score"], row["pattern_score"]),
        reverse=True,
    )
    for key in ("high_neg_funding_12h_short", "short_crowd_high_volume_12h_short"):
        signals[key].sort(
            key=lambda row: (
                number(row.get("short_setup_score")),
                number(row.get("pattern_score")),
                number(row.get("quote_volume_24h")),
            ),
            reverse=True,
        )
    signals["strict_momentum_4h_long"].sort(
        key=lambda row: (
            number(row.get("short_setup_score")),
            number(row.get("pattern_score")),
            number(row.get("quote_volume_24h")),
        ),
        reverse=True,
    )
    return signals


def build_signal(row: dict[str, Any], pattern_key: str, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pattern = PATTERNS[pattern_key]
    horizon = pattern["horizon"]
    evidence = stats.get(pattern_key, {}).get("horizons", {}).get(str(horizon), {})
    drawdown_evidence = stats.get(pattern_key, {}).get("drawdown_horizons", {}).get(str(horizon), {})
    confidence = confidence_from_evidence(evidence)
    signal = {
        "pattern_version": PATTERN_VERSION,
        "timestamp_utc": row["timestamp_utc"],
        "pattern_key": pattern_key,
        "pattern_name": pattern["name"],
        "direction": pattern["direction"],
        "confidence": confidence,
        "symbol": row["symbol"],
        "coin": row.get("coin"),
        "price": row.get("price"),
        "original_risk_score": row.get("risk_score"),
        "pattern_score": pattern_score(row, pattern_key),
        "context_score": context_score(row, pattern_key),
        "funding_rate": row.get("funding_rate"),
        "oi_change_1h": row.get("oi_change_1h"),
        "oi_change_24h": row.get("oi_change_24h"),
        "price_change_1h": row.get("price_change_1h"),
        "price_change_4h": row.get("price_change_4h"),
        "price_change_24h": row.get("price_change_24h"),
        "price_position_24h": row.get("price_position_24h"),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "quote_volume_change_24h": row.get("quote_volume_change_24h"),
        "funding_same_sign_count": row.get("funding_same_sign_count"),
        "funding_avg_abs_6": row.get("funding_avg_abs_6"),
        "market_regime": row.get("market_regime"),
        "market_median_24h": row.get("market_median_24h"),
        "market_breadth": row.get("market_breadth"),
        "evidence_horizon": f"{horizon}h",
        "evidence_sample_count": evidence.get("sample_count", 0),
        "up_probability_pct": evidence.get("up_probability_pct"),
        "down_probability_pct": evidence.get("down_probability_pct"),
        "avg_return_pct": evidence.get("avg_return_pct"),
        "median_return_pct": evidence.get("median_return_pct"),
        "drawdown_sample_count": drawdown_evidence.get("sample_count", 0),
        "drawdown_probability_pct": drawdown_evidence.get("down_probability_pct"),
        "avg_drawdown_pct": drawdown_evidence.get("avg_drawdown_pct"),
        "median_drawdown_pct": drawdown_evidence.get("median_drawdown_pct"),
    }
    if pattern_key in {
        "oi_4h_short_reversal",
        "high_neg_funding_12h_short",
        "short_crowd_high_volume_12h_short",
    }:
        signal.update(build_short_trade_plan(row, horizon))
    elif pattern_key == "strict_momentum_4h_long":
        signal.update(build_long_trade_plan(row, horizon))
    return signal


def save_pattern_signals(signals: dict[str, list[dict[str, Any]]]) -> None:
    rows = [signal for group in signals.values() for signal in group]
    if not rows:
        return
    created_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(PATTERN_SQLITE_DB_FILE) as conn:
        conn.executemany(
            """
            INSERT INTO pattern_signals (
                pattern_version, timestamp_utc, pattern_key, pattern_name, direction,
                confidence, symbol, coin, price, original_risk_score, pattern_score,
                context_score, funding_rate, oi_change_1h, oi_change_24h,
                price_change_1h, price_change_4h, price_change_24h, price_position_24h,
                quote_volume_24h, quote_volume_change_24h, funding_same_sign_count,
                funding_avg_abs_6, evidence_horizon,
                evidence_sample_count, up_probability_pct, down_probability_pct,
                avg_return_pct, median_return_pct, entry_side, entry_price,
                stop_loss_pct, stop_loss_price, first_take_profit_pct,
                first_take_profit_price, final_take_profit_pct, final_take_profit_price,
                max_hold_hours, short_setup_score, short_setup_reasons, created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["pattern_version"],
                    row["timestamp_utc"],
                    row["pattern_key"],
                    row["pattern_name"],
                    row["direction"],
                    row["confidence"],
                    row["symbol"],
                    row["coin"],
                    row["price"],
                    row["original_risk_score"],
                    row["pattern_score"],
                    row["context_score"],
                    row["funding_rate"],
                    row["oi_change_1h"],
                    row["oi_change_24h"],
                    row["price_change_1h"],
                    row["price_change_4h"],
                    row["price_change_24h"],
                    row["price_position_24h"],
                    row["quote_volume_24h"],
                    row["quote_volume_change_24h"],
                    row["funding_same_sign_count"],
                    row["funding_avg_abs_6"],
                    row["evidence_horizon"],
                    row["evidence_sample_count"],
                    row["up_probability_pct"],
                    row["down_probability_pct"],
                    row["avg_return_pct"],
                    row["median_return_pct"],
                    row.get("entry_side"),
                    row.get("entry_price"),
                    row.get("stop_loss_pct"),
                    row.get("stop_loss_price"),
                    row.get("first_take_profit_pct"),
                    row.get("first_take_profit_price"),
                    row.get("final_take_profit_pct"),
                    row.get("final_take_profit_price"),
                    row.get("max_hold_hours"),
                    row.get("short_setup_score"),
                    row.get("short_setup_reasons"),
                    created_at,
                )
                for row in rows
            ],
        )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def build_payload(
    signals: dict[str, list[dict[str, Any]]],
    stats: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = max((row["timestamp_utc"] for row in history), default=None)
    market_regime = classify_market_regime_from_history(history, latest)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pattern_version": PATTERN_VERSION,
        "source_history_db": str(SQLITE_DB_FILE),
        "pattern_db": str(PATTERN_SQLITE_DB_FILE),
        "history_rows": len(history),
        "history_latest_utc": latest,
        "market_regime": market_regime,
        "signals": signals,
        "stats": stats,
    }


def write_pattern_json(payload: dict[str, Any]) -> None:
    PATTERN_SIGNALS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERN_SIGNALS_JSON_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pattern_oi_4h_short_reversal(row: dict[str, Any]) -> bool:
    """24h高位叠加OI异常后的4小时反向做空观察模式。"""
    tag = str(row.get("anomaly_tag") or "")
    has_oi_tag = "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag
    has_oi_pressure = has_oi_tag or number(row.get("oi_change_1h")) >= 5
    return (
        has_oi_pressure
        and number(row.get("price_position_24h")) >= HIGH_POSITION_THRESHOLD
        and number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
        and "多头拥挤、杠杆过热" not in tag
    )


def pattern_high_neg_funding_12h_short(row: dict[str, Any]) -> bool:
    return (
        number(row.get("funding_rate")) <= -0.001
        and number(row.get("price_position_24h")) >= HIGH_POSITION_THRESHOLD
        and number(row.get("quote_volume_24h")) >= 30_000_000
        and number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    )


def pattern_short_crowd_high_volume_12h_short(row: dict[str, Any]) -> bool:
    tag = str(row.get("anomaly_tag") or "")
    return (
        "空头拥挤" in tag
        and number(row.get("price_position_24h")) >= HIGH_POSITION_THRESHOLD
        and number(row.get("quote_volume_change_24h")) >= 100
        and number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    )


def pattern_strict_momentum_4h_long(row: dict[str, Any]) -> bool:
    tag = str(row.get("anomaly_tag") or "")
    position = number(row.get("price_position_24h"))
    return (
        "多头拥挤" in tag
        and "杠杆过热" in tag
        and str(row.get("market_regime") or "") != "weak"
        and 45 <= position <= 90
        and number(row.get("price_change_1h")) >= 0
        and number(row.get("price_change_4h")) >= 2
        and number(row.get("price_change_24h")) >= 0
        and number(row.get("quote_volume_change_24h")) >= 25
    )


def pattern_score(row: dict[str, Any], pattern_key: str) -> int:
    if pattern_key == "oi_4h_short_reversal":
        score = 50
        if "OI异常增加" in str(row.get("anomaly_tag") or ""):
            score += 12
        score += min(number(row.get("oi_change_1h")) * 2, 24)
        if number(row.get("risk_score")) >= 70:
            score += 12
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    if pattern_key == "high_neg_funding_12h_short":
        score = 55
        score += min(abs(number(row.get("funding_rate"))) / 0.001 * 10, 20)
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    if pattern_key == "short_crowd_high_volume_12h_short":
        score = 55
        score += min(number(row.get("quote_volume_change_24h")) / 100 * 10, 25)
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    if pattern_key == "strict_momentum_4h_long":
        score = 50
        score += min(number(row.get("price_change_4h")) * 2, 20)
        score += min(number(row.get("quote_volume_change_24h")) / 100 * 8, 16)
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    return min(max(int(50 + context_score(row, pattern_key)), 0), 100)


def context_score(row: dict[str, Any], pattern_key: str) -> int:
    """Score whether price, volume, and funding context supports the pattern."""
    if pattern_key in {
        "oi_4h_short_reversal",
        "high_neg_funding_12h_short",
        "short_crowd_high_volume_12h_short",
    }:
        score = 0
        if number(row.get("price_change_1h")) >= 2:
            score += 6
        if number(row.get("price_position_24h")) >= 80:
            score += 12
        if number(row.get("price_change_24h")) >= 20:
            score += 10
        if number(row.get("funding_same_sign_count")) >= 4 and number(row.get("funding_rate")) > 0:
            score += 4
        if number(row.get("quote_volume_change_24h")) >= 100:
            score += 10
        elif number(row.get("quote_volume_change_24h")) >= 25:
            score += 4
        if str(row.get("market_regime") or "") == "weak":
            score += 8
        if str(row.get("market_regime") or "") == "strong":
            score -= 20
        if number(row.get("price_change_1h")) <= -2:
            score -= 5
        if number(row.get("price_change_1h")) <= CHASE_DOWN_1H_LIMIT:
            score -= 20
        if number(row.get("price_position_24h")) <= 20:
            score -= 30
        if pattern_key == "oi_4h_short_reversal" and "多头拥挤、杠杆过热" in str(row.get("anomaly_tag") or ""):
            score -= 25
        return score

    score = 0
    if pattern_key == "strict_momentum_4h_long":
        if str(row.get("market_regime") or "") == "strong":
            score += 15
        if str(row.get("market_regime") or "") == "weak":
            score -= 35
        if number(row.get("price_change_1h")) >= 0:
            score += 6
        if number(row.get("price_change_4h")) >= 2:
            score += 10
        if 45 <= number(row.get("price_position_24h")) <= 90:
            score += 10
        if number(row.get("quote_volume_change_24h")) >= 100:
            score += 8
        elif number(row.get("quote_volume_change_24h")) >= 25:
            score += 4
        if number(row.get("price_change_1h")) >= 8:
            score -= 12
        if number(row.get("price_position_24h")) > 95:
            score -= 20
        return score

    if number(row.get("price_change_4h")) >= 0:
        score += 5
    if number(row.get("price_change_24h")) >= 0:
        score += 5
    if 30 <= number(row.get("price_position_24h")) <= 85:
        score += 5
    if number(row.get("quote_volume_change_24h")) >= 25:
        score += 5
    if number(row.get("price_change_4h")) <= -5:
        score -= 8
    return score


def build_short_trade_plan(row: dict[str, Any], max_hold_hours: int = SHORT_MAX_HOLD_HOURS) -> dict[str, Any]:
    """Return a simple 4h short plan for OI reversal candidates."""
    price = number(row.get("price"))
    reasons = short_setup_reasons(row)
    return {
        "entry_side": "SHORT",
        "entry_price": price,
        "stop_loss_pct": SHORT_STOP_LOSS_PCT,
        "stop_loss_price": round(price * (1 + SHORT_STOP_LOSS_PCT / 100), 10) if price else None,
        "first_take_profit_pct": SHORT_FIRST_TAKE_PROFIT_PCT,
        "first_take_profit_price": round(price * (1 - SHORT_FIRST_TAKE_PROFIT_PCT / 100), 10) if price else None,
        "final_take_profit_pct": SHORT_FINAL_TAKE_PROFIT_PCT,
        "final_take_profit_price": round(price * (1 - SHORT_FINAL_TAKE_PROFIT_PCT / 100), 10) if price else None,
        "max_hold_hours": max_hold_hours,
        "time_exit_rule": f"{max_hold_hours}小时后仍未明显盈利则平仓或减仓",
        "position_rule": "第一止盈先平一半，剩余仓位移动止损到开仓价附近",
        "short_setup_score": short_setup_score(row),
        "short_setup_reasons": "；".join(reasons),
    }


def build_long_trade_plan(row: dict[str, Any], max_hold_hours: int = LONG_MAX_HOLD_HOURS) -> dict[str, Any]:
    price = number(row.get("price"))
    return {
        "entry_side": "LONG",
        "entry_price": price,
        "stop_loss_pct": LONG_STOP_LOSS_PCT,
        "stop_loss_price": round(price * (1 - LONG_STOP_LOSS_PCT / 100), 10) if price else None,
        "first_take_profit_pct": LONG_FIRST_TAKE_PROFIT_PCT,
        "first_take_profit_price": round(price * (1 + LONG_FIRST_TAKE_PROFIT_PCT / 100), 10) if price else None,
        "final_take_profit_pct": LONG_FINAL_TAKE_PROFIT_PCT,
        "final_take_profit_price": round(price * (1 + LONG_FINAL_TAKE_PROFIT_PCT / 100), 10) if price else None,
        "max_hold_hours": max_hold_hours,
        "time_exit_rule": f"{max_hold_hours}小时后仍未明显盈利则平仓或减仓",
        "position_rule": "第一止盈先平一半，剩余仓位移动止损到开仓价附近",
        "short_setup_score": long_setup_score(row),
        "short_setup_reasons": "；".join(long_setup_reasons(row)),
    }


def short_setup_score(row: dict[str, Any]) -> int:
    score = 0
    if "OI异常增加" in str(row.get("anomaly_tag") or "") or "OI寮傚父澧炲姞" in str(row.get("anomaly_tag") or ""):
        score += 25
    if number(row.get("oi_change_1h")) >= 5:
        score += 20
    if number(row.get("risk_score")) >= 70:
        score += 20
    if number(row.get("price_position_24h")) >= 74:
        score += 12
    if number(row.get("price_change_24h")) >= 20:
        score += 15
    if number(row.get("quote_volume_change_24h")) >= 100:
        score += 12
    elif number(row.get("quote_volume_change_24h")) >= 25:
        score += 8
    if number(row.get("quote_volume_24h")) >= 30_000_000:
        score += 8
    if number(row.get("funding_rate")) <= -0.001:
        score += 10
    elif number(row.get("funding_rate")) > 0:
        score += 5
    if str(row.get("market_regime") or "") == "weak":
        score += 10
    if str(row.get("market_regime") or "") == "strong":
        score -= 25
    if number(row.get("price_change_1h")) <= -3:
        score -= 20
    if number(row.get("price_position_24h")) <= 20:
        score -= 40
    if "多头拥挤、杠杆过热" in str(row.get("anomaly_tag") or ""):
        score -= 15
    return max(0, min(score, 100))


def long_setup_score(row: dict[str, Any]) -> int:
    score = 0
    if "多头拥挤" in str(row.get("anomaly_tag") or ""):
        score += 20
    if "杠杆过热" in str(row.get("anomaly_tag") or ""):
        score += 15
    if str(row.get("market_regime") or "") == "strong":
        score += 20
    if str(row.get("market_regime") or "") == "weak":
        score -= 35
    if number(row.get("price_change_4h")) >= 2:
        score += 15
    if 45 <= number(row.get("price_position_24h")) <= 90:
        score += 15
    if number(row.get("quote_volume_change_24h")) >= 25:
        score += 10
    if number(row.get("price_change_1h")) >= 8:
        score -= 15
    if number(row.get("price_position_24h")) > 95:
        score -= 25
    return max(0, min(score, 100))


def short_setup_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    tag = str(row.get("anomaly_tag") or "")
    if "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag:
        reasons.append("标签含OI异常增加")
    if number(row.get("oi_change_1h")) >= 5:
        reasons.append(f"1h OI增加{number(row.get('oi_change_1h')):.2f}%")
    if number(row.get("risk_score")) >= 70:
        reasons.append(f"风险评分{int(number(row.get('risk_score')))}")
    if number(row.get("price_position_24h")) >= 74:
        reasons.append(f"24h价格位置{number(row.get('price_position_24h')):.1f}%")
    if number(row.get("price_change_24h")) >= 20:
        reasons.append(f"24h涨幅{number(row.get('price_change_24h')):+.2f}%")
    if number(row.get("quote_volume_change_24h")) >= 25:
        reasons.append(f"24h成交额变化{number(row.get('quote_volume_change_24h')):+.1f}%")
    if number(row.get("quote_volume_24h")) >= 30_000_000:
        reasons.append("24h成交额超过3000万")
    if number(row.get("funding_rate")) <= -0.001:
        reasons.append(f"Funding显著为负{number(row.get('funding_rate')) * 100:+.4f}%")
    elif number(row.get("funding_rate")) > 0:
        reasons.append(f"Funding为正{number(row.get('funding_rate')) * 100:+.4f}%")
    if row.get("market_regime") == "weak":
        reasons.append("市场横截面偏弱")
    if row.get("market_regime") == "strong":
        reasons.append("强市环境，机械做空降级")
    if number(row.get("price_change_1h")) <= -3:
        reasons.append("1h已大跌，谨慎追空")
    if number(row.get("price_position_24h")) <= 20:
        reasons.append("24h低位，禁止低位追空")
    if "多头拥挤、杠杆过热" in tag:
        reasons.append("强势拥挤样本，禁止机械做空")
    return reasons or ["OI异常增加反向空观察"]


def long_setup_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    tag = str(row.get("anomaly_tag") or "")
    if "多头拥挤" in tag:
        reasons.append("标签含多头拥挤")
    if "杠杆过热" in tag:
        reasons.append("标签含杠杆过热")
    if row.get("market_regime") == "strong":
        reasons.append("市场横截面强")
    if row.get("market_regime") == "weak":
        reasons.append("弱市，多头降级")
    if number(row.get("price_change_4h")) >= 2:
        reasons.append(f"4h涨幅{number(row.get('price_change_4h')):+.2f}%")
    if 45 <= number(row.get("price_position_24h")) <= 90:
        reasons.append(f"24h位置{number(row.get('price_position_24h')):.1f}%")
    if number(row.get("quote_volume_change_24h")) >= 25:
        reasons.append(f"24h成交额变化{number(row.get('quote_volume_change_24h')):+.1f}%")
    if number(row.get("price_change_1h")) >= 8:
        reasons.append("1h涨幅过大，谨慎追多")
    return reasons or ["强势延续观察"]


def summarize_returns(values: list[float | None]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {
            "sample_count": 0,
            "avg_return_pct": None,
            "median_return_pct": None,
            "up_probability_pct": None,
            "down_probability_pct": None,
            "max_return_pct": None,
            "min_return_pct": None,
        }
    up_probability = sum(1 for value in clean if value > 0) / len(clean) * 100
    return {
        "sample_count": len(clean),
        "avg_return_pct": round(mean(clean), 4),
        "median_return_pct": round(median(clean), 4),
        "up_probability_pct": round(up_probability, 2),
        "down_probability_pct": round(100 - up_probability, 2),
        "max_return_pct": round(max(clean), 4),
        "min_return_pct": round(min(clean), 4),
    }


def summarize_drawdowns(values: list[float | None]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {
            "sample_count": 0,
            "avg_drawdown_pct": None,
            "median_drawdown_pct": None,
            "down_probability_pct": None,
            "max_drawdown_pct": None,
            "min_drawdown_pct": None,
        }
    down_probability = sum(1 for value in clean if value > 0) / len(clean) * 100
    return {
        "sample_count": len(clean),
        "avg_drawdown_pct": round(mean(clean), 4),
        "median_drawdown_pct": round(median(clean), 4),
        "down_probability_pct": round(down_probability, 2),
        "max_drawdown_pct": round(max(clean), 4),
        "min_drawdown_pct": round(min(clean), 4),
    }


def confidence_from_evidence(evidence: dict[str, Any], cap: str | None = None) -> str:
    sample_count = int(evidence.get("sample_count") or 0)
    up_probability = evidence.get("up_probability_pct")
    if up_probability is None:
        confidence = "low"
    else:
        consistency = max(float(up_probability), 100 - float(up_probability))
        if sample_count >= 50 and consistency >= 60:
            confidence = "high"
        elif sample_count >= 15 and consistency >= 55:
            confidence = "medium"
        else:
            confidence = "low"
    if cap == "medium" and confidence == "high":
        return "medium"
    return confidence


def classify_market_regime_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        float(item["price_change_24h_pct"])
        for item in items
        if item.get("price_change_24h_pct") is not None
    ]
    return classify_market_regime(values)


def classify_market_regime_from_history(
    history: list[dict[str, Any]], latest_timestamp: str | None
) -> dict[str, Any]:
    if not latest_timestamp:
        return classify_market_regime([])
    values = [
        float(row["price_change_24h"])
        for row in history
        if row.get("timestamp_utc") == latest_timestamp and row.get("price_change_24h") is not None
    ]
    return classify_market_regime(values)


def classify_market_regime(values: list[float]) -> dict[str, Any]:
    clean = [value for value in values if value == value]
    if not clean:
        return {
            "regime": "unknown",
            "median_24h_change_pct": None,
            "up_breadth_pct": None,
            "sample_count": 0,
            "label": "市场环境未知",
        }

    median_24h = median(clean)
    up_breadth = sum(1 for value in clean if value > 0) / len(clean) * 100
    if median_24h >= STRONG_MARKET_MEDIAN_24H and up_breadth >= STRONG_MARKET_BREADTH:
        regime = "strong"
        label = "强市，暂停机械空头"
    elif median_24h <= WEAK_MARKET_MEDIAN_24H and up_breadth <= WEAK_MARKET_BREADTH:
        regime = "weak"
        label = "弱市，优先高位去杠杆空"
    else:
        regime = "neutral"
        label = "中性市，只做高质量信号"

    return {
        "regime": regime,
        "median_24h_change_pct": round(median_24h, 4),
        "up_breadth_pct": round(up_breadth, 2),
        "sample_count": len(clean),
        "label": label,
    }


def normalize_current_item(item: dict[str, Any], timestamp_utc: str) -> dict[str, Any]:
    tags = item.get("tags") or []
    return {
        "timestamp_utc": timestamp_utc,
        "coin": item.get("coin"),
        "symbol": item.get("symbol"),
        "price": item.get("price"),
        "funding_rate": item.get("funding_rate"),
        "oi_change_1h": item.get("oi_change_1h_pct"),
        "oi_change_24h": item.get("oi_change_24h_pct"),
        "price_change_1h": item.get("price_change_1h_pct"),
        "price_change_4h": item.get("price_change_4h_pct"),
        "price_change_24h": item.get("price_change_24h_pct"),
        "price_position_24h": item.get("price_position_24h_pct"),
        "quote_volume_24h": item.get("quote_volume_24h"),
        "quote_volume_change_24h": item.get("quote_volume_change_24h_pct"),
        "funding_same_sign_count": item.get("funding_same_sign_count"),
        "funding_avg_abs_6": item.get("funding_avg_abs_6"),
        "risk_score": item.get("risk_score"),
        "anomaly_tag": "、".join(tags) if isinstance(tags, list) else str(tags),
    }


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
