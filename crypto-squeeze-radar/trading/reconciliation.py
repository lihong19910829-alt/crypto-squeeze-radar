"""Backfill Binance fills/income and reconcile them to local trading decisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from config import TRADING_DB_FILE
from trading.binance_futures import BinanceFuturesTradingClient
from trading.store import (
    load_exchange_fills,
    load_funding_income,
    reconcilable_decisions,
    reconciliation_counts,
    update_decision_reconciliation,
    upsert_exchange_fills,
    upsert_exchange_income,
)


API_WINDOW_MS = int(timedelta(days=6, hours=23).total_seconds() * 1000)
ENTRY_LOOKBACK_MS = int(timedelta(minutes=2).total_seconds() * 1000)
ENTRY_MATCH_MS = int(timedelta(minutes=5).total_seconds() * 1000)
EXIT_PADDING_MS = int(timedelta(minutes=2).total_seconds() * 1000)
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
OPEN_STATUSES = {"OPEN_SUBMITTED", "OPEN_UNPROTECTED"}


def backfill_and_reconcile(
    client: BinanceFuturesTradingClient,
    only_unreconciled: bool = True,
    include_open: bool = False,
) -> dict[str, Any]:
    """Download read-only exchange history, persist it, then compute actual net PnL."""
    decisions = reconcilable_decisions(
        only_unreconciled=only_unreconciled,
        include_open=include_open,
        db_file=TRADING_DB_FILE,
    )
    summary: dict[str, Any] = {
        "decisions_selected": len(decisions),
        "symbols_selected": 0,
        "fills_fetched": 0,
        "income_rows_fetched": 0,
        "remote_errors": 0,
        "reconciled": 0,
        "status_counts": {},
    }
    if not decisions:
        summary["status_counts"] = reconciliation_counts(TRADING_DB_FILE)
        return summary

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        by_symbol[str(decision["symbol"]).upper()].append(decision)
    summary["symbols_selected"] = len(by_symbol)

    for symbol, symbol_decisions in by_symbol.items():
        start_ms = min(_to_milliseconds(row["opened_at_utc"]) for row in symbol_decisions)
        try:
            fills = fetch_user_trades_range(
                client,
                symbol,
                max(0, start_ms - ENTRY_LOOKBACK_MS),
                now_ms,
            )
            upsert_exchange_fills(fills, TRADING_DB_FILE)
            summary["fills_fetched"] += len(fills)
        except Exception as error:
            summary["remote_errors"] += 1
            print(f"成交回填跳过 {symbol}：{error}")

    earliest_ms = min(_to_milliseconds(row["opened_at_utc"]) for row in decisions)
    latest_ms = now_ms
    for income_type in INCOME_TYPES:
        try:
            income_rows = fetch_income_range(client, income_type, earliest_ms, latest_ms)
            upsert_exchange_income(income_rows, TRADING_DB_FILE)
            summary["income_rows_fetched"] += len(income_rows)
        except Exception as error:
            summary["remote_errors"] += 1
            print(f"{income_type} 收益流水回填跳过：{error}")

    summary["reconciled"] = reconcile_local_decisions(decisions, now_ms)
    summary["status_counts"] = reconciliation_counts(TRADING_DB_FILE)
    return summary


def fetch_user_trades_range(
    client: BinanceFuturesTradingClient,
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[dict[str, Any]]:
    """Fetch every fill in API-safe sub-seven-day windows."""
    fetched: dict[int, dict[str, Any]] = {}
    window_start = start_time_ms
    while window_start <= end_time_ms:
        window_end = min(window_start + API_WINDOW_MS, end_time_ms)
        cursor = window_start
        while cursor <= window_end:
            rows = client.user_trades(
                symbol=symbol,
                start_time_ms=cursor,
                end_time_ms=window_end,
                limit=1000,
            )
            for row in rows:
                fetched[int(row["id"])] = row
            if len(rows) < 1000:
                break
            last_time = max(int(row.get("time") or cursor) for row in rows)
            if last_time < cursor:
                break
            cursor = last_time + 1
        window_start = window_end + 1
    return sorted(fetched.values(), key=lambda row: (int(row.get("time") or 0), int(row["id"])))


def fetch_income_range(
    client: BinanceFuturesTradingClient,
    income_type: str,
    start_time_ms: int,
    end_time_ms: int,
) -> list[dict[str, Any]]:
    """Fetch all relevant income rows with explicit page and time-window pagination."""
    fetched: dict[tuple[int, str, int], dict[str, Any]] = {}
    window_start = start_time_ms
    while window_start <= end_time_ms:
        window_end = min(window_start + API_WINDOW_MS, end_time_ms)
        page = 1
        while True:
            rows = client.income_history(
                income_type=income_type,
                start_time_ms=window_start,
                end_time_ms=window_end,
                page=page,
                limit=1000,
            )
            for row in rows:
                key = (
                    int(row.get("tranId") or 0),
                    str(row.get("symbol") or ""),
                    int(row.get("time") or 0),
                )
                fetched[key] = row
            if len(rows) < 1000:
                break
            page += 1
        window_start = window_end + 1
    return sorted(fetched.values(), key=lambda row: int(row.get("time") or 0))


def reconcile_local_decisions(decisions: list[dict[str, Any]], now_ms: int | None = None) -> int:
    """Map exchange fills to each short lifecycle and write weighted prices and net PnL."""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        by_symbol[str(decision["symbol"]).upper()].append(decision)

    reconciled = 0
    for symbol, symbol_decisions in by_symbol.items():
        symbol_decisions.sort(key=lambda row: (_to_milliseconds(row["opened_at_utc"]), row["id"]))
        used_trade_ids: set[int] = set()
        for index, decision in enumerate(symbol_decisions):
            opened_ms = _to_milliseconds(decision["opened_at_utc"])
            next_opened_ms = (
                _to_milliseconds(symbol_decisions[index + 1]["opened_at_utc"])
                if index + 1 < len(symbol_decisions)
                else None
            )
            # Older local rows were sometimes marked CLOSED_EXCHANGE before the
            # final exchange fill appeared. The next opening is a safer hard
            # boundary; without one, inspect through the latest downloaded fill.
            lifecycle_end_ms = next_opened_ms - 1 if next_opened_ms is not None else now_ms
            fills = load_exchange_fills(
                symbol,
                max(0, opened_ms - ENTRY_LOOKBACK_MS),
                lifecycle_end_ms,
                TRADING_DB_FILE,
            )
            available = [
                row
                for row in fills
                if int(row["trade_id"]) not in used_trade_ids
                and (not row.get("decision_signal_id") or row["decision_signal_id"] == decision["signal_id"])
            ]
            entry_fills = _match_entry_fills(available, opened_ms, decision.get("quantity"))
            entry_ids = {int(row["trade_id"]) for row in entry_fills}
            exit_fills = _match_exit_fills(
                [row for row in available if int(row["trade_id"]) not in entry_ids],
                entry_fills,
            )
            matched = entry_fills + exit_fills
            matched_ids = [int(row["trade_id"]) for row in matched]
            used_trade_ids.update(matched_ids)
            values = _reconciliation_values(decision, entry_fills, exit_fills, symbol, now_ms)
            update_decision_reconciliation(
                decision["signal_id"],
                symbol,
                values,
                matched_ids,
                TRADING_DB_FILE,
            )
            reconciled += 1
    return reconciled


def _match_entry_fills(
    fills: list[dict[str, Any]],
    opened_ms: int,
    planned_quantity: float | None,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in fills
        if str(row.get("side") or "").upper() == "SELL"
        and opened_ms - ENTRY_LOOKBACK_MS <= int(row["trade_time_ms"]) <= opened_ms + ENTRY_MATCH_MS
    ]
    if not candidates:
        return []
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        groups[int(row.get("order_id") or -int(row["trade_id"]))].append(row)
    target = float(planned_quantity or 0)

    def group_score(rows: list[dict[str, Any]]) -> tuple[float, float]:
        nearest_time = min(abs(int(row["trade_time_ms"]) - opened_ms) for row in rows)
        quantity = sum(float(row.get("quantity") or 0) for row in rows)
        quantity_gap = abs(quantity - target) / target if target > 0 else 0.0
        return nearest_time, quantity_gap

    return min(groups.values(), key=group_score)


def _match_exit_fills(
    fills: list[dict[str, Any]],
    entry_fills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not entry_fills:
        return []
    entry_qty = sum(float(row.get("quantity") or 0) for row in entry_fills)
    entry_end_ms = max(int(row["trade_time_ms"]) for row in entry_fills)
    exits: list[dict[str, Any]] = []
    exit_qty = 0.0
    for row in sorted(fills, key=lambda item: (int(item["trade_time_ms"]), int(item["trade_id"]))):
        if str(row.get("side") or "").upper() != "BUY" or int(row["trade_time_ms"]) < entry_end_ms:
            continue
        exits.append(row)
        exit_qty += float(row.get("quantity") or 0)
        if entry_qty > 0 and exit_qty >= entry_qty * 0.999:
            break
    return exits


def _reconciliation_values(
    decision: dict[str, Any],
    entry_fills: list[dict[str, Any]],
    exit_fills: list[dict[str, Any]],
    symbol: str,
    now_ms: int,
) -> dict[str, Any]:
    entry_qty = sum(float(row.get("quantity") or 0) for row in entry_fills)
    exit_qty = sum(float(row.get("quantity") or 0) for row in exit_fills)
    entry_price = _weighted_price(entry_fills)
    exit_price = _weighted_price(exit_fills)
    gross_pnl = sum(float(row.get("realized_pnl") or 0) for row in exit_fills)
    commissions = sum(
        abs(float(row.get("commission") or 0))
        for row in entry_fills + exit_fills
        if str(row.get("commission_asset") or "USDT").upper() == "USDT"
    )
    first_fill_ms = min((int(row["trade_time_ms"]) for row in entry_fills), default=None)
    last_fill_ms = max((int(row["trade_time_ms"]) for row in entry_fills + exit_fills), default=None)
    funding_end_ms = last_fill_ms or _decision_remote_end_ms(decision, now_ms)
    funding_rows = (
        load_funding_income(symbol, first_fill_ms, funding_end_ms, TRADING_DB_FILE)
        if first_fill_ms is not None
        else []
    )
    funding_fee = sum(float(row.get("income") or 0) for row in funding_rows)
    is_open = str(decision.get("status") or "") in OPEN_STATUSES
    fully_closed = entry_qty > 0 and exit_qty >= entry_qty * 0.995
    if not entry_fills:
        status = "NO_FILLS"
        note = "未在本地开仓时间附近匹配到交易所 SELL 成交"
    elif is_open and not fully_closed:
        status = "OPEN_RECONCILED"
        note = "已匹配开仓成交，持仓尚未完全退出"
    elif fully_closed:
        status = "COMPLETE"
        note = "开仓与退出成交均已匹配，净盈亏含手续费和持仓期间资金费"
    else:
        status = "PARTIAL"
        note = "已匹配开仓成交，但退出成交数量不完整"
    reconciled_at = datetime.now(timezone.utc).isoformat()
    return {
        "entry_fill_price": entry_price,
        "exit_fill_price": exit_price,
        "entry_filled_qty": entry_qty or None,
        "exit_filled_qty": exit_qty or None,
        "gross_realized_pnl_usdt": gross_pnl if entry_fills else None,
        "commission_usdt": commissions if entry_fills else None,
        "funding_fee_usdt": funding_fee if entry_fills else None,
        "net_realized_pnl_usdt": gross_pnl - commissions + funding_fee if entry_fills else None,
        "first_fill_at_utc": _milliseconds_to_utc(first_fill_ms) if first_fill_ms is not None else None,
        "last_fill_at_utc": _milliseconds_to_utc(last_fill_ms) if last_fill_ms is not None else None,
        "exchange_trade_count": len(entry_fills) + len(exit_fills),
        "reconciliation_status": status,
        "reconciled_at_utc": reconciled_at,
        "reconciliation_note": note,
    }


def _weighted_price(rows: list[dict[str, Any]]) -> float | None:
    quantity = sum(float(row.get("quantity") or 0) for row in rows)
    if quantity <= 0:
        return None
    return sum(float(row.get("price") or 0) * float(row.get("quantity") or 0) for row in rows) / quantity


def _decision_remote_end_ms(decision: dict[str, Any], now_ms: int) -> int:
    closed_at = decision.get("closed_at_utc")
    return min(_to_milliseconds(closed_at), now_ms) if closed_at else now_ms


def _to_milliseconds(value: str) -> int:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _milliseconds_to_utc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
