# Hourly updater

Vercel 上当前部署的是静态网页，它不会自己每小时运行 `python main.py`。

现在项目里新增了一个完整更新流水线：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
python run_once.py
```

`run_once.py` 会依次执行：

- 抓取最新市场数据
- 写入 CSV 和 SQLite 历史记录
- 生成报告、推文草稿和 X dry-run 预览
- 导出 `web/data.js`
- 同步静态文件到 `D:\Codex\加密货币监控\vercel-site`

## 安装 Windows 每小时任务

请用“管理员身份”打开 PowerShell，然后运行：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
.\install_windows_task.ps1
```

测试任务：

```powershell
schtasks /Run /TN CryptoSqueezeRadarHourly
```

查看日志：

```text
D:\Codex\加密货币监控\crypto-squeeze-radar\logs\hourly_runner.log
```

## 让线上 Vercel 页面也每小时更新

默认情况下，每小时任务只更新本地数据和 `vercel-site` 目录，不会自动发布线上。

如果你希望每小时抓完数据后自动部署到 Vercel，需要设置用户级环境变量：

```powershell
[Environment]::SetEnvironmentVariable("AUTO_DEPLOY_VERCEL", "true", "User")
[Environment]::SetEnvironmentVariable("VERCEL_TOKEN", "你的 Vercel Token", "User")
```

设置后请重新打开一个 PowerShell 窗口，再安装或测试计划任务。

## 临时手动更新线上页面

如果只想手动更新一次线上页面：

```powershell
cd D:\Codex\加密货币监控\crypto-squeeze-radar
python run_once.py
.\deploy_vercel.ps1
```
