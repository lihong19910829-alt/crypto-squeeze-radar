"""Coinglass 数据接口占位：后续用于更完整的清算、全市场 OI 和多空数据。"""

from __future__ import annotations


class CoinglassClient:
    """Coinglass 通常需要 API Key，MVP 先保留可替换接口。"""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    def is_configured(self) -> bool:
        """判断是否已配置 API Key。"""
        return bool(self.api_key)

    def get_liquidations(self, coin: str) -> dict[str, float]:
        """后续接入 Coinglass 后，在这里返回多头/空头清算额。"""
        raise NotImplementedError(f"{coin} 的 Coinglass 清算接口尚未接入")

