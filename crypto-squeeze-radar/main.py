"""Crypto Squeeze Radar MVP 入口。"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from config import (
    BINANCE_MAX_WORKERS,
    BINANCE_SYMBOLS,
    HISTORY_FILE,
    MONITOR_ALL_BINANCE_SYMBOLS,
    SQLITE_DB_FILE,
    TOP_N,
    WATCHLIST,
)
from data_sources.exchange import BinanceFuturesClient
from data_sources.hyperliquid import HyperliquidClient
from indicators.scoring import evaluate_snapshot
from output.report import save_report
from output.tweets import save_tweets
from output.x_publisher import publish_eligible_tweets
from storage.sqlite_store import save_market_snapshots


def main() -> None:
    """运行一次监控任务：拉数据、打标签、评分、输出报告和推文。"""
    binance = BinanceFuturesClient()
    hyperliquid = HyperliquidClient()
    evaluated: list[dict[str, Any]] = []
    universe = get_monitoring_universe(binance)
    premium_map = binance.get_all_mark_prices_and_funding() if MONITOR_ALL_BINANCE_SYMBOLS else {}

    print(f"本轮监控交易对数量：{len(universe)}")
    print(f"Binance 并发抓取线程数：{BINANCE_MAX_WORKERS}")

    # Binance 全量永续合约数量较多，串行抓取很容易超过计划任务窗口，所以这里并发执行。
    with ThreadPoolExecutor(max_workers=max(1, BINANCE_MAX_WORKERS)) as executor:
        futures = [
            executor.submit(process_symbol, item, binance, hyperliquid, premium_map)
            for item in universe
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            evaluated.append(future.result())
            if index % 50 == 0 or index == len(futures):
                print(f"已完成 {index}/{len(futures)} 个交易对")

    ranked = sorted(evaluated, key=lambda item: item["risk_score"], reverse=True)[:TOP_N]
    append_history(evaluated)
    save_market_snapshots(evaluated)
    save_report(ranked)
    tweets = save_tweets(ranked)
    publish_eligible_tweets(tweets)

    print("Crypto Squeeze Radar 本轮运行完成")
    print(f"已输出：{HISTORY_FILE}")
    print(f"已写入 SQLite：{SQLITE_DB_FILE}")
    print("已输出：output/report.md、output/tweets.json、output/tweets.md、output/x_post_preview.md")


def get_monitoring_universe(binance: BinanceFuturesClient) -> list[dict[str, str]]:
    """生成本轮监控交易对列表。"""
    if MONITOR_ALL_BINANCE_SYMBOLS:
        return binance.get_trading_symbols()
    return [{"coin": coin, "symbol": BINANCE_SYMBOLS[coin]} for coin in WATCHLIST]


def process_symbol(
    item: dict[str, str],
    binance: BinanceFuturesClient,
    hyperliquid: HyperliquidClient,
    premium_map: dict[str, Any],
) -> dict[str, Any]:
    """抓取并评估单个交易对；供并发线程调用。"""
    coin = item["coin"]
    symbol = item["symbol"]
    try:
        snapshot = binance.build_snapshot(
            coin,
            symbol=symbol,
            premium_data=premium_map.get(symbol),
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


def append_history(items: list[dict[str, Any]]) -> None:
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
        "long_liquidation_usd",
        "short_liquidation_usd",
        "risk_score",
        "risk_level",
        "tags",
        "source",
    ]
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
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
                    "long_liquidation_usd": item.get("long_liquidation_usd"),
                    "short_liquidation_usd": item.get("short_liquidation_usd"),
                    "risk_score": item.get("risk_score"),
                    "risk_level": item.get("risk_level"),
                    "tags": "、".join(item.get("tags", [])),
                    "source": item.get("source"),
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


if __name__ == "__main__":
    main()
