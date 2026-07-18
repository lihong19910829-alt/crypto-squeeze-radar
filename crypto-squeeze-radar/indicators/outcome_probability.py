"""Post-signal outcome labels derived from accumulated local backtests."""

from __future__ import annotations

from typing import Any


TRADE_PROBABILITY_THRESHOLD = 83


def estimate_outcome_probability(item: dict[str, Any]) -> dict[str, Any]:
    """Estimate directional odds for the signal using simple backtest buckets.

    The numbers are not live predictions. They summarize the current local
    history's conditional behavior after similar signals.
    """
    score = int(item.get("risk_score") or 0)
    funding = item.get("funding_rate")
    oi_1h = _first_number(item, "oi_change_1h_pct", "oi_change_1h")
    oi_24h = _first_number(item, "oi_change_24h_pct", "oi_change_24h")

    if score >= 70 and _is_negative(funding) and _gte(oi_1h, 5):
        return _result(
            direction="偏下跌/去杠杆",
            horizon="3-6h",
            up_probability=13,
            down_probability=87,
            basis="score>=70 + 负 Funding + 1h OI>=5%，本地样本 6h 上涨约13%。",
        )

    if score >= 80:
        return _result(
            direction="偏下跌/去杠杆",
            horizon="3-6h",
            up_probability=23,
            down_probability=77,
            basis="score>=80，本地样本 3-6h 后多数回落。",
        )

    if score >= 70 and _is_positive(funding) and _gte(oi_1h, 5):
        return _result(
            direction="偏下跌/多头踩踏",
            horizon="3-6h",
            up_probability=25,
            down_probability=75,
            basis="高分且多头继续加杠杆，历史更接近踩踏风险。",
        )

    if score >= 70 and _is_negative(funding) and _lt(oi_1h, 5) and _gte(oi_24h, 100):
        return _result(
            direction="短线偏弱，6h 反抽五五开",
            horizon="1-6h",
            up_probability=50,
            down_probability=50,
            basis="负 Funding 高分但 1h OI 放缓、24h OI 极高，1-3h 偏弱，6h 反抽概率回升。",
        )

    if score >= 70:
        return _result(
            direction="偏下跌/波动扩大",
            horizon="3-6h",
            up_probability=27,
            down_probability=73,
            basis="score>=70，本地样本 3-6h 上涨占比约27%。",
        )

    if 40 <= score < 70 and _gte(oi_24h, 100):
        return _result(
            direction="偏上涨/补涨",
            horizon="6-12h",
            up_probability=75,
            down_probability=25,
            basis="score<70 且 24h OI>=100%，本地小样本 12h 上涨占比较高。",
        )

    if 40 <= score < 70 and _gte(oi_24h, 25) and not _is_extreme_funding(funding):
        return _result(
            direction="中性偏震荡",
            horizon="3-6h",
            up_probability=48,
            down_probability=52,
            basis="中分位杠杆升温但 Funding 不极端，历史接近五五开。",
        )

    if _gte_abs_funding(funding, 0.001):
        return _result(
            direction="偏下跌/拥挤修正",
            horizon="3-12h",
            up_probability=25,
            down_probability=75,
            basis="Funding 绝对值偏高，历史更容易出现拥挤修正。",
        )

    return _result(
        direction="中性观察",
        horizon="1-6h",
        up_probability=50,
        down_probability=50,
        basis="当前信号不落入高胜率历史分组。",
    )


def format_outcome_probability(item: dict[str, Any]) -> str:
    """Return a short Chinese label for reports and alert text."""
    outcome = item.get("outcome_probability") or estimate_outcome_probability(item)
    text = (
        f"后验概率（{outcome['horizon']}）：上涨约{outcome['up_probability']}%，"
        f"下跌约{outcome['down_probability']}%；{outcome['direction']}。"
    )
    if outcome.get("trade_action") in {"做多", "做空"}:
        text += f" {outcome['trade_label']}。"
    return text


def _result(
    *,
    direction: str,
    horizon: str,
    up_probability: int,
    down_probability: int,
    basis: str,
) -> dict[str, Any]:
    trade_hint = _trade_hint(up_probability, down_probability)
    return {
        "direction": direction,
        "horizon": horizon,
        "up_probability": up_probability,
        "down_probability": down_probability,
        "basis": basis,
        **trade_hint,
    }


def _trade_hint(up_probability: int, down_probability: int) -> dict[str, Any]:
    if up_probability > TRADE_PROBABILITY_THRESHOLD and up_probability > down_probability:
        return {
            "trade_action": "做多",
            "trade_confidence": up_probability,
            "trade_label": f"后验概率>{TRADE_PROBABILITY_THRESHOLD}%，可关注做多",
        }
    if down_probability > TRADE_PROBABILITY_THRESHOLD and down_probability > up_probability:
        return {
            "trade_action": "做空",
            "trade_confidence": down_probability,
            "trade_label": f"后验概率>{TRADE_PROBABILITY_THRESHOLD}%，可关注做空",
        }
    return {
        "trade_action": "观望",
        "trade_confidence": max(up_probability, down_probability),
        "trade_label": f"后验概率未超过{TRADE_PROBABILITY_THRESHOLD}%，观望",
    }


def _first_number(item: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return float(value)
    return None


def _is_negative(value: float | None) -> bool:
    return value is not None and value < 0


def _is_positive(value: float | None) -> bool:
    return value is not None and value > 0


def _gte(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _lt(value: float | None, threshold: float) -> bool:
    return value is not None and value < threshold


def _is_extreme_funding(value: float | None) -> bool:
    return _gte_abs_funding(value, 0.001)


def _gte_abs_funding(value: float | None, threshold: float) -> bool:
    return value is not None and abs(value) >= threshold
