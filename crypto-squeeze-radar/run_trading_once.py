"""Run one trading execution pass from output/pattern_signals.json."""

from __future__ import annotations

import sys
from datetime import datetime

from trading.executor import run_trading_cycle


def main() -> None:
    print(f"[{now()}] 开始交易执行层")
    summary = run_trading_cycle()
    print(
        f"[{now()}] 完成：信号 {summary['signals_seen']}，"
        f"提交/计划 {summary['submitted']}，跳过 {summary['skipped']}，错误 {summary['errors']}"
    )


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[{now()}] 交易执行失败：{error}", file=sys.stderr)
        raise
