# Trading Layer

这个目录把现有监控信号转换为 Binance.com U 本位合约执行计划。策略代码不需要改动；交易层默认读取：

```text
output/pattern_signals.json
```

## 默认安全状态

代码支持真实下单，但默认不会真实交易：

```powershell
$env:TRADING_ENABLED="false"
$env:TRADING_DRY_RUN="true"
```

只有同时满足下面两个条件，才会向 Binance 发送真实订单：

```powershell
$env:TRADING_ENABLED="true"
$env:TRADING_DRY_RUN="false"
```

API Key 必须放在环境变量中，不要写进代码：

```powershell
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
```

## 当前执行规则

- 交易所：Binance.com
- 市场：U 本位合约
- 方向：只做空
- 品种：默认允许所有命中的 USDT 永续合约
- 最大同时持仓：4
- 单笔最大止损亏损：账户权益的 5%
- 仓位修正：继续使用信号里的 `position_multiplier`
- 信号门槛：默认要求 `is_star=true`，且 `trade_grade` 属于 `主交易` 或 `可交易`
- 直接交易只来自高质量确认层：高位≥90%、24h涨幅≥20%、成交额变化≥50%；或高位≥70%、24h涨幅≥20%并叠加空头拥挤/负 Funding。
- 仓位系数：`主交易=1.0x`、`可交易=0.8x`、`小仓确认=0.25x`、`观察=0x`。
- A 加强版：4% 止损，3%/7% 止盈，最长 4 小时。
- B/C 空头拥挤或负 Funding 确认层：8% 止损，8%/13% 止盈，最长 12 小时。
- B/C 成交额确认层（成交额变化≥50%）：10% 止损，8%/13% 止盈，最长 12 小时。
- 杠杆：默认固定 10x；也可以设为该品种最大杠杆
- 下单数量：`账户权益 * 5% * position_multiplier / abs(止损价 - 入场价)`
- 杠杆只用于设置 Binance 初始杠杆和估算占用保证金，不决定仓位大小

## 单独运行交易层

先让监控生成最新信号，再运行：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
python run_trading_once.py
```

dry-run 时会写入：

```text
storage/trading.sqlite3
```

## 跟随每小时任务自动运行

每小时任务现在会在生成信号后自动调用交易层。默认仍然是 dry-run，不会真实下单：

```powershell
$env:TRADING_AUTO_EXECUTE="true"
$env:TRADING_ENABLED="false"
$env:TRADING_DRY_RUN="true"
```

真实小额交易示例：

```powershell
$env:TRADING_AUTO_EXECUTE="true"
$env:TRADING_ENABLED="true"
$env:TRADING_DRY_RUN="false"
$env:TRADING_MAX_OPEN_POSITIONS="4"
$env:TRADING_RISK_PCT="5"
$env:TRADING_LEVERAGE_MODE="fixed"
$env:TRADING_LEVERAGE="10"
python run_once.py
```

如果要使用该品种最大杠杆：

```powershell
$env:TRADING_LEVERAGE_MODE="max"
```

## 常用保护开关

```powershell
$env:TRADING_REQUIRE_STAR="true"
$env:TRADING_ALLOWED_GRADES="主交易,可交易"
$env:TRADING_ALLOWED_SYMBOLS=""
$env:TRADING_PLACE_EXITS="true"
$env:TRADING_RISK_PCT="5"
```

紧急停机：

```powershell
$env:TRADING_ENABLED="false"
```

## 订单行为

真实模式下，交易层会：

1. 读取账户权益。
2. 读取当前空头持仓数，超过上限则跳过。
3. 按 `账户权益 * TRADING_RISK_PCT * position_multiplier` 计算本单最大止损亏损。
4. 用 `stop_loss_price - entry_price` 反推出下单数量。
5. 设置该 symbol 的初始杠杆。
6. 市价开空。
7. 挂 reduce-only 的止损和两档止盈条件单。

每次交易扫描会先检查本地台账中的到期仓位：

1. 以真实开仓成交时间计算最大持仓截止时间。
2. 到期后先撤销剩余止损/止盈条件单。
3. 按交易所当前空头持仓数量提交 reduce-only 市价平仓。
4. 将台账更新为 `CLOSED_TIME_EXIT`；交易所已无对应持仓时记录为 `CLOSED_EXCHANGE`。
5. 如果保护单和紧急平仓都失败，记录为 `OPEN_UNPROTECTED`，后续扫描仍会继续管理和尝试到期平仓。

如果 Binance 返回超时、429、5xx 或状态未知，不要手工重复运行连续追单；先查 Binance 订单和 `storage/trading.sqlite3` 台账。

## API Key 放在哪里

推荐放在 Windows 用户环境变量里，计划任务也能读取：

```powershell
[Environment]::SetEnvironmentVariable("BINANCE_API_KEY", "你的 API Key", "User")
[Environment]::SetEnvironmentVariable("BINANCE_API_SECRET", "你的 API Secret", "User")
```

设置后重新打开 PowerShell，或重启计划任务所在会话。

也可以在项目目录创建 `.env`：

```text
D:\Codex\加密货币监控\crypto-squeeze-radar\.env
```

参考 `.env.example` 填写。`.env` 已加入 `.gitignore`，不要提交真实密钥。
