"""导出静态仪表盘数据：让 web/index.html 可直接打开查看。"""

from __future__ import annotations

import json

from web_server import build_summary, load_history, read_json_file
from config import BASE_DIR, TWEETS_JSON_FILE, X_POST_PREVIEW_JSON_FILE


def main() -> None:
    """把当前 SQLite 和输出文件打包成浏览器可直接读取的 data.js。"""
    payload = {
        "summary": build_summary(),
        "history": load_history(320),
        "tweets": read_json_file(TWEETS_JSON_FILE, []),
        "xPreview": read_json_file(X_POST_PREVIEW_JSON_FILE, {}),
    }
    target = BASE_DIR / "web" / "data.js"
    target.write_text(
        "window.RADAR_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )
    print(f"已导出静态仪表盘数据：{target}")


if __name__ == "__main__":
    main()

