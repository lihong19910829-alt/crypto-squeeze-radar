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
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trading_decisions_symbol_status
            ON trading_decisions (symbol, status)
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


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
