"""清算指标判断。"""

from config import SCORING_THRESHOLDS


def liquidation_tag(long_liq_usd: float | None, short_liq_usd: float | None) -> str | None:
    """判断最近 1 小时是否出现清算异常。"""
    total = (long_liq_usd or 0.0) + (short_liq_usd or 0.0)
    if total >= SCORING_THRESHOLDS["liquidation_attention_usd"]:
        return "清算异常"
    return None

