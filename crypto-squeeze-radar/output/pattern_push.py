"""Push concise pattern signals to WeChat-friendly webhook services."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from config import (
    HTTP_TIMEOUT_SECONDS,
    PATTERN_PUSH_CHANNEL,
    PATTERN_PUSH_ENABLED,
    PUSHPLUS_TOKEN,
    SERVERCHAN_SENDKEY,
)


PATTERN_SECTIONS = [
    ("oi_4h_short_reversal", "高位OI异常4H空"),
    ("high_neg_funding_12h_short", "高位负Funding 12H空"),
    ("short_crowd_high_volume_12h_short", "空头拥挤高位放量12H空"),
    ("strict_momentum_4h_long", "强势延续4H多"),
]


def push_pattern_signals(payload: dict[str, Any]) -> None:
    if not PATTERN_PUSH_ENABLED:
        print("微信推送未开启：PATTERN_PUSH_ENABLED=false")
        return

    title = "Crypto Radar 信号"
    content = format_pattern_message(payload)

    try:
        if PATTERN_PUSH_CHANNEL == "serverchan":
            sent = send_serverchan(title, content)
        else:
            sent = send_pushplus(title, content)
        if sent:
            print(f"微信推送完成：{PATTERN_PUSH_CHANNEL}")
    except Exception as error:
        print(f"微信推送失败，不影响本轮监控：{error}")


def format_pattern_message(payload: dict[str, Any]) -> str:
    signals = payload.get("signals") or {}
    lines = [
        "Crypto Radar 信号",
        f"生成：{format_time(payload.get('generated_at_utc'))}",
        f"样本：{format_time(payload.get('history_latest_utc'))}",
        format_market_regime(payload.get("market_regime") or {}),
        "",
    ]

    total_starred = 0
    for key, label in PATTERN_SECTIONS:
        rows = sorted(
            signals.get(key) or [],
            key=lambda row: (is_star_signal(row), number(row.get("short_setup_score"))),
            reverse=True,
        )
        starred_rows = [row for row in rows if is_star_signal(row)]
        total_starred += len(starred_rows)
        lines.append(f"{label}：命中 {len(rows)}，星标 {len(starred_rows)}")
        for row in starred_rows[:5]:
            lines.extend(format_trade_lines(row))
        if not starred_rows:
            lines.append("  无星标")
        lines.append("")

    lines.append(f"星标合计：{total_starred}")
    lines.append("仅用于市场结构观察，不构成投资建议。")
    return "\n".join(lines).strip()


def format_trade_lines(row: dict[str, Any]) -> list[str]:
    side = str(row.get("entry_side") or "--")
    price = number_or_none(row.get("entry_price") or row.get("price"))
    stop = number_or_none(row.get("stop_loss_price"))
    first_tp = number_or_none(row.get("first_take_profit_price"))
    final_tp = number_or_none(row.get("final_take_profit_price"))
    hold_hours = int(number(row.get("max_hold_hours")) or 4)
    score = int(number(row.get("short_setup_score")))
    direction_probability = (
        number_or_none(row.get("down_probability_pct"))
        if side == "SHORT"
        else number_or_none(row.get("up_probability_pct"))
    )
    return [
        f"* {row.get('coin') or coin_from_symbol(row.get('symbol', ''))} ({row.get('symbol', '--')}) {side}",
        (
            f"  入场 {format_price(price)}｜止损 {format_price(stop)}｜"
            f"止盈 {format_price(first_tp)} / {format_price(final_tp)}｜{hold_hours}H"
        ),
        (
            f"  分数 {score}｜概率 {format_percent(direction_probability)}｜"
            f"样本 {int(number(row.get('evidence_sample_count')))}"
        ),
        f"  原因：{row.get('short_setup_reasons') or '--'}",
    ]


def is_star_signal(row: dict[str, Any]) -> bool:
    side = str(row.get("entry_side") or "")
    score = number(row.get("short_setup_score"))
    sample_count = number(row.get("evidence_sample_count"))
    if side == "LONG":
        probability = number(row.get("up_probability_pct"))
        return (
            score >= 70
            and sample_count >= 15
            and probability >= 52
            and str(row.get("market_regime") or "") != "weak"
        )

    probability = number(row.get("down_probability_pct"))
    return (
        side == "SHORT"
        and score >= 65
        and sample_count >= 15
        and probability >= 55
        and number(row.get("price_change_1h")) > -3
        and str(row.get("market_regime") or "") != "strong"
    )


def format_market_regime(regime: dict[str, Any]) -> str:
    label = regime.get("label") or "市场环境未知"
    median_24h = number_or_none(regime.get("median_24h_change_pct"))
    breadth = number_or_none(regime.get("up_breadth_pct"))
    sample_count = int(number(regime.get("sample_count")))
    return (
        f"市场：{label}｜24h中位 {format_signed_percent(median_24h)}｜"
        f"上涨占比 {format_percent(breadth)}｜样本 {sample_count}"
    )


def send_pushplus(title: str, content: str) -> bool:
    if not PUSHPLUS_TOKEN:
        print("未配置微信推送：缺少 PUSHPLUS_TOKEN")
        return False
    post_json(
        "https://www.pushplus.plus/send",
        {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "txt",
        },
    )
    return True


def send_serverchan(title: str, content: str) -> bool:
    if not SERVERCHAN_SENDKEY:
        print("未配置微信推送：缺少 SERVERCHAN_SENDKEY")
        return False
    post_form(
        f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
        {
            "title": title,
            "desp": content,
        },
    )
    return True


def post_json(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.read()


def post_form(url: str, payload: dict[str, Any]) -> None:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.read()


def number(value: Any) -> float:
    parsed = number_or_none(value)
    return 0.0 if parsed is None else parsed


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 100:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def format_signed_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def format_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}%"


def format_time(value: Any) -> str:
    if not value:
        return "--"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def coin_from_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol
