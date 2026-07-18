"""本地可视化服务：把 SQLite 和输出文件转换成浏览器可读取的 API。"""

from __future__ import annotations

import json
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import (
    BASE_DIR,
    PATTERN_SIGNALS_JSON_FILE,
    SQLITE_DB_FILE,
    TOP_N,
    TWEETS_JSON_FILE,
    X_POST_PREVIEW_JSON_FILE,
)
from indicators.outcome_probability import estimate_outcome_probability


WEB_DIR = BASE_DIR / "web"


class RadarRequestHandler(SimpleHTTPRequestHandler):
    """静态页面 + JSON API 请求处理器。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        """按路径分发 API 或静态资源。"""
        parsed = urlparse(self.path)
        if parsed.path == "/api/summary":
            return self._send_json(build_summary())
        if parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            limit = int(query.get("limit", ["240"])[0])
            return self._send_json(load_history(limit))
        if parsed.path == "/api/tweets":
            return self._send_json(read_json_file(TWEETS_JSON_FILE, []))
        if parsed.path == "/api/x-preview":
            return self._send_json(read_json_file(X_POST_PREVIEW_JSON_FILE, {}))
        if parsed.path == "/api/patterns":
            return self._send_json(read_json_file(PATTERN_SIGNALS_JSON_FILE, {}))
        return super().do_GET()

    def _send_json(self, payload: object) -> None:
        """返回 JSON 响应。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_summary() -> dict[str, object]:
    """读取最新一轮数据，生成仪表盘摘要。"""
    rows = add_first_alert_context(load_latest_snapshots())
    x_preview = read_json_file(X_POST_PREVIEW_JSON_FILE, {})
    tweets = read_json_file(TWEETS_JSON_FILE, [])
    high_risk = [row for row in rows if (row.get("risk_score") or 0) >= 70]
    max_risk = max([row.get("risk_score") or 0 for row in rows], default=0)
    last_updated = rows[0]["timestamp_utc"] if rows else None
    return {
        "last_updated": last_updated,
        "coins": rows,
        "top": sorted(rows, key=lambda row: row.get("risk_score") or 0, reverse=True)[:TOP_N],
        "patterns": read_json_file(PATTERN_SIGNALS_JSON_FILE, {}),
        "tweet_count": len(tweets),
        "publish_candidates": len(high_risk),
        "x_preview": {
            "dry_run": x_preview.get("dry_run", True),
            "post_to_x": x_preview.get("post_to_x", False),
            "min_risk_score": x_preview.get("min_risk_score", 70),
            "items_count": len(x_preview.get("items", [])),
        },
        "max_risk": max_risk,
    }


def load_latest_snapshots() -> list[dict[str, object]]:
    """按币种读取数据库中最新一条记录。"""
    if not SQLITE_DB_FILE.exists():
        return []
    sql = """
        SELECT timestamp_utc, coin, symbol, price, funding_rate, open_interest,
               oi_change_1h, oi_change_24h, long_liquidation, short_liquidation,
               price_change_1h, price_change_4h, price_change_24h,
               price_position_24h, quote_volume_24h, quote_volume_change_24h,
               funding_same_sign_count, funding_avg_abs_6,
               risk_score, anomaly_tag, source
        FROM market_snapshots
        WHERE timestamp_utc = (
            SELECT MAX(timestamp_utc)
            FROM market_snapshots
        )
        ORDER BY risk_score DESC, symbol ASC
    """
    rows = query_rows(sql)
    for row in rows:
        row["outcome_probability"] = estimate_outcome_probability(row)
    return rows


def add_first_alert_context(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """为最新异常信号补充首次提示时间、价格和后续最大涨跌幅。"""
    if not rows or not SQLITE_DB_FILE.exists():
        return rows

    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        for row in rows:
            tag = str(row.get("anomaly_tag") or "")
            symbol = row.get("symbol")
            if not symbol or _is_normal_tag(tag):
                row["first_alert_at"] = None
                row["first_alert_price"] = None
                row["max_gain_since_first_alert_pct"] = None
                row["max_drawdown_since_first_alert_pct"] = None
                continue
            first_alert = conn.execute(
                """
                SELECT timestamp_utc, price
                FROM market_snapshots
                WHERE symbol = ?
                  AND anomaly_tag = ?
                  AND anomaly_tag NOT IN ('正常', '姝ｅ父')
                  AND price IS NOT NULL
                ORDER BY timestamp_utc ASC
                LIMIT 1
                """,
                (symbol, tag),
            ).fetchone()
            if not first_alert:
                row["first_alert_at"] = None
                row["first_alert_price"] = None
                row["max_gain_since_first_alert_pct"] = None
                row["max_drawdown_since_first_alert_pct"] = None
                continue

            first_seen, first_price = first_alert
            row["first_alert_at"] = first_seen
            row["first_alert_price"] = first_price
            move = conn.execute(
                """
                SELECT MAX(price), MIN(price)
                FROM market_snapshots
                WHERE symbol = ?
                  AND timestamp_utc >= ?
                  AND timestamp_utc <= ?
                  AND price IS NOT NULL
                """,
                (symbol, first_seen, row.get("timestamp_utc")),
            ).fetchone()
            max_price, min_price = move if move else (None, None)
            if first_price and max_price is not None and min_price is not None:
                row["max_gain_since_first_alert_pct"] = (max_price - first_price) / first_price * 100
                row["max_drawdown_since_first_alert_pct"] = (min_price - first_price) / first_price * 100
            else:
                row["max_gain_since_first_alert_pct"] = None
                row["max_drawdown_since_first_alert_pct"] = None

    return rows


def _is_normal_tag(tag: str) -> bool:
    """兼容早期乱码数据和当前中文正常标签。"""
    cleaned = tag.strip()
    return cleaned in {"", "正常", "姝ｅ父"}


def load_history(limit: int) -> list[dict[str, object]]:
    """读取最近 N 条历史记录，用于前端趋势图。"""
    if not SQLITE_DB_FILE.exists():
        return []
    sql = """
        SELECT *
        FROM (
            SELECT timestamp_utc, coin, symbol, price, funding_rate, open_interest,
                   oi_change_1h, oi_change_24h, long_liquidation, short_liquidation,
                   price_change_1h, price_change_4h, price_change_24h,
                   price_position_24h, quote_volume_24h, quote_volume_change_24h,
                   funding_same_sign_count, funding_avg_abs_6,
                   risk_score, anomaly_tag, source
            FROM market_snapshots
            ORDER BY timestamp_utc DESC, symbol ASC
            LIMIT ?
        )
        ORDER BY timestamp_utc ASC, symbol ASC
    """
    rows = query_rows(sql, (limit,))
    for row in rows:
        row["outcome_probability"] = estimate_outcome_probability(row)
    return rows


def query_rows(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    """执行查询并返回字典列表。"""
    with sqlite3.connect(SQLITE_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def read_json_file(path: Path, default: object) -> object:
    """读取 JSON 文件；不存在或格式异常时返回默认值。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """启动本地 Web 服务。"""
    server = ThreadingHTTPServer((host, port), RadarRequestHandler)
    print(f"Crypto Squeeze Radar dashboard: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
