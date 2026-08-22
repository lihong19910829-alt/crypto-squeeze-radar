"""独立监控高质量 OI 多空模式，并写入单独的数据文件。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
from bisect import bisect_left
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from config import PATTERN_SIGNALS_JSON_FILE, PATTERN_SQLITE_DB_FILE, SQLITE_DB_FILE


HORIZONS = [1, 4, 6, 12, 24]
PATTERN_VERSION = "oi-patterns-v6"
STRATEGY_REVISION = "2026-08-22-b-quality-gate-v1"
SHORT_STOP_LOSS_PCT = 6.0
SHORT_FIRST_TAKE_PROFIT_PCT = 4.0
SHORT_FINAL_TAKE_PROFIT_PCT = 8.0
SHORT_MAX_HOLD_HOURS = 4
SHORT_TRADE_PLANS = {
    "oi_4h_short_reversal": {
        "stop_loss_pct": 4.0,
        "first_take_profit_pct": 3.0,
        "final_take_profit_pct": 7.0,
        "max_hold_hours": 4,
    },
    "high_neg_funding_12h_short": {
        "stop_loss_pct": 8.0,
        "first_take_profit_pct": 6.0,
        "final_take_profit_pct": 10.0,
        "max_hold_hours": 12,
    },
    "short_crowd_high_volume_12h_short": {
        "stop_loss_pct": 8.0,
        "first_take_profit_pct": 6.0,
        "final_take_profit_pct": 10.0,
        "max_hold_hours": 12,
    },
}
# 强市允许 B/C 进入交易，但强趋势中的空单需要更宽的反抽容错，
# 同时降低目标、保持 12 小时观察窗口，避免把强市反抽误判成失败。
STRONG_MARKET_SHORT_PLANS = {
    "high_neg_funding_12h_short": {
        "stop_loss_pct": 8.0,
        "first_take_profit_pct": 6.0,
        "final_take_profit_pct": 10.0,
        "max_hold_hours": 12,
    },
    "short_crowd_high_volume_12h_short": {
        "stop_loss_pct": 8.0,
        "first_take_profit_pct": 6.0,
        "final_take_profit_pct": 10.0,
        "max_hold_hours": 12,
    },
}
LONG_STOP_LOSS_PCT = 2.0
LONG_FIRST_TAKE_PROFIT_PCT = 2.0
LONG_FINAL_TAKE_PROFIT_PCT = 4.0
LONG_MAX_HOLD_HOURS = 4
HIGH_POSITION_THRESHOLD = 80
LOW_POSITION_THRESHOLD = 20
CHASE_DOWN_1H_LIMIT = -3
CHASE_UP_1H_LIMIT = 3
B_STRICT_FUNDING_RATE = -0.001
STRONG_MARKET_MEDIAN_24H = 0.5
STRONG_MARKET_BREADTH = 55
WEAK_MARKET_MEDIAN_24H = -2
WEAK_MARKET_BREADTH = 25

PATTERNS = {
    "oi_4h_short_reversal": {
        "name": "高位OI异常强势上涨后4H反向空",
        "direction": "做空候选",
        "horizon": 4,
        "description": "24h高位、24h涨幅至少20%叠加短时OI快速堆积，不追多，优先观察未来4小时去杠杆/回落机会；采用4%止损、3%减半、7%终止盈、4小时退出。",
    },
    "high_neg_funding_12h_short": {
        "name": "高位负Funding弱币延续空",
        "direction": "做空候选",
        "horizon": 12,
        "description": "24h高位、Funding显著为负且成交额充足；统一8%止损，未确认1H收盘转弱时止盈6%/10%，确认后放宽至8%/13%，12小时退出。",
    },
    "short_crowd_high_volume_12h_short": {
        "name": "空头拥挤高位放量12H空",
        "direction": "做空候选",
        "horizon": 12,
        "description": "空头拥挤叠加24h高位和成交额爆发，仅作为B模式增强信息；风险参数跟随B模式的1H收盘确认分层。",
    },
    "oi_4h_long_reversal": {
        "name": "低位OI异常后4H反向多",
        "direction": "做多候选",
        "horizon": 4,
        "description": "24h低位叠加短时OI快速堆积，不追空，优先观察未来4小时反向修复机会；默认2%止损、2%减半、4%全止盈、4小时退出。",
    },
    "low_pos_funding_12h_long": {
        "name": "低位正Funding强币延续多",
        "direction": "做多候选",
        "horizon": 12,
        "description": "24h低位、Funding显著为正且成交额充足，作为高位负Funding做空逻辑的反向观察。",
    },
    "long_crowd_low_volume_12h_long": {
        "name": "多头拥挤低位放量12H多",
        "direction": "做多候选",
        "horizon": 12,
        "description": "多头拥挤叠加24h低位和成交额爆发，优先观察杀跌释放后的12小时修复。",
    },
}


def strategy_config_hash() -> str:
    """Fingerprint every execution-relevant rule so historical signals stay comparable."""
    normalized_source = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    strategy_config = {
        "pattern_version": PATTERN_VERSION,
        "revision": STRATEGY_REVISION,
        "short_trade_plans": SHORT_TRADE_PLANS,
        "strong_market_short_plans": STRONG_MARKET_SHORT_PLANS,
        "high_position_threshold": HIGH_POSITION_THRESHOLD,
        "low_position_threshold": LOW_POSITION_THRESHOLD,
        "chase_down_1h_limit": CHASE_DOWN_1H_LIMIT,
        "chase_up_1h_limit": CHASE_UP_1H_LIMIT,
        "strong_market_median_24h": STRONG_MARKET_MEDIAN_24H,
        "strong_market_breadth": STRONG_MARKET_BREADTH,
        "weak_market_median_24h": WEAK_MARKET_MEDIAN_24H,
        "weak_market_breadth": WEAK_MARKET_BREADTH,
        "mode_rules": {
            "a": "paused_by_default;weak_market_and_shadow_confirmation_when_enabled",
            "b": "primary_short_mode;oi24>=20_and_volume24>=100=1.0x;funding<=-0.001=0.8x;other_b=record_only_0x;closed_1h_rollover_controls_targets;cvd=auxiliary",
            "c": "b_confirmation_only",
            "cvd": "auxiliary_only_after_closed_1h_rollover;(buy-sell)/(buy+sell)",
        },
        # Including normalized strategy source prevents an implementation-only
        # threshold edit from silently reusing the same historical fingerprint.
        "strategy_module_sha256": hashlib.sha256(normalized_source.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(strategy_config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


STRATEGY_CONFIG_HASH = strategy_config_hash()


def run_pattern_monitor(
    items: list[dict[str, Any]],
    market_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """识别当前命中的模式，单独入库，并导出仪表盘 JSON。"""
    init_pattern_db()
    samples, history_metadata = load_pattern_samples()
    print(
        f"模式统计候选预筛：全历史 {history_metadata['total_rows']} 条，"
        f"候选 {len(samples)} 条"
    )
    stats = summarize_pattern_samples(samples)
    active_market_regime = market_regime or classify_market_regime_from_items(items)
    signals = detect_current_signals(items, stats, market_regime=active_market_regime)
    try:
        save_pattern_signals(signals)
    except sqlite3.Error as error:
        print(f"模式信号入库失败，已跳过 SQLite 写入：{error}")
    payload = build_payload(
        signals,
        stats,
        samples,
        market_regime=active_market_regime,
        history_total_rows=history_metadata["total_rows"],
        history_latest_utc=history_metadata["latest_utc"],
    )
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
                strategy_revision TEXT,
                strategy_config_hash TEXT,
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
                cvd_1h REAL,
                cvd_24h REAL,
                cvd_ratio_1h REAL,
                cvd_ratio_24h REAL,
                oi_change_1h REAL,
                oi_change_24h REAL,
                price_change_1h REAL,
                price_change_4h REAL,
                price_change_24h REAL,
                closed_1h_close REAL,
                previous_closed_1h_close REAL,
                closed_1h_close_time_utc TEXT,
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
                move_stop_to_breakeven_after_tp1 INTEGER,
                breakeven_buffer_pct REAL,
                short_setup_score INTEGER,
                short_setup_reasons TEXT,
                is_star INTEGER,
                mode_hit_count INTEGER,
                trade_grade TEXT,
                position_multiplier REAL,
                first_take_profit_close_pct REAL,
                final_take_profit_close_pct REAL,
                time_exit_close_pct REAL,
                position_rule TEXT,
                execution_note TEXT,
                bc_volume_confirmation INTEGER,
                oi_volume_double_confirmation INTEGER,
                cvd_confirmation TEXT,
                price_turn_confirmation INTEGER,
                shadow_entry_confirmed INTEGER,
                shadow_confirmation_count INTEGER,
                market_regime TEXT,
                market_median_24h REAL,
                market_breadth REAL,
                market_sample_count INTEGER,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        _ensure_columns(
            conn,
            "pattern_signals",
            {
                "strategy_revision": "TEXT",
                "strategy_config_hash": "TEXT",
                "context_score": "INTEGER",
                "cvd_1h": "REAL",
                "cvd_24h": "REAL",
                "cvd_ratio_1h": "REAL",
                "cvd_ratio_24h": "REAL",
                "price_change_1h": "REAL",
                "price_change_4h": "REAL",
                "price_change_24h": "REAL",
                "closed_1h_close": "REAL",
                "previous_closed_1h_close": "REAL",
                "closed_1h_close_time_utc": "TEXT",
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
                "move_stop_to_breakeven_after_tp1": "INTEGER",
                "breakeven_buffer_pct": "REAL",
                "short_setup_score": "INTEGER",
                "short_setup_reasons": "TEXT",
                "is_star": "INTEGER",
                "mode_hit_count": "INTEGER",
                "trade_grade": "TEXT",
                "position_multiplier": "REAL",
                "first_take_profit_close_pct": "REAL",
                "final_take_profit_close_pct": "REAL",
                "time_exit_close_pct": "REAL",
                "position_rule": "TEXT",
                "execution_note": "TEXT",
                "bc_volume_confirmation": "INTEGER",
                "oi_volume_double_confirmation": "INTEGER",
                "cvd_confirmation": "TEXT",
                "price_turn_confirmation": "INTEGER",
                "shadow_entry_confirmed": "INTEGER",
                "shadow_confirmation_count": "INTEGER",
                "market_regime": "TEXT",
                "market_median_24h": "REAL",
                "market_breadth": "REAL",
                "market_sample_count": "INTEGER",
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
        configure_history_reader(conn)
        rows = query_history_rows(conn)

    return rows


def load_pattern_samples() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load only rows that can match a pattern, then attach their future paths.

    Pattern rules match a small fraction of the market history.  Reading and
    copying every snapshot on every hourly run made runtime and memory grow
    linearly with the whole database.  SQL applies a conservative superset of
    the six rules first; the existing Python predicates still make the final
    exact decision, so the statistical definition does not change.
    """
    if not SQLITE_DB_FILE.exists():
        return [], {"total_rows": 0, "latest_utc": None}
    condition, params = pattern_candidate_condition()
    with sqlite3.connect(f"file:{SQLITE_DB_FILE}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        configure_history_reader(conn)
        total_rows, latest_utc = conn.execute(
            """
            SELECT COUNT(*), MAX(timestamp_utc)
            FROM market_snapshots
            WHERE symbol IS NOT NULL AND price IS NOT NULL
            """
        ).fetchone()
        candidates = query_history_rows(conn, condition, params)
        samples = attach_candidate_future_returns(conn, candidates)
    return samples, {"total_rows": int(total_rows or 0), "latest_utc": latest_utc}


def pattern_candidate_condition() -> tuple[str, tuple[Any, ...]]:
    """Return an indexed-history prefilter that is a superset of all patterns."""
    condition = """
        (
            (
                (
                    COALESCE(oi_change_1h, 0) >= ?
                    OR instr(COALESCE(anomaly_tag, ''), ?) > 0
                    OR instr(COALESCE(anomaly_tag, ''), ?) > 0
                )
                AND (
                    (
                        COALESCE(price_position_24h, 0) >= ?
                        AND COALESCE(price_change_24h, 0) >= ?
                        AND COALESCE(price_change_1h, 0) > ?
                    )
                    OR (
                        COALESCE(price_position_24h, 0) <= ?
                        AND COALESCE(price_change_1h, 0) < ?
                    )
                )
            )
            OR (
                COALESCE(funding_rate, 0) <= ?
                AND COALESCE(price_position_24h, 0) >= ?
                AND COALESCE(price_change_24h, 0) >= ?
                AND COALESCE(price_change_1h, 0) > ?
            )
            OR (
                instr(COALESCE(anomaly_tag, ''), ?) > 0
                AND COALESCE(price_position_24h, 0) >= ?
                AND COALESCE(quote_volume_change_24h, 0) >= ?
                AND COALESCE(price_change_1h, 0) > ?
            )
            OR (
                COALESCE(funding_rate, 0) >= ?
                AND COALESCE(price_position_24h, 0) <= ?
                AND COALESCE(quote_volume_24h, 0) >= ?
                AND COALESCE(price_change_1h, 0) < ?
            )
            OR (
                instr(COALESCE(anomaly_tag, ''), ?) > 0
                AND COALESCE(price_position_24h, 0) <= ?
                AND COALESCE(quote_volume_change_24h, 0) >= ?
                AND COALESCE(price_change_1h, 0) < ?
            )
        )
    """
    params: tuple[Any, ...] = (
        5,
        "OI异常增加",
        "OI寮傚父澧炲姞",
        HIGH_POSITION_THRESHOLD,
        20,
        CHASE_DOWN_1H_LIMIT,
        LOW_POSITION_THRESHOLD,
        CHASE_UP_1H_LIMIT,
        -0.0003,
        HIGH_POSITION_THRESHOLD,
        10,
        CHASE_DOWN_1H_LIMIT,
        "空头拥挤",
        HIGH_POSITION_THRESHOLD,
        100,
        CHASE_DOWN_1H_LIMIT,
        0.001,
        LOW_POSITION_THRESHOLD,
        30_000_000,
        CHASE_UP_1H_LIMIT,
        "多头拥挤",
        LOW_POSITION_THRESHOLD,
        100,
        CHASE_UP_1H_LIMIT,
    )
    return condition, params


def configure_history_reader(conn: sqlite3.Connection) -> None:
    """Use a larger read cache without changing database contents or semantics."""
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -131072")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 268435456")


def query_history_rows(
    conn: sqlite3.Connection,
    condition: str = "1 = 1",
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT timestamp_utc, coin, symbol, price, funding_rate, open_interest,
                   oi_change_1h, oi_change_24h, price_change_1h, price_change_4h,
                   price_change_24h, closed_1h_close, previous_closed_1h_close,
                   closed_1h_close_time_utc, price_close_1h_confirmation,
                   price_position_24h, quote_volume_24h,
                   quote_volume_change_24h, cvd_1h, cvd_24h,
                   cvd_ratio_1h, cvd_ratio_24h, funding_same_sign_count,
                   funding_avg_abs_6, risk_score, anomaly_tag, source
            FROM market_snapshots
            WHERE {condition} AND symbol IS NOT NULL AND price IS NOT NULL
            ORDER BY symbol, timestamp_utc
            """,
            params,
        ).fetchall()
    ]
    for row in rows:
        row["dt"] = parse_time(row["timestamp_utc"])
    return rows


def build_pattern_stats(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return summarize_pattern_samples(attach_future_returns(history))


def summarize_pattern_samples(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets = {
        "oi_4h_short_reversal": [row for row in samples if pattern_oi_4h_short_reversal(row)],
        "high_neg_funding_12h_short": [
            row for row in samples if pattern_high_neg_funding_12h_short(row)
        ],
        "short_crowd_high_volume_12h_short": [
            row for row in samples if pattern_short_crowd_high_volume_12h_short(row)
        ],
        "oi_4h_long_reversal": [row for row in samples if pattern_oi_4h_long_reversal(row)],
        "low_pos_funding_12h_long": [
            row for row in samples if pattern_low_pos_funding_12h_long(row)
        ],
        "long_crowd_low_volume_12h_long": [
            row for row in samples if pattern_long_crowd_low_volume_12h_long(row)
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


def attach_candidate_future_returns(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the same horizon outcomes while loading price paths one symbol at a time."""
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row["symbol"])].append(row)

    samples: list[dict[str, Any]] = []
    for symbol, candidates in by_symbol.items():
        first_timestamp = min(str(row["timestamp_utc"]) for row in candidates)
        price_rows = conn.execute(
            """
            SELECT timestamp_utc, price
            FROM market_snapshots
            WHERE symbol = ? AND price IS NOT NULL AND timestamp_utc >= ?
            ORDER BY timestamp_utc
            """,
            (symbol, first_timestamp),
        ).fetchall()
        timestamps = [parse_time(str(row[0])) for row in price_rows]
        prices = [number(row[1]) for row in price_rows]
        for row in candidates:
            item = dict(row)
            entry_price = number(row.get("price"))
            start = bisect_left(timestamps, row["dt"])
            for horizon in HORIZONS:
                end = bisect_left(
                    timestamps,
                    row["dt"] + timedelta(hours=horizon),
                    lo=start,
                )
                if end >= len(prices) or entry_price <= 0:
                    item[f"return_{horizon}h_pct"] = None
                    item[f"drawdown_{horizon}h_pct"] = None
                    continue
                future_price = prices[end]
                period_low = min(prices[start : end + 1])
                item[f"return_{horizon}h_pct"] = (
                    future_price - entry_price
                ) / entry_price * 100
                item[f"drawdown_{horizon}h_pct"] = (
                    entry_price - period_low
                ) / entry_price * 100
            samples.append(item)
    return samples


def attach_future_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)

    samples: list[dict[str, Any]] = []
    for series in by_symbol.values():
        timestamps = [row["dt"] for row in series]
        future_lows = future_low_prices(series, timestamps)
        for index, row in enumerate(series):
            item = dict(row)
            entry_price = number(row.get("price"))
            for horizon in HORIZONS:
                future = first_price_at_or_after(series, timestamps, index, row["dt"] + timedelta(hours=horizon))
                period_low = future_lows[horizon][index]
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


def future_low_prices(
    series: list[dict[str, Any]],
    timestamps: list[datetime],
) -> dict[int, list[float | None]]:
    """Precompute future-window lows with one monotonic deque per horizon.

    The previous implementation sliced and scanned every future window for
    every sample.  As the history grew, that repeated work dominated the
    hourly run.  Window endpoints move monotonically with the sample index,
    so each price can enter and leave a deque at most once per horizon.
    """
    prices = [number(row.get("price")) if row.get("price") is not None else None for row in series]
    result: dict[int, list[float | None]] = {}
    for horizon in HORIZONS:
        lows: list[float | None] = [None] * len(series)
        window: deque[int] = deque()
        right = -1
        for index, timestamp in enumerate(timestamps):
            end = bisect_left(timestamps, timestamp + timedelta(hours=horizon), lo=index)
            if end >= len(series):
                break
            while right < end:
                right += 1
                value = prices[right]
                if value is None:
                    continue
                while window and prices[window[-1]] >= value:
                    window.pop()
                window.append(right)
            while window and window[0] < index:
                window.popleft()
            lows[index] = prices[window[0]] if window else None
        result[horizon] = lows
    return result


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
    items: list[dict[str, Any]],
    stats: dict[str, dict[str, Any]],
    market_regime: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    signals = {
        "oi_4h_short_reversal": [],
        "high_neg_funding_12h_short": [],
        "short_crowd_high_volume_12h_short": [],
        "oi_4h_long_reversal": [],
        "low_pos_funding_12h_long": [],
        "long_crowd_low_volume_12h_long": [],
    }
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    active_market_regime = market_regime or classify_market_regime_from_items(items)

    for item in items:
        row = normalize_current_item(item, timestamp_utc)
        row["market_regime"] = active_market_regime["regime"]
        row["market_median_24h"] = active_market_regime["median_24h_change_pct"]
        row["market_breadth"] = active_market_regime["up_breadth_pct"]
        row["market_sample_count"] = active_market_regime["sample_count"]
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
        if pattern_oi_4h_long_reversal(row):
            signals["oi_4h_long_reversal"].append(build_signal(row, "oi_4h_long_reversal", stats))
        if pattern_low_pos_funding_12h_long(row):
            signals["low_pos_funding_12h_long"].append(
                build_signal(row, "low_pos_funding_12h_long", stats)
            )
        if pattern_long_crowd_low_volume_12h_long(row):
            signals["long_crowd_low_volume_12h_long"].append(
                build_signal(row, "long_crowd_low_volume_12h_long", stats)
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
    for key in ("oi_4h_long_reversal", "low_pos_funding_12h_long", "long_crowd_low_volume_12h_long"):
        signals[key].sort(
            key=lambda row: (
                number(row.get("short_setup_score")),
                number(row.get("pattern_score")),
                number(row.get("quote_volume_24h")),
            ),
            reverse=True,
        )
    annotate_trade_plan_metadata(signals)
    return signals


def annotate_trade_plan_metadata(signals: dict[str, list[dict[str, Any]]]) -> None:
    """Add execution-facing fields used by push messages and dashboard tables."""
    short_rows = [
        row
        for key in (
            "oi_4h_short_reversal",
            "high_neg_funding_12h_short",
            "short_crowd_high_volume_12h_short",
        )
        for row in signals.get(key, [])
    ]
    modes_by_symbol: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in short_rows:
        modes_by_symbol[(str(row.get("timestamp_utc")), str(row.get("symbol")))].add(
            str(row.get("pattern_key"))
        )

    for row in short_rows:
        matched_modes = modes_by_symbol[(str(row.get("timestamp_utc")), str(row.get("symbol")))]
        mode_count = len(matched_modes)
        row["bc_volume_confirmation"] = {
            "high_neg_funding_12h_short",
            "short_crowd_high_volume_12h_short",
        }.issubset(matched_modes)
        row["oi_volume_double_confirmation"] = (
            number(row.get("oi_change_24h")) >= 20
            and number(row.get("quote_volume_change_24h")) >= 100
        )
        add_shadow_confirmation_metadata(row)
        starred = is_star_short_signal(row)
        row["is_star"] = starred
        row["mode_hit_count"] = mode_count
        row["trade_grade"] = trade_grade(row, mode_count, starred)
        row["position_multiplier"] = position_multiplier(row, mode_count, starred)
        first_pct, final_pct, time_pct = close_ratios(row, starred)
        row["first_take_profit_close_pct"] = first_pct
        row["final_take_profit_close_pct"] = final_pct
        row["time_exit_close_pct"] = time_pct
        row["position_rule"] = (
            f"第一止盈平{first_pct:.0f}%，最终止盈平{final_pct:.0f}%，"
            f"剩余{time_pct:.0f}%到时间退出"
        )
        if row.get("move_stop_to_breakeven_after_tp1"):
            row["position_rule"] += "；第一止盈后剩余仓位止损移到成本保护位"
        row["execution_note"] = execution_note(row, mode_count, starred)


def add_shadow_confirmation_metadata(row: dict[str, Any]) -> None:
    """Apply the primary 1h-close confirmation and auxiliary CVD layer."""
    cvd_ratio = row.get("cvd_ratio_1h")
    if cvd_ratio is None:
        cvd_confirmation = "unknown"
    elif number(cvd_ratio) < 0:
        cvd_confirmation = "bearish"
    elif number(cvd_ratio) > 0:
        cvd_confirmation = "bullish"
    else:
        cvd_confirmation = "neutral"
    # Only a fully closed hourly candle can promote B sizing.  CVD remains
    # useful context, but it cannot promote an otherwise unconfirmed entry.
    price_turn = row.get("price_close_1h_confirmation") is True
    cvd_aux_confirmed = price_turn and cvd_confirmation == "bearish"
    row["cvd_confirmation"] = cvd_confirmation
    row["price_turn_confirmation"] = price_turn
    row["shadow_entry_confirmed"] = price_turn
    row["shadow_confirmation_count"] = int(price_turn) + int(cvd_aux_confirmed)


def is_star_short_signal(row: dict[str, Any]) -> bool:
    if str(row.get("entry_side") or "") != "SHORT":
        return False
    base_star = (
        is_high_quality_short_signal(row)
        and number(row.get("evidence_sample_count")) >= 15
        and number(row.get("down_probability_pct")) >= 55
        and number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    )
    if not base_star:
        return False
    regime = str(row.get("market_regime") or "")
    pattern_key = str(row.get("pattern_key") or "")
    if pattern_key == "short_crowd_high_volume_12h_short":
        return False
    if pattern_key == "oi_4h_short_reversal":
        return regime == "weak" and bool(row.get("shadow_entry_confirmed"))
    if regime != "strong":
        return True
    # 强市只放行 B，且要求比中性市更强的价格与 Funding 确认。
    return pattern_key == "high_neg_funding_12h_short" and is_strong_market_short_confirmation(
        row, pattern_key
    )


def is_strong_market_short_confirmation(row: dict[str, Any], pattern_key: str) -> bool:
    """强市 B/C 例外：只接收更强确认，A 仍不允许进入星标。"""
    position = number(row.get("price_position_24h"))
    price_change_24h = number(row.get("price_change_24h"))
    no_chase = number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    if not no_chase or position < 80 or price_change_24h < 20:
        return False
    if pattern_key == "high_neg_funding_12h_short":
        return number(row.get("funding_rate")) <= -0.0005
    return (
        "空头拥挤" in str(row.get("anomaly_tag") or "")
        and number(row.get("quote_volume_change_24h")) >= 100
    )


def is_high_quality_short_signal(row: dict[str, Any]) -> bool:
    """Return whether a short signal matches the validated confirmation layer."""
    position = number(row.get("price_position_24h"))
    price_change_24h = number(row.get("price_change_24h"))
    volume_change_24h = number(row.get("quote_volume_change_24h"))
    funding = number(row.get("funding_rate"))
    tag = str(row.get("anomaly_tag") or "")
    no_chase = number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    high_volume_breakout = (
        position >= 90
        and price_change_24h >= 20
        and volume_change_24h >= 50
        and no_chase
    )
    crowded_or_negative_funding = (
        position >= 70
        and price_change_24h >= 20
        and no_chase
        and ("空头拥挤" in tag or funding <= -0.0003)
    )
    return high_volume_breakout or crowded_or_negative_funding


def short_confirmation_tier(row: dict[str, Any]) -> str:
    """Classify the confirmation layer used for B/C risk parameters."""
    position = number(row.get("price_position_24h"))
    price_change_24h = number(row.get("price_change_24h"))
    volume_change_24h = number(row.get("quote_volume_change_24h"))
    funding = number(row.get("funding_rate"))
    tag = str(row.get("anomaly_tag") or "")
    no_chase = number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
    if position >= 90 and price_change_24h >= 20 and volume_change_24h >= 100 and no_chase:
        return "premium_volume"
    if position >= 90 and price_change_24h >= 20 and volume_change_24h >= 50 and no_chase:
        return "volume_confirmation"
    if (
        position >= 70
        and price_change_24h >= 20
        and no_chase
        and ("空头拥挤" in tag or funding <= -0.0003)
    ):
        return "crowd_funding_confirmation"
    return "base"


def is_strong_reaction_signal(row: dict[str, Any]) -> bool:
    return is_high_quality_short_signal(row)


def trade_grade(row: dict[str, Any], mode_count: int, starred: bool) -> str:
    pattern_key = str(row.get("pattern_key") or "")
    if pattern_key == "short_crowd_high_volume_12h_short":
        return "增强确认" if row.get("bc_volume_confirmation") else "观察"
    if pattern_key == "high_neg_funding_12h_short":
        if row.get("oi_volume_double_confirmation"):
            return "主交易"
        if is_strict_b_funding(row):
            return "可交易"
        return "观察"
    if starred and is_premium_short_signal(row):
        return "主交易"
    if starred:
        return "可交易"
    if mode_count >= 2:
        return "小仓确认"
    return "观察"


def position_multiplier(row: dict[str, Any], mode_count: int, starred: bool) -> float:
    pattern_key = str(row.get("pattern_key") or "")
    if pattern_key == "short_crowd_high_volume_12h_short":
        return 0.0
    if pattern_key == "high_neg_funding_12h_short":
        if row.get("oi_volume_double_confirmation"):
            return 1.0
        if is_strict_b_funding(row):
            return 0.8
        return 0.0
    if starred:
        if str(row.get("market_regime") or "") == "strong":
            return 0.6
        # A 的 4H 反向空历史优势弱于 B/C 的 12H 延续空，星标也降一档。
        if pattern_key == "oi_4h_short_reversal":
            return 0.8 if is_premium_short_signal(row) else 0.6
        if is_premium_short_signal(row):
            return 1.0
        return 0.8
    return 0.25 if mode_count >= 2 else 0.0


def is_strict_b_funding(row: dict[str, Any]) -> bool:
    """Return whether a B signal clears the stricter negative-funding tier."""
    return number(row.get("funding_rate")) <= B_STRICT_FUNDING_RATE


def is_premium_short_signal(row: dict[str, Any]) -> bool:
    return (
        number(row.get("price_position_24h")) >= 90
        and number(row.get("price_change_24h")) >= 20
        and number(row.get("quote_volume_change_24h")) >= 100
    )


def close_ratios(row: dict[str, Any], starred: bool) -> tuple[float, float, float]:
    if str(row.get("pattern_key")) == "short_crowd_high_volume_12h_short" and starred:
        # 强趋势中的高质量信号：第一次止盈后，最终止盈平掉全部剩余仓位。
        return 40.0, 60.0, 0.0
    # 普通交易：最终止盈不再预留低价值尾仓；时间退出仅作为兜底。
    return 50.0, 50.0, 0.0


def execution_note(row: dict[str, Any], mode_count: int, starred: bool) -> str:
    pattern_key = str(row.get("pattern_key") or "")
    if pattern_key == "short_crowd_high_volume_12h_short":
        if row.get("bc_volume_confirmation"):
            return "模式C仅作为模式B的放量增强确认，不单独开仓。"
        return "模式C单独命中仅观察，不自动开仓。"
    if pattern_key == "high_neg_funding_12h_short":
        confirmation_count = int(number(row.get("shadow_confirmation_count")))
        target_label = {
            0: "1H收盘尚未转弱，使用6%/10%止盈",
            1: "1H收盘转弱，使用8%/13%止盈",
        }.get(confirmation_count, "1H收盘转弱且CVD为负，使用8%/13%止盈")
        if row.get("oi_volume_double_confirmation"):
            sizing_label = "OI+成交额双确认，按1.0x计划仓位"
        elif is_strict_b_funding(row):
            sizing_label = "Funding≤-0.10%，按0.8x计划仓位"
        else:
            sizing_label = "未满足高质量仓位层，仅记录，不自动开仓"
        c_suffix = "；模式C同步命中，记为额外放量信息" if row.get("bc_volume_confirmation") else ""
        return f"B模式：{sizing_label}；{target_label}{c_suffix}。"
    c_suffix = " 模式C同步命中，记为放量增强确认。" if row.get("bc_volume_confirmation") else ""
    if starred:
        if str(row.get("market_regime") or "") == "strong":
            return "强市B例外：仅高质量确认可交易，统一8%止损，止盈按1H收盘确认使用6%/10%或8%/13%，最长12小时。" + c_suffix
        if pattern_key == "oi_4h_short_reversal":
            multiplier = "0.8x" if is_premium_short_signal(row) else "0.6x"
            return f"A模式降仓：通过高质量确认层，按{multiplier}计划仓位，等反抽失败。"
        if is_premium_short_signal(row):
            return "主交易：高位≥90%、24h涨幅≥20%、成交额放大≥100%，按1.0x计划仓位，等反抽失败。" + c_suffix
        return "可交易：通过高质量确认层，按0.8x计划仓位，等反抽失败。" + c_suffix
    if mode_count >= 2:
        return "小仓确认：多模式共振但未通过确认层，最多0.25x，必须等反抽失败。"
    return "观察：单模式或未通过确认层，不直接交易。"


def build_signal(row: dict[str, Any], pattern_key: str, stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pattern = PATTERNS[pattern_key]
    horizon = pattern["horizon"]
    evidence = stats.get(pattern_key, {}).get("horizons", {}).get(str(horizon), {})
    drawdown_evidence = stats.get(pattern_key, {}).get("drawdown_horizons", {}).get(str(horizon), {})
    confidence = confidence_from_evidence(evidence)
    signal = {
        "pattern_version": PATTERN_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "strategy_config_hash": STRATEGY_CONFIG_HASH,
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
        "cvd_1h": row.get("cvd_1h"),
        "cvd_24h": row.get("cvd_24h"),
        "cvd_ratio_1h": row.get("cvd_ratio_1h"),
        "cvd_ratio_24h": row.get("cvd_ratio_24h"),
        "oi_change_1h": row.get("oi_change_1h"),
        "oi_change_24h": row.get("oi_change_24h"),
        "price_change_1h": row.get("price_change_1h"),
        "price_change_4h": row.get("price_change_4h"),
        "price_change_24h": row.get("price_change_24h"),
        "closed_1h_close": row.get("closed_1h_close"),
        "previous_closed_1h_close": row.get("previous_closed_1h_close"),
        "closed_1h_close_time_utc": row.get("closed_1h_close_time_utc"),
        "price_close_1h_confirmation": row.get("price_close_1h_confirmation"),
        "price_position_24h": row.get("price_position_24h"),
        "quote_volume_24h": row.get("quote_volume_24h"),
        "quote_volume_change_24h": row.get("quote_volume_change_24h"),
        "funding_same_sign_count": row.get("funding_same_sign_count"),
        "funding_avg_abs_6": row.get("funding_avg_abs_6"),
        "market_regime": row.get("market_regime"),
        "market_median_24h": row.get("market_median_24h"),
        "market_breadth": row.get("market_breadth"),
        "market_sample_count": row.get("market_sample_count"),
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
        signal.update(build_short_trade_plan(row, pattern_key))
    elif pattern_key in {
        "oi_4h_long_reversal",
        "low_pos_funding_12h_long",
        "long_crowd_low_volume_12h_long",
    }:
        signal.update(build_long_trade_plan(row, horizon))
    return signal


def save_pattern_signals(signals: dict[str, list[dict[str, Any]]]) -> None:
    rows = [signal for group in signals.values() for signal in group]
    if not rows:
        return
    created_at = datetime.now(timezone.utc).isoformat()
    columns = [
        "pattern_version", "strategy_revision", "strategy_config_hash",
        "timestamp_utc", "pattern_key", "pattern_name", "direction", "confidence",
        "symbol", "coin", "price", "original_risk_score", "pattern_score",
        "context_score", "funding_rate", "cvd_1h", "cvd_24h", "cvd_ratio_1h",
        "cvd_ratio_24h", "oi_change_1h", "oi_change_24h", "price_change_1h",
        "price_change_4h", "price_change_24h", "closed_1h_close",
        "previous_closed_1h_close", "closed_1h_close_time_utc", "price_position_24h",
        "quote_volume_24h", "quote_volume_change_24h", "funding_same_sign_count",
        "funding_avg_abs_6", "evidence_horizon", "evidence_sample_count",
        "up_probability_pct", "down_probability_pct", "avg_return_pct",
        "median_return_pct", "entry_side", "entry_price", "stop_loss_pct",
        "stop_loss_price", "first_take_profit_pct", "first_take_profit_price",
        "final_take_profit_pct", "final_take_profit_price", "max_hold_hours",
        "move_stop_to_breakeven_after_tp1", "breakeven_buffer_pct",
        "short_setup_score", "short_setup_reasons", "is_star", "mode_hit_count",
        "trade_grade", "position_multiplier", "first_take_profit_close_pct",
        "final_take_profit_close_pct", "time_exit_close_pct", "position_rule",
        "execution_note", "bc_volume_confirmation", "oi_volume_double_confirmation",
        "cvd_confirmation",
        "price_turn_confirmation", "shadow_entry_confirmed", "shadow_confirmation_count",
        "market_regime",
        "market_median_24h", "market_breadth", "market_sample_count", "created_at_utc",
    ]
    prepared_rows = []
    for row in rows:
        record = dict(row)
        record["short_setup_reasons"] = text_value(record.get("short_setup_reasons"))
        for field in (
            "is_star",
            "bc_volume_confirmation",
            "oi_volume_double_confirmation",
            "price_turn_confirmation",
            "shadow_entry_confirmed",
            "move_stop_to_breakeven_after_tp1",
        ):
            record[field] = 1 if record.get(field) else 0
        record["created_at_utc"] = created_at
        prepared_rows.append(tuple(record.get(column) for column in columns))
    with sqlite3.connect(PATTERN_SQLITE_DB_FILE) as conn:
        conn.executemany(
            f"INSERT INTO pattern_signals ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            prepared_rows,
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
    market_regime: dict[str, Any] | None = None,
    history_total_rows: int | None = None,
    history_latest_utc: str | None = None,
) -> dict[str, Any]:
    latest = history_latest_utc or max((row["timestamp_utc"] for row in history), default=None)
    active_market_regime = market_regime or classify_market_regime_from_history(history, latest)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pattern_version": PATTERN_VERSION,
        "strategy_revision": STRATEGY_REVISION,
        "strategy_config_hash": STRATEGY_CONFIG_HASH,
        "source_history_db": str(SQLITE_DB_FILE),
        "pattern_db": str(PATTERN_SQLITE_DB_FILE),
        "history_rows": len(history) if history_total_rows is None else history_total_rows,
        "history_latest_utc": latest,
        "market_regime": active_market_regime,
        "signals": signals,
        "stats": stats,
    }


def write_pattern_json(payload: dict[str, Any]) -> None:
    PATTERN_SIGNALS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATTERN_SIGNALS_JSON_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    archive_pattern_json(PATTERN_SIGNALS_JSON_FILE, payload)


def archive_pattern_json(path: Path, payload: dict[str, Any]) -> None:
    """Keep every generated signal payload for later trading/debug audits."""
    try:
        generated_at = parse_time(str(payload.get("generated_at_utc") or ""))
    except ValueError:
        generated_at = datetime.now(timezone.utc)
    archive_dir = path.parent / "pattern_signal_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"pattern_signals_{stamp}.json"
    if not archive_path.exists():
        shutil.copy2(path, archive_path)


def pattern_oi_4h_short_reversal(row: dict[str, Any]) -> bool:
    """24h高位叠加OI异常后的4小时反向做空观察模式。"""
    tag = str(row.get("anomaly_tag") or "")
    has_oi_tag = "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag
    has_oi_pressure = has_oi_tag or number(row.get("oi_change_1h")) >= 5
    return (
        has_oi_pressure
        and number(row.get("price_position_24h")) >= HIGH_POSITION_THRESHOLD
        and number(row.get("price_change_24h")) >= 20
        and number(row.get("price_change_1h")) > CHASE_DOWN_1H_LIMIT
        and "多头拥挤、杠杆过热" not in tag
    )


def pattern_high_neg_funding_12h_short(row: dict[str, Any]) -> bool:
    return (
        number(row.get("funding_rate")) <= -0.0003
        and number(row.get("price_position_24h")) >= HIGH_POSITION_THRESHOLD
        and number(row.get("price_change_24h")) >= 10
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


def pattern_oi_4h_long_reversal(row: dict[str, Any]) -> bool:
    """24h低位叠加OI异常后的4小时反向做多观察模式。"""
    tag = str(row.get("anomaly_tag") or "")
    has_oi_tag = "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag
    has_oi_pressure = has_oi_tag or number(row.get("oi_change_1h")) >= 5
    return (
        has_oi_pressure
        and number(row.get("price_position_24h")) <= LOW_POSITION_THRESHOLD
        and number(row.get("price_change_1h")) < CHASE_UP_1H_LIMIT
        and "空头拥挤、杠杆过热" not in tag
    )


def pattern_low_pos_funding_12h_long(row: dict[str, Any]) -> bool:
    return (
        number(row.get("funding_rate")) >= 0.001
        and number(row.get("price_position_24h")) <= LOW_POSITION_THRESHOLD
        and number(row.get("quote_volume_24h")) >= 30_000_000
        and number(row.get("price_change_1h")) < CHASE_UP_1H_LIMIT
    )


def pattern_long_crowd_low_volume_12h_long(row: dict[str, Any]) -> bool:
    tag = str(row.get("anomaly_tag") or "")
    return (
        "多头拥挤" in tag
        and number(row.get("price_position_24h")) <= LOW_POSITION_THRESHOLD
        and number(row.get("quote_volume_change_24h")) >= 100
        and number(row.get("price_change_1h")) < CHASE_UP_1H_LIMIT
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
    if pattern_key == "oi_4h_long_reversal":
        score = 50
        if "OI异常增加" in str(row.get("anomaly_tag") or ""):
            score += 12
        score += min(number(row.get("oi_change_1h")) * 2, 24)
        if number(row.get("risk_score")) >= 70:
            score += 12
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    if pattern_key == "low_pos_funding_12h_long":
        score = 55
        score += min(abs(number(row.get("funding_rate"))) / 0.001 * 10, 20)
        score += context_score(row, pattern_key)
        return min(max(int(score), 0), 100)
    if pattern_key == "long_crowd_low_volume_12h_long":
        score = 55
        score += min(number(row.get("quote_volume_change_24h")) / 100 * 10, 25)
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

    if pattern_key in {
        "oi_4h_long_reversal",
        "low_pos_funding_12h_long",
        "long_crowd_low_volume_12h_long",
    }:
        score = 0
        if number(row.get("price_change_1h")) <= -2:
            score += 6
        if number(row.get("price_position_24h")) <= 20:
            score += 12
        if number(row.get("price_change_24h")) <= -20:
            score += 10
        if number(row.get("funding_same_sign_count")) >= 4 and number(row.get("funding_rate")) < 0:
            score += 4
        if number(row.get("quote_volume_change_24h")) >= 100:
            score += 10
        elif number(row.get("quote_volume_change_24h")) >= 25:
            score += 4
        if str(row.get("market_regime") or "") == "strong":
            score += 8
        if str(row.get("market_regime") or "") == "weak":
            score -= 20
        if number(row.get("price_change_1h")) >= 2:
            score -= 5
        if number(row.get("price_change_1h")) >= CHASE_UP_1H_LIMIT:
            score -= 20
        if number(row.get("price_position_24h")) >= 80:
            score -= 30
        if pattern_key == "oi_4h_long_reversal" and "空头拥挤、杠杆过热" in str(row.get("anomaly_tag") or ""):
            score -= 25
        return score

    score = 0
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


def build_short_trade_plan(row: dict[str, Any], pattern_key: str | None = None) -> dict[str, Any]:
    """Return the backtest-calibrated short plan for the matched pattern."""
    price = number(row.get("price"))
    reasons = short_setup_reasons(row)
    plan = dict(SHORT_TRADE_PLANS.get(
        pattern_key or "",
        {
            "stop_loss_pct": SHORT_STOP_LOSS_PCT,
            "first_take_profit_pct": SHORT_FIRST_TAKE_PROFIT_PCT,
            "final_take_profit_pct": SHORT_FINAL_TAKE_PROFIT_PCT,
            "max_hold_hours": SHORT_MAX_HOLD_HOURS,
        },
    ))
    if str(row.get("market_regime") or "") == "strong":
        plan.update(STRONG_MARKET_SHORT_PLANS.get(pattern_key or "", {}))
    is_b_or_c = pattern_key in {
        "high_neg_funding_12h_short",
        "short_crowd_high_volume_12h_short",
    }
    price_turn_confirmed = row.get("price_close_1h_confirmation") is True
    if is_b_or_c:
        # Latest path and live-trade review: 6% is too tight and 10% does not
        # earn back the additional loss size.  Keep the stop at 8%, then let
        # the closed-hour rollover decide whether targets can remain wider.
        plan["stop_loss_pct"] = 8.0
        plan["first_take_profit_pct"] = 8.0 if price_turn_confirmed else 6.0
        plan["final_take_profit_pct"] = 13.0 if price_turn_confirmed else 10.0
    stop_loss_pct = plan["stop_loss_pct"]
    first_take_profit_pct = plan["first_take_profit_pct"]
    final_take_profit_pct = plan["final_take_profit_pct"]
    max_hold_hours = int(plan["max_hold_hours"])
    return {
        "entry_side": "SHORT",
        "entry_price": price,
        "stop_loss_pct": stop_loss_pct,
        "stop_loss_price": round(price * (1 + stop_loss_pct / 100), 10) if price else None,
        "first_take_profit_pct": first_take_profit_pct,
        "first_take_profit_price": round(price * (1 - first_take_profit_pct / 100), 10) if price else None,
        "final_take_profit_pct": final_take_profit_pct,
        "final_take_profit_price": round(price * (1 - final_take_profit_pct / 100), 10) if price else None,
        "max_hold_hours": max_hold_hours,
        "time_exit_rule": f"{max_hold_hours}小时后仍未明显盈利则平仓或减仓",
        "move_stop_to_breakeven_after_tp1": is_b_or_c,
        "breakeven_buffer_pct": 0.3 if is_b_or_c else None,
        "position_rule": "第一止盈先平一半，剩余仓位移动止损到成本保护位",
        "short_setup_score": short_setup_score(row),
        "short_setup_reasons": "；".join(reasons),
    }


def short_stop_loss_pct(row: dict[str, Any], max_hold_hours: int) -> float:
    """Use wider stops for 12h high-volatility short setups."""
    if max_hold_hours >= 12:
        if number(row.get("price_change_24h")) >= 20 or number(row.get("quote_volume_change_24h")) >= 100:
            return 12.0
        return 10.0
    return SHORT_STOP_LOSS_PCT


def short_first_take_profit_pct(max_hold_hours: int) -> float:
    return 5.0 if max_hold_hours >= 12 else SHORT_FIRST_TAKE_PROFIT_PCT


def short_final_take_profit_pct(max_hold_hours: int) -> float:
    return 10.0 if max_hold_hours >= 12 else SHORT_FINAL_TAKE_PROFIT_PCT


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
    tag = str(row.get("anomaly_tag") or "")
    if "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag:
        score += 25
    if number(row.get("oi_change_1h")) >= 5:
        score += 20
    if number(row.get("risk_score")) >= 70:
        score += 20
    if number(row.get("price_position_24h")) <= 26:
        score += 12
    if number(row.get("price_change_24h")) <= -20:
        score += 15
    if number(row.get("quote_volume_change_24h")) >= 100:
        score += 12
    elif number(row.get("quote_volume_change_24h")) >= 25:
        score += 8
    if number(row.get("quote_volume_24h")) >= 30_000_000:
        score += 8
    if number(row.get("funding_rate")) >= 0.001:
        score += 10
    elif number(row.get("funding_rate")) < 0:
        score += 5
    if str(row.get("market_regime") or "") == "strong":
        score += 10
    if str(row.get("market_regime") or "") == "weak":
        score -= 25
    if number(row.get("price_change_1h")) >= 3:
        score -= 20
    if number(row.get("price_position_24h")) >= 80:
        score -= 40
    if "空头拥挤、杠杆过热" in tag:
        score -= 15
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
    if "OI异常增加" in tag or "OI寮傚父澧炲姞" in tag:
        reasons.append("标签含OI异常增加")
    if number(row.get("oi_change_1h")) >= 5:
        reasons.append(f"1h OI增加{number(row.get('oi_change_1h')):.2f}%")
    if number(row.get("risk_score")) >= 70:
        reasons.append(f"风险评分{int(number(row.get('risk_score')))}")
    if number(row.get("price_position_24h")) <= 26:
        reasons.append(f"24h价格位置{number(row.get('price_position_24h')):.1f}%")
    if number(row.get("price_change_24h")) <= -20:
        reasons.append(f"24h跌幅{number(row.get('price_change_24h')):+.2f}%")
    if number(row.get("quote_volume_change_24h")) >= 25:
        reasons.append(f"24h成交额变化{number(row.get('quote_volume_change_24h')):+.1f}%")
    if number(row.get("quote_volume_24h")) >= 30_000_000:
        reasons.append("24h成交额超过3000万")
    if number(row.get("funding_rate")) >= 0.001:
        reasons.append(f"Funding显著为正{number(row.get('funding_rate')) * 100:+.4f}%")
    elif number(row.get("funding_rate")) < 0:
        reasons.append(f"Funding为负{number(row.get('funding_rate')) * 100:+.4f}%")
    if row.get("market_regime") == "strong":
        reasons.append("市场横截面偏强")
    if row.get("market_regime") == "weak":
        reasons.append("弱市环境，机械做多降级")
    if number(row.get("price_change_1h")) >= 3:
        reasons.append("1h已大涨，谨慎追多")
    if number(row.get("price_position_24h")) >= 80:
        reasons.append("24h高位，禁止高位追多")
    if "空头拥挤、杠杆过热" in tag:
        reasons.append("弱势拥挤样本，禁止机械做多")
    return reasons or ["OI异常增加反向多观察"]


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


def classify_market_regime_from_tickers(
    ticker_24h_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Classify the broad USDT-perpetual market from the existing batch ticker response."""
    values = [
        float(row["priceChangePercent"])
        for symbol, row in ticker_24h_map.items()
        if symbol.endswith("USDT") and row.get("priceChangePercent") is not None
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
        "cvd_1h": item.get("cvd_1h"),
        "cvd_24h": item.get("cvd_24h"),
        "cvd_ratio_1h": item.get("cvd_ratio_1h"),
        "cvd_ratio_24h": item.get("cvd_ratio_24h"),
        "oi_change_1h": item.get("oi_change_1h_pct"),
        "oi_change_24h": item.get("oi_change_24h_pct"),
        "price_change_1h": item.get("price_change_1h_pct"),
        "price_change_4h": item.get("price_change_4h_pct"),
        "price_change_24h": item.get("price_change_24h_pct"),
        "closed_1h_close": item.get("closed_1h_close"),
        "previous_closed_1h_close": item.get("previous_closed_1h_close"),
        "closed_1h_close_time_utc": item.get("closed_1h_close_time_utc"),
        "price_close_1h_confirmation": item.get("price_close_1h_confirmation"),
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


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "；".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
