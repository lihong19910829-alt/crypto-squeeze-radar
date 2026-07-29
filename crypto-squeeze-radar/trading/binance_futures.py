"""Small Binance USD-M Futures trading client.

Only the execution layer imports this module. Market monitoring keeps using the
public-data client in data_sources.exchange.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import HTTP_TIMEOUT_SECONDS, TRADING_RECV_WINDOW_MS


class BinanceFuturesTradingClient:
    """Signed REST client for Binance USD-M Futures trade endpoints."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("缺少 BINANCE_API_KEY 或 BINANCE_API_SECRET")
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self._time_offset_ms = 0
        self._time_offset_ready = False

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def position_risk(self) -> list[dict[str, Any]]:
        return self._request("GET", "/fapi/v2/positionRisk", signed=True)

    def position_mode(self) -> str:
        """Read the account's actual futures position mode."""
        result = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return "hedge" if bool(result.get("dualSidePosition")) else "one_way"

    def exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo", signed=False)

    def leverage_bracket(self, symbol: str) -> Any:
        return self._request(
            "GET",
            "/fapi/v1/leverageBracket",
            {"symbol": symbol},
            signed=True,
        )

    def change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    def new_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def algo_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a USD-M conditional order (stop loss or take profit)."""
        return self._request("POST", "/fapi/v1/algoOrder", params, signed=True)

    def cancel_algo_order(self, symbol: str, algo_id: int) -> dict[str, Any]:
        """Cancel one USD-M conditional order."""
        return self._request(
            "DELETE",
            "/fapi/v1/algoOrder",
            {"symbol": symbol, "algoId": algo_id},
            signed=True,
        )

    def test_order(self, params: dict[str, Any]) -> dict[str, Any]:
        """Validate an order without sending it to the matching engine."""
        return self._request("POST", "/fapi/v1/order/test", params, signed=True)

    def ticker_price(self, symbol: str) -> Decimal:
        result = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        return Decimal(str(result["price"]))

    def query_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def _sync_server_time(self) -> None:
        """Calibrate signed-request timestamps against Binance server time."""
        before = int(time.time() * 1000)
        result = self._request("GET", "/fapi/v1/time", signed=False)
        after = int(time.time() * 1000)
        server_time = int(result["serverTime"])
        midpoint = (before + after) // 2
        self._time_offset_ms = server_time - midpoint
        self._time_offset_ready = True

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        base_params = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: Exception | None = None
        for attempt in range(1, 4):
            request_params = dict(base_params)
            if signed:
                if not self._time_offset_ready:
                    try:
                        self._sync_server_time()
                    except Exception:
                        # If the public time endpoint is temporarily
                        # unavailable, make one local-time attempt; a later
                        # -1021 response will force another calibration.
                        self._time_offset_ready = True
                        self._time_offset_ms = 0
                request_params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
                request_params["recvWindow"] = TRADING_RECV_WINDOW_MS
                query = urlencode(request_params)
                request_params["signature"] = hmac.new(
                    self.api_secret,
                    query.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()

            headers = {"User-Agent": "crypto-squeeze-radar-trading/0.1"}
            if self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key
            body = None
            query_string = urlencode(request_params)
            url = f"{self.BASE_URL}{path}"
            if method in {"GET", "DELETE"} and query_string:
                url = f"{url}?{query_string}"
            elif query_string:
                body = query_string.encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                payload = exc.read().decode("utf-8", errors="ignore")
                if signed and exc.code == 400 and '"-1021"' in payload and attempt < 3:
                    self._time_offset_ready = False
                    last_error = RuntimeError(f"Binance HTTP {exc.code}: {payload}")
                    continue
                if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < 3:
                    last_error = RuntimeError(f"Binance HTTP {exc.code}: {payload}")
                    time.sleep(1.5 * attempt)
                    continue
                raise RuntimeError(f"Binance HTTP {exc.code}: {payload}") from exc
            except (URLError, TimeoutError, ConnectionResetError, OSError) as exc:
                last_error = exc
                if attempt == 3:
                    raise RuntimeError(f"Binance 网络错误: {getattr(exc, 'reason', exc)}") from exc
                time.sleep(1.5 * attempt)

        raise RuntimeError(f"Binance 请求失败: {last_error}")


def build_symbol_rules(exchange_info: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    """Extract quantity and notional filters by symbol."""
    rules: dict[str, dict[str, Decimal]] = {}
    for item in exchange_info.get("symbols", []):
        symbol = item.get("symbol")
        if not symbol:
            continue
        filters = {row.get("filterType"): row for row in item.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        notional = filters.get("MIN_NOTIONAL") or {}
        rules[symbol] = {
            "step_size": Decimal(str(lot.get("stepSize", "0.001"))),
            "min_qty": Decimal(str(lot.get("minQty", "0"))),
            "min_notional": Decimal(str(notional.get("notional", "0"))),
            "tick_size": Decimal(str((filters.get("PRICE_FILTER") or {}).get("tickSize", "0"))),
        }
    return rules


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
