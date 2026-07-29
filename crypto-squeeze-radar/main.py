"""Crypto Squeeze Radar MVP 入口。"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    BINANCE_MAX_WORKERS,
    BINANCE_QUOTE_ASSET,
    BINANCE_SYMBOLS,
    HISTORY_FILE,
    MONITOR_ALL_BINANCE_SYMBOLS,
    PATTERN_SIGNALS_JSON_FILE,
    RADAR_SCAN_MODE,
    SIGNAL_SCAN_GAINERS_TOP_N,
    SIGNAL_SCAN_HIGH_POSITION_TOP_N,
    SIGNAL_SCAN_LOSERS_TOP_N,
    SIGNAL_SCAN_LOW_POSITION_TOP_N,
    SIGNAL_SCAN_MAX_SYMBOLS,
    SIGNAL_SCAN_PREVIOUS_SIGNAL_HOURS,
    SIGNAL_SCAN_QUOTE_VOLUME_TOP_N,
    SQLITE_DB_FILE,
    TOP_N,
    WATCHLIST,
)
from data_sources.exchange import BinanceFuturesClient
from data_sources.hyperliquid import HyperliquidClient
from indicators.market_context import enrich_market_context
from indicators.scoring import evaluate_snapshot
from output.pattern_push import push_pattern_signals
from output.report import save_report
from output.tweets import save_tweets
from output.x_publisher import publish_eligible_tweets
from patterns.oi_pattern_monitor import run_pattern_monitor
from storage.sqlite_store import save_market_snapshots


def main(
    scan_mode: str | None = None,
    emit_outputs: bool = True,
    return_pattern_payload: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """运行一次监控任务：拉数据、打标签、评分、输出报告和推文。"""
    started = time.perf_counter()
    last_stage = started

    def log_stage(name: str) -> None:
        nonlocal last_stage
        now_time = time.perf_counter()
        print(f"阶段耗时：{name} {now_time - last_stage:.1f}s，累计 {now_time - started:.1f}s")
        last_stage = now_time

    mode = normalize_scan_mode(scan_mode or RADAR_SCAN_MODE)
    binance = BinanceFuturesClient()
    hyperliquid = HyperliquidClient()
    evaluated: list[dict[str, Any]] = []
    premium_map = get_premium_map(binance) if MONITOR_ALL_BINANCE_SYMBOLS else {}
    ticker_24h_map = get_24h_ticker_map(binance) if MONITOR_ALL_BINANCE_SYMBOLS else {}
    log_stage("批量行情")
    universe = get_monitoring_universe(binance, bool(premium_map), ticker_24h_map, mode)
    log_stage("构建扫描池")

    print(f"本轮扫描模式：{mode}")
    print(f"本轮深度 OI 扫描交易对数量：{len(universe)}")
    print(f"Binance 并发抓取线程数：{BINANCE_MAX_WORKERS}")

    # Binance 全量永续合约数量较多，串行抓取很容易超过计划任务窗口，所以这里并发执行。
    with ThreadPoolExecutor(max_workers=max(1, BINANCE_MAX_WORKERS)) as executor:
        future_to_item = {
            executor.submit(process_symbol, item, binance, hyperliquid, premium_map, ticker_24h_map): item
            for item in universe
        }
        for index, future in enumerate(as_completed(future_to_item), start=1):
            item = future_to_item[future]
            result = future.result()
            result["scan_mode"] = mode
            result["universe_reason"] = item.get("universe_reason")
            evaluated.append(result)
            if index % 50 == 0 or index == len(future_to_item):
                print(f"已完成 {index}/{len(future_to_item)} 个交易对")
    log_stage("深度 OI 扫描")

    enrich_market_context(evaluated)
    log_stage("历史上下文")
    ranked = sorted(evaluated, key=lambda item: item["risk_score"], reverse=True)[:TOP_N]
    append_history(evaluated, mode)
    save_market_snapshots(evaluated, scan_mode=mode)
    log_stage("写入历史库")

    pattern_payload: dict[str, Any] | None = None
    if emit_outputs:
        pattern_payload = run_pattern_monitor(evaluated)
        log_stage("模式统计")
        push_pattern_signals(pattern_payload)
        log_stage("微信推送")
        save_report(ranked)
        tweets = save_tweets(ranked)
        publish_eligible_tweets(tweets)
        log_stage("报告和预览")
    else:
        print("全量补数模式：已跳过模式信号、推送、报告和 X 预览输出")

    print("Crypto Squeeze Radar 本轮运行完成")
    print(f"已输出：{HISTORY_FILE}")
    print(f"已写入 SQLite：{SQLITE_DB_FILE}")
    if emit_outputs:
        print("已输出：output/report.md、output/tweets.json、output/tweets.md、output/x_post_preview.md")
    if return_pattern_payload:
        return evaluated, pattern_payload
    return evaluated


def get_monitoring_universe(
    binance: BinanceFuturesClient,
    bulk_market_ok: bool = True,
    ticker_24h_map: dict[str, Any] | None = None,
    scan_mode: str = "signal_scan",
) -> list[dict[str, str]]:
    """生成本轮监控交易对列表。"""
    if MONITOR_ALL_BINANCE_SYMBOLS:
        if not bulk_market_ok:
            print("Binance 批量行情不可用，本轮降级为核心观察列表，避免全市场逐币超时导致停更")
            return [{"coin": coin, "symbol": BINANCE_SYMBOLS[coin]} for coin in WATCHLIST]
        try:
            symbols = binance.get_trading_symbols()
            if scan_mode == "signal_scan":
                return select_signal_scan_universe(symbols, ticker_24h_map or {})
            return add_universe_reason(symbols, "full_scan")
        except Exception as error:
            fallback = load_previous_universe()
            if fallback:
                print(f"Binance 交易对列表暂不可用，复用上一轮 {len(fallback)} 个交易对: {error}")
                return fallback
            print(f"Binance 交易对列表暂不可用，退回核心观察列表: {error}")
    return [{"coin": coin, "symbol": BINANCE_SYMBOLS[coin]} for coin in WATCHLIST]


def select_signal_scan_universe(
    symbols: list[dict[str, str]],
    ticker_24h_map: dict[str, Any],
) -> list[dict[str, str]]:
    """Use cheap all-market ticker data to choose the hourly deep OI scan pool."""
    by_symbol = {row["symbol"]: dict(row) for row in symbols}
    selected_reasons: dict[str, set[str]] = {}

    def add(symbol: str, reason: str) -> None:
        if symbol in by_symbol:
            selected_reasons.setdefault(symbol, set()).add(reason)

    ticker_rows = [
        {
            "symbol": symbol,
            "quote_volume": number(data.get("quoteVolume")),
            "price_change": number(data.get("priceChangePercent")),
            "position": price_position_from_ticker(data),
        }
        for symbol, data in ticker_24h_map.items()
        if symbol in by_symbol
    ]

    for row in top_by(ticker_rows, "quote_volume", SIGNAL_SCAN_QUOTE_VOLUME_TOP_N):
        add(row["symbol"], "quote_volume_top")
    for row in top_by(ticker_rows, "price_change", SIGNAL_SCAN_GAINERS_TOP_N):
        add(row["symbol"], "gainer_top")
    for row in top_by(ticker_rows, "price_change", SIGNAL_SCAN_LOSERS_TOP_N, reverse=False):
        add(row["symbol"], "loser_top")
    for row in top_by(ticker_rows, "position", SIGNAL_SCAN_HIGH_POSITION_TOP_N):
        add(row["symbol"], "high_position_top")
    for row in top_by(ticker_rows, "position", SIGNAL_SCAN_LOW_POSITION_TOP_N, reverse=False):
        add(row["symbol"], "low_position_top")

    for symbol in load_recent_signal_symbols():
        add(symbol, "recent_signal")
    for symbol in load_recent_trading_symbols():
        add(symbol, "recent_trade")

    ticker_by_symbol = {row["symbol"]: row for row in ticker_rows}
    selected = []
    for symbol in sorted(selected_reasons):
        item = by_symbol[symbol]
        item["universe_reason"] = ",".join(sorted(selected_reasons[symbol]))
        selected.append(item)

    if not selected:
        print("轻量筛选池为空，本轮退回全市场深扫")
        return add_universe_reason(symbols, "signal_scan_fallback_full")

    if SIGNAL_SCAN_MAX_SYMBOLS > 0 and len(selected) > SIGNAL_SCAN_MAX_SYMBOLS:
        selected.sort(
            key=lambda item: signal_pool_rank(item, selected_reasons, ticker_by_symbol),
            reverse=True,
        )
        selected = selected[:SIGNAL_SCAN_MAX_SYMBOLS]
        selected.sort(key=lambda item: item["symbol"])

    print(
        "轻量全市场筛选完成："
        f"全市场 {len(symbols)} 个，精选深扫 {len(selected)} 个"
    )
    return selected


def signal_pool_rank(
    item: dict[str, str],
    selected_reasons: dict[str, set[str]],
    ticker_by_symbol: dict[str, dict[str, Any]],
) -> tuple[int, float, float]:
    """Rank capped signal-pool members by multi-factor overlap and liquidity."""
    symbol = item["symbol"]
    ticker = ticker_by_symbol.get(symbol, {})
    reasons = selected_reasons.get(symbol, set())
    return (
        len(reasons),
        number(ticker.get("quote_volume")),
        abs(number(ticker.get("price_change"))),
    )


def top_by(
    rows: list[dict[str, Any]],
    key: str,
    limit: int,
    reverse: bool = True,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    clean = [row for row in rows if row.get(key) is not None]
    return sorted(clean, key=lambda row: row[key], reverse=reverse)[:limit]


def price_position_from_ticker(data: dict[str, Any]) -> float | None:
    price = number_or_none(data.get("lastPrice")) or number_or_none(data.get("weightedAvgPrice"))
    low = number_or_none(data.get("lowPrice"))
    high = number_or_none(data.get("highPrice"))
    if price is None or low is None or high is None or high <= low:
        return None
    return (price - low) / (high - low) * 100


def load_recent_signal_symbols() -> set[str]:
    """Keep recent candidates in the hourly pool so follow-through is not lost."""
    if not PATTERN_SIGNALS_JSON_FILE.exists():
        return set()
    try:
        payload = json.loads(PATTERN_SIGNALS_JSON_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SIGNAL_SCAN_PREVIOUS_SIGNAL_HOURS)
    symbols: set[str] = set()
    for rows in (payload.get("signals") or {}).values():
        for row in rows or []:
            timestamp = parse_time_or_none(str(row.get("timestamp_utc") or ""))
            if timestamp is not None and timestamp < cutoff:
                continue
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                symbols.add(symbol)
    return symbols


def load_recent_trading_symbols() -> set[str]:
    db_file = SQLITE_DB_FILE.parent / "trading.sqlite3"
    if not db_file.exists():
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    try:
        with sqlite3.connect(db_file) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM trading_decisions
                WHERE created_at_utc >= ?
                  AND symbol IS NOT NULL
                  AND symbol != ''
                """,
                (cutoff,),
            ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]).upper() for row in rows}


def add_universe_reason(rows: list[dict[str, str]], reason: str) -> list[dict[str, str]]:
    return [{**row, "universe_reason": reason} for row in rows]


def get_premium_map(binance: BinanceFuturesClient) -> dict[str, Any]:
    """批量读取标记价格/Funding；失败时退回单币接口。"""
    try:
        return binance.get_all_mark_prices_and_funding()
    except Exception as error:
        print(f"Binance 批量价格/Funding 暂不可用，改为逐币抓取: {error}")
        return {}


def get_24h_ticker_map(binance: BinanceFuturesClient) -> dict[str, Any]:
    """Batch read 24h ticker data for momentum, volume, and high-low context."""
    try:
        return binance.get_all_24h_tickers()
    except Exception as error:
        print(f"Binance 24h ticker unavailable, skip context fields this run: {error}")
        return {}


def load_previous_universe() -> list[dict[str, str]]:
    """从 SQLite 最近一轮快照恢复交易对列表，避免发现接口抖动导致整轮停更。"""
    if not SQLITE_DB_FILE.exists():
        return []

    sql = """
        SELECT coin, symbol
        FROM market_snapshots
        WHERE timestamp_utc = (
            SELECT MAX(timestamp_utc)
            FROM market_snapshots
        )
          AND symbol IS NOT NULL
          AND symbol != ''
        ORDER BY symbol ASC
    """
    try:
        with sqlite3.connect(SQLITE_DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
    except sqlite3.Error as error:
        print(f"读取上一轮交易对列表失败: {error}")
        return []

    return [
        {
            "coin": row["coin"] or coin_from_symbol(row["symbol"]),
            "symbol": row["symbol"],
        }
        for row in rows
    ]


def coin_from_symbol(symbol: str) -> str:
    """从交易对名称里提取币种。"""
    if symbol.endswith(BINANCE_QUOTE_ASSET):
        return symbol[: -len(BINANCE_QUOTE_ASSET)]
    return symbol


def process_symbol(
    item: dict[str, str],
    binance: BinanceFuturesClient,
    hyperliquid: HyperliquidClient,
    premium_map: dict[str, Any],
    ticker_24h_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """抓取并评估单个交易对；供并发线程调用。"""
    coin = item["coin"]
    symbol = item["symbol"]
    try:
        snapshot = binance.build_snapshot(
            coin,
            symbol=symbol,
            premium_data=premium_map.get(symbol),
            ticker_24h_data=(ticker_24h_map or {}).get(symbol),
        )
    except Exception as binance_error:
        if MONITOR_ALL_BINANCE_SYMBOLS:
            return _failed_item(coin, symbol, binance_error, None)
        try:
            snapshot = hyperliquid.build_snapshot(coin)
            snapshot.warnings.append(f"Binance 数据失败，已切换 Hyperliquid: {binance_error}")
        except Exception as fallback_error:
            return _failed_item(coin, symbol, binance_error, fallback_error)

    return evaluate_snapshot(snapshot)


def append_history(items: list[dict[str, Any]], scan_mode: str = "signal_scan") -> None:
    """把每轮快照追加到 CSV，方便后续做趋势和回测。"""
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    exists = HISTORY_FILE.exists()
    fields = [
        "timestamp_utc",
        "coin",
        "symbol",
        "price",
        "funding_rate",
        "open_interest",
        "open_interest_value_usd",
        "oi_change_1h_pct",
        "oi_change_24h_pct",
        "price_change_1h_pct",
        "price_change_4h_pct",
        "price_change_24h_pct",
        "price_position_24h_pct",
        "quote_volume_24h",
        "quote_volume_change_24h_pct",
        "funding_same_sign_count",
        "funding_avg_abs_6",
        "long_liquidation_usd",
        "short_liquidation_usd",
        "risk_score",
        "risk_level",
        "tags",
        "source",
        "scan_mode",
        "universe_reason",
    ]
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as file:
        active_fields = load_history_csv_fields(fields) if exists else fields
        writer = csv.DictWriter(file, fieldnames=active_fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        timestamp_utc = datetime.now(timezone.utc).isoformat()
        for item in items:
            writer.writerow(
                {
                    "timestamp_utc": timestamp_utc,
                    "coin": item.get("coin"),
                    "symbol": item.get("symbol"),
                    "price": item.get("price"),
                    "funding_rate": item.get("funding_rate"),
                    "open_interest": item.get("open_interest"),
                    "open_interest_value_usd": item.get("open_interest_value_usd"),
                    "oi_change_1h_pct": item.get("oi_change_1h_pct"),
                    "oi_change_24h_pct": item.get("oi_change_24h_pct"),
                    "price_change_1h_pct": item.get("price_change_1h_pct"),
                    "price_change_4h_pct": item.get("price_change_4h_pct"),
                    "price_change_24h_pct": item.get("price_change_24h_pct"),
                    "price_position_24h_pct": item.get("price_position_24h_pct"),
                    "quote_volume_24h": item.get("quote_volume_24h"),
                    "quote_volume_change_24h_pct": item.get("quote_volume_change_24h_pct"),
                    "funding_same_sign_count": item.get("funding_same_sign_count"),
                    "funding_avg_abs_6": item.get("funding_avg_abs_6"),
                    "long_liquidation_usd": item.get("long_liquidation_usd"),
                    "short_liquidation_usd": item.get("short_liquidation_usd"),
                    "risk_score": item.get("risk_score"),
                    "risk_level": item.get("risk_level"),
                    "tags": "、".join(item.get("tags", [])),
                    "source": item.get("source"),
                    "scan_mode": item.get("scan_mode") or scan_mode,
                    "universe_reason": item.get("universe_reason"),
                }
            )


def _failed_item(
    coin: str,
    symbol: str,
    binance_error: Exception,
    fallback_error: Exception | None,
) -> dict[str, Any]:
    """两个数据源都失败时，生成可追踪的失败记录。"""
    return {
        "coin": coin,
        "symbol": symbol,
        "price": None,
        "funding_rate": None,
        "open_interest": None,
        "open_interest_value_usd": None,
        "oi_change_1h_pct": None,
        "oi_change_24h_pct": None,
        "price_change_1h_pct": None,
        "price_change_4h_pct": None,
        "price_change_24h_pct": None,
        "price_position_24h_pct": None,
        "high_24h": None,
        "low_24h": None,
        "quote_volume_24h": None,
        "quote_volume_change_24h_pct": None,
        "funding_same_sign_count": None,
        "funding_avg_abs_6": None,
        "long_liquidation_usd": 0.0,
        "short_liquidation_usd": 0.0,
        "source": "none",
        "warnings": _error_warnings(binance_error, fallback_error),
        "tags": ["正常"],
        "risk_score": 0,
        "risk_level": "正常",
    }


def _error_warnings(binance_error: Exception, fallback_error: Exception | None) -> list[str]:
    """整理失败原因，避免主流程里拼接过多分支。"""
    warnings = [f"Binance 失败: {binance_error}"]
    if fallback_error is not None:
        warnings.append(f"Hyperliquid 失败: {fallback_error}")
    return warnings


def load_history_csv_fields(default_fields: list[str]) -> list[str]:
    try:
        with HISTORY_FILE.open("r", newline="", encoding="utf-8") as file:
            header = next(csv.reader(file), [])
    except (OSError, StopIteration):
        return default_fields
    return header or default_fields


def normalize_scan_mode(value: str) -> str:
    if value in {"full", "full_scan"}:
        return "full_scan"
    return "signal_scan"


def parse_time_or_none(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def number(value: Any) -> float:
    result = number_or_none(value)
    return 0.0 if result is None else result


if __name__ == "__main__":
    main()
