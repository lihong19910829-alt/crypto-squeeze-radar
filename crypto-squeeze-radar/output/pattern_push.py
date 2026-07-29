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
    ("oi_4h_short_reversal", "A加强版：高位OI异常4H空（4%止损，3%/7%止盈）"),
    ("high_neg_funding_12h_short", "B：高位负Funding12H空（确认层分档止损，8%/13%止盈）"),
    ("short_crowd_high_volume_12h_short", "C：空头拥挤高位放量12H空（确认层分档止损，8%/13%止盈）"),
]


def push_pattern_signals(payload: dict[str, Any]) -> None:
    if not PATTERN_PUSH_ENABLED:
        print("微信推送未开启：PATTERN_PUSH_ENABLED=false")
        return

    strong_rows = collect_strong_reaction_signals(payload)
    title = (
        f"Crypto Radar 强信号 {len(strong_rows)}"
        if strong_rows
        else "Crypto Radar 信号"
    )
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
    trade_rows = collect_trade_rows(payload)
    star_count = sum(1 for row in trade_rows if is_star_signal(row))
    lines = [
        "Crypto Radar 空头交易表",
        f"生成：{format_time(payload.get('generated_at_utc'))}",
        f"样本：{format_time(payload.get('history_latest_utc'))}",
        format_market_regime(payload.get("market_regime") or {}),
        f"候选：{len(trade_rows)}，标星：{star_count}",
        "",
    ]

    for key, label in PATTERN_SECTIONS:
        rows = sorted(
            signals.get(key) or [],
            key=lambda row: (
                is_star_signal(row),
                number(row.get("position_multiplier")),
                number(row.get("short_setup_score")),
            ),
            reverse=True,
        )
        starred_rows = [row for row in rows if is_star_signal(row)]
        lines.append(f"{label}：命中 {len(rows)}，标星 {len(starred_rows)}")
        if rows:
            lines.extend(format_trade_table(rows[:8]))
        else:
            lines.append("  无命中")
        lines.append("")

    lines.append("交易分级：主交易1.0x；可交易0.8x；小仓确认0.25x；观察0x。")
    lines.append("确认层：高位≥90%且24h涨幅≥20%且成交额放大≥50%，或高位≥70%且涨幅≥20%并满足空头拥挤/负Funding。")
    lines.append("仅用于市场结构观察，不构成投资建议。")
    return "\n".join(lines).strip()


def collect_trade_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    signals = payload.get("signals") or {}
    rows = [
        row
        for key, _ in PATTERN_SECTIONS
        for row in (signals.get(key) or [])
    ]
    return sorted(
        rows,
        key=lambda row: (
            is_star_signal(row),
            number(row.get("position_multiplier")),
            number(row.get("short_setup_score")),
        ),
        reverse=True,
    )


def collect_strong_reaction_signals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    signals = payload.get("signals") or {}
    rows = [row for group in signals.values() for row in (group or [])]
    strong_by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not is_strong_reaction_signal(row):
            continue
        symbol = str(row.get("symbol") or "")
        current = strong_by_symbol.get(symbol)
        if current is None or strong_signal_rank(row) > strong_signal_rank(current):
            strong_by_symbol[symbol] = row
    strong_rows = list(strong_by_symbol.values())
    return sorted(
        strong_rows,
        key=strong_signal_rank,
        reverse=True,
    )


def strong_signal_rank(row: dict[str, Any]) -> tuple[float, float, float]:
    return (
        number(row.get("short_setup_score")),
        number(row.get("quote_volume_change_24h")),
        abs(number(row.get("funding_rate"))),
    )


def is_strong_reaction_signal(row: dict[str, Any]) -> bool:
    if str(row.get("entry_side") or "") != "SHORT":
        return False
    position = number(row.get("price_position_24h"))
    funding = number(row.get("funding_rate"))
    price_change_24h = number(row.get("price_change_24h"))
    volume_change_24h = number(row.get("quote_volume_change_24h"))
    return (
        position >= 90
        and price_change_24h >= 20
        and volume_change_24h >= 50
        and number(row.get("price_change_1h")) > -3
        or (
            position >= 70
            and price_change_24h >= 20
            and number(row.get("price_change_1h")) > -3
            and ("空头拥挤" in str(row.get("anomaly_tag") or "") or funding <= -0.0003)
        )
    )


def format_reaction_lines(row: dict[str, Any]) -> list[str]:
    side = str(row.get("entry_side") or "--")
    symbol = row.get("symbol", "--")
    coin = row.get("coin") or coin_from_symbol(symbol)
    price = number_or_none(row.get("entry_price") or row.get("price"))
    position = number_or_none(row.get("price_position_24h"))
    funding_pct = number(row.get("funding_rate")) * 100
    price_change_24h = number_or_none(row.get("price_change_24h"))
    volume_change_24h = number_or_none(row.get("quote_volume_change_24h"))
    score = int(number(row.get("short_setup_score")))
    return [
        f"!!! {coin} ({symbol}) {side} 高位脆弱",
        (
            f"  价格 {format_price(price)}，位置 {format_percent(position)}，"
            f"Funding {format_signed_percent(funding_pct)}"
        ),
        (
            f"  24h涨跌 {format_signed_percent(price_change_24h)}，"
            f"成交额变化 {format_signed_percent(volume_change_24h)}，分数 {score}"
        ),
        "  反应：加入强观察；等5m/15m反抽失败、量能衰减、无法创新高后再处理。",
    ]


def format_trade_lines(row: dict[str, Any]) -> list[str]:
    side = str(row.get("entry_side") or "--")
    price = number_or_none(row.get("entry_price") or row.get("price"))
    stop = number_or_none(row.get("stop_loss_price"))
    stop_pct = number_or_none(row.get("stop_loss_pct"))
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


def format_trade_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "```",
        "星 品种    仓位 止损        TP1         TP2         持仓",
    ]
    for row in rows:
        lines.append(format_trade_table_row(row))
    lines.append("```")
    return lines


def format_trade_table_row(row: dict[str, Any]) -> str:
    symbol = str(row.get("symbol") or "--")
    coin = row.get("coin") or coin_from_symbol(symbol)
    star = "★" if is_star_signal(row) else "·"
    multiplier = number(row.get("position_multiplier"))
    stop = number_or_none(row.get("stop_loss_price"))
    first_tp = number_or_none(row.get("first_take_profit_price"))
    final_tp = number_or_none(row.get("final_take_profit_price"))
    hold_hours = int(number(row.get("max_hold_hours")) or 4)
    grade = str(row.get("trade_grade") or ("可交易" if is_star_signal(row) else "观察"))
    close_rule = format_close_rule(row)
    return (
        f"{star} {coin_from_display(str(coin)):<7} "
        f"{multiplier:.2f}x {format_price(stop):<11} "
        f"{format_price(first_tp):<11} {format_price(final_tp):<11} "
        f"{hold_hours}H {grade} {close_rule}"
    )


def coin_from_display(value: str) -> str:
    return value[:7]


def format_close_rule(row: dict[str, Any]) -> str:
    first = int(number(row.get("first_take_profit_close_pct")) or 50)
    final = int(number(row.get("final_take_profit_close_pct")) or 30)
    time_exit = int(number(row.get("time_exit_close_pct")) or 20)
    return f"{first}/{final}/{time_exit}"


def is_star_signal(row: dict[str, Any]) -> bool:
    if row.get("is_star") is not None:
        return bool(row.get("is_star"))
    if is_strong_reaction_signal(row):
        return True

    side = str(row.get("entry_side") or "")
    score = number(row.get("short_setup_score"))
    sample_count = number(row.get("evidence_sample_count"))
    if side == "LONG":
        return False

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
