"""SQLite 历史数据库：用于长期积累信号和后续回测。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import SQLITE_DB_FILE


def init_db(db_file: Path = SQLITE_DB_FILE) -> None:
    """初始化 SQLite 表结构；如果表已存在则不会重复创建。"""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                coin TEXT,
                symbol TEXT NOT NULL,
                price REAL,
                funding_rate REAL,
                open_interest REAL,
                oi_change_1h REAL,
                oi_change_24h REAL,
                price_change_1h REAL,
                price_change_4h REAL,
                price_change_24h REAL,
                price_position_24h REAL,
                high_24h REAL,
                low_24h REAL,
                quote_volume_24h REAL,
                quote_volume_change_24h REAL,
                funding_same_sign_count INTEGER,
                funding_avg_abs_6 REAL,
                long_liquidation REAL,
                short_liquidation REAL,
                risk_score INTEGER,
                anomaly_tag TEXT,
                source TEXT,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        _ensure_columns(
            conn,
            "market_snapshots",
            {
                "price_change_1h": "REAL",
                "price_change_4h": "REAL",
                "price_change_24h": "REAL",
                "price_position_24h": "REAL",
                "high_24h": "REAL",
                "low_24h": "REAL",
                "quote_volume_24h": "REAL",
                "quote_volume_change_24h": "REAL",
                "funding_same_sign_count": "INTEGER",
                "funding_avg_abs_6": "REAL",
            },
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_time
            ON market_snapshots (symbol, timestamp_utc)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_anomaly_tag
            ON market_snapshots (anomaly_tag)
            """
        )


def save_market_snapshots(items: list[dict[str, Any]], db_file: Path = SQLITE_DB_FILE) -> None:
    """把本轮每个币种的监控结果写入 SQLite。"""
    init_db(db_file)
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    rows = []

    for item in items:
        # 数据源完全失败时 symbol 为空，这种记录不适合做价格回测，仍保留 coin 但跳过入库。
        if not item.get("symbol"):
            continue
        rows.append(
            (
                timestamp_utc,
                item.get("coin"),
                item.get("symbol"),
                item.get("price"),
                item.get("funding_rate"),
                item.get("open_interest"),
                item.get("oi_change_1h_pct"),
                item.get("oi_change_24h_pct"),
                item.get("price_change_1h_pct"),
                item.get("price_change_4h_pct"),
                item.get("price_change_24h_pct"),
                item.get("price_position_24h_pct"),
                item.get("high_24h"),
                item.get("low_24h"),
                item.get("quote_volume_24h"),
                item.get("quote_volume_change_24h_pct"),
                item.get("funding_same_sign_count"),
                item.get("funding_avg_abs_6"),
                item.get("long_liquidation_usd"),
                item.get("short_liquidation_usd"),
                item.get("risk_score"),
                "、".join(item.get("tags", [])),
                item.get("source"),
                timestamp_utc,
            )
        )

    with sqlite3.connect(db_file) as conn:
        conn.executemany(
            """
            INSERT INTO market_snapshots (
                timestamp_utc,
                coin,
                symbol,
                price,
                funding_rate,
                open_interest,
                oi_change_1h,
                oi_change_24h,
                price_change_1h,
                price_change_4h,
                price_change_24h,
                price_position_24h,
                high_24h,
                low_24h,
                quote_volume_24h,
                quote_volume_change_24h,
                funding_same_sign_count,
                funding_avg_abs_6,
                long_liquidation,
                short_liquidation,
                risk_score,
                anomaly_tag,
                source,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """为已有 SQLite 表补列，避免历史库需要手工迁移。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
