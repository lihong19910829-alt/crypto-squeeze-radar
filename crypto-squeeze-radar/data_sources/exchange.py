"""交易所公开数据接口：优先从 Binance U 本位永续获取 MVP 所需数据。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import (
    BINANCE_CONTRACT_TYPE,
    BINANCE_QUOTE_ASSET,
    BINANCE_SYMBOLS,
    ENABLE_BINANCE_FORCE_ORDERS,
    HTTP_TIMEOUT_SECONDS,
    MAX_BINANCE_SYMBOLS,
)


@dataclass
class MarketSnapshot:
    """单个币种在某一时刻的市场快照。"""

    coin: str
    symbol: str
    price: float | None
    funding_rate: float | None
    open_interest: float | None
    open_interest_value_usd: float | None
    oi_change_1h_pct: float | None
    oi_change_24h_pct: float | None
    long_liquidation_usd: float | None
    short_liquidation_usd: float | None
    source: str
    warnings: list[str]


class BinanceFuturesClient:
    """Binance USD-M Futures 公开 REST 客户端。"""

    BASE_URL = "https://fapi.binance.com"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """发送 GET 请求，并把 JSON 响应转换为 Python 对象。"""
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.BASE_URL}{path}{query}"
        request = Request(url, headers={"User-Agent": "crypto-squeeze-radar/0.1"})
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Binance HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Binance 网络错误: {exc.reason}") from exc

    def get_mark_price_and_funding(self, symbol: str) -> tuple[float | None, float | None]:
        """获取标记价格和最近一期 Funding Rate。"""
        data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return _to_float(data.get("markPrice")), _to_float(data.get("lastFundingRate"))

    def get_all_mark_prices_and_funding(self) -> dict[str, dict[str, Any]]:
        """一次性获取全部交易对的标记价格和 Funding，减少 API 请求数。"""
        rows = self._get("/fapi/v1/premiumIndex")
        return {row.get("symbol"): row for row in rows if row.get("symbol")}

    def get_trading_symbols(self) -> list[dict[str, str]]:
        """自动发现 Binance U 本位永续里正在交易的 USDT 永续合约。"""
        data = self._get("/fapi/v1/exchangeInfo")
        symbols: list[dict[str, str]] = []
        for item in data.get("symbols", []):
            if item.get("status") != "TRADING":
                continue
            if item.get("quoteAsset") != BINANCE_QUOTE_ASSET:
                continue
            if item.get("contractType") != BINANCE_CONTRACT_TYPE:
                continue

            symbol = item["symbol"]
            coin = item.get("baseAsset") or _coin_from_symbol(symbol)
            symbols.append({"coin": coin, "symbol": symbol})

        symbols.sort(key=lambda row: row["symbol"])
        if MAX_BINANCE_SYMBOLS > 0:
            return symbols[:MAX_BINANCE_SYMBOLS]
        return symbols

    def get_open_interest(self, symbol: str) -> float | None:
        """获取当前未平仓量，单位通常是合约基础币数量。"""
        data = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        return _to_float(data.get("openInterest"))

    def get_open_interest_history(self, symbol: str, period: str, limit: int) -> list[dict[str, Any]]:
        """获取 OI 历史，用于计算 1 小时和 24 小时变化。"""
        return self._get(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit},
        )

    def get_recent_force_orders(self, symbol: str) -> list[dict[str, Any]]:
        """获取近期强平订单。若交易所限制该接口，调用方会降级为 0。"""
        return self._get("/fapi/v1/allForceOrders", {"symbol": symbol, "limit": 100})

    def build_snapshot(
        self,
        coin: str,
        symbol: str | None = None,
        premium_data: dict[str, Any] | None = None,
        fetch_liquidations: bool = ENABLE_BINANCE_FORCE_ORDERS,
    ) -> MarketSnapshot:
        """聚合 Binance 多个接口，形成统一快照。"""
        symbol = symbol or BINANCE_SYMBOLS[coin]
        warnings: list[str] = []

        if premium_data:
            price = _to_float(premium_data.get("markPrice"))
            funding_rate = _to_float(premium_data.get("lastFundingRate"))
        else:
            price, funding_rate = self.get_mark_price_and_funding(symbol)
        open_interest = self.get_open_interest(symbol)

        oi_change_1h_pct, oi_change_24h_pct, oi_value_usd = None, None, None
        try:
            hourly = self.get_open_interest_history(symbol, "1h", 25)
            oi_change_1h_pct = _pct_change_from_history(hourly, 1)
            oi_change_24h_pct = _pct_change_from_history(hourly, 24)
            oi_value_usd = _latest_oi_value(hourly)
        except RuntimeError as exc:
            warnings.append(f"OI 历史暂不可用: {exc}")

        long_liq, short_liq = 0.0, 0.0
        if fetch_liquidations:
            try:
                orders = self.get_recent_force_orders(symbol)
                long_liq, short_liq = _sum_liquidations_last_hour(orders)
            except RuntimeError as exc:
                warnings.append(f"清算 REST 数据暂不可用，已按 0 处理: {exc}")

        # 如果 OI 历史没有返回美元价值，则用当前 OI * 价格粗略估算。
        if oi_value_usd is None and open_interest is not None and price is not None:
            oi_value_usd = open_interest * price

        return MarketSnapshot(
            coin=coin,
            symbol=symbol,
            price=price,
            funding_rate=funding_rate,
            open_interest=open_interest,
            open_interest_value_usd=oi_value_usd,
            oi_change_1h_pct=oi_change_1h_pct,
            oi_change_24h_pct=oi_change_24h_pct,
            long_liquidation_usd=long_liq,
            short_liquidation_usd=short_liq,
            source="binance",
            warnings=warnings,
        )


def _to_float(value: Any) -> float | None:
    """安全转换浮点数，缺失或异常时返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coin_from_symbol(symbol: str) -> str:
    """从交易对名称里提取币种名称。"""
    if symbol.endswith(BINANCE_QUOTE_ASSET):
        return symbol[: -len(BINANCE_QUOTE_ASSET)]
    return symbol


def _pct_change_from_history(rows: list[dict[str, Any]], periods_back: int) -> float | None:
    """按历史序列计算百分比变化。"""
    if len(rows) <= periods_back:
        return None
    latest = _to_float(rows[-1].get("sumOpenInterest"))
    previous = _to_float(rows[-1 - periods_back].get("sumOpenInterest"))
    if latest is None or previous in (None, 0):
        return None
    return (latest - previous) / previous * 100


def _latest_oi_value(rows: list[dict[str, Any]]) -> float | None:
    """读取最新一条 OI 美元价值。"""
    if not rows:
        return None
    return _to_float(rows[-1].get("sumOpenInterestValue"))


def _sum_liquidations_last_hour(orders: list[dict[str, Any]]) -> tuple[float, float]:
    """统计最近 1 小时多头/空头清算金额。

    Binance 强平订单里 SELL 通常代表多头被强平，BUY 通常代表空头被强平。
    """
    now_ms = int(time.time() * 1000)
    one_hour_ago = now_ms - 60 * 60 * 1000
    long_liq = 0.0
    short_liq = 0.0

    for order in orders:
        order_time = int(order.get("time", 0))
        if order_time < one_hour_ago:
            continue
        price = _to_float(order.get("avgPrice")) or _to_float(order.get("price")) or 0.0
        qty = _to_float(order.get("executedQty")) or _to_float(order.get("origQty")) or 0.0
        notional = price * qty
        if order.get("side") == "SELL":
            long_liq += notional
        elif order.get("side") == "BUY":
            short_liq += notional

    return long_liq, short_liq
