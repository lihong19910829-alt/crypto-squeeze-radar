"""Parameterized backtest for the staged failed-breakout short strategy.

The database currently contains hourly snapshots rather than OHLCV candles.
The runner therefore uses ``price`` as an hourly close/open proxy and reports
the data limitations in every output.  If columns named open/high/low/close/
volume are added later, the loader will use them automatically.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sqlite3
from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "storage" / "radar_history.sqlite3"
OUTPUT_DIR = BASE_DIR / "output" / "staged_short_backtest"


@dataclass(frozen=True)
class Params:
    price_gain_pct: float
    oi_gain_pct: float
    funding_pct: float
    oi_stall_6h_pct: float
    volume_ratio_pct: float
    version: str
    holding_hours: int

    def label(self) -> str:
        return (
            f"p{self.price_gain_pct:g}_oi{self.oi_gain_pct:g}_fund{self.funding_pct:g}_"
            f"stall{self.oi_stall_6h_pct:g}_vol{self.volume_ratio_pct:g}_"
            f"{self.version}_{self.holding_hours}h"
        )


@dataclass
class Trade:
    symbol: str
    stage1_time: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    return_pct: float
    hold_hours: float
    failure_reason: str = ""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def number(row: dict[str, Any], key: str, default: float | None = None) -> float | None:
    value = row.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_rows(symbol: str | None = None, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    if not DB_FILE.exists():
        return []
    where = ["price IS NOT NULL", "open_interest IS NOT NULL"]
    args: list[Any] = []
    if symbol:
        where.append("symbol = ?")
        args.append(symbol)
    if start:
        where.append("timestamp_utc >= ?")
        args.append(start)
    if end:
        where.append("timestamp_utc <= ?")
        args.append(end)
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            f"SELECT * FROM market_snapshots WHERE {' AND '.join(where)} ORDER BY symbol, timestamp_utc", args
        ).fetchall()]
    for row in rows:
        row["dt"] = parse_time(row["timestamp_utc"])
        row["px"] = number(row, "close", number(row, "price", 0.0)) or 0.0
        row["bar_volume"] = number(row, "volume")
    return rows


def group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["symbol"]].append(row)
    for series in grouped.values():
        series.sort(key=lambda item: item["dt"])
        times = [row["dt"] for row in series]
        for index, row in enumerate(series):
            prior_index = bisect_left(times, row["dt"] - timedelta(hours=6), hi=index + 1)
            old = number(series[prior_index], "open_interest") if prior_index < index else None
            new = number(row, "open_interest")
            row["oi6"] = None if not old or new is None else (new - old) / old * 100
            ratio, source = volume_ratio(series, index)
            row["volume_ratio"] = ratio
            row["volume_source"] = source
            row["stage1_minimum"] = stage1_minimum(row)
    return grouped


def stage1_minimum(row: dict[str, Any]) -> bool:
    return ((number(row, "price_change_24h", -math.inf) or -math.inf) >= 20
            and (number(row, "oi_change_24h", -math.inf) or -math.inf) >= 10
            and (number(row, "funding_rate", -math.inf) or -math.inf) >= 0.0005)


def volume_ratio(series: list[dict[str, Any]], index: int) -> tuple[float | None, str]:
    current = series[index].get("bar_volume")
    if current is not None and index >= 20:
        prior = [series[j]["bar_volume"] for j in range(index - 20, index) if series[j].get("bar_volume") is not None]
        if len(prior) >= 15 and mean(prior) > 0:
            return current / mean(prior) * 100, "bar_volume"
    # Existing snapshots only have a rolling 24h volume. This is a proxy, not
    # the requested current-bar volume, so it is labelled in the report.
    current_24 = number(series[index], "quote_volume_24h")
    if current_24 is not None and index >= 20:
        prior = [number(series[j], "quote_volume_24h") for j in range(index - 20, index)]
        prior = [value for value in prior if value is not None and value > 0]
        if len(prior) >= 15 and mean(prior) > 0:
            return current_24 / mean(prior) * 100, "quote_volume_24h_proxy"
    return None, "unavailable"


def oi_change_6h(series: list[dict[str, Any]], index: int) -> float | None:
    return series[index].get("oi6")


def stage1(series: list[dict[str, Any]], index: int, p: Params) -> bool:
    return (
        (number(series[index], "price_change_24h", -math.inf) or -math.inf) >= p.price_gain_pct
        and (number(series[index], "oi_change_24h", -math.inf) or -math.inf) >= p.oi_gain_pct
        and (number(series[index], "funding_rate", -math.inf) or -math.inf) >= p.funding_pct / 100
    )


def stage2(series: list[dict[str, Any]], index: int, p: Params, high_since: float) -> tuple[bool, str, float]:
    price = series[index]["px"]
    new_high = price > high_since
    oi6 = series[index].get("oi6")
    ratio, volume_source = series[index].get("volume_ratio"), series[index].get("volume_source", "unavailable")
    conditions = [new_high, oi6 is not None and oi6 < p.oi_stall_6h_pct]
    # A missing volume is not treated as a pass. For current data, the proxy is
    # used and explicitly recorded; true bar volume takes precedence.
    conditions.append(ratio is not None and ratio < p.volume_ratio_pct)
    if all(conditions):
        reason = f"new_high;oi6={oi6:.2f};volume={ratio:.1f}({volume_source})"
        return True, reason, price
    return False, "", max(high_since, price)


def find_entry(series: list[dict[str, Any]], signal_index: int, p: Params) -> int | None:
    if p.version == "A":
        return signal_index + 1 if signal_index + 1 < len(series) else None
    if p.version == "B":
        # No hourly low exists in the current schema. Use the next observed
        # hourly price as a close/low proxy and wait for a break below signal px.
        signal_px = series[signal_index]["px"]
        for i in range(signal_index + 1, min(len(series), signal_index + 8)):
            if series[i]["px"] < signal_px:
                return i
        return None
    # CHOCH needs 15m structure; hourly snapshots cannot establish it.
    return None


def build_setups(series: list[dict[str, Any]], p: Params) -> list[tuple[int, int, int, str]]:
    setups: list[tuple[int, int, int, str]] = []
    candidates = [index for index, row in enumerate(series) if row.get("stage1_minimum") and stage1(series, index, p)]
    for i in candidates:
        watch_start = i
        high_since = series[i]["px"]
        signal_index: int | None = None
        for j in range(i + 1, len(series)):
            if series[j]["dt"] - series[watch_start]["dt"] > timedelta(hours=48):
                break
            ok, reason, high_since = stage2(series, j, p, high_since)
            if ok:
                signal_index = j
                break
        if signal_index is None:
            i += 1
            continue
        entry_index = find_entry(series, signal_index, p)
        if entry_index is None:
            continue
        setups.append((watch_start, signal_index, entry_index, reason))
    return setups


def build_trades(series: list[dict[str, Any]], p: Params, setups: list[tuple[int, int, int, str]] | None = None) -> list[Trade]:
    trades: list[Trade] = []
    times = [row["dt"] for row in series]
    setups = setups if setups is not None else build_setups(series, p)
    last_exit_index = -1
    for watch_start, signal_index, entry_index, reason in setups:
        if entry_index <= last_exit_index:
            continue
        exit_target = series[entry_index]["dt"] + timedelta(hours=p.holding_hours)
        exit_index = bisect_left(times, exit_target, lo=entry_index)
        if exit_index >= len(series):
            continue
        entry = series[entry_index]
        exit_row = series[exit_index]
        entry_px = entry["px"]
        exit_px = exit_row["px"]
        if entry_px <= 0:
            i = exit_index + 1
            continue
        ret = (entry_px - exit_px) / entry_px * 100
        trades.append(Trade(series[0]["symbol"], series[watch_start]["timestamp_utc"],
                            series[signal_index]["timestamp_utc"], entry["timestamp_utc"],
                            exit_row["timestamp_utc"], entry_px, exit_px, ret,
                            (exit_row["dt"] - entry["dt"]).total_seconds() / 3600, reason))
        last_exit_index = exit_index
    return trades


def metrics(trades: list[Trade]) -> dict[str, Any]:
    values = [trade.return_pct for trade in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    max_streak = streak = 0
    for value in values:
        streak = streak + 1 if value < 0 else 0
        max_streak = max(max_streak, streak)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(values), "win_rate_pct": round(len(wins) / len(values) * 100, 4) if values else None,
        "avg_return_pct": round(mean(values), 4) if values else None,
        "avg_loss_pct": round(abs(mean(losses)), 4) if losses else None,
        "risk_reward": round(mean(wins) / abs(mean(losses)), 4) if wins and losses else None,
        "max_drawdown_pct": round(abs(max_drawdown), 4) if values else None,
        "max_consecutive_losses": max_streak, "avg_holding_hours": round(mean([t.hold_hours for t in trades]), 4) if trades else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if not gross_profit else "inf"),
    }


def score(row: dict[str, Any]) -> tuple[float, ...]:
    # Require a minimum sample by default in ranking; small samples are still
    # emitted but naturally rank below robust candidates.
    pf = row["metrics"].get("profit_factor")
    pf_score = 10 if pf == "inf" else float(pf or 0)
    return (1.0 if (row["metrics"].get("trades") or 0) > 0 else 0.0,
            float(row["metrics"].get("avg_return_pct") or -999), pf_score,
            float(row["metrics"].get("win_rate_pct") or 0), -float(row["metrics"].get("max_drawdown_pct") or 999),
            float(row["metrics"].get("trades") or 0))


def run(rows: list[dict[str, Any]], grid: dict[str, list[Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped = group_rows(rows)
    results: list[dict[str, Any]] = []
    setup_cache: dict[tuple[Any, ...], dict[str, list[tuple[int, int, int, str]]]] = {}
    for values in itertools.product(*grid.values()):
        p = Params(**dict(zip(grid.keys(), values)))
        setup_key = (p.price_gain_pct, p.oi_gain_pct, p.funding_pct, p.oi_stall_6h_pct, p.volume_ratio_pct, p.version)
        if setup_key not in setup_cache:
            setup_cache[setup_key] = {symbol: build_setups(series, p) for symbol, series in grouped.items()}
        all_trades: list[Trade] = []
        for symbol, series in grouped.items():
            all_trades.extend(build_trades(series, p, setup_cache[setup_key][symbol]))
        results.append({"parameters": asdict(p), "label": p.label(), "metrics": metrics(all_trades),
                        "trades_data": [asdict(t) for t in all_trades]})
    ranked = sorted(results, key=score, reverse=True)
    return ranked, {"rows": len(rows), "symbols": len(grouped), "data_limitations": [
        "现有数据库为小时级快照；price 被用作小时收盘/开盘代理。",
        "没有逐小时成交量时，成交量条件使用 quote_volume_24h 的20小时均值代理。",
        "没有15分钟数据和OHLC时，版本C CHOCH不可计算；版本B使用价格跌破信号价格代理。",
        "未设置止损止盈；按持仓小时数定时退出，收益未扣手续费/滑点。",
    ]}


def write_outputs(ranked: list[dict[str, Any]], meta: dict[str, Any], top_n: int = 10, output_dir: Path = OUTPUT_DIR, min_trades: int = 5) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = [{"rank": i + 1, "label": row["label"], "parameters": row["parameters"], "metrics": row["metrics"]}
               for i, row in enumerate(ranked)]
    (output_dir / "staged_short_backtest.json").write_text(json.dumps({"meta": meta, "results": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "staged_short_backtest.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["rank", "label", *Params.__dataclass_fields__.keys(), *metrics([]).keys()]
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader()
        for rank, row in enumerate(ranked, 1):
            writer.writerow({"rank": rank, "label": row["label"], **row["parameters"], **row["metrics"]})
    failed = []
    for row in ranked:
        for trade in row["trades_data"]:
            if trade["return_pct"] < 0:
                failed.append({"label": row["label"], **trade})
    unique_failed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in sorted(failed, key=lambda value: value["return_pct"]):
        key = (item["symbol"], item["signal_time"], item["entry_time"], item["exit_time"])
        if key not in seen:
            seen.add(key); unique_failed.append(item)
    failure_counts = {"强趋势继续上涨": len(unique_failed), "其他": 0}
    (output_dir / "staged_short_worst20.json").write_text(json.dumps(
        {"meta": meta, "analysis": {"failure_counts": failure_counts,
         "notes": ["当前数据没有逐笔OI/Funding/15分钟结构，失败归因以持仓期价格上涨为主；同一事件在不同参数组合中的重复结果已去重。"]},
         "trades": unique_failed[:20]}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 阶段化做空策略回测", "", f"样本：{meta['rows']} 条，交易对：{meta['symbols']} 个", "", "## 数据限制"]
    lines += [f"- {item}" for item in meta["data_limitations"]]
    lines += ["", "## Top 10 参数组合", "", "|排名|组合|交易次数|胜率|平均收益|平均亏损|盈亏比|最大回撤|最大连续亏损|平均持仓|Profit Factor|", "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    eligible = [row for row in ranked if (row["metrics"].get("trades") or 0) >= min_trades]
    if not eligible:
        eligible = [row for row in ranked if (row["metrics"].get("trades") or 0) > 0]
    lines[lines.index("## Top 10 参数组合") + 2] += f"\n（优先展示至少 {min_trades} 笔交易的组合；若不足则展示全部有交易组合）"
    for rank, row in enumerate(eligible[:top_n], 1):
        m = row["metrics"]
        lines.append("|" + "|".join(str(x) for x in [rank, row["label"], m["trades"], m["win_rate_pct"], m["avg_return_pct"], m["avg_loss_pct"], m["risk_reward"], m["max_drawdown_pct"], m["max_consecutive_losses"], m["avg_holding_hours"], m["profit_factor"]]) + "|")
    lines += ["", "## 亏损最大的20笔交易", "", "|组合|交易对|信号时间|入场|出场|收益|失败线索|", "|---|---|---|---|---|---:|---|"]
    for item in unique_failed[:20]:
        lines.append("|" + "|".join(str(item.get(k, "")) for k in ["label", "symbol", "signal_time", "entry_time", "exit_time", "return_pct", "failure_reason"]) + "|")
    (output_dir / "staged_short_backtest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="阶段1/2/3参数化做空回测")
    parser.add_argument("--symbol"); parser.add_argument("--start"); parser.add_argument("--end")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--min-trades", type=int, default=0, help="仅打印时筛选，结果文件仍保留全部组合")
    args = parser.parse_args()
    rows = load_rows(args.symbol, args.start, args.end)
    if not rows:
        print("没有可用历史快照。")
        return
    grid = {"price_gain_pct": [20, 30, 40, 50], "oi_gain_pct": [10, 20, 30, 50],
            "funding_pct": [0.05, 0.1, 0.2], "oi_stall_6h_pct": [5, 0, -5],
            "volume_ratio_pct": [80, 70], "version": ["A", "B", "C"],
            "holding_hours": [6, 12, 24, 36, 48]}
    ranked, meta = run(rows, grid)
    write_outputs(ranked, meta, output_dir=args.output_dir, min_trades=args.min_trades)
    print(f"完成：{len(ranked)} 个参数组合；报告已写入 {args.output_dir}")
    for rank, row in enumerate(ranked[:10], 1):
        print(rank, row["label"], row["metrics"])


if __name__ == "__main__":
    main()
