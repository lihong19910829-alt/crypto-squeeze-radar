"""Run one full-market deep OI scan for database backfill."""

from __future__ import annotations

import sys
from datetime import datetime

import main as radar_main


def main() -> None:
    print(f"[{now()}] 开始全市场深度 OI 补数")
    rows = radar_main.main(scan_mode="full_scan", emit_outputs=False)
    print(f"[{now()}] 全市场深度 OI 补数完成：写入 {len(rows)} 个交易对")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[{now()}] 全市场深度 OI 补数失败：{error}", file=sys.stderr)
        raise
