"""Open Interest 指标判断。"""

from config import SCORING_THRESHOLDS


def oi_tags(oi_change_1h_pct: float | None, oi_change_24h_pct: float | None) -> list[str]:
    """根据 1 小时和 24 小时 OI 变化生成标签。"""
    tags: list[str] = []
    if oi_change_1h_pct is not None and oi_change_1h_pct >= SCORING_THRESHOLDS["oi_1h_attention"]:
        tags.append("OI异常增加")
    if oi_change_24h_pct is not None and oi_change_24h_pct >= SCORING_THRESHOLDS["oi_24h_attention"]:
        tags.append("杠杆过热")
    return tags

