# Crypto Squeeze Radar MVP

这是一个加密货币轧空/轧多雷达 MVP。它不做喊单、不做投资建议，只监控价格、Funding、Open Interest、清算等市场结构数据，并生成可人工审核的中文推文草稿。

## 第一版能力

- 监控币种：BTC、ETH、SOL、HYPE
- 拉取当前价格、Funding Rate、当前 OI、1 小时 OI 变化、24 小时 OI 变化
- 尝试统计最近 1 小时多头/空头清算金额
- 自动生成异常标签：多头拥挤、空头拥挤、OI异常增加、清算异常、杠杆过热、正常
- 生成 0-100 风险评分：正常、关注、危险、极端
- 输出 Top 5 异常币种
- 生成 `output/tweets.json` 和 `output/tweets.md`，先人工审核，不自动发推
- 读取本轮 Top 5 推文草稿，默认 dry-run 生成 X 发布预览
- 仅当 `risk_score >= 70` 且 `POST_TO_X=true` 时，才尝试发布到 X
- 保存历史快照到 `storage/history.csv`
- 每小时保存历史快照到 SQLite：`storage/radar_history.sqlite3`

## 数据源

第一版优先使用 Binance USD-M Futures 公开 API：

- `GET /fapi/v1/premiumIndex`：标记价格和最近 Funding Rate
- `GET /fapi/v1/openInterest`：当前 OI
- `GET /futures/data/openInterestHist`：OI 历史，用于计算 1 小时和 24 小时变化
- `GET /fapi/v1/allForceOrders`：近期强平订单，若接口不可用会自动降级为 0

备用数据源：

- Hyperliquid 公开 Info API：当 Binance 某个币种不可用时，尝试获取价格、Funding、OI
- Coinglass：已预留接口，后续可接入更完整的全市场 OI、清算、多空数据

## 需要哪些 API Key

MVP 当前不强制需要 API Key。

后续建议补充：

- Coinglass API Key：用于更稳定的全市场清算、交易所聚合 OI、多空比
- X/Twitter API Key：用于自动发推，目前第一版不会自动发布
- Telegram Bot Token：用于把异常提醒推送到群或频道
- 数据库连接信息：用于替换本地 CSV，例如 PostgreSQL、SQLite、Supabase

## 哪些数据可以免费获取

- Binance 公开合约行情：价格、Funding、当前 OI、OI 历史一般可免费获取
- Hyperliquid 公开市场数据：价格、Funding、OI 可免费获取
- 本地生成的评分、标签、报告和推文草稿不依赖付费服务

注意：公开 API 可能有频率限制、地区限制或临时不可用，代码里已经做了降级和 warning 输出。

## 哪些地方需要后续补充

- 用 Coinglass 替换或增强清算数据，获得更完整的全市场清算统计
- 增加价格变化、成交量、多空比、资金费率连续性等信号
- 把阈值从固定配置改为按币种动态阈值
- 接入 X API，但继续保留人工审核开关
- 接入 Telegram Bot，把极端风险推送到私有频道
- 把 `storage/history.csv` 替换为数据库，支持回测和可视化

## 如何运行

要求：Python 3.10 或更高版本。

```bash
cd crypto-squeeze-radar
python main.py
```

运行后会生成：

- `output/report.md`
- `output/tweets.json`
- `output/tweets.md`
- `output/x_post_preview.json`
- `output/x_post_preview.md`
- `storage/history.csv`
- `storage/radar_history.sqlite3`

## 本地可视化仪表盘

先运行一次监控任务生成数据：

```bash
python main.py
```

推荐先导出静态仪表盘数据：

```bash
python export_dashboard_data.py
```

然后直接打开：

```text
web/index.html
```

也可以启动本地仪表盘服务：

```bash
python web_server.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

仪表盘会读取 SQLite 历史数据、推文草稿和 X dry-run 预览，展示风险榜、风险评分趋势、杠杆热度和推文审核队列。

## X/Twitter API

默认不会真实发布，只会生成预览文件：

- `output/x_post_preview.json`
- `output/x_post_preview.md`

发布规则：

- 只读取本轮生成的 Top 5 推文草稿
- 只处理 `risk_score >= 70` 的信号
- 文案会过滤禁止词：买入、卖出、稳赚、暴涨、必涨
- 文案强调异常监控和风险提示
- 只有设置 `POST_TO_X=true` 时才会调用 X API

需要配置的环境变量：

```bash
POST_TO_X=true
X_USER_ACCESS_TOKEN=你的X OAuth2 User Access Token
```

可选配置：

```bash
X_MIN_RISK_SCORE=70
```

Windows PowerShell 示例：

```powershell
$env:POST_TO_X="true"
$env:X_USER_ACCESS_TOKEN="你的X OAuth2 User Access Token"
python main.py
```

如果没有设置 `POST_TO_X=true`，即使配置了 Token，也只会 dry-run 预览，不会真实发布。

## SQLite 历史记录

每次运行 `main.py` 时，程序会把每个币种的一条快照写入 `market_snapshots` 表。

核心字段包括：

- `timestamp_utc`：记录时间
- `symbol`：交易对
- `price`：价格
- `funding_rate`：资金费率
- `open_interest`：未平仓量
- `oi_change_1h`：1 小时 OI 变化
- `oi_change_24h`：24 小时 OI 变化
- `long_liquidation`：多头清算金额
- `short_liquidation`：空头清算金额
- `risk_score`：风险评分
- `anomaly_tag`：异常标签

## 回测异常信号

`backtest_anomaly.py` 用于统计某类异常出现后，未来 1 小时、4 小时、24 小时的价格涨跌表现。

示例：

```bash
python backtest_anomaly.py --tag 多头拥挤
python backtest_anomaly.py --tag OI异常增加 --symbol BTCUSDT
python backtest_anomaly.py --tag 杠杆过热 --min-score 60
```

如果不传 `--tag`，默认统计全部非“正常”的异常样本。

注意：刚开始数据量少时，未来 4 小时、24 小时样本可能为空。持续按小时运行后，统计结果才会逐渐有意义。

## 如何每小时自动运行

### Windows 任务计划程序

1. 打开“任务计划程序”
2. 创建基本任务
3. 触发器选择“每天”，高级设置里勾选“重复任务间隔：1 小时”
4. 操作选择“启动程序”
5. 程序填写 Python 路径，例如：`python`
6. 参数填写：`main.py`
7. 起始于填写项目目录，例如：`D:\Codex\加密货币监控\crypto-squeeze-radar`

### Linux / macOS cron

```bash
0 * * * * cd /path/to/crypto-squeeze-radar && python3 main.py
```

## 免责声明

本项目只用于市场结构监控和内容草稿生成，不构成投资建议。所有推文草稿都应人工审核后再发布。

## 监控全部 Binance 交易对

项目现在默认会自动读取 Binance USD-M Futures 的 `exchangeInfo`，筛选：

- `status = TRADING`
- `quoteAsset = USDT`
- `contractType = PERPETUAL`

也就是适合做 Funding / OI 监控的 U 本位 USDT 永续合约。现货交易对没有 Funding / OI，不纳入这个轧空/轧多雷达。

可选环境变量：

```bash
MONITOR_ALL_BINANCE_SYMBOLS=true
MAX_BINANCE_SYMBOLS=0
ENABLE_BINANCE_FORCE_ORDERS=false
```

说明：

- `MONITOR_ALL_BINANCE_SYMBOLS=true`：自动监控 Binance 全部 USDT 永续合约，当前为默认值
- `MAX_BINANCE_SYMBOLS=0`：不限制数量；调试时可以设为 `20`
- 每小时任务脚本默认同样使用 `MAX_BINANCE_SYMBOLS=0`，不会再只抓取前 120 个交易对
- `ENABLE_BINANCE_FORCE_ORDERS=false`：默认不请求 Binance 强平 REST 接口，因为该接口经常维护或不可用；后续建议用 Coinglass 增强清算数据

如果只想监控手工列表，可以设置：

```bash
MONITOR_ALL_BINANCE_SYMBOLS=false
```

然后修改 `config.py` 里的 `WATCHLIST` 和 `BINANCE_SYMBOLS`。

监控全部交易对时，一轮运行会比 4 个核心币种慢一些，因为每个交易对都需要读取 OI 和 OI 历史。建议每小时运行一次即可。
