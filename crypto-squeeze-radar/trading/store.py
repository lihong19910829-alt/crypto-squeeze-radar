"""SQLite ledger for trading decisions and exchange order IDs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import TRADING_DB_FILE


OPEN_DECISION_STATUSES = ("DRY_RUN_PLANNED", "OPEN_SUBMITTED", "OPEN_UNPROTECTED")


def init_trading_db(db_file: Path = TRADING_DB_FILE) -> None:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trade_grade TEXT,
                is_star INTEGER NOT NULL,
                status TEXT NOT NULL,
                dry_run INTEGER NOT NULL,
                reason TEXT,
                entry_price REAL,
                stop_loss_price REAL,
                first_take_profit_price REAL,
                final_take_profit_price REAL,
                leverage INTEGER,
                margin_usdt REAL,
                max_hold_hours INTEGER,
                time_exit_due_utc TEXT,
                quantity REAL,
                notional_usdt REAL,
                planned_risk_usdt REAL,
                position_multiplier REAL,
                open_client_order_id TEXT,
                stop_client_order_id TEXT,
                first_tp_client_order_id TEXT,
                final_tp_client_order_id TEXT,
                opened_at_utc TEXT,
                closed_at_utc TEXT,
                close_client_order_id TEXT,
                close_reason TEXT,
                entry_fill_price REAL,
                exit_fill_price REAL,
                entry_filled_qty REAL,
                exit_filled_qty REAL,
                gross_realized_pnl_usdt REAL,
                commission_usdt REAL,
                funding_fee_usdt REAL,
                net_realized_pnl_usdt REAL,
                first_fill_at_utc TEXT,
                last_fill_at_utc TEXT,
                exchange_trade_count INTEGER,
                reconciliation_status TEXT,
                reconciled_at_utc TEXT,
                reconciliation_note TEXT,
                protection_state TEXT,
                breakeven_stop_price REAL,
                protection_updated_at_utc TEXT,
                exchange_response TEXT,
                raw_signal TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trading_decisions_signal
            ON trading_decisions (signal_id)
            """
        )
        _ensure_columns(
            conn,
            "trading_decisions",
            {
                "leverage": "INTEGER",
                "margin_usdt": "REAL",
                "max_hold_hours": "INTEGER",
                "time_exit_due_utc": "TEXT",
                "opened_at_utc": "TEXT",
                "closed_at_utc": "TEXT",
                "close_client_order_id": "TEXT",
                "close_reason": "TEXT",
                "entry_fill_price": "REAL",
                "exit_fill_price": "REAL",
                "entry_filled_qty": "REAL",
                "exit_filled_qty": "REAL",
                "gross_realized_pnl_usdt": "REAL",
                "commission_usdt": "REAL",
                "funding_fee_usdt": "REAL",
                "net_realized_pnl_usdt": "REAL",
                "first_fill_at_utc": "TEXT",
                "last_fill_at_utc": "TEXT",
                "exchange_trade_count": "INTEGER",
                "reconciliation_status": "TEXT",
                "reconciled_at_utc": "TEXT",
                "reconciliation_note": "TEXT",
                "protection_state": "TEXT",
                "breakeven_stop_price": "REAL",
                "protection_updated_at_utc": "TEXT",
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trading_decisions_symbol_status
            ON trading_decisions (symbol, status)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exchange_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_id INTEGER NOT NULL,
                order_id INTEGER,
                side TEXT,
                position_side TEXT,
                price REAL,
                quantity REAL,
                quote_quantity REAL,
                realized_pnl REAL,
                commission REAL,
                commission_asset TEXT,
                trade_time_ms INTEGER NOT NULL,
                trade_time_utc TEXT NOT NULL,
                buyer INTEGER,
                maker INTEGER,
                decision_signal_id TEXT,
                raw_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE (symbol, trade_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_exchange_fills_symbol_time
            ON exchange_fills (symbol, trade_time_ms)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exchange_income (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                income_type TEXT NOT NULL,
                tran_id INTEGER NOT NULL,
                symbol TEXT,
                income REAL,
                asset TEXT,
                trade_id TEXT,
                info TEXT,
                trade_time_ms INTEGER NOT NULL,
                trade_time_utc TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE (income_type, tran_id, symbol, trade_time_ms)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_exchange_income_symbol_time
            ON exchange_income (symbol, trade_time_ms, income_type)
            """
        )


def save_decision(decision: dict[str, Any], db_file: Path = TRADING_DB_FILE) -> None:
    init_trading_db(db_file)
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO trading_decisions (
                run_id, signal_id, timestamp_utc, symbol, side, trade_grade,
                is_star, status, dry_run, reason, entry_price, stop_loss_price,
                first_take_profit_price, final_take_profit_price,
                leverage, margin_usdt, max_hold_hours, time_exit_due_utc,
                quantity, notional_usdt, planned_risk_usdt, position_multiplier,
                open_client_order_id, stop_client_order_id,
                first_tp_client_order_id, final_tp_client_order_id,
                opened_at_utc, closed_at_utc, close_client_order_id, close_reason,
                exchange_response, raw_signal, created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision["run_id"],
                decision["signal_id"],
                decision["timestamp_utc"],
                decision["symbol"],
                decision["side"],
                decision.get("trade_grade"),
                1 if decision.get("is_star") else 0,
                decision["status"],
                1 if decision.get("dry_run") else 0,
                decision.get("reason"),
                decision.get("entry_price"),
                decision.get("stop_loss_price"),
                decision.get("first_take_profit_price"),
                decision.get("final_take_profit_price"),
                decision.get("leverage"),
                decision.get("margin_usdt"),
                decision.get("max_hold_hours"),
                decision.get("time_exit_due_utc"),
                decision.get("quantity"),
                decision.get("notional_usdt"),
                decision.get("planned_risk_usdt"),
                decision.get("position_multiplier"),
                decision.get("open_client_order_id"),
                decision.get("stop_client_order_id"),
                decision.get("first_tp_client_order_id"),
                decision.get("final_tp_client_order_id"),
                decision.get("opened_at_utc"),
                decision.get("closed_at_utc"),
                decision.get("close_client_order_id"),
                decision.get("close_reason"),
                json.dumps(decision.get("exchange_response"), ensure_ascii=False),
                json.dumps(decision["raw_signal"], ensure_ascii=False),
                now,
            ),
        )


def due_open_decisions(
    now_utc: str,
    db_file: Path = TRADING_DB_FILE,
) -> list[dict[str, Any]]:
    """Return locally open decisions whose time exit is due."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trading_decisions
            WHERE status IN ('DRY_RUN_PLANNED', 'OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
              AND time_exit_due_utc IS NOT NULL
              AND time_exit_due_utc <= ?
            ORDER BY time_exit_due_utc, id
            """,
            (now_utc,),
        ).fetchall()
    return [dict(row) for row in rows]


def open_protection_decisions(
    db_file: Path = TRADING_DB_FILE,
) -> list[dict[str, Any]]:
    """Return live decisions whose TP1 protection may need maintenance."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM trading_decisions
            WHERE status IN ('OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
              AND COALESCE(protection_state, '') != 'BREAKEVEN_ACTIVE'
            ORDER BY opened_at_utc, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_decision_protection(
    signal_id: str,
    stop_client_order_id: str,
    protection_state: str,
    breakeven_stop_price: float | None,
    exchange_response: Any,
    db_file: Path = TRADING_DB_FILE,
) -> None:
    """Persist a stop replacement so protection management stays idempotent."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            UPDATE trading_decisions
            SET stop_client_order_id = ?, protection_state = ?,
                breakeven_stop_price = ?, protection_updated_at_utc = ?,
                exchange_response = ?
            WHERE signal_id = ?
              AND status IN ('OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
            """,
            (
                stop_client_order_id,
                protection_state,
                breakeven_stop_price,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(exchange_response, ensure_ascii=False),
                signal_id,
            ),
        )


def mark_decision_unprotected(
    signal_id: str,
    reason: str,
    exchange_response: Any,
    db_file: Path = TRADING_DB_FILE,
) -> None:
    """Record a protection failure that requires urgent operator attention."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            UPDATE trading_decisions
            SET status = 'OPEN_UNPROTECTED', reason = ?,
                protection_state = 'UNPROTECTED', protection_updated_at_utc = ?,
                exchange_response = ?
            WHERE signal_id = ?
              AND status IN ('OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
            """,
            (
                reason,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(exchange_response, ensure_ascii=False),
                signal_id,
            ),
        )


def update_decision_close(
    signal_id: str,
    status: str,
    closed_at_utc: str,
    close_reason: str,
    close_client_order_id: str | None = None,
    exchange_response: Any = None,
    db_file: Path = TRADING_DB_FILE,
) -> None:
    """Mark an open decision as closed exactly once."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            UPDATE trading_decisions
            SET status = ?, closed_at_utc = ?, close_reason = ?,
                close_client_order_id = ?, exchange_response = ?
            WHERE signal_id = ?
              AND status IN ('DRY_RUN_PLANNED', 'OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
            """,
            (
                status,
                closed_at_utc,
                close_reason,
                close_client_order_id,
                json.dumps(exchange_response, ensure_ascii=False),
                signal_id,
            ),
        )


def existing_signal_ids(db_file: Path = TRADING_DB_FILE) -> set[str]:
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute("SELECT signal_id FROM trading_decisions").fetchall()
    return {row[0] for row in rows}


def open_local_decision_count(db_file: Path = TRADING_DB_FILE) -> int:
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM trading_decisions
            WHERE status IN ('DRY_RUN_PLANNED', 'OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
            """
        ).fetchone()
    return int(row[0] or 0)


def open_local_symbols(db_file: Path = TRADING_DB_FILE) -> set[str]:
    """Return symbols with a locally recorded open or planned position."""
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT symbol
            FROM trading_decisions
            WHERE status IN ('DRY_RUN_PLANNED', 'OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
            """
        ).fetchall()
    return {str(row[0]).upper() for row in rows if row[0]}


def reconcilable_decisions(
    only_unreconciled: bool = True,
    include_open: bool = False,
    db_file: Path = TRADING_DB_FILE,
) -> list[dict[str, Any]]:
    """Return real decisions that have an exchange-side opening timestamp."""
    init_trading_db(db_file)
    clauses = ["dry_run = 0", "opened_at_utc IS NOT NULL"]
    params: list[Any] = []
    if not include_open:
        clauses.append("status NOT IN ('OPEN_SUBMITTED', 'OPEN_UNPROTECTED')")
    if only_unreconciled:
        clauses.append("COALESCE(reconciliation_status, '') != 'COMPLETE'")
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM trading_decisions WHERE {' AND '.join(clauses)} "
            "ORDER BY symbol, opened_at_utc, id",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_exchange_fills(
    rows: list[dict[str, Any]],
    db_file: Path = TRADING_DB_FILE,
) -> int:
    if not rows:
        return 0
    init_trading_db(db_file)
    now = datetime.now(timezone.utc).isoformat()
    values = []
    for row in rows:
        time_ms = int(row.get("time") or 0)
        values.append(
            (
                str(row.get("symbol") or "").upper(),
                int(row.get("id") or 0),
                _optional_int(row.get("orderId")),
                row.get("side"),
                row.get("positionSide"),
                _optional_float(row.get("price")),
                _optional_float(row.get("qty")),
                _optional_float(row.get("quoteQty")),
                _optional_float(row.get("realizedPnl")),
                _optional_float(row.get("commission")),
                row.get("commissionAsset"),
                time_ms,
                _milliseconds_to_utc(time_ms),
                1 if row.get("buyer") else 0,
                1 if row.get("maker") else 0,
                json.dumps(row, ensure_ascii=False),
                now,
                now,
            )
        )
    with sqlite3.connect(db_file) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO exchange_fills (
                symbol, trade_id, order_id, side, position_side, price, quantity,
                quote_quantity, realized_pnl, commission, commission_asset,
                trade_time_ms, trade_time_utc, buyer, maker, raw_json,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_id) DO UPDATE SET
                order_id=excluded.order_id, side=excluded.side,
                position_side=excluded.position_side, price=excluded.price,
                quantity=excluded.quantity, quote_quantity=excluded.quote_quantity,
                realized_pnl=excluded.realized_pnl, commission=excluded.commission,
                commission_asset=excluded.commission_asset,
                trade_time_ms=excluded.trade_time_ms,
                trade_time_utc=excluded.trade_time_utc, buyer=excluded.buyer,
                maker=excluded.maker, raw_json=excluded.raw_json,
                updated_at_utc=excluded.updated_at_utc
            """,
            values,
        )
        return conn.total_changes - before


def upsert_exchange_income(
    rows: list[dict[str, Any]],
    db_file: Path = TRADING_DB_FILE,
) -> int:
    if not rows:
        return 0
    init_trading_db(db_file)
    now = datetime.now(timezone.utc).isoformat()
    values = []
    for row in rows:
        time_ms = int(row.get("time") or 0)
        values.append(
            (
                str(row.get("incomeType") or ""),
                int(row.get("tranId") or 0),
                str(row.get("symbol") or "").upper(),
                _optional_float(row.get("income")),
                row.get("asset"),
                str(row.get("tradeId") or ""),
                row.get("info"),
                time_ms,
                _milliseconds_to_utc(time_ms),
                json.dumps(row, ensure_ascii=False),
                now,
                now,
            )
        )
    with sqlite3.connect(db_file) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO exchange_income (
                income_type, tran_id, symbol, income, asset, trade_id, info,
                trade_time_ms, trade_time_utc, raw_json, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(income_type, tran_id, symbol, trade_time_ms) DO UPDATE SET
                income=excluded.income, asset=excluded.asset,
                trade_id=excluded.trade_id, info=excluded.info,
                raw_json=excluded.raw_json, updated_at_utc=excluded.updated_at_utc
            """,
            values,
        )
        return conn.total_changes - before


def load_exchange_fills(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    db_file: Path = TRADING_DB_FILE,
) -> list[dict[str, Any]]:
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM exchange_fills
            WHERE symbol = ? AND trade_time_ms BETWEEN ? AND ?
            ORDER BY trade_time_ms, trade_id
            """,
            (symbol.upper(), start_time_ms, end_time_ms),
        ).fetchall()
    return [dict(row) for row in rows]


def load_funding_income(
    symbol: str,
    start_time_ms: int,
    end_time_ms: int,
    db_file: Path = TRADING_DB_FILE,
) -> list[dict[str, Any]]:
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM exchange_income
            WHERE symbol = ? AND income_type = 'FUNDING_FEE'
              AND trade_time_ms BETWEEN ? AND ?
            ORDER BY trade_time_ms, id
            """,
            (symbol.upper(), start_time_ms, end_time_ms),
        ).fetchall()
    return [dict(row) for row in rows]


def update_decision_reconciliation(
    signal_id: str,
    symbol: str,
    values: dict[str, Any],
    fill_trade_ids: list[int],
    db_file: Path = TRADING_DB_FILE,
) -> None:
    init_trading_db(db_file)
    fields = [
        "entry_fill_price", "exit_fill_price", "entry_filled_qty", "exit_filled_qty",
        "gross_realized_pnl_usdt", "commission_usdt", "funding_fee_usdt",
        "net_realized_pnl_usdt", "first_fill_at_utc", "last_fill_at_utc",
        "exchange_trade_count", "reconciliation_status", "reconciled_at_utc",
        "reconciliation_note",
    ]
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            f"UPDATE trading_decisions SET {', '.join(f'{field} = ?' for field in fields)} "
            "WHERE signal_id = ?",
            tuple(values.get(field) for field in fields) + (signal_id,),
        )
        if values.get("reconciliation_status") == "COMPLETE":
            conn.execute(
                """
                UPDATE trading_decisions
                SET status = 'CLOSED_RECONCILED',
                    closed_at_utc = COALESCE(closed_at_utc, ?),
                    close_reason = COALESCE(
                        close_reason,
                        '交易所成交对账确认仓位已完全退出'
                    )
                WHERE signal_id = ?
                  AND status IN ('OPEN_SUBMITTED', 'OPEN_UNPROTECTED')
                """,
                (values.get("last_fill_at_utc"), signal_id),
            )
        if fill_trade_ids:
            placeholders = ", ".join("?" for _ in fill_trade_ids)
            conn.execute(
                f"UPDATE exchange_fills SET decision_signal_id = ? "
                f"WHERE symbol = ? AND trade_id IN ({placeholders})",
                (signal_id, symbol.upper(), *fill_trade_ids),
            )


def reconciliation_counts(db_file: Path = TRADING_DB_FILE) -> dict[str, int]:
    init_trading_db(db_file)
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(reconciliation_status, 'UNRECONCILED'), COUNT(*)
            FROM trading_decisions
            WHERE dry_run = 0 AND opened_at_utc IS NOT NULL
            GROUP BY COALESCE(reconciliation_status, 'UNRECONCILED')
            """
        ).fetchall()
    return {str(status): int(count) for status, count in rows}


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _milliseconds_to_utc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
