"""项目配置：集中管理监控币种、阈值、路径和数据源参数。"""

import os
from pathlib import Path


# 项目根目录，后续所有输出文件都基于这个目录定位。
BASE_DIR = Path(__file__).resolve().parent

# 第一版核心监控币种。后续扩展时只需要追加币种和交易对映射。
WATCHLIST = ["BTC", "ETH", "SOL", "HYPE"]

# 是否自动监控 Binance U 本位永续的全部 USDT 永续合约。
# 设为 true 后，WATCHLIST / BINANCE_SYMBOLS 只作为备用手工列表。
MONITOR_ALL_BINANCE_SYMBOLS = os.getenv("MONITOR_ALL_BINANCE_SYMBOLS", "true").lower() == "true"
BINANCE_QUOTE_ASSET = os.getenv("BINANCE_QUOTE_ASSET", "USDT")
BINANCE_CONTRACT_TYPE = os.getenv("BINANCE_CONTRACT_TYPE", "PERPETUAL")

# 可选：限制自动监控数量，0 代表不限制。调试时可以设成 20 加快运行。
MAX_BINANCE_SYMBOLS = int(os.getenv("MAX_BINANCE_SYMBOLS", "0"))

# 并发抓取交易对数量。监控 Binance 全部永续合约时必须并发，否则一轮会跑很久。
BINANCE_MAX_WORKERS = int(os.getenv("BINANCE_MAX_WORKERS", "12"))

# Binance 强平 REST 接口经常不可用；默认关闭，后续建议用 Coinglass 增强清算数据。
ENABLE_BINANCE_FORCE_ORDERS = os.getenv("ENABLE_BINANCE_FORCE_ORDERS", "false").lower() == "true"

# Binance U 本位永续合约交易对。HYPE 如遇交易所不支持，会自动走备用数据源。
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "HYPE": "HYPEUSDT",
}

# Hyperliquid 使用币种名称，不带 USDT。
HYPERLIQUID_COINS = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "HYPE": "HYPE",
}

# 输出路径。
OUTPUT_DIR = BASE_DIR / "output"
STORAGE_DIR = BASE_DIR / "storage"
HISTORY_FILE = STORAGE_DIR / "history.csv"
SQLITE_DB_FILE = STORAGE_DIR / "radar_history.sqlite3"
TWEETS_JSON_FILE = OUTPUT_DIR / "tweets.json"
TWEETS_MD_FILE = OUTPUT_DIR / "tweets.md"
REPORT_MD_FILE = OUTPUT_DIR / "report.md"
X_POST_PREVIEW_JSON_FILE = OUTPUT_DIR / "x_post_preview.json"
X_POST_PREVIEW_MD_FILE = OUTPUT_DIR / "x_post_preview.md"

# HTTP 超时时间，避免某个公开 API 卡住整个任务。
HTTP_TIMEOUT_SECONDS = 12

# Top N 异常币种数量。
TOP_N = 5

# X/Twitter 发布配置。默认只预览，不真实发布。
POST_TO_X = os.getenv("POST_TO_X", "false").lower() == "true"
X_USER_ACCESS_TOKEN = os.getenv("X_USER_ACCESS_TOKEN", "")
X_MIN_RISK_SCORE = int(os.getenv("X_MIN_RISK_SCORE", "70"))
X_CREATE_POST_URL = "https://api.x.com/2/tweets"
X_FORBIDDEN_WORDS = ["买入", "卖出", "稳赚", "暴涨", "必涨"]

# 风险评分阈值。数值越低越敏感，MVP 阶段可先保守观察再调参。
SCORING_THRESHOLDS = {
    "funding_hot_positive": 0.0003,  # 0.03%
    "funding_hot_negative": -0.0003,
    "oi_1h_attention": 5.0,
    "oi_1h_extreme": 12.0,
    "oi_24h_attention": 10.0,
    "oi_24h_extreme": 25.0,
    "liquidation_attention_usd": 1_000_000,
    "liquidation_extreme_usd": 10_000_000,
}
