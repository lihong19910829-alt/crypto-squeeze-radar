"""运行一次完整更新流水线：抓数据、导出仪表盘数据、同步静态部署目录。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import export_dashboard_data
import main as radar_main
from config import BASE_DIR, TRADING_AUTO_EXECUTE
from trading.executor import run_trading_cycle


WEB_FILES = [
    "index.html",
    "styles.css",
    "app.js",
    "data.js",
    "radar-scan.svg",
    "vercel.json",
]


def main() -> None:
    """执行一轮更新；给计划任务调用，避免只更新本地数据库、不更新网页数据。"""
    print(f"[{now()}] 开始 Crypto Squeeze Radar 更新")

    # 第一步：抓取市场数据，并写入 CSV、SQLite、报告、推文草稿。
    _, pattern_payload = radar_main.main(
        scan_mode="signal_scan",
        emit_outputs=True,
        return_pattern_payload=True,
    )

    # 第二步：把 SQLite 和输出文件导出成静态网页可读的 web/data.js。
    export_dashboard_data.main()

    # 第三步：可选运行独立交易层。默认关闭，避免普通监控任务触发真实交易。
    if TRADING_AUTO_EXECUTE:
        summary = run_trading_cycle(payload=pattern_payload)
        print(
            f"[{now()}] 交易层完成：信号 {summary['signals_seen']}，"
            f"提交/计划 {summary['submitted']}，跳过 {summary['skipped']}，错误 {summary['errors']}"
        )
        print(
            f"[{now()}] 交易层审计：来源 {summary['source']}，"
            f"候选 {summary['candidate_signals']}，过滤 {summary['filtered_reasons']}"
        )
    else:
        print(f"[{now()}] 已跳过交易层：TRADING_AUTO_EXECUTE=false")

    # 第四步：同步到 Vercel 实际部署目录，保证下次部署包含最新 data.js 和静态资产。
    sync_vercel_site()

    # 第五步：默认把每小时快照部署到 Vercel；可用 AUTO_DEPLOY_VERCEL=false 临时暂停。
    if should_auto_deploy():
        deploy_to_vercel()
    else:
        print(f"[{now()}] 已跳过自动部署。需要线上同步时设置 AUTO_DEPLOY_VERCEL=true 并配置 VERCEL_TOKEN")

    print(f"[{now()}] 本轮更新完成")


def sync_vercel_site() -> None:
    """把 web 目录中的静态页面文件复制到 workspace 根目录的 vercel-site。"""
    source_dir = BASE_DIR / "web"
    target_dir = BASE_DIR.parent / "vercel-site"
    target_dir.mkdir(parents=True, exist_ok=True)

    for filename in WEB_FILES:
        source = source_dir / filename
        target = target_dir / filename
        if source.exists():
            shutil.copy2(source, target)
            print(f"[{now()}] 已同步 {source.name} -> {target}")
        else:
            print(f"[{now()}] 跳过缺失文件：{source}")


def should_auto_deploy() -> bool:
    """判断本轮是否发布 Vercel 快照；有 token 时默认开启，可显式关闭。"""
    value = os.getenv("AUTO_DEPLOY_VERCEL")
    if value is None:
        return bool(os.getenv("VERCEL_TOKEN"))
    return value.lower() in {"1", "true", "yes", "on"}


def deploy_to_vercel() -> None:
    """调用现有部署脚本；计划任务场景要求提前设置 VERCEL_TOKEN，不能依赖交互输入。"""
    deploy_script = BASE_DIR / "deploy_vercel.ps1"
    if not deploy_script.exists():
        raise FileNotFoundError(f"找不到部署脚本：{deploy_script}")

    if not os.getenv("VERCEL_TOKEN"):
        raise RuntimeError("AUTO_DEPLOY_VERCEL=true 时必须先设置 VERCEL_TOKEN 环境变量")

    print(f"[{now()}] 开始自动部署到 Vercel")
    deploy_timeout = int(os.getenv("VERCEL_DEPLOY_TIMEOUT_SECONDS", "180"))
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(deploy_script),
            ],
            cwd=BASE_DIR,
            check=True,
            timeout=deploy_timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"[{now()}] Vercel CLI 超时未退出；部署可能已提交，任务继续结束以免阻塞下一小时")


def now() -> str:
    """返回本地日志时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"[{now()}] 更新失败：{error}", file=sys.stderr)
        raise
