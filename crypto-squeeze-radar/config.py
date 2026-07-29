"""项目配置：集中管理监控币种、阈值、路径和数据源参数。"""

import os
from pathlib import Path


# 项目根目录，后续所有输出文件都基于这个目录定位。
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Load local .env values without overriding process environment variables."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _repair_mojibake_text(value: str) -> str:
    """Repair UTF-8 text accidentally decoded as ANSI by Windows PowerShell."""
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except UnicodeError:
        return value
    if any("\u4e00" <= char <= "\u9fff" for char in repaired):
        return repaired
    return value


DEFAULT_TRADING_ALLOWED_GRADES = ["主交易", "可交易"]


def _parse_trading_allowed_grades(value: str) -> list[str]:
    grades = [
        _repair_mojibake_text(grade.strip())
        for grade in value.split(",")
        if grade.strip()
    ]
    if (
        value
        and not any(grade in DEFAULT_TRADING_ALLOWED_GRADES for grade in grades)
        and any(marker in value for marker in ("ä", "å", "æ", "�"))
    ):
        return DEFAULT_TRADING_ALLOWED_GRADES.copy()
    return grades

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

# 扫描模式：
# - signal_scan：每小时先用批量行情轻量筛全市场，只深扫精选池，用于出信号/推送/交易。
# - full_scan：每 4 小时全市场深度 OI 扫描，只补数据库，不触发交易。
RADAR_SCAN_MODE = os.getenv("RADAR_SCAN_MODE", "signal_scan").lower()
SIGNAL_SCAN_QUOTE_VOLUME_TOP_N = int(os.getenv("SIGNAL_SCAN_QUOTE_VOLUME_TOP_N", "90"))
SIGNAL_SCAN_GAINERS_TOP_N = int(os.getenv("SIGNAL_SCAN_GAINERS_TOP_N", "70"))
SIGNAL_SCAN_LOSERS_TOP_N = int(os.getenv("SIGNAL_SCAN_LOSERS_TOP_N", "50"))
SIGNAL_SCAN_HIGH_POSITION_TOP_N = int(os.getenv("SIGNAL_SCAN_HIGH_POSITION_TOP_N", "70"))
SIGNAL_SCAN_LOW_POSITION_TOP_N = int(os.getenv("SIGNAL_SCAN_LOW_POSITION_TOP_N", "50"))
SIGNAL_SCAN_MAX_SYMBOLS = int(os.getenv("SIGNAL_SCAN_MAX_SYMBOLS", "240"))
SIGNAL_SCAN_PREVIOUS_SIGNAL_HOURS = int(os.getenv("SIGNAL_SCAN_PREVIOUS_SIGNAL_HOURS", "24"))

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
PATTERN_SQLITE_DB_FILE = STORAGE_DIR / "pattern_monitor.sqlite3"
PATTERN_SIGNALS_JSON_FILE = OUTPUT_DIR / "pattern_signals.json"

# Trading execution layer. The code can place live Binance USD-M Futures orders,
# but live trading requires both TRADING_ENABLED=true and TRADING_DRY_RUN=false.
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
TRADING_DRY_RUN = os.getenv("TRADING_DRY_RUN", "true").lower() == "true"
TRADING_AUTO_EXECUTE = os.getenv("TRADING_AUTO_EXECUTE", "true").lower() == "true"
TRADING_SIGNALS_FILE = Path(os.getenv("TRADING_SIGNALS_FILE", str(PATTERN_SIGNALS_JSON_FILE)))
TRADING_DB_FILE = Path(os.getenv("TRADING_DB_FILE", str(STORAGE_DIR / "trading.sqlite3")))
TRADING_EXCHANGE = os.getenv("TRADING_EXCHANGE", "binance").lower()
TRADING_MARKET = os.getenv("TRADING_MARKET", "usdm_futures").lower()
TRADING_SIDE = os.getenv("TRADING_SIDE", "SHORT").upper()
TRADING_MAX_OPEN_POSITIONS = int(os.getenv("TRADING_MAX_OPEN_POSITIONS", "4"))
TRADING_RISK_PCT = float(os.getenv("TRADING_RISK_PCT", os.getenv("TRADING_MARGIN_PCT", "5")))
TRADING_LEVERAGE_MODE = os.getenv("TRADING_LEVERAGE_MODE", "fixed").lower()
TRADING_LEVERAGE = int(os.getenv("TRADING_LEVERAGE", "10"))
TRADING_ALLOWED_SYMBOLS = [
    symbol.strip().upper()
    for symbol in os.getenv("TRADING_ALLOWED_SYMBOLS", "").split(",")
    if symbol.strip()
]
TRADING_ALLOWED_GRADES = _parse_trading_allowed_grades(
    os.getenv("TRADING_ALLOWED_GRADES", ",".join(DEFAULT_TRADING_ALLOWED_GRADES))
)
TRADING_REQUIRE_STAR = os.getenv("TRADING_REQUIRE_STAR", "true").lower() == "true"
TRADING_PLACE_EXITS = os.getenv("TRADING_PLACE_EXITS", "true").lower() == "true"
TRADING_POSITION_MODE = os.getenv("TRADING_POSITION_MODE", "one_way").lower()
TRADING_RECV_WINDOW_MS = int(os.getenv("TRADING_RECV_WINDOW_MS", "5000"))
TRADING_ORDER_PREFIX = os.getenv("TRADING_ORDER_PREFIX", "csr")
TRADING_MIN_NOTIONAL_USDT = float(os.getenv("TRADING_MIN_NOTIONAL_USDT", "5"))

# OI 模式微信推送配置。已配置 PushPlus/ServerChan token 时默认开启。
PATTERN_PUSH_CHANNEL = os.getenv("PATTERN_PUSH_CHANNEL", "pushplus").lower()
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")
PATTERN_PUSH_DEFAULT = "true" if PUSHPLUS_TOKEN or SERVERCHAN_SENDKEY else "false"
PATTERN_PUSH_ENABLED = os.getenv("PATTERN_PUSH_ENABLED", PATTERN_PUSH_DEFAULT).lower() == "true"

# HTTP 超时时间，避免某个公开 API 卡住整个任务。
HTTP_TIMEOUT_SECONDS = 12

# Top N 异常币种数量。
TOP_N = 10

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
