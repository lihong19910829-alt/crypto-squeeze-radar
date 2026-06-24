"""X/Twitter 发布模块：默认 dry-run，只在 POST_TO_X=true 时真实发布。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (
    POST_TO_X,
    X_CREATE_POST_URL,
    X_FORBIDDEN_WORDS,
    X_MIN_RISK_SCORE,
    X_POST_PREVIEW_JSON_FILE,
    X_POST_PREVIEW_MD_FILE,
    X_USER_ACCESS_TOKEN,
)


def publish_eligible_tweets(tweets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛选并处理符合条件的 Top 5 推文。

    默认 dry-run：只保存预览，不真实发布。
    只有 POST_TO_X=true 且 X_USER_ACCESS_TOKEN 已配置时，才调用 X API。
    """
    candidates = [
        _prepare_tweet(tweet)
        for tweet in tweets
        if int(tweet.get("risk_score") or 0) >= X_MIN_RISK_SCORE
    ]
    candidates = [tweet for tweet in candidates if tweet is not None]

    dry_run = not POST_TO_X
    results: list[dict[str, Any]] = []
    for tweet in candidates:
        if tweet.get("status") == "blocked":
            results.append(tweet)
            continue

        if dry_run:
            results.append({**tweet, "status": "dry_run", "x_post_id": None, "error": None})
            continue

        if not X_USER_ACCESS_TOKEN:
            results.append({**tweet, "status": "skipped", "x_post_id": None, "error": "缺少 X_USER_ACCESS_TOKEN"})
            continue

        try:
            x_post_id = _post_to_x(tweet["tweet"])
            results.append({**tweet, "status": "posted", "x_post_id": x_post_id, "error": None})
        except RuntimeError as exc:
            results.append({**tweet, "status": "failed", "x_post_id": None, "error": str(exc)})

    save_x_preview(results, dry_run)
    return results


def save_x_preview(results: list[dict[str, Any]], dry_run: bool) -> None:
    """保存 X 发布预览/结果，方便人工审核。"""
    X_POST_PREVIEW_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "min_risk_score": X_MIN_RISK_SCORE,
        "post_to_x": POST_TO_X,
        "items": results,
    }
    X_POST_PREVIEW_JSON_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# X Post Preview",
        "",
        f"- dry_run：{dry_run}",
        f"- POST_TO_X：{POST_TO_X}",
        f"- 最低发布评分：{X_MIN_RISK_SCORE}",
        "",
    ]
    if not results:
        lines.append("本轮没有 risk_score 达到发布阈值的信号。")
    for index, item in enumerate(results, start=1):
        lines.extend(
            [
                f"## {index}. {item['coin']} - {item['risk_score']}/100",
                "",
                f"- 状态：{item['status']}",
                f"- X Post ID：{item.get('x_post_id') or 'N/A'}",
                f"- 错误：{item.get('error') or 'N/A'}",
                "",
                item["tweet"],
                "",
            ]
        )

    X_POST_PREVIEW_MD_FILE.write_text("\n".join(lines), encoding="utf-8")


def _prepare_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    """清理投资建议词，并确认文案满足发布约束。"""
    text = _sanitize_text(tweet.get("tweet", ""))
    if not text:
        return None
    if _contains_forbidden_word(text):
        return {
            **tweet,
            "tweet": text,
            "status": "blocked",
            "x_post_id": None,
            "error": "推文仍包含禁止词，已阻止发布",
        }
    return {**tweet, "tweet": text}


def _sanitize_text(text: str) -> str:
    """把禁止词替换成更客观的风险监控表述。"""
    replacements = {
        "买入": "关注",
        "卖出": "降低风险暴露",
        "稳赚": "确定性",
        "暴涨": "大幅波动",
        "必涨": "单边预期",
    }
    clean = text
    for word, replacement in replacements.items():
        clean = clean.replace(word, replacement)

    # 统一补充客观定位，避免文案像投资建议。
    if "异常监控" not in clean:
        clean = clean.replace("仅用于市场结构观察", "异常监控，仅用于市场结构观察")
    if "风险提示" not in clean:
        clean = clean.replace("不构成投资建议", "风险提示，不构成投资建议")
    return clean.strip()


def _contains_forbidden_word(text: str) -> bool:
    """检查推文是否仍包含禁止词。"""
    return any(word in text for word in X_FORBIDDEN_WORDS)


def _post_to_x(text: str) -> str:
    """调用 X API 创建 Post。"""
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = Request(
        X_CREATE_POST_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {X_USER_ACCESS_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "crypto-squeeze-radar/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"X API HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"X API 网络错误: {exc.reason}") from exc

    post_id = data.get("data", {}).get("id")
    if not post_id:
        raise RuntimeError(f"X API 响应缺少 post id: {data}")
    return str(post_id)
