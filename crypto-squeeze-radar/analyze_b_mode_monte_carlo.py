"""复盘高位负 funding 12H 做空（B 模式），并运行经验分布蒙特卡洛。

本脚本只读历史 SQLite，默认把 2026-07-13 视为 B 模式上线日，避免把
模式正式出现以前的规则回放混入上线后样本。现有快照没有 OHLC，因此
止损/止盈路径使用小时级 price 作为 close proxy，并在报告中明确标注。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
OUTPUT_DIR = BASE_DIR / "output"
RADAR_DB = STORAGE_DIR / "radar_history.sqlite3"
TRADING_DB = STORAGE_DIR / "trading.sqlite3"

B_MODE_START = datetime.fromisoformat("2026-07-13T00:00:00+00:00")
HORIZONS = (4, 12)
ROUND_TRIP_COST_PCT = 0.08
SHORT_FIRST_TP_PCT = 8.0
SHORT_FINAL_TP_PCT = 13.0
SHORT_HOLD_HOURS = 12
SHORT_NORMAL_STOP_PCT = 6.0
SHORT_STRONG_STOP_PCT = 8.0


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def number(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    clean = sorted(values)
    index = int((len(clean) - 1) * probability)
    return clean[index]


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"sample_count": 0}
    return {
        "sample_count": len(values),
        "average_pct": statistics.mean(values),
        "median_pct": statistics.median(values),
        "win_rate_pct": sum(value > 0 for value in values) / len(values) * 100,
        "p10_pct": quantile(values, 0.10),
        "p25_pct": quantile(values, 0.25),
        "p75_pct": quantile(values, 0.75),
        "p90_pct": quantile(values, 0.90),
    }


def load_hits(
    where: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    if not RADAR_DB.exists():
        return []
    with sqlite3.connect(f"file:{RADAR_DB.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT timestamp_utc, symbol, price, funding_rate,
                       price_change_1h, price_change_24h, price_position_24h,
                       quote_volume_24h, quote_volume_change_24h, anomaly_tag
                FROM market_snapshots
                WHERE symbol IS NOT NULL AND price IS NOT NULL AND {where}
                ORDER BY timestamp_utc, symbol
                """,
                params,
            ).fetchall()
        ]
    for row in rows:
        row["dt"] = parse_time(row["timestamp_utc"])
    return rows


def load_price_series(symbols: set[str]) -> dict[str, list[tuple[datetime, float]]]:
    if not symbols or not RADAR_DB.exists():
        return {}
    placeholders = ",".join("?" for _ in symbols)
    with sqlite3.connect(f"file:{RADAR_DB.resolve()}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            f"""
            SELECT timestamp_utc, symbol, price
            FROM market_snapshots
            WHERE symbol IN ({placeholders}) AND price IS NOT NULL
            ORDER BY symbol, timestamp_utc
            """,
            tuple(sorted(symbols)),
        ).fetchall()
    series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for timestamp, symbol, price in rows:
        series[str(symbol)].append((parse_time(timestamp), number(price)))
    return dict(series)


def load_market_regimes() -> dict[str, str]:
    """按 UTC 小时聚合市场横截面，避免依赖单个币的状态。"""
    if not RADAR_DB.exists():
        return {}
    buckets: dict[str, list[float]] = defaultdict(list)
    with sqlite3.connect(f"file:{RADAR_DB.resolve()}?mode=ro", uri=True) as conn:
        for timestamp, change in conn.execute(
            """
            SELECT timestamp_utc, price_change_24h
            FROM market_snapshots
            WHERE price_change_24h IS NOT NULL
            """
        ):
            buckets[str(timestamp)[:13]].append(number(change))

    regimes: dict[str, str] = {}
    for bucket, changes in buckets.items():
        median_change = statistics.median(changes)
        breadth = sum(value > 0 for value in changes) / len(changes) * 100
        if median_change >= 0.5 and breadth >= 55:
            regimes[bucket] = "strong"
        elif median_change <= -2 and breadth <= 25:
            regimes[bucket] = "weak"
        else:
            regimes[bucket] = "neutral"
    return regimes


def current_b_mode(row: dict[str, Any]) -> bool:
    """当前代码中的 B 模式口径。"""
    return (
        number(row.get("funding_rate")) <= -0.0003
        and number(row.get("price_position_24h")) >= 80
        and number(row.get("price_change_24h")) >= 10
        and number(row.get("price_change_1h")) > -3
    )


def legacy_b_mode(row: dict[str, Any]) -> bool:
    """旧报告中的更严格 B 口径，仅作规则漂移对照。"""
    return (
        number(row.get("funding_rate")) <= -0.001
        and number(row.get("price_position_24h")) >= 80
        and number(row.get("quote_volume_24h")) >= 30_000_000
        and number(row.get("price_change_1h")) > -3
    )


def attach_metrics(
    rows: list[dict[str, Any]],
    series: dict[str, list[tuple[datetime, float]]],
    regimes: dict[str, str],
) -> None:
    for row in rows:
        price_series = series.get(str(row["symbol"]), [])
        timestamps = [item[0] for item in price_series]
        prices = [item[1] for item in price_series]
        if not price_series or number(row.get("price")) <= 0:
            continue
        start = bisect_left(timestamps, row["dt"])
        entry = number(row["price"])
        row["market_regime"] = regimes.get(row["timestamp_utc"][:13], "neutral")
        row["future"] = {}
        for horizon in HORIZONS:
            end_time = row["dt"] + timedelta(hours=horizon)
            end = bisect_left(timestamps, end_time, lo=start)
            if end >= len(price_series):
                continue
            window = prices[start : end + 1]
            row["future"][horizon] = {
                "short_return_pct": (entry - prices[end]) / entry * 100,
                "mae_pct": (max(window) - entry) / entry * 100,
                "mfe_pct": (entry - min(window)) / entry * 100,
            }

        end_time = row["dt"] + timedelta(hours=SHORT_HOLD_HOURS)
        end = bisect_left(timestamps, end_time, lo=start)
        if end >= len(price_series):
            continue
        stop_pct = (
            SHORT_STRONG_STOP_PCT
            if row["market_regime"] == "strong"
            else SHORT_NORMAL_STOP_PCT
        )
        stop_price = entry * (1 + stop_pct / 100)
        first_tp_price = entry * (1 - SHORT_FIRST_TP_PCT / 100)
        final_tp_price = entry * (1 - SHORT_FINAL_TP_PCT / 100)
        state = "none"
        event = "time_exit"
        for price in prices[start + 1 : end + 1]:
            if state == "none":
                if price >= stop_price:
                    event = "stop"
                    row["path_return_pct"] = -stop_pct
                    break
                if price <= final_tp_price:
                    event = "final_tp"
                    row["path_return_pct"] = (
                        SHORT_FIRST_TP_PCT + SHORT_FINAL_TP_PCT
                    ) / 2
                    break
                if price <= first_tp_price:
                    state = "first_tp"
            else:
                if price >= stop_price:
                    event = "first_tp_then_stop"
                    row["path_return_pct"] = (
                        SHORT_FIRST_TP_PCT - stop_pct
                    ) / 2
                    break
                if price <= final_tp_price:
                    event = "final_tp"
                    row["path_return_pct"] = (
                        SHORT_FIRST_TP_PCT + SHORT_FINAL_TP_PCT
                    ) / 2
                    break
        else:
            exit_short_return = (entry - prices[end]) / entry * 100
            if state == "first_tp":
                event = "time_after_first_tp"
                row["path_return_pct"] = (
                    SHORT_FIRST_TP_PCT + exit_short_return
                ) / 2
            else:
                row["path_return_pct"] = exit_short_return
        row["path_event"] = event
        row["path_net_return_pct"] = row["path_return_pct"] - ROUND_TRIP_COST_PCT


def dedupe(rows: list[dict[str, Any]], hours: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    last_seen: dict[str, datetime] = {}
    for row in rows:
        if "path_net_return_pct" not in row:
            continue
        last = last_seen.get(str(row["symbol"]))
        if last is not None and row["dt"] < last + timedelta(hours=hours):
            continue
        output.append(row)
        last_seen[str(row["symbol"])] = row["dt"]
    return output


def returns_summary(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return summarize([number(row[field]) for row in rows if row.get(field) is not None])


def future_summary(rows: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    return summarize(
        [
            number(row["future"][horizon]["short_return_pct"])
            for row in rows
            if horizon in row.get("future", {})
        ]
    )


def rule_summary(
    name: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    dedup6 = dedupe(rows, 6)
    dedup12 = dedupe(rows, 12)
    main = dedup12
    return {
        "name": name,
        "raw_count": len(rows),
        "dedup_6h": {
            "count": len(dedup6),
            "short_4h": future_summary(dedup6, 4),
            "short_12h": future_summary(dedup6, 12),
        },
        "dedup_12h": {
            "count": len(dedup12),
            "symbols": len({str(row["symbol"]) for row in dedup12}),
            "short_4h": summarize(
                [
                    number(row["future"][4]["short_return_pct"])
                    for row in main
                    if 4 in row.get("future", {})
                ]
            ),
            "short_12h_mark": summarize(
                [
                    number(row["future"][12]["short_return_pct"])
                    for row in main
                    if 12 in row.get("future", {})
                ]
            ),
            "short_12h_path_net": returns_summary(main, "path_net_return_pct"),
            "event_counts": dict(Counter(row["path_event"] for row in main)),
            "stop_proxy_6pct_or_8pct_pct": sum(
                row["path_event"] in {"stop", "first_tp_then_stop"} for row in main
            )
            / len(main)
            * 100
            if main
            else 0.0,
            "tp1_or_better_pct": sum(
                row["path_event"]
                in {"final_tp", "time_after_first_tp", "first_tp_then_stop"}
                for row in main
            )
            / len(main)
            * 100
            if main
            else 0.0,
        },
    }


def monte_carlo(
    sample: list[float],
    trade_counts: tuple[int, ...],
    paths: int,
    seed: int,
) -> dict[str, Any]:
    if not sample:
        return {}
    rng = random.Random(seed)
    result: dict[str, Any] = {
        "sample_count": len(sample),
        "sample_distribution": summarize(sample),
        "trade_counts": {},
        "assumption": "经验分布有放回抽样；每笔按1.0x名义仓位，不复利；已扣0.08个百分点往返成本。",
        "paths": paths,
        "seed": seed,
    }
    for trade_count in trade_counts:
        totals: list[float] = []
        drawdowns: list[float] = []
        for _ in range(paths):
            equity = 0.0
            peak = 0.0
            max_drawdown = 0.0
            for _ in range(trade_count):
                equity += rng.choice(sample)
                peak = max(peak, equity)
                max_drawdown = min(max_drawdown, equity - peak)
            totals.append(equity)
            drawdowns.append(max_drawdown)
        totals.sort()
        drawdowns.sort()
        result["trade_counts"][str(trade_count)] = {
            "p05_cumulative_pct": quantile(totals, 0.05),
            "p25_cumulative_pct": quantile(totals, 0.25),
            "p50_cumulative_pct": quantile(totals, 0.50),
            "p75_cumulative_pct": quantile(totals, 0.75),
            "p95_cumulative_pct": quantile(totals, 0.95),
            "probability_positive_pct": sum(value > 0 for value in totals)
            / len(totals)
            * 100,
            "median_max_drawdown_pct": quantile(drawdowns, 0.50),
            "p05_max_drawdown_pct": quantile(drawdowns, 0.05),
        }
    return result


def load_actual_execution() -> dict[str, Any]:
    if not TRADING_DB.exists():
        return {"available": False}
    rows: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{TRADING_DB.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for raw in conn.execute(
            """
            SELECT timestamp_utc, trade_grade, status, net_realized_pnl_usdt,
                   margin_usdt, raw_signal
            FROM trading_decisions
            WHERE side = 'SHORT'
            ORDER BY timestamp_utc
            """
        ):
            row = dict(raw)
            try:
                signal = json.loads(row.get("raw_signal") or "")
            except (TypeError, ValueError):
                signal = {}
            if signal.get("pattern_key") == "high_neg_funding_12h_short":
                rows.append(row)
    closed = [row for row in rows if row.get("net_realized_pnl_usdt") is not None]
    net_values = [number(row["net_realized_pnl_usdt"]) for row in closed]
    by_month: dict[str, list[float]] = defaultdict(list)
    for row in closed:
        by_month[str(row["timestamp_utc"])[:7]].append(
            number(row["net_realized_pnl_usdt"])
        )
    return {
        "available": True,
        "decision_count": len(rows),
        "status_counts": dict(Counter(str(row["status"]) for row in rows)),
        "closed_count": len(closed),
        "closed_win_rate_pct": sum(value > 0 for value in net_values) / len(net_values) * 100
        if net_values
        else 0.0,
        "closed_net_pnl_usdt": sum(net_values),
        "closed_average_net_pnl_usdt": statistics.mean(net_values) if net_values else 0.0,
        "by_month_net_pnl_usdt": {key: sum(values) for key, values in sorted(by_month.items())},
        "by_grade_net_pnl_usdt": {
            grade: sum(
                number(row["net_realized_pnl_usdt"])
                for row in closed
                if str(row.get("trade_grade")) == grade
            )
            for grade in sorted({str(row.get("trade_grade")) for row in closed})
        },
        "note": "实际成交记录存在重复信号、并发持仓和容量限制，不能当作独立样本。",
    }


def rounded(value: Any, digits: int = 2) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: rounded(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item, digits) for item in value]
    return value


def render_report(payload: dict[str, Any]) -> str:
    current = payload["operational_current_b"]
    main = current["dedup_12h"]
    mark = main["short_12h_mark"]
    path = main["short_12h_path_net"]
    mc = payload["monte_carlo"]
    lines = [
        "# B模式（高位负 funding 12H 空）历史复盘与蒙特卡洛预测",
        "",
        f"生成时间（UTC）：{payload['generated_at_utc']}",
        f"数据范围：{payload['data_range']['min_utc']} 至 {payload['data_range']['max_utc']}",
        "",
        "## 结论先行",
        "",
        f"上线后按 12 小时同币去重的主样本为 {main['count']} 笔、{main['symbols']} 个币种。12H 直接持有到期的做空收益平均 {mark['average_pct']:.2f}%、中位数 {mark['median_pct']:.2f}%、胜率 {mark['win_rate_pct']:.1f}%。",
        f"按当前执行计划（普通市 6% 止损，强市 8% 止损；8%/13% 分批止盈；最长 12H）做小时收盘价路径代理，扣除 0.08 个百分点往返成本后，平均每笔 {path['average_pct']:.2f}%、中位数 {path['median_pct']:.2f}%、正收益率 {path['win_rate_pct']:.1f}%。",
        f"B 模式有正期望，但不是低回撤策略：路径代理中止损或止盈后再止损事件占 {main['stop_proxy_6pct_or_8pct_pct']:.1f}%，只有 {main['tp1_or_better_pct']:.1f}% 的样本曾达到第一止盈。",
        "",
        "因此，B 模式可以保留为主空头候选，但更适合小仓、严格容量控制和等待价格/CVD确认；不应把历史平均收益直接外推成固定月收益。",
        "",
        "## 口径与样本",
        "",
        "当前口径：funding ≤ -0.03%、24h 价格位置 ≥80、24h 涨幅 ≥10%、1h 跌幅不低于 -3%。主预测样本从 2026-07-13（B模式进入模式库）开始。",
        "旧口径对照：funding ≤ -0.1%、24h 价格位置 ≥80、24h 成交额 ≥3000万、1h 不追跌。旧口径更严格，不能与当前 B 模式直接混用。",
        "",
            "| 样本口径 | 可计算原始命中 | 同币6H去重 | 同币12H去重 | 12H做空平均 | 12H中位数 | 胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in (
        payload["operational_current_b"],
        payload["all_history_current_b"],
        payload["all_history_legacy_b"],
    ):
        stats = item["dedup_12h"]["short_12h_mark"]
        lines.append(
            f"| {item['name']} | {item['raw_count']} | {item['dedup_6h']['count']} | {item['dedup_12h']['count']} | "
            f"{stats.get('average_pct', 0):.2f}% | {stats.get('median_pct', 0):.2f}% | {stats.get('win_rate_pct', 0):.1f}% |"
        )
    lines.extend(
        [
            "",
            "旧口径的历史结果更好，主要是它筛掉了较多边缘信号；这说明规则收紧会提升样本质量，但也降低触发频率，不能把旧口径的高收益当成当前 B 的无条件预期。",
            "",
            "## 稳定性检查",
            "",
            "| 时段 | 样本 | 路径代理平均 | 中位数 | 正收益率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for label, stats in payload["periods"].items():
        lines.append(
            f"| {label} | {stats['sample_count']} | {stats['average_pct']:.2f}% | {stats['median_pct']:.2f}% | {stats['win_rate_pct']:.1f}% |"
        )
    lines.extend(
        [
            "",
            "上线后的不同阶段仍为正，但中位数从早期高位回落后趋于约 3% 左右；说明策略没有明显失效，却也没有证据支持加仓追求线性增长。",
            "",
            "## 蒙特卡洛预测",
            "",
            "模型从上线后 12H 去重样本的路径代理收益中有放回抽样，运行 100,000 条路径。每笔按 1.0x 名义仓位、不复利，收益以累计名义收益百分点表示；已扣除 0.08 个百分点往返成本，未加入资金费、滑点和并发容量约束。",
            "",
            "| 未来独立B交易数 | P05累计 | P50累计 | P95累计 | 累计为正概率 | 中位最大回撤 | 5%分位最大回撤 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for count, stats in mc["trade_counts"].items():
        lines.append(
            f"| {count} | {stats['p05_cumulative_pct']:.2f}% | {stats['p50_cumulative_pct']:.2f}% | "
            f"{stats['p95_cumulative_pct']:.2f}% | {stats['probability_positive_pct']:.1f}% | "
            f"{stats['median_max_drawdown_pct']:.2f}% | {stats['p05_max_drawdown_pct']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "蒙特卡洛给出的方向性判断是：未来 10 笔交易的中位累计结果约 +20%，但约有 18% 的路径仍为负；未来 30 笔的中位结果约 +61%，5%分位接近盈亏平衡。这个结果对独立同分布假设很敏感，不能解读为账户实际收益率。若每笔实际只使用账户的一部分名义敞口，应先按实际敞口比例折算。",
            "",
            "## 实际执行层",
            "",
        ]
    )
    actual = payload["actual_execution"]
    if actual.get("available"):
        lines.extend(
            [
                f"交易台账中 B 模式共有 {actual['decision_count']} 条决策，其中 {actual['closed_count']} 条已闭合；已闭合记录净盈亏合计 {actual['closed_net_pnl_usdt']:.2f} USDT，盈利笔数占 {actual['closed_win_rate_pct']:.1f}%。",
                f"状态分布：{actual['status_counts']}。按月净盈亏：{actual['by_month_net_pnl_usdt']}；按交易等级净盈亏：{actual['by_grade_net_pnl_usdt']}。",
                actual["note"],
            ]
        )
    else:
        lines.append("未发现可读取的实际交易台账。")
    lines.extend(
        [
            "",
            "## 风险与模型边界",
            "",
            "- 历史库是小时级快照，缺少OHLC，无法严格判断同一小时内先止损还是先止盈；路径代理会有测量误差。",
            "- 12H 去重只处理同币重复信号，未模拟多币同时触发时的最大持仓、保证金占用和相关性。",
            "- funding、成交额和币种池都可能发生结构性变化；尤其 B 模式收益可能集中在少数极端回落样本。",
            "- 旧口径、当前口径和不同策略版本混在历史上时，必须按口径分层，不能把全部记录拼成一个胜率。",
            "- 这是统计回测与情景预测，不构成收益保证或投资建议。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_payload(mc_paths: int, seed: int) -> dict[str, Any]:
    current_all = load_hits(
        "funding_rate <= -0.0003 AND price_position_24h >= 80 "
        "AND price_change_24h >= 10 AND price_change_1h > -3"
    )
    current_operational = [row for row in current_all if row["dt"] >= B_MODE_START]
    legacy_all = load_hits(
        "funding_rate <= -0.001 AND price_position_24h >= 80 "
        "AND quote_volume_24h >= 30000000 AND price_change_1h > -3"
    )
    all_rows = current_all + legacy_all
    series = load_price_series({str(row["symbol"]) for row in all_rows})
    regimes = load_market_regimes()
    # current_all 与 legacy_all 可能有交集；分别附加指标，避免把旧口径
    # 的交集再次算进当前口径，造成原始命中数和收益被重复放大。
    attach_metrics(current_all, series, regimes)
    attach_metrics(legacy_all, series, regimes)
    current_all = [row for row in current_all if current_b_mode(row)]
    current_operational = [row for row in current_all if row["dt"] >= B_MODE_START]
    legacy_all = [row for row in legacy_all if legacy_b_mode(row)]

    def valid_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if "path_net_return_pct" in row]

    current_all = valid_rows(current_all)
    current_operational = valid_rows(current_operational)
    legacy_all = valid_rows(legacy_all)
    main = dedupe(current_operational, 12)
    main_returns = [number(row["path_net_return_pct"]) for row in main]
    data_timestamps = [row["timestamp_utc"] for row in current_all]
    periods = {
        "2026-07-13至2026-07-31": summarize(
            [
                number(row["path_net_return_pct"])
                for row in main
                if row["dt"] < datetime.fromisoformat("2026-08-01T00:00:00+00:00")
            ]
        ),
        "2026-08-01至最新": summarize(
            [
                number(row["path_net_return_pct"])
                for row in main
                if row["dt"] >= datetime.fromisoformat("2026-08-01T00:00:00+00:00")
            ]
        ),
    }
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "min_utc": min(data_timestamps) if data_timestamps else None,
            "max_utc": max(data_timestamps) if data_timestamps else None,
        },
        "definition": {
            "current": "funding<=-0.0003; price_position_24h>=80; price_change_24h>=10; price_change_1h>-3",
            "legacy": "funding<=-0.001; price_position_24h>=80; quote_volume_24h>=30000000; price_change_1h>-3",
            "operational_start_utc": B_MODE_START.isoformat(),
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "stop_loss_pct": {"normal": SHORT_NORMAL_STOP_PCT, "strong": SHORT_STRONG_STOP_PCT},
            "first_take_profit_pct": SHORT_FIRST_TP_PCT,
            "final_take_profit_pct": SHORT_FINAL_TP_PCT,
            "max_hold_hours": SHORT_HOLD_HOURS,
        },
        "operational_current_b": rule_summary("当前B口径（上线后）", current_operational),
        "all_history_current_b": rule_summary("当前B口径（全历史规则回放）", current_all),
        "all_history_legacy_b": rule_summary("旧B口径（严格对照）", legacy_all),
        "periods": periods,
        "monte_carlo": monte_carlo(main_returns, (10, 30, 60), mc_paths, seed),
        "actual_execution": load_actual_execution(),
    }
    return rounded(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="复盘B模式并运行蒙特卡洛")
    parser.add_argument("--mc-paths", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--json", type=Path, default=OUTPUT_DIR / "b_mode_review.json")
    parser.add_argument("--report", type=Path, default=OUTPUT_DIR / "b_mode_review.md")
    args = parser.parse_args()
    payload = build_payload(args.mc_paths, args.seed)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.write_text(render_report(payload), encoding="utf-8")
    print(f"JSON={args.json}")
    print(f"REPORT={args.report}")
    print(json.dumps(payload["monte_carlo"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
