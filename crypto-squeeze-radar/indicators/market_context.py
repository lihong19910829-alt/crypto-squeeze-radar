"""Local-history market context enrichment."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from config import SQLITE_DB_FILE


def enrich_market_context(items: list[dict[str, Any]]) -> None:
    """Add price momentum, volume comparison, and funding continuity in-place."""
    if not SQLITE_DB_FILE.exists():
        for item in items:
            _apply_empty_context(item)
        return

    available_columns = _table_columns()
    include_volume = "quote_volume_24h" in available_columns
    fields = "timestamp_utc, symbol, price, funding_rate"
    if include_volume:
        fields += ", quote_volume_24h"

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            f"""
            SELECT {fields}
            FROM market_snapshots
            WHERE symbol IS NOT NULL
              AND price IS NOT NULL
            ORDER BY symbol, timestamp_utc
            """
        ):
            item = dict(row)
            item["dt"] = parse_time(item["timestamp_utc"])
            by_symbol.setdefault(item["symbol"], []).append(item)

    now = datetime.now().astimezone()
    for item in items:
        history = by_symbol.get(item.get("symbol") or "", [])
        price = number_or_none(item.get("price"))
        item["price_change_1h_pct"] = pct_from_history(price, history, now - timedelta(hours=1))
        item["price_change_4h_pct"] = pct_from_history(price, history, now - timedelta(hours=4))
        item["quote_volume_change_24h_pct"] = volume_change_from_history(
            item.get("quote_volume_24h"),
            history,
            now - timedelta(hours=24),
            include_volume,
        )
        funding_context = funding_continuity(item.get("funding_rate"), history)
        item.update(funding_context)


def pct_from_history(price: float | None, history: list[dict[str, Any]], target: datetime) -> float | None:
    if price is None:
        return None
    previous = last_at_or_before(history, target)
    previous_price = number_or_none(previous.get("price") if previous else None)
    if previous_price in (None, 0):
        return None
    return (price - previous_price) / previous_price * 100


def volume_change_from_history(
    current_volume: Any,
    history: list[dict[str, Any]],
    target: datetime,
    include_volume: bool,
) -> float | None:
    if not include_volume:
        return None
    current = number_or_none(current_volume)
    previous = last_at_or_before(history, target)
    previous_volume = number_or_none(previous.get("quote_volume_24h") if previous else None)
    if current is None or previous_volume in (None, 0):
        return None
    return (current - previous_volume) / previous_volume * 100


def funding_continuity(current_funding: Any, history: list[dict[str, Any]]) -> dict[str, Any]:
    current = number_or_none(current_funding)
    values = [number_or_none(row.get("funding_rate")) for row in history[-5:]]
    clean = [value for value in values if value is not None]
    if current is not None:
        clean.append(current)
    if not clean:
        return {"funding_same_sign_count": None, "funding_avg_abs_6": None}

    sign = 1 if clean[-1] > 0 else -1 if clean[-1] < 0 else 0
    same_sign_count = 0
    if sign:
        for value in reversed(clean):
            value_sign = 1 if value > 0 else -1 if value < 0 else 0
            if value_sign != sign:
                break
            same_sign_count += 1
    return {
        "funding_same_sign_count": same_sign_count,
        "funding_avg_abs_6": sum(abs(value) for value in clean[-6:]) / min(len(clean), 6),
    }


def last_at_or_before(history: list[dict[str, Any]], target: datetime) -> dict[str, Any] | None:
    candidate = None
    for row in history:
        if row["dt"] <= target:
            candidate = row
        else:
            break
    return candidate


def _table_columns() -> set[str]:
    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)").fetchall()}


def _apply_empty_context(item: dict[str, Any]) -> None:
    item["price_change_1h_pct"] = None
    item["price_change_4h_pct"] = None
    item["quote_volume_change_24h_pct"] = None
    item["funding_same_sign_count"] = None
    item["funding_avg_abs_6"] = None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
