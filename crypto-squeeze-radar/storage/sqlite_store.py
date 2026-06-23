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
                long_liquidation REAL,
                short_liquidation REAL,
                risk_score INTEGER,
                anomaly_tag TEXT,
                source TEXT,
                created_at_utc TEXT NOT NULL
            )
            """
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
                long_liquidation,
                short_liquidation,
                risk_score,
                anomaly_tag,
                source,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

