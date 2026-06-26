"""生成 Markdown 监控报告。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import REPORT_MD_FILE
from indicators.outcome_probability import format_outcome_probability


def save_report(items: list[dict[str, Any]]) -> None:
    """保存本轮 Top 异常报告。"""
    REPORT_MD_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Crypto Squeeze Radar Report",
        "",
        f"- 生成时间 UTC：{datetime.now(timezone.utc).isoformat()}",
        "- 说明：本报告只用于市场结构监控，不构成投资建议。",
        "",
        "| 排名 | 币种 | 评分 | 等级 | 标签 | 后验概率 | 价格 | Funding | OI 1h | OI 24h | 多头清算 | 空头清算 | 数据源 |",
        "| --- | --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(
            "| {rank} | {coin} | {score} | {level} | {tags} | {outcome} | {price} | {funding} | {oi1h} | {oi24h} | {long_liq} | {short_liq} | {source} |".format(
                rank=index,
                coin=item["coin"],
                score=item["risk_score"],
                level=item["risk_level"],
                tags="、".join(item["tags"]),
                outcome=format_outcome_probability(item),
                price=_fmt_num(item.get("price")),
                funding=_fmt_pct((item.get("funding_rate") or 0) * 100 if item.get("funding_rate") is not None else None),
                oi1h=_fmt_pct(item.get("oi_change_1h_pct")),
                oi24h=_fmt_pct(item.get("oi_change_24h_pct")),
                long_liq=_fmt_usd(item.get("long_liquidation_usd")),
                short_liq=_fmt_usd(item.get("short_liquidation_usd")),
                source=item["source"],
            )
        )

    warnings = [warning for item in items for warning in item.get("warnings", [])]
    if warnings:
        lines.extend(["", "## 数据提示", ""])
        lines.extend(f"- {warning}" for warning in warnings)

    REPORT_MD_FILE.write_text("\n".join(lines), encoding="utf-8")


def _fmt_num(value: float | None) -> str:
    """格式化普通数字。"""
    if value is None:
        return "N/A"
    return f"{value:,.4f}"


def _fmt_usd(value: float | None) -> str:
    """格式化美元金额。"""
    if value is None:
        return "N/A"
    return f"${value:,.0f}"


def _fmt_pct(value: float | None) -> str:
    """格式化百分比。"""
    if value is None:
        return "N/A"
    return f"{value:.2f}%"
