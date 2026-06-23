"""风险评分：把 Funding、OI、清算等因素合成为 0-100 分。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from config import SCORING_THRESHOLDS
from data_sources.exchange import MarketSnapshot
from indicators.funding import funding_bias
from indicators.liquidation import liquidation_tag
from indicators.oi import oi_tags


def evaluate_snapshot(snapshot: MarketSnapshot) -> dict[str, Any]:
    """生成标签、风险分和可输出的数据结构。"""
    score = 0
    tags: list[str] = []

    bias = funding_bias(snapshot.funding_rate)
    if bias in ("多头拥挤", "空头拥挤"):
        tags.append(bias)
        score += 20

    if snapshot.funding_rate is not None:
        score += min(int(abs(snapshot.funding_rate) / 0.0001) * 4, 20)

    for tag in oi_tags(snapshot.oi_change_1h_pct, snapshot.oi_change_24h_pct):
        if tag not in tags:
            tags.append(tag)

    if snapshot.oi_change_1h_pct is not None:
        if snapshot.oi_change_1h_pct >= SCORING_THRESHOLDS["oi_1h_extreme"]:
            score += 30
        elif snapshot.oi_change_1h_pct >= SCORING_THRESHOLDS["oi_1h_attention"]:
            score += 18
        elif snapshot.oi_change_1h_pct > 0:
            score += 6

    if snapshot.oi_change_24h_pct is not None:
        if snapshot.oi_change_24h_pct >= SCORING_THRESHOLDS["oi_24h_extreme"]:
            score += 24
        elif snapshot.oi_change_24h_pct >= SCORING_THRESHOLDS["oi_24h_attention"]:
            score += 14
        elif snapshot.oi_change_24h_pct > 0:
            score += 5

    liq_tag = liquidation_tag(snapshot.long_liquidation_usd, snapshot.short_liquidation_usd)
    if liq_tag:
        tags.append(liq_tag)
        total_liq = (snapshot.long_liquidation_usd or 0.0) + (snapshot.short_liquidation_usd or 0.0)
        if total_liq >= SCORING_THRESHOLDS["liquidation_extreme_usd"]:
            score += 25
        else:
            score += 12

    score = max(0, min(score, 100))
    if not tags:
        tags.append("正常")

    data = asdict(snapshot)
    data["tags"] = tags
    data["risk_score"] = score
    data["risk_level"] = risk_level(score)
    return data


def risk_level(score: int) -> str:
    """把分数映射为中文风险等级。"""
    if score <= 30:
        return "正常"
    if score <= 60:
        return "关注"
    if score <= 80:
        return "危险"
    return "极端"

