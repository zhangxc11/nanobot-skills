#!/usr/bin/env python3
"""
review_connector.py - Review connector for digital assistant.

Outputs Markdown guidance text (not JSON) for agent consumption.

Usage:
    python3 review_connector.py pending          # list all pending reviews
    python3 review_connector.py load R-xxx       # load full context for a review
"""

import os
import sys
from pathlib import Path

# Ensure scripts/ is on sys.path so we can import task_store
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import task_store as bm


# ──────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────

def cmd_pending() -> None:
    """List all pending review items with guidance text."""
    reviews = bm.list_reviews(status_filter="pending")

    if not reviews:
        print("## 待审项列表\n\n当前没有待审项。\n")
        return

    lines = [
        f"## 待审项列表（共 {len(reviews)} 项）",
        "",
    ]

    for r in reviews:
        wait = bm._review_wait_str(r.get("created", ""))
        lines += [
            f"### [{r['id']}] {r.get('summary', '(无摘要)')}",
            f"- **任务**: {r.get('task_id', '?')}",
            f"- **等待时长**: {wait}",
            f"- **创建时间**: {r.get('created', '?')}",
            "",
        ]

    lines += [
        "---",
        "",
        "## 建议下一步操作",
        "",
        "- 使用以下命令查看特定待审项的完整上下文：",
        "  ```",
        "  python3 review_connector.py load <review_id>",
        "  ```",
        "- 批准：`python3 task_store.py review resolve <review_id> --decision approved`",
        "- 拒绝：`python3 task_store.py review resolve <review_id> --decision rejected --note '原因'`",
        "- 推迟：`python3 task_store.py review resolve <review_id> --decision deferred`",
        "",
    ]

    print("\n".join(lines))


def cmd_load(review_id: str) -> None:
    """Load full context for a specific review item."""
    try:
        review = bm.load_review(review_id)
    except FileNotFoundError:
        print(f"## 错误\n\n找不到 Review {review_id}\n")
        return

    task_id    = review.get("task_id", "")
    task_info  = None
    task_files: list = []

    try:
        task       = bm.load_task(task_id)
        task_info  = task
        task_files = task.get("context", {}).get("files", [])
    except FileNotFoundError:
        pass

    wait = bm._review_wait_str(review.get("created", ""))

    # Generate short ID hint
    short_id_hint = ""
    try:
        from feishu_notify import extract_short_id
        if task_info:
            short_id = extract_short_id(task_info["id"])
            short_id_hint = f" (飞书回复: {short_id} Go/NoGo)"
    except ImportError:
        pass

    lines = [
        f"## Review 上下文: {review_id}{short_id_hint}",
        "",
        f"**摘要**: {review.get('summary', '(无摘要)')}",
        f"**状态**: {review.get('status', '?')}",
        f"**等待时长**: {wait}",
        f"**创建时间**: {review.get('created', '?')}",
        "",
        "### Review 提示词",
        "",
        review.get("prompt", "(无提示词)"),
        "",
    ]

    if task_info:
        lines += [
            "### 关联任务信息",
            "",
            f"- **任务 ID**: {task_info.get('id', '?')}",
            f"- **标题**: {task_info.get('title', '?')}",
            f"- **状态**: {task_info.get('status', '?')}",
            f"- **优先级**: {task_info.get('priority', '?')}",
            f"- **描述**: {task_info.get('description') or '(无描述)'}",
            "",
        ]

    if task_files:
        lines += [
            "### 相关文件",
            "",
        ]
        for f in task_files:
            lines.append(f"- `{f}`")
        lines.append("")

    lines += [
        "### 建议的 Review 步骤",
        "",
        "1. 阅读以上 Review 提示词",
        "2. 检查相关文件（如有）",
        "3. 做出决策：批准 / 拒绝 / 推迟",
        "",
        "### 操作指令",
        "",
        f"**批准**: `python3 task_store.py review resolve {review_id} --decision approved`",
        f"**拒绝**: `python3 task_store.py review resolve {review_id} --decision rejected --note '原因'`",
        f"**推迟**: `python3 task_store.py review resolve {review_id} --decision deferred`",
        "",
    ]

    print("\n".join(lines))


def cmd_notify_all() -> None:
    """Generate batch notification for all pending reviews."""
    try:
        from feishu_notify import format_review_notify
    except ImportError:
        print("## 错误\n\nfeishu_notify.py 模块不可用。\n")
        return

    reviews = bm.list_reviews(status_filter="pending")
    if not reviews:
        print("当前没有待审项。")
        return

    for r in reviews:
        try:
            task = bm.load_task(r["task_id"])
        except FileNotFoundError:
            continue
        print(format_review_notify(task, r))
        print()


# ──────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python3 review_connector.py <pending|load|notify-all> [review_id]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "pending":
        cmd_pending()
    elif command == "load":
        if len(sys.argv) < 3:
            print("用法: python3 review_connector.py load <review_id>")
            sys.exit(1)
        cmd_load(sys.argv[2])
    elif command == "notify-all":
        cmd_notify_all()
    else:
        print(f"未知命令: {command}")
        print("用法: python3 review_connector.py <pending|load|notify-all> [review_id]")
        sys.exit(1)


if __name__ == "__main__":
    main()
