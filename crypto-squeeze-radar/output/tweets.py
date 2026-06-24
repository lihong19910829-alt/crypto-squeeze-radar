"""生成中文推文文案，并保存为 JSON/Markdown 供人工审核。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import TWEETS_JSON_FILE, TWEETS_MD_FILE


def build_tweet(item: dict[str, Any]) -> str:
    """根据单个异常币种生成中文短文案。"""
    coin = item["coin"]
    score = item["risk_score"]
    level = item["risk_level"]
    tags = "、".join(item["tags"])
    oi_1h = _fmt_pct(item.get("oi_change_1h_pct"))
    oi_24h = _fmt_pct(item.get("oi_change_24h_pct"))
    funding = _fmt_pct((item.get("funding_rate") or 0) * 100 if item.get("funding_rate") is not None else None)

    direction = _direction_text(item)
    return (
        f"🚨 {coin} 杠杆风险{level}\n"
        f"风险评分：{score}/100，标签：{tags}\n"
        f"过去1小时OI变化：{oi_1h}；24小时OI变化：{oi_24h}；Funding：{funding}。\n"
        f"{direction}\n"
        f"仅用于市场结构观察，不构成投资建议。\n"
        f"#{coin} #Crypto"
    )


def save_tweets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存 tweets.json 和 tweets.md。"""
    TWEETS_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    tweets = [
        {
            "coin": item["coin"],
            "risk_score": item["risk_score"],
            "risk_level": item["risk_level"],
            "tags": item["tags"],
            "created_at_utc": now,
            "tweet": build_tweet(item),
        }
        for item in items
    ]

    TWEETS_JSON_FILE.write_text(
        json.dumps(tweets, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    md = ["# Crypto Squeeze Radar Tweets", ""]
    for index, tweet in enumerate(tweets, start=1):
        md.append(f"## {index}. {tweet['coin']} - {tweet['risk_level']}")
        md.append("")
        md.append(tweet["tweet"])
        md.append("")
    TWEETS_MD_FILE.write_text("\n".join(md), encoding="utf-8")
    return tweets


def _direction_text(item: dict[str, Any]) -> str:
    """根据标签组合生成风险解释。"""
    tags = item.get("tags", [])
    if "多头拥挤" in tags and "OI异常增加" in tags:
        return "Funding 偏正且 OI 上升，说明多头仓位正在堆积，需警惕多头踩踏风险。"
    if "空头拥挤" in tags and "OI异常增加" in tags:
        return "Funding 偏负且 OI 上升，说明空头仓位正在堆积，需警惕空头回补风险。"
    if "清算异常" in tags:
        return "近期清算放大，说明杠杆仓位正在被动出清，波动风险升高。"
    if "杠杆过热" in tags or "OI异常增加" in tags:
        return "OI 明显抬升，说明市场杠杆参与度升高，后续波动可能扩大。"
    return "当前未发现明显仓位异常，继续观察 Funding、OI 和清算变化。"


def _fmt_pct(value: float | None) -> str:
    """格式化百分比数字。"""
    if value is None:
        return "暂无数据"
    return f"{value:.2f}%"

