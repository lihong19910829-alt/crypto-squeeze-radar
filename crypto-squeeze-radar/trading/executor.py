"""Consume pattern signals and optionally execute Binance futures orders."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from config import (
    TRADING_ALLOWED_GRADES,
    TRADING_ALLOWED_SYMBOLS,
    TRADING_DB_FILE,
    TRADING_DRY_RUN,
    TRADING_ENABLED,
    TRADING_LEVERAGE,
    TRADING_LEVERAGE_MODE,
    TRADING_MARKET,
    TRADING_MAX_OPEN_POSITIONS,
    TRADING_MODE_A_ENABLED,
    TRADING_MIN_NOTIONAL_USDT,
    TRADING_ORDER_PREFIX,
    TRADING_PLACE_EXITS,
    TRADING_POSITION_MODE,
    TRADING_REQUIRE_STAR,
    TRADING_RECONCILE_ENABLED,
    TRADING_RISK_PCT,
    TRADING_SIDE,
    TRADING_SIGNALS_FILE,
)
from trading.binance_futures import (
    BinanceFuturesTradingClient,
    build_symbol_rules,
    floor_to_step,
)
from trading.store import (
    due_open_decisions,
    existing_signal_ids,
    mark_decision_unprotected,
    open_protection_decisions,
    open_local_decision_count,
    open_local_symbols,
    save_decision,
    update_decision_close,
    update_decision_protection,
)
from patterns.oi_pattern_monitor import (
    STRATEGY_CONFIG_HASH,
    STRATEGY_REVISION,
    annotate_trade_plan_metadata,
)
from trading.reconciliation import backfill_and_reconcile


SHORT_PATTERN_KEYS = {
    "oi_4h_short_reversal",
    "high_neg_funding_12h_short",
    "short_crowd_high_volume_12h_short",
}

# B 是主交易模式；C 只给同一时刻的 B 增加放量确认，不独立开仓。
CANDIDATE_TRADE_PATTERN_KEYS = {
    "high_neg_funding_12h_short",
}


def run_trading_cycle(
    signals_file: Path = TRADING_SIGNALS_FILE,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one trading pass from the latest pattern_signals.json."""
    source = "in-memory payload" if payload is not None else str(signals_file)
    if payload is None:
        payload = load_signal_payload(signals_file)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rows = select_trade_signals(payload)
    diagnostics = diagnose_trade_filter(payload)
    seen = existing_signal_ids(TRADING_DB_FILE)
    client = build_client_if_needed()
    position_mode = client.position_mode() if client and not TRADING_DRY_RUN else TRADING_POSITION_MODE
    if client and not TRADING_PLACE_EXITS:
        raise RuntimeError("真实交易必须开启 TRADING_PLACE_EXITS，禁止无保护开仓")
    exchange_rules = build_symbol_rules(client.exchange_info()) if client and not TRADING_DRY_RUN else {}
    equity = account_equity(client) if client and not TRADING_DRY_RUN else Decimal("1000")
    position_rows = client.position_risk() if client and not TRADING_DRY_RUN else []
    protection = manage_tp1_breakeven_protection(
        run_id=run_id,
        client=client,
        position_mode=position_mode,
        position_rows=position_rows,
        exchange_rules=exchange_rules,
    )
    expiry = expire_due_positions(
        run_id=run_id,
        client=client,
        position_mode=position_mode,
        position_rows=position_rows,
        exchange_rules=exchange_rules,
    )
    if client and not TRADING_DRY_RUN:
        open_count, open_symbols = current_open_position_state_from_rows(position_rows, position_mode)
        for symbol in expiry["closed_symbols"]:
            if symbol in open_symbols:
                open_count = max(0, open_count - 1)
            # Do not immediately reopen the same symbol in this cycle while
            # the exchange position snapshot is still from before the close.
            open_symbols.add(symbol)
        for symbol in protection["closed_symbols"]:
            if symbol in open_symbols:
                open_count = max(0, open_count - 1)
            open_symbols.add(symbol)
    else:
        open_symbols = open_local_symbols()
        open_count = open_local_decision_count()
        for symbol in expiry["closed_symbols"]:
            if symbol in open_symbols:
                open_count = max(0, open_count - 1)
            open_symbols.add(symbol)
    leverage_cache: dict[str, int] = {}

    summary = {
        "run_id": run_id,
        "dry_run": TRADING_DRY_RUN or not TRADING_ENABLED,
        "source": source,
        "candidate_signals": diagnostics["candidate_signals"],
        "filtered_reasons": diagnostics["filtered_reasons"],
        "signals_seen": len(rows),
        "submitted": 0,
        "skipped": 0,
        "errors": expiry["errors"] + protection["errors"],
        "time_exit_checked": expiry["checked"],
        "time_exit_closed": expiry["closed"],
        "time_exit_errors": expiry["errors"],
        "tp1_protection_checked": protection["checked"],
        "tp1_protection_activated": protection["activated"],
        "tp1_protection_closed": protection["closed"],
        "tp1_protection_errors": protection["errors"],
        "tp1_protection_error_details": protection["error_details"],
    }

    for row in rows:
        signal_id = signal_key(row)
        if signal_id in seen:
            save_skip(run_id, row, "已处理过同一信号")
            summary["skipped"] += 1
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol in open_symbols:
            save_skip(run_id, row, f"品种已有持仓，禁止重复开仓：{symbol}")
            summary["skipped"] += 1
            continue
        if open_count >= TRADING_MAX_OPEN_POSITIONS:
            save_skip(run_id, row, f"已达到最大同时持仓数 {TRADING_MAX_OPEN_POSITIONS}")
            summary["skipped"] += 1
            continue

        try:
            leverage = resolve_leverage(client, row["symbol"], leverage_cache)
            plan = build_order_plan(row, equity, exchange_rules.get(row["symbol"], {}), leverage)
            decision = submit_or_record(run_id, row, plan, client, position_mode)
            save_decision(decision)
            if decision["status"] in {"DRY_RUN_PLANNED", "OPEN_SUBMITTED", "OPEN_UNPROTECTED"}:
                open_count += 1
                open_symbols.add(symbol)
                summary["submitted"] += 1
            elif decision["status"] == "ERROR":
                summary["errors"] += 1
            else:
                summary["skipped"] += 1
        except Exception as error:
            decision = base_decision(run_id, row)
            decision.update({"status": "ERROR", "reason": str(error)})
            save_decision(decision)
            summary["errors"] += 1

    summary["reconciliation"] = None
    if client and not TRADING_DRY_RUN and TRADING_RECONCILE_ENABLED:
        try:
            summary["reconciliation"] = backfill_and_reconcile(
                client,
                only_unreconciled=True,
                include_open=False,
            )
        except Exception as error:
            summary["reconciliation"] = {"error": str(error)}
            summary["errors"] += 1

    return summary


def load_signal_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"找不到交易信号文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def select_trade_signals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    signals = payload.get("signals") or {}
    annotate_trade_plan_metadata(signals)
    rows = [
        row
        for key in SHORT_PATTERN_KEYS
        for row in signals.get(key, [])
        if should_trade(row)
    ]
    rows.sort(
        key=lambda row: (
            bool(row.get("is_star")),
            Decimal(str(row.get("position_multiplier") or 0)),
            Decimal(str(row.get("short_setup_score") or 0)),
        ),
        reverse=True,
    )
    return rows


def should_trade(row: dict[str, Any]) -> bool:
    return trade_reject_reason(row) is None


def trade_reject_reason(row: dict[str, Any]) -> str | None:
    if TRADING_SIDE != "SHORT" or row.get("entry_side") != "SHORT":
        return "方向不是 SHORT"
    if (
        row.get("strategy_revision") != STRATEGY_REVISION
        or row.get("strategy_config_hash") != STRATEGY_CONFIG_HASH
    ):
        return "信号规则版本已过期，等待下一轮扫描"
    if TRADING_ALLOWED_SYMBOLS and str(row.get("symbol", "")).upper() not in TRADING_ALLOWED_SYMBOLS:
        return "不在允许交易品种"
    pattern_key = str(row.get("pattern_key") or "")
    if pattern_key == "short_crowd_high_volume_12h_short":
        return "模式C仅作为模式B增强确认"
    if pattern_key == "oi_4h_short_reversal" and not TRADING_MODE_A_ENABLED:
        return "模式A已暂停"
    try:
        if Decimal(str(row.get("position_multiplier") or "0")) <= 0:
            return "仓位系数为0，仅记录"
    except (ArithmeticError, ValueError):
        return "仓位系数无效"
    candidate_trade = pattern_key in CANDIDATE_TRADE_PATTERN_KEYS
    if TRADING_REQUIRE_STAR and not row.get("is_star") and not candidate_trade:
        return "未标星"
    # A star is the final strategy-layer approval. Do not let a stale or
    # differently encoded grade setting silently block a starred signal.
    if (
        TRADING_ALLOWED_GRADES
        and not row.get("is_star")
        and not candidate_trade
        and row.get("trade_grade") not in TRADING_ALLOWED_GRADES
    ):
        return f"等级不允许：{row.get('trade_grade')}"
    return None


def diagnose_trade_filter(payload: dict[str, Any]) -> dict[str, Any]:
    signals = payload.get("signals") or {}
    annotate_trade_plan_metadata(signals)
    rows = [
        row
        for key in SHORT_PATTERN_KEYS
        for row in signals.get(key, [])
    ]
    reasons: dict[str, int] = {}
    for row in rows:
        reason = trade_reject_reason(row)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "candidate_signals": len(rows),
        "filtered_reasons": reasons,
    }


def build_client_if_needed() -> BinanceFuturesTradingClient | None:
    if TRADING_DRY_RUN or not TRADING_ENABLED:
        return None
    if TRADING_MARKET != "usdm_futures":
        raise ValueError(f"暂不支持的交易市场：{TRADING_MARKET}")
    return BinanceFuturesTradingClient(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )


def account_equity(client: BinanceFuturesTradingClient | None) -> Decimal:
    if client is None:
        return Decimal("1000")
    account = client.account()
    for key in ("totalMarginBalance", "totalWalletBalance"):
        if account.get(key) is not None:
            return Decimal(str(account[key]))
    raise RuntimeError("无法读取账户权益")


def current_open_position_count(client: BinanceFuturesTradingClient | None) -> int:
    if client is None:
        return 0
    count = 0
    for row in client.position_risk():
        amount = Decimal(str(row.get("positionAmt", "0")))
        position_side = str(row.get("positionSide") or "BOTH")
        if TRADING_POSITION_MODE == "hedge" and position_side == "SHORT" and amount > 0:
            count += 1
        elif TRADING_POSITION_MODE != "hedge" and amount < 0:
            count += 1
    return count


def current_open_position_state(
    client: BinanceFuturesTradingClient,
    position_mode: str,
) -> tuple[int, set[str]]:
    """Read the exchange once for both the position cap and per-symbol guard."""
    return current_open_position_state_from_rows(client.position_risk(), position_mode)


def current_open_position_state_from_rows(
    position_rows: list[dict[str, Any]],
    position_mode: str,
) -> tuple[int, set[str]]:
    """Build the position cap state from an already fetched position snapshot."""
    count = 0
    symbols: set[str] = set()
    for row in position_rows:
        amount = Decimal(str(row.get("positionAmt", "0")))
        if amount == 0:
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            symbols.add(symbol)
        position_side = str(row.get("positionSide") or "BOTH")
        if position_mode == "hedge":
            if position_side in {"LONG", "SHORT"} and amount != 0:
                count += 1
        elif amount != 0:
            count += 1
    return count, symbols


def manage_tp1_breakeven_protection(
    run_id: str,
    client: BinanceFuturesTradingClient | None,
    position_mode: str,
    position_rows: list[dict[str, Any]],
    exchange_rules: dict[str, dict[str, Decimal]],
) -> dict[str, Any]:
    """Move the remaining B position stop after TP1 has reduced its size.

    The exchange position quantity is used as the source of truth.  Once it is
    at or below the expected post-TP1 remainder, the original stop is replaced
    with a cost-protection stop.  Existing trades created before this rule do
    not opt in and are left untouched.
    """
    result = {
        "checked": 0,
        "activated": 0,
        "closed": 0,
        "errors": 0,
        "error_details": [],
        "closed_symbols": set(),
    }
    if client is None:
        return result

    positions = {
        str(row.get("symbol") or "").upper(): row
        for row in position_rows
        if position_amount(row, position_mode) > 0
    }
    now = datetime.now(timezone.utc)
    handled_symbols: set[str] = set()
    for decision in open_protection_decisions(TRADING_DB_FILE):
        raw_signal = json_object(decision.get("raw_signal"))
        if not raw_signal.get("move_stop_to_breakeven_after_tp1"):
            continue
        due = parse_datetime_or_none(decision.get("time_exit_due_utc"))
        if due is not None and due <= now:
            continue

        symbol = str(decision.get("symbol") or "").upper()
        if not symbol or symbol in handled_symbols:
            continue
        handled_symbols.add(symbol)
        result["checked"] += 1
        position = positions.get(symbol)
        if position is None:
            continue

        rules = exchange_rules.get(symbol, {})
        step_size = rules.get("step_size", Decimal("0.001"))
        current_quantity = floor_to_step(position_amount(position, position_mode), step_size)
        initial_quantity = Decimal(str(decision.get("quantity") or "0"))
        if current_quantity <= 0 or initial_quantity <= 0:
            continue
        first_close_pct = Decimal(
            str(raw_signal.get("first_take_profit_close_pct") or "50")
        ) / Decimal("100")
        first_quantity = floor_to_step(initial_quantity * first_close_pct, step_size)
        expected_remainder = max(Decimal("0"), initial_quantity - first_quantity)
        if current_quantity > expected_remainder + step_size:
            continue

        try:
            entry_price = Decimal(
                str(position.get("entryPrice") or decision.get("entry_price") or "0")
            )
            mark_price = Decimal(str(position.get("markPrice") or "0"))
            original_stop = Decimal(str(decision.get("stop_loss_price") or "0"))
            if entry_price <= 0 or original_stop <= entry_price:
                raise ValueError(f"{symbol} 缺少有效入场价或原止损价")

            buffer_pct = Decimal(str(raw_signal.get("breakeven_buffer_pct") or "0.3"))
            target_stop = entry_price * (Decimal("1") - buffer_pct / Decimal("100"))
            # If the hourly manager sees the position after price has already
            # crossed the ideal protection level, place a near-market guard
            # instead of submitting an invalid trigger below the current mark.
            if mark_price > 0 and mark_price >= target_stop:
                target_stop = mark_price * Decimal("1.003")
            if target_stop >= original_stop:
                # The original stop is already tighter than a late replacement.
                continue

            responses = json_object(decision.get("exchange_response"))
            old_stop = responses.get("stop")
            old_algo_id = old_stop.get("algoId") if isinstance(old_stop, dict) else None
            if old_algo_id:
                try:
                    responses["stop_cancel_before_breakeven"] = client.cancel_algo_order(
                        symbol,
                        int(old_algo_id),
                    )
                except Exception as error:
                    responses["stop_cancel_before_breakeven"] = {"error": str(error)}

            stop_client_id = client_order_id_from_run(run_id, symbol, "be-stop")
            try:
                new_stop = client.algo_order(
                    algo_order_params(
                        algo_type="CONDITIONAL",
                        symbol=symbol,
                        side="BUY",
                        order_type="STOP_MARKET",
                        quantity=current_quantity,
                        client_id=stop_client_id,
                        stop_price=target_stop,
                        reduce_only=True,
                        position_mode=position_mode,
                        tick_size=rules.get("tick_size", Decimal("0")),
                    )
                )
                if not new_stop.get("algoId"):
                    raise RuntimeError("成本保护止损未返回 algoId")
            except Exception as protection_error:
                rollback_client_id = client_order_id_from_run(run_id, symbol, "sl-rearm")
                try:
                    rollback = client.algo_order(
                        algo_order_params(
                            algo_type="CONDITIONAL",
                            symbol=symbol,
                            side="BUY",
                            order_type="STOP_MARKET",
                            quantity=current_quantity,
                            client_id=rollback_client_id,
                            stop_price=original_stop,
                            reduce_only=True,
                            position_mode=position_mode,
                            tick_size=rules.get("tick_size", Decimal("0")),
                        )
                    )
                    responses["stop"] = rollback
                    responses["breakeven_error"] = str(protection_error)
                    update_decision_protection(
                        decision["signal_id"],
                        rollback_client_id,
                        "ORIGINAL_REARMED",
                        None,
                        responses,
                        TRADING_DB_FILE,
                    )
                except Exception as rollback_error:
                    failure = RuntimeError(
                        f"成本保护止损失败且原止损恢复失败：{protection_error}；{rollback_error}"
                    )
                    responses["breakeven_error"] = str(protection_error)
                    responses["stop_rearm_error"] = str(rollback_error)
                    mark_decision_unprotected(
                        decision["signal_id"],
                        str(failure),
                        responses,
                        TRADING_DB_FILE,
                    )
                    raise failure from rollback_error
                result["errors"] += 1
                result["error_details"].append(
                    f"{symbol}: 成本保护替换失败，已恢复原止损：{protection_error}"
                )
                continue

            responses["stop_before_breakeven"] = old_stop
            responses["stop"] = new_stop
            responses["breakeven_management"] = {
                "activated_at_utc": now.isoformat(),
                "entry_price": decimal_text(entry_price),
                "mark_price": decimal_text(mark_price),
                "trigger_price": decimal_text(target_stop),
                "remaining_quantity": decimal_text(current_quantity),
                "buffer_pct": decimal_text(buffer_pct),
            }
            update_decision_protection(
                decision["signal_id"],
                stop_client_id,
                "BREAKEVEN_ACTIVE",
                float(target_stop),
                responses,
                TRADING_DB_FILE,
            )
            result["activated"] += 1
        except Exception as error:
            result["errors"] += 1
            result["error_details"].append(f"{symbol}: {error}")
    return result


def expire_due_positions(
    run_id: str,
    client: BinanceFuturesTradingClient | None,
    position_mode: str,
    position_rows: list[dict[str, Any]],
    exchange_rules: dict[str, dict[str, Decimal]],
) -> dict[str, Any]:
    """Close locally tracked positions whose actual hold deadline has passed."""
    now = datetime.now(timezone.utc)
    decisions = due_open_decisions(now.isoformat(), TRADING_DB_FILE)
    result = {"checked": len(decisions), "closed": 0, "errors": 0, "closed_symbols": set()}
    if not decisions:
        return result

    positions = {
        str(row.get("symbol") or "").upper(): row
        for row in position_rows
        if position_amount(row, position_mode) > 0
    }
    for decision in decisions:
        symbol = str(decision.get("symbol") or "").upper()
        try:
            if symbol in result["closed_symbols"]:
                update_decision_close(
                    decision["signal_id"],
                    "CLOSED_EXCHANGE",
                    now.isoformat(),
                    "同品种到期仓位已在本轮处理，清理重复本地台账",
                    db_file=TRADING_DB_FILE,
                )
                result["closed"] += 1
                continue
            if client is None:
                update_decision_close(
                    decision["signal_id"],
                    "DRY_RUN_TIME_EXIT",
                    now.isoformat(),
                    "dry-run 到期平仓",
                )
                result["closed"] += 1
                result["closed_symbols"].add(symbol)
                continue

            cancel_results = cancel_protection_orders(client, decision)
            position = positions.get(symbol)
            amount = position_amount(position, position_mode) if position else Decimal("0")
            responses: dict[str, Any] = {"cancel_exits": cancel_results}
            close_client_id = client_order_id_from_run(run_id, symbol, "time-exit")
            if amount > 0:
                rules = exchange_rules.get(symbol, {})
                quantity = floor_to_step(amount, rules.get("step_size", Decimal("0.001")))
                if quantity > 0:
                    responses["close"] = client.new_order(
                        order_params(
                            symbol=symbol,
                            side="BUY",
                            order_type="MARKET",
                            quantity=quantity,
                            client_id=close_client_id,
                            reduce_only=True,
                            position_mode=position_mode,
                        )
                    )
            status = "CLOSED_TIME_EXIT" if amount > 0 else "CLOSED_EXCHANGE"
            update_decision_close(
                decision["signal_id"],
                status,
                now.isoformat(),
                "持仓达到最大持仓时间，已市价平仓" if amount > 0 else "交易所已无对应持仓，清理本地台账",
                close_client_id if amount > 0 else None,
                responses,
            )
            result["closed"] += 1
            result["closed_symbols"].add(symbol)
        except Exception as error:
            result["errors"] += 1
            update_decision_close(
                decision["signal_id"],
                "TIME_EXIT_ERROR",
                now.isoformat(),
                f"到期平仓失败：{error}",
            )
    return result


def position_amount(row: dict[str, Any] | None, position_mode: str) -> Decimal:
    if not row:
        return Decimal("0")
    amount = Decimal(str(row.get("positionAmt", "0")))
    position_side = str(row.get("positionSide") or "BOTH")
    if position_mode == "hedge":
        return amount if position_side == "SHORT" and amount > 0 else Decimal("0")
    return abs(amount) if amount < 0 else Decimal("0")


def cancel_protection_orders(
    client: BinanceFuturesTradingClient,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort cancellation of stop/TP algo orders before time exit."""
    raw = decision.get("exchange_response")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    results: dict[str, Any] = {}
    symbol = str(decision.get("symbol") or "").upper()
    for name in ("stop", "first_tp", "final_tp"):
        response = raw.get(name)
        algo_id = response.get("algoId") if isinstance(response, dict) else None
        if not algo_id:
            continue
        try:
            results[name] = client.cancel_algo_order(symbol, int(algo_id))
        except Exception as error:
            results[name] = {"error": str(error)}
    return results


def build_order_plan(
    row: dict[str, Any],
    equity: Decimal,
    rules: dict[str, Decimal],
    leverage: int,
) -> dict[str, Any]:
    entry = Decimal(str(row.get("entry_price") or row.get("price") or "0"))
    stop = Decimal(str(row.get("stop_loss_price") or "0"))
    if entry <= 0 or stop <= entry:
        raise ValueError("SHORT 信号缺少有效入场价或止损价")

    multiplier = Decimal(str(row.get("position_multiplier") or "1"))
    planned_risk = equity * Decimal(str(TRADING_RISK_PCT)) / Decimal("100") * multiplier
    risk_per_unit = stop - entry
    quantity = planned_risk / risk_per_unit

    step_size = rules.get("step_size", Decimal("0.001"))
    min_qty = rules.get("min_qty", Decimal("0"))
    min_notional = max(rules.get("min_notional", Decimal("0")), Decimal(str(TRADING_MIN_NOTIONAL_USDT)))
    quantity = floor_to_step(quantity, step_size)
    notional = quantity * entry
    if quantity <= 0 or quantity < min_qty:
        raise ValueError("计算后的下单数量低于交易所最小数量")
    if notional < min_notional:
        raise ValueError(f"计算后的名义金额低于最小值 {min_notional} USDT")
    margin_usdt = notional / Decimal(str(leverage))

    first_close_value = row.get("first_take_profit_close_pct")
    final_close_value = row.get("final_take_profit_close_pct")
    first_close_pct = Decimal(str(50 if first_close_value is None else first_close_value)) / Decimal("100")
    final_close_pct = Decimal(str(50 if final_close_value is None else final_close_value)) / Decimal("100")
    first_qty = floor_to_step(quantity * first_close_pct, step_size)
    final_qty = floor_to_step(quantity * final_close_pct, step_size)

    return {
        "quantity": quantity,
        "notional": notional,
        "margin_usdt": margin_usdt,
        "planned_risk": planned_risk,
        "leverage": leverage,
        "first_tp_quantity": first_qty,
        "final_tp_quantity": final_qty,
        "tick_size": rules.get("tick_size", Decimal("0")),
    }


def submit_or_record(
    run_id: str,
    row: dict[str, Any],
    plan: dict[str, Any],
    client: BinanceFuturesTradingClient | None,
    position_mode: str,
) -> dict[str, Any]:
    decision = base_decision(run_id, row)
    decision.update(
        {
            "status": "DRY_RUN_PLANNED" if client is None else "OPEN_SUBMITTED",
            "dry_run": client is None,
            "quantity": float(plan["quantity"]),
            "notional_usdt": float(plan["notional"]),
            "planned_risk_usdt": float(plan["planned_risk"]),
            "leverage": plan["leverage"],
            "margin_usdt": float(plan["margin_usdt"]),
            "open_client_order_id": client_order_id(run_id, row, "open"),
        }
    )
    if client is None:
        opened_at = datetime.now(timezone.utc)
        decision["opened_at_utc"] = opened_at.isoformat()
        decision["time_exit_due_utc"] = time_exit_due(row, opened_at)
        decision["reason"] = "dry-run 或 TRADING_ENABLED 未开启，未提交真实订单"
        return decision

    symbol = row["symbol"]
    client.change_leverage(symbol, int(plan["leverage"]))
    responses: dict[str, Any] = {}
    open_params = order_params(
        symbol=symbol,
        side="SELL",
        order_type="MARKET",
        quantity=plan["quantity"],
        client_id=decision["open_client_order_id"],
        reduce_only=False,
        position_mode=position_mode,
    )
    responses["open"] = client.new_order(open_params)
    opened_at = order_response_time(responses["open"]) or datetime.now(timezone.utc)
    decision["opened_at_utc"] = opened_at.isoformat()
    decision["time_exit_due_utc"] = time_exit_due(row, opened_at)

    if TRADING_PLACE_EXITS:
        decision["stop_client_order_id"] = client_order_id(run_id, row, "stop")
        decision["first_tp_client_order_id"] = client_order_id(run_id, row, "tp1")
        decision["final_tp_client_order_id"] = client_order_id(run_id, row, "tp2")

        protection_errors: list[str] = []

        def submit_protection(name: str, order_type: str, quantity: Decimal, client_id: str, price: Any) -> bool:
            if quantity <= 0:
                return True
            try:
                responses[name] = client.algo_order(
                    algo_order_params(
                        algo_type="CONDITIONAL",
                        symbol=symbol,
                        side="BUY",
                        order_type=order_type,
                        quantity=quantity,
                        client_id=client_id,
                        stop_price=price,
                        reduce_only=True,
                        position_mode=position_mode,
                        tick_size=plan["tick_size"],
                    )
                )
                return bool(responses[name].get("algoId"))
            except Exception as error:
                protection_errors.append(f"{name}: {error}")
                return False

        stop_ok = submit_protection(
            "stop",
            "STOP_MARKET",
            plan["quantity"],
            decision["stop_client_order_id"],
            row["stop_loss_price"],
        )
        if not stop_ok:
            # Never leave a newly opened position without a confirmed stop.
            try:
                responses["emergency_close"] = client.new_order(
                    order_params(
                        symbol=symbol,
                        side="BUY",
                        order_type="MARKET",
                        quantity=plan["quantity"],
                        client_id=client_order_id(run_id, row, "emergency-close"),
                        reduce_only=True,
                        position_mode=position_mode,
                    )
                )
                decision["status"] = "ERROR"
                decision["reason"] = "止损单未确认，已紧急市价平仓；" + "；".join(protection_errors)
            except Exception as error:
                decision["status"] = "OPEN_UNPROTECTED"
                decision["reason"] = (
                    "止损单未确认且紧急平仓失败，存在未保护仓位："
                    + "；".join(protection_errors)
                    + f"；emergency_close: {error}"
                )
            decision["exchange_response"] = responses
            return decision
        if plan["first_tp_quantity"] > 0:
            submit_protection(
                "first_tp",
                "TAKE_PROFIT_MARKET",
                plan["first_tp_quantity"],
                decision["first_tp_client_order_id"],
                row["first_take_profit_price"],
            )
        submit_protection(
            "final_tp",
            "TAKE_PROFIT_MARKET",
            plan["final_tp_quantity"],
            decision["final_tp_client_order_id"],
            row["final_take_profit_price"],
        )
        if protection_errors:
            decision["reason"] = "保护单部分失败：" + "；".join(protection_errors)
    decision["exchange_response"] = responses
    return decision


def order_params(
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    client_id: str,
    reduce_only: bool,
    stop_price: float | str | None = None,
    position_mode: str = TRADING_POSITION_MODE,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": decimal_text(quantity),
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }
    if reduce_only:
        if position_mode != "hedge":
            params["reduceOnly"] = "true"
    if stop_price is not None:
        params["stopPrice"] = stop_price
        params["workingType"] = "MARK_PRICE"
    if position_mode == "hedge":
        params["positionSide"] = "SHORT"
    return params


def algo_order_params(
    algo_type: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    client_id: str,
    reduce_only: bool,
    position_mode: str,
    tick_size: Decimal,
    stop_price: float | str,
) -> dict[str, Any]:
    trigger = format_trigger_price(stop_price, tick_size, order_type)
    params: dict[str, Any] = {
        "algoType": algo_type,
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": decimal_text(quantity),
        "triggerPrice": decimal_text(trigger),
        "workingType": "MARK_PRICE",
        "clientAlgoId": client_id,
    }
    if reduce_only and position_mode != "hedge":
        params["reduceOnly"] = "true"
    if position_mode == "hedge":
        params["positionSide"] = "SHORT"
    return params


def format_trigger_price(
    price: float | str | Decimal,
    tick_size: Decimal,
    order_type: str,
) -> Decimal:
    """Format a trigger price to the symbol's PRICE_FILTER tick size."""
    value = Decimal(str(price))
    if tick_size <= 0:
        return value
    rounding = ROUND_CEILING if order_type == "STOP_MARKET" else ROUND_FLOOR
    units = (value / tick_size).to_integral_value(rounding=rounding)
    return (units * tick_size).quantize(tick_size)


def base_decision(run_id: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "signal_id": signal_key(row),
        "timestamp_utc": row.get("timestamp_utc"),
        "symbol": row["symbol"],
        "side": row.get("entry_side"),
        "trade_grade": row.get("trade_grade"),
        "is_star": bool(row.get("is_star")),
        "status": "SKIPPED",
        "dry_run": TRADING_DRY_RUN or not TRADING_ENABLED,
        "entry_price": row.get("entry_price"),
        "stop_loss_price": row.get("stop_loss_price"),
        "first_take_profit_price": row.get("first_take_profit_price"),
        "final_take_profit_price": row.get("final_take_profit_price"),
        "max_hold_hours": row.get("max_hold_hours"),
        "position_multiplier": row.get("position_multiplier"),
        "raw_signal": row,
    }


def save_skip(run_id: str, row: dict[str, Any], reason: str) -> None:
    decision = base_decision(run_id, row)
    decision.update({"status": "SKIPPED", "reason": reason})
    save_decision(decision)


def signal_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("pattern_version") or ""),
            str(row.get("strategy_config_hash") or ""),
            str(row.get("timestamp_utc") or ""),
            str(row.get("pattern_key") or ""),
            str(row.get("symbol") or ""),
            str(row.get("entry_side") or ""),
        ]
    )


def client_order_id(run_id: str, row: dict[str, Any], suffix: str) -> str:
    return client_order_id_from_run(run_id, str(row["symbol"]), suffix)


def client_order_id_from_run(run_id: str, symbol: str, suffix: str) -> str:
    raw = f"{TRADING_ORDER_PREFIX}_{run_id}_{symbol}_{suffix}"
    return raw[:36]


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def resolve_leverage(
    client: BinanceFuturesTradingClient | None,
    symbol: str,
    cache: dict[str, int],
) -> int:
    if symbol in cache:
        return cache[symbol]
    if TRADING_LEVERAGE_MODE != "max" or client is None:
        leverage = TRADING_LEVERAGE
    else:
        leverage = max_symbol_leverage(client, symbol)
    leverage = max(1, int(leverage))
    cache[symbol] = leverage
    return leverage


def max_symbol_leverage(client: BinanceFuturesTradingClient, symbol: str) -> int:
    data = client.leverage_bracket(symbol)
    item = data[0] if isinstance(data, list) and data else data
    brackets = item.get("brackets", []) if isinstance(item, dict) else []
    leverages = [int(row.get("initialLeverage", 0)) for row in brackets if row.get("initialLeverage")]
    if not leverages:
        raise RuntimeError(f"无法读取 {symbol} 最大杠杆")
    return max(leverages)


def order_response_time(response: Any) -> datetime | None:
    if not isinstance(response, dict):
        return None
    timestamp = response.get("transactTime") or response.get("updateTime")
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def parse_datetime_or_none(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def time_exit_due(row: dict[str, Any], opened_at: datetime | None = None) -> str | None:
    hours = row.get("max_hold_hours")
    timestamp = row.get("timestamp_utc")
    if hours is None:
        return None
    if opened_at is None:
        if not timestamp:
            return None
        opened_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    return (opened_at + timedelta(hours=int(hours))).astimezone(timezone.utc).isoformat()
