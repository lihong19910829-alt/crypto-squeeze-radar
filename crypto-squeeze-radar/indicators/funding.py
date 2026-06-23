"""Funding Rate 指标判断。"""

from config import SCORING_THRESHOLDS


def funding_bias(funding_rate: float | None) -> str:
    """根据 Funding 判断多空拥挤方向。"""
    if funding_rate is None:
        return "未知"
    if funding_rate >= SCORING_THRESHOLDS["funding_hot_positive"]:
        return "多头拥挤"
    if funding_rate <= SCORING_THRESHOLDS["funding_hot_negative"]:
        return "空头拥挤"
    return "中性"

