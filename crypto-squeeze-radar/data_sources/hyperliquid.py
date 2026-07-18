"""Hyperliquid 公开数据接口：作为 Binance 缺失币种的备用数据源。"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import HTTP_TIMEOUT_SECONDS, HYPERLIQUID_COINS
from data_sources.exchange import MarketSnapshot


class HyperliquidClient:
    """Hyperliquid 公开 Info API 客户端。"""

    BASE_URL = "https://api.hyperliquid.xyz/info"

    def _post(self, payload: dict[str, Any]) -> Any:
        """发送 POST 请求，并返回 JSON 数据。"""
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.BASE_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "crypto-squeeze-radar/0.1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Hyperliquid HTTP {exc.code}: {body_text}") from exc
        except URLError as exc:
            raise RuntimeError(f"Hyperliquid 网络错误: {exc.reason}") from exc

    def build_snapshot(self, coin: str) -> MarketSnapshot:
        """从 Hyperliquid metaAndAssetCtxs 中提取价格、Funding 和 OI。"""
        hl_coin = HYPERLIQUID_COINS[coin]
        meta, contexts = self._post({"type": "metaAndAssetCtxs"})

        universe = meta.get("universe", [])
        index = next((i for i, item in enumerate(universe) if item.get("name") == hl_coin), None)
        if index is None:
            raise RuntimeError(f"Hyperliquid 未找到币种: {hl_coin}")

        ctx = contexts[index]
        price = _to_float(ctx.get("midPx")) or _to_float(ctx.get("markPx"))
        funding_rate = _to_float(ctx.get("funding"))
        open_interest = _to_float(ctx.get("openInterest"))
        oi_value_usd = open_interest * price if open_interest is not None and price is not None else None

        return MarketSnapshot(
            coin=coin,
            symbol=f"{hl_coin}-PERP",
            price=price,
            funding_rate=funding_rate,
            open_interest=open_interest,
            open_interest_value_usd=oi_value_usd,
            oi_change_1h_pct=None,
            oi_change_24h_pct=None,
            price_change_24h_pct=None,
            price_position_24h_pct=None,
            high_24h=None,
            low_24h=None,
            quote_volume_24h=None,
            long_liquidation_usd=0.0,
            short_liquidation_usd=0.0,
            source="hyperliquid",
            warnings=["Hyperliquid MVP 备用源暂未计算 OI 变化和清算拆分"],
        )


def _to_float(value: Any) -> float | None:
    """安全转换浮点数。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
