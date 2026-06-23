"""异常信号回测：统计某类异常出现后未来价格表现。

这个脚本不做预测，只用于长期积累数据后验证信号有效性。
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from config import SQLITE_DB_FILE


HORIZONS = [1, 4, 24]


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="统计异常标签出现后的未来价格表现")
    parser.add_argument("--tag", help="要回测的异常标签，例如：多头拥挤、空头拥挤、OI异常增加、清算异常、杠杆过热")
    parser.add_argument("--symbol", help="只统计某个交易对，例如 BTCUSDT")
    parser.add_argument("--min-score", type=int, default=0, help="只统计风险评分大于等于该值的样本")
    args = parser.parse_args()

    snapshots = load_snapshots(args.tag, args.symbol, args.min_score)
    if not snapshots:
        print("没有找到符合条件的历史样本。请先运行 main.py 积累 SQLite 数据。")
        return

    results = backtest(snapshots)
    print_summary(results, args.tag, args.symbol, args.min_score)


def load_snapshots(tag: str | None, symbol: str | None, min_score: int) -> list[dict[str, Any]]:
    """从 SQLite 读取历史快照，并按时间排序。"""
    if not SQLITE_DB_FILE.exists():
        return []

    conditions = ["price IS NOT NULL", "risk_score >= ?"]
    params: list[Any] = [min_score]

    if tag:
        conditions.append("anomaly_tag LIKE ?")
        params.append(f"%{tag}%")
    else:
        # 默认只统计非正常信号，避免把普通小时样本混入异常验证。
        conditions.append("anomaly_tag NOT LIKE ?")
        params.append("%正常%")

    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)

    sql = f"""
        SELECT timestamp_utc, symbol, price, risk_score, anomaly_tag
        FROM market_snapshots
        WHERE {' AND '.join(conditions)}
        ORDER BY symbol, timestamp_utc
    """

    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def load_all_prices() -> dict[str, list[dict[str, Any]]]:
    """读取所有价格点，作为未来收益计算的价格序列。"""
    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT timestamp_utc, symbol, price
            FROM market_snapshots
            WHERE price IS NOT NULL
            ORDER BY symbol, timestamp_utc
            """
        ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        item["dt"] = parse_time(item["timestamp_utc"])
        grouped[item["symbol"]].append(item)
    return grouped


def backtest(signals: list[dict[str, Any]]) -> dict[int, list[float]]:
    """对每个异常信号寻找未来 1h/4h/24h 的价格表现。"""
    prices_by_symbol = load_all_prices()
    returns: dict[int, list[float]] = {horizon: [] for horizon in HORIZONS}

    for signal in signals:
        start_price = signal["price"]
        if not start_price:
            continue

        signal_time = parse_time(signal["timestamp_utc"])
        price_series = prices_by_symbol.get(signal["symbol"], [])

        for horizon in HORIZONS:
            future_price = find_future_price(price_series, signal_time + timedelta(hours=horizon))
            if future_price is None:
                continue
            returns[horizon].append((future_price - start_price) / start_price * 100)

    return returns


def find_future_price(price_series: list[dict[str, Any]], target_time: datetime) -> float | None:
    """寻找目标时间之后的第一个价格点。"""
    for item in price_series:
        if item["dt"] >= target_time:
            return item["price"]
    return None


def print_summary(results: dict[int, list[float]], tag: str | None, symbol: str | None, min_score: int) -> None:
    """打印回测统计摘要。"""
    label = tag or "全部非正常异常"
    symbol_text = symbol or "全部交易对"
    print(f"异常标签：{label}")
    print(f"交易对：{symbol_text}")
    print(f"最低评分：{min_score}")
    print("")
    print("| 周期 | 样本数 | 平均涨跌幅 | 中位数 | 上涨占比 | 最大涨幅 | 最大跌幅 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")

    for horizon in HORIZONS:
        values = results[horizon]
        if not values:
            print(f"| {horizon}h | 0 | N/A | N/A | N/A | N/A | N/A |")
            continue
        win_rate = sum(1 for value in values if value > 0) / len(values) * 100
        avg_return = sum(values) / len(values)
        print(
            f"| {horizon}h | {len(values)} | {avg_return:.2f}% | "
            f"{median(values):.2f}% | {win_rate:.2f}% | {max(values):.2f}% | {min(values):.2f}% |"
        )


def parse_time(value: str) -> datetime:
    """解析 UTC ISO 时间字符串。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    main()

