# Hourly updater

Vercel 上当前部署的是静态网页，它不会自己每小时运行 `python main.py`。

现在项目里新增了一个完整更新流水线：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
python run_once.py
```

`run_once.py` 会依次执行：

- 用 Binance 批量行情做轻量全市场筛选
- 对精选池做深度 OI 扫描
- 写入 CSV 和 SQLite 历史记录
- 检测 OI 模式信号并推送到微信（已配置 `PUSHPLUS_TOKEN` 时默认开启）
- 生成报告、推文草稿和 X dry-run 预览
- 导出 `web/data.js`
- 同步静态文件到 `D:\Codex\加密货币监控\vercel-site`
- 当已配置 `VERCEL_TOKEN` 时，把本轮快照自动部署到 Vercel

## 安装 Windows 每小时任务

请用“管理员身份”打开 PowerShell，然后运行：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
.\install_windows_task.ps1
```

安装脚本会创建两个任务：

- `CryptoSqueezeRadarHourly`：每小时整点启动，等到第 5 秒开始信号扫描、推送、交易和部署
- `CryptoSqueezeRadarFullScan4H`：每 4 小时在第 10 分钟运行全市场深度 OI 补数，只写数据库，不推送、不交易、不部署

测试任务：

```powershell
schtasks /Run /TN CryptoSqueezeRadarHourly
schtasks /Run /TN CryptoSqueezeRadarFullScan4H
```

查看日志：

```text
D:\Codex\加密货币监控\crypto-squeeze-radar\logs\hourly_runner.log
D:\Codex\加密货币监控\crypto-squeeze-radar\logs\full_scan_runner.log
```

## 数据分流

SQLite `market_snapshots` 表会写入：

- `scan_mode=signal_scan`：每小时精选池深扫，用于当前信号、推送、交易和仪表盘
- `scan_mode=full_scan`：每 4 小时全市场深扫，用于补充长期样本和后续回测
- `universe_reason`：说明某个交易对为什么进入本轮信号精选池，例如成交额榜、涨幅榜、跌幅榜、24h 高低位或最近信号延续

## 让线上 Vercel 页面也每小时更新

默认情况下，只要计划任务环境里能读到 `VERCEL_TOKEN`，每小时任务会在抓数、推送微信、同步 `vercel-site` 之后，自动把本轮静态快照发布到 Vercel。

建议设置用户级环境变量：

```powershell
[Environment]::SetEnvironmentVariable("AUTO_DEPLOY_VERCEL", "true", "User")
[Environment]::SetEnvironmentVariable("VERCEL_TOKEN", "你的 Vercel Token", "User")
```

设置后请重新打开一个 PowerShell 窗口，再安装或测试计划任务。

如果临时不想每小时部署线上页面，可以设置：

```powershell
[Environment]::SetEnvironmentVariable("AUTO_DEPLOY_VERCEL", "false", "User")
```

## 临时手动更新线上页面

如果只想手动更新一次线上页面：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
python run_once.py
.\deploy_vercel.ps1
```
