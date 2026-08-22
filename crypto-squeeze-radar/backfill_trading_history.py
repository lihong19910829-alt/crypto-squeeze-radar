"""One-off read-only Binance history backfill and local trade reconciliation."""

from __future__ import annotations

import argparse
import os

from trading.binance_futures import BinanceFuturesTradingClient
from trading.reconciliation import backfill_and_reconcile


def main() -> None:
    parser = argparse.ArgumentParser(description="回填成交、手续费、资金费并对账真实盈亏")
    parser.add_argument("--all", action="store_true", help="重新核对已完成的决策")
    parser.add_argument("--include-open", action="store_true", help="同时核对未平仓决策")
    args = parser.parse_args()

    client = BinanceFuturesTradingClient(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )
    summary = backfill_and_reconcile(
        client,
        only_unreconciled=not args.all,
        include_open=args.include_open,
    )
    print(
        "回填完成："
        f"决策 {summary['decisions_selected']}，品种 {summary['symbols_selected']}，"
        f"成交 {summary['fills_fetched']}，收益流水 {summary['income_rows_fetched']}，"
        f"已核对 {summary['reconciled']}，远端错误 {summary['remote_errors']}"
    )
    print(f"对账状态：{summary['status_counts']}")


if __name__ == "__main__":
    main()
