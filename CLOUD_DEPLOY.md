# 免费云端自动更新

这个项目可以使用 **GitHub Actions + GitHub Pages** 免费运行，不需要家里的电脑保持开机。

## 工作方式

- GitHub Actions 每小时第 7 分钟自动抓取一次市场数据。
- 云端节点监控 BTC、ETH、SOL、HYPE，并在 Binance 受地区限制时自动使用 Hyperliquid 公共数据；本机版仍可继续使用 Binance 全市场模式。
- 历史 CSV 和 SQLite 数据保存在 GitHub Actions 缓存中，不会公开提交到仓库。
- 每次更新完成后，GitHub Pages 自动发布最新仪表盘。
- `POST_TO_X` 固定为 `false`，云端任务不会自动发布推文。
- 也可以在 GitHub 仓库的 **Actions → Cloud Crypto Radar → Run workflow** 手动刷新。

## 免费条件

- 仓库设为公开时，GitHub Actions 和 GitHub Pages 可使用免费额度。
- 公开仓库意味着项目源代码可以被其他人查看；市场监控数据本身来自公开 API。
- GitHub 的定时任务可能因平台拥堵延迟数分钟，并不保证在第 7 分钟整准时执行。
- GitHub Actions 缓存会被平台定期清理；若缓存被清理，仪表盘会从新的历史记录重新开始，但实时数据仍会继续更新。
- 工作流每月会自动提交一次 `.cloud/last-keepalive.txt`，避免公开仓库因长期无活动而被 GitHub 暂停定时任务。

## 首次启用

1. 把此项目推送到一个公开 GitHub 仓库。
2. 在仓库 **Settings → Pages → Build and deployment** 中，把 Source 设为 **GitHub Actions**。
3. 打开 **Actions → Cloud Crypto Radar → Run workflow**，执行第一次更新。
4. 完成后，页面地址通常为：

   `https://你的GitHub用户名.github.io/仓库名/`
