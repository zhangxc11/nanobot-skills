#!/usr/bin/env python3
"""
feishu_notify.py - 飞书通知格式化 + 用户回复解析

功能：
1. format_*() — 各场景通知文本生成
2. parse_task_reply() — 用户回复解析
3. CLI — 支持命令行测试

Usage:
    # 生成通知
    python3 feishu_notify.py format-review <task_id> [--review-id R-xxx]
    python3 feishu_notify.py format-status <task_id> --old-status S1 --new-status S2
    python3 feishu_notify.py format-done <task_id> [--duration "2h 15m"] [--artifacts f1 f2]
    python3 feishu_notify.py format-error <task_id> --reason "xxx"
    python3 feishu_notify.py format-batch [--json-file <path>]

    # 解析回复
    python3 feishu_notify.py parse "T-001 Go"
    python3 feishu_notify.py parse "T-001 不行，需要改"
    python3 feishu_notify.py parse "今天天气怎么样"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Emoji mapping by notification type
NOTIFY_EMOJI = {
    "review":        "📋",
    "status_change": "🔄",
    "done":          "✅",
    "error":         "🚨",
    "batch_summary": "📊",
}

# ── Regex: match T-xxx or R-xxx at start of message ──
TASK_REF_PATTERN = re.compile(
    r'^[「\s]*'                          # optional leading whitespace / Chinese quotes
    r'(?P<prefix>[TR])-'                 # T- or R-
    r'(?P<id>'
        r'(?:\d{8}-)?'                   # optional date part YYYYMMDD-
        r'\d{1,3}'                       # sequence 001-999
    r')'
    r'[\s:：]*'                          # separator
    r'(?P<rest>.*)',                      # rest of message
    re.IGNORECASE | re.DOTALL
)

# ── Action keywords → canonical action ──
# Sorted by length (longest first) at match time to avoid prefix collisions
ACTION_KEYWORDS = {
    # Approve
    "go":       "approve",
    "通过":     "approve",
    "批准":     "approve",
    "ok":       "approve",
    "approve":  "approve",
    "lgtm":     "approve",
    "没问题":   "approve",
    "可以":     "approve",

    # Reject
    "nogo":     "reject",
    "no-go":    "reject",
    "不行":     "reject",
    "拒绝":     "reject",
    "reject":   "reject",
    "打回":     "reject",
    "不通过":   "reject",

    # Defer
    "推迟":     "defer",
    "defer":    "defer",
    "稍后":     "defer",
    "等等":     "defer",

    # Control
    "暂停":     "pause",
    "pause":    "pause",
    "取消":     "cancel",
    "cancel":   "cancel",
    "继续":     "resume",
    "resume":   "resume",

    # Conditional approve (explicit keyword forms)
    "go但":     "conditional_approve",
    "go 但":    "conditional_approve",
}


# ──────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────

@dataclass
class TaskReply:
    """Parsed result from a user's Feishu reply message."""
    task_id: str           # full task ID (e.g. T-20260330-001)
    ref_type: str          # "task" or "review"
    action: str            # approve / reject / defer / pause / cancel / resume / conditional_approve / comment
    comment: str           # user's additional text
    raw_short_id: str      # original short code (e.g. T-001)
    confidence: float      # 0.0-1.0; full ID → 1.0, short code → 0.9


# ──────────────────────────────────────────
# Short ID helpers
# ──────────────────────────────────────────

def extract_short_id(full_id: str) -> str:
    """Extract short ID from full ID.

    T-20260330-001 → T-001
    R-20260330-012 → R-012
    """
    parts = full_id.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{parts[-1]}"
    # Already short or unexpected format — return as-is
    return full_id


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


# ──────────────────────────────────────────
# Short ID resolution (requires brain_manager)
# ──────────────────────────────────────────

def _get_bm():
    """Lazy-import task_store to avoid circular deps and allow standalone testing."""
    _scripts_dir = Path(__file__).resolve().parent
    if str(_scripts_dir) not in sys.path:
        sys.path.insert(0, str(_scripts_dir))
    import task_store as bm
    return bm


def resolve_short_id(prefix: str, id_part: str) -> str | None:
    """Resolve short sequence → full task/review ID.

    Strategy:
    1. If id_part already has date (e.g. 20260330-001), return directly
    2. Try today's date first
    3. Search all active entities
    """
    prefix = prefix.upper()

    if "-" in id_part:
        # Already full format
        return f"{prefix}-{id_part}"

    seq = id_part.zfill(3)
    today = _today_str()

    # Try today first
    candidate = f"{prefix}-{today}-{seq}"
    if _entity_exists(prefix, candidate):
        return candidate

    # Search active entities
    return _search_active_entity(prefix, seq)


def _entity_exists(prefix: str, full_id: str) -> bool:
    """Check if a task or review entity exists on disk."""
    try:
        bm = _get_bm()
        if prefix == "T":
            bm.load_task(full_id)
        else:
            bm.load_review(full_id)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _search_active_entity(prefix: str, seq: str) -> str | None:
    """Search active entities matching the sequence number (newest first)."""
    try:
        bm = _get_bm()
        if prefix == "T":
            tasks = bm.list_tasks(
                status_filter={"queued", "executing", "review", "blocked", "revision"}
            )
            for t in reversed(tasks):
                if t["id"].endswith(f"-{seq}"):
                    return t["id"]
        else:
            reviews = bm.list_reviews(status_filter="pending")
            for r in reversed(reviews):
                if r["id"].endswith(f"-{seq}"):
                    return r["id"]
    except Exception:
        pass
    return None


# ──────────────────────────────────────────
# Action extraction
# ──────────────────────────────────────────

def extract_action(text: str) -> tuple[str, str]:
    """Extract action and comment from the rest of a reply message.

    Rules:
    1. Take first token, try to match ACTION_KEYWORDS
    2. Match → (action, remainder as comment)
    3. No match → ("comment", full text)

    Special:
    - "Go 但xxx" → conditional_approve, comment=xxx
    - "不行，xxx" → reject, comment=xxx
    """
    if not text:
        return ("comment", "")

    text_lower = text.lower()

    # Try keywords longest-first to avoid prefix collisions
    for keyword, action in sorted(ACTION_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if text_lower.startswith(keyword):
            remainder = text[len(keyword):].lstrip("，,、: ：")
            if action == "approve" and remainder:
                # "Go 但需要补测试" → conditional_approve
                if remainder.startswith("但") or remainder.startswith("不过"):
                    return ("conditional_approve", remainder.lstrip("但不过，,").strip())
            return (action, remainder.strip())

    # No keyword match → treat as comment
    return ("comment", text)


# ──────────────────────────────────────────
# Reply parsing (main entry)
# ──────────────────────────────────────────

def parse_task_reply(message: str, *, resolve_id: bool = True) -> TaskReply | None:
    """Parse a user's Feishu reply message for task/review reference.

    Returns None if the message doesn't contain a task reference at the start.

    Args:
        message: raw message text
        resolve_id: if True, resolve short ID to full ID via brain_manager.
                    if False, construct full ID using today's date (for testing).
    """
    if not message:
        return None

    m = TASK_REF_PATTERN.match(message.strip())
    if not m:
        return None

    prefix = m.group("prefix").upper()
    id_part = m.group("id")
    rest = m.group("rest").strip()

    # Build raw short ID
    raw_short_id = f"{prefix}-{id_part}"

    # Resolve to full ID
    if resolve_id:
        full_id = resolve_short_id(prefix, id_part)
        if full_id is None:
            return None  # Can't match any active entity
    else:
        # Standalone mode: construct from today's date
        if "-" in id_part:
            full_id = f"{prefix}-{id_part}"
        else:
            full_id = f"{prefix}-{_today_str()}-{id_part.zfill(3)}"

    ref_type = "task" if prefix == "T" else "review"

    # Parse action from rest
    action, comment = extract_action(rest)

    # Confidence: full ID (has date) → 1.0, short code → 0.9
    confidence = 1.0 if "-" in id_part and len(id_part) > 3 else 0.9

    return TaskReply(
        task_id=full_id,
        ref_type=ref_type,
        action=action,
        comment=comment,
        raw_short_id=raw_short_id,
        confidence=confidence,
    )


# ──────────────────────────────────────────
# Notification formatting
# ──────────────────────────────────────────

def format_review_notify(task: dict, review: dict) -> str:
    """Scene 1: Review completion notification (needs user Go/NoGo).

    Args:
        task: task dict (from brain_manager.load_task)
        review: review dict (from brain_manager.load_review)
    """
    task_id = task.get("id", "T-???")
    short_id = extract_short_id(task_id)
    title = task.get("title", "Unknown Task")
    review_summary = review.get("summary", "")
    review_prompt = review.get("prompt", "")

    lines = [
        f"📋 [{short_id}] {title} — Review 完成",
        SEPARATOR,
    ]

    # Add review details if available
    if review_prompt:
        lines.append(review_prompt)
        lines.append("")

    if review_summary:
        lines.append(f"摘要: {review_summary}")
        lines.append("")

    lines += [
        f"📌 需要你确认: Go / NoGo / 修改意见",
        f"💡 回复示例:",
        f"  {short_id} Go",
        f"  {short_id} NoGo 原因说明",
        f"  {short_id} Go 但下个版本补测试",
    ]

    return "\n".join(lines)


def format_status_change(task: dict, old_status: str, new_status: str) -> str:
    """Scene 2: Task status change notification (informational)."""
    task_id = task.get("id", "T-???")
    short_id = extract_short_id(task_id)
    title = task.get("title", "Unknown Task")

    # Status-specific description
    desc_map = {
        ("queued", "executing"):  "调度器已派发，Worker 开始执行。",
        ("executing", "review"):  "开发完成，等待 Review。",
        ("review", "executing"):  "Review 通过，继续执行。",
        ("review", "revision"):   "Review 要求修改，进入修订。",
        ("revision", "executing"): "修订完成，重新执行。",
    }
    desc = desc_map.get((old_status, new_status), f"状态已从 {old_status} 变为 {new_status}。")

    lines = [
        f"🔄 [{short_id}] {title} — 状态变更",
        SEPARATOR,
        f"{old_status} → {new_status}",
        desc,
        "",
        f"💡 如需干预，回复: {short_id} 暂停 / {short_id} 取消",
    ]

    return "\n".join(lines)


def format_done_notify(task: dict, duration: str = "", artifacts: list[str] | None = None) -> str:
    """Scene 3: Task completion notification (informational)."""
    task_id = task.get("id", "T-???")
    short_id = extract_short_id(task_id)
    title = task.get("title", "Unknown Task")

    lines = [
        f"✅ [{short_id}] {title} — 已完成",
        SEPARATOR,
    ]

    if duration:
        lines.append(f"耗时: {duration}")

    if artifacts:
        lines.append("产出:")
        for a in artifacts:
            lines.append(f"  - {a}")

    lines += [
        "",
        f"💡 如有问题，回复: {short_id} 有问题 <描述>",
    ]

    return "\n".join(lines)


def format_error_notify(task: dict, reason: str) -> str:
    """Scene 4: Error/block alert (needs user intervention)."""
    task_id = task.get("id", "T-???")
    short_id = extract_short_id(task_id)
    title = task.get("title", "Unknown Task")

    lines = [
        f"🚨 [{short_id}] {title} — 执行阻塞",
        SEPARATOR,
        f"原因: {reason}",
        "",
        f"📌 需要你决定:",
        f"  {short_id} 继续（跳过依赖）",
        f"  {short_id} 取消",
    ]

    return "\n".join(lines)


def format_batch_summary(
    dispatched: list[dict] | None = None,
    review_pending: list[dict] | None = None,
    errors: list[dict] | None = None,
) -> str:
    """Scene 5: Batch schedule summary notification.

    Args:
        dispatched: list of {"task_id": ..., "priority": ..., "title": ...}
        review_pending: list of {"id": ..., "task_id": ..., "summary": ...}
        errors: list of {"task_id": ..., "error": ...}
    """
    dispatched = dispatched or []
    review_pending = review_pending or []
    errors = errors or []

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📊 调度摘要 — {now_str}",
        SEPARATOR,
    ]

    if dispatched:
        lines.append("🚀 新派发:")
        for d in dispatched:
            tid = d.get("task_id", "?")
            short = extract_short_id(tid)
            title = d.get("title", "")
            priority = d.get("priority", "")
            label = f"[{short}] {title}" if title else f"[{short}]"
            if priority:
                label += f" ({priority})"
            lines.append(f"  {label}")
        lines.append("")

    if review_pending:
        lines.append("⏳ 等待 Review:")
        for r in review_pending:
            tid = r.get("task_id", r.get("id", "?"))
            short = extract_short_id(tid)
            summary = r.get("summary", "")
            lines.append(f"  [{short}] {summary} — {short} Go/NoGo?")
        lines.append("")

    if errors:
        lines.append("🔴 需关注:")
        for e in errors:
            tid = e.get("task_id", "?")
            short = extract_short_id(tid)
            error_msg = e.get("error", "unknown error")
            lines.append(f"  [{short}] {error_msg}")
        lines.append("")

    if not dispatched and not review_pending and not errors:
        lines.append("当前无需处理的事项。")
        lines.append("")

    lines.append("💡 逐个回复即可: T-xxx Go / T-xxx NoGo / T-xxx 取消")

    return "\n".join(lines)


# ──────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────

def _cli_output(data: dict) -> None:
    """Print JSON output."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cli_parse(args: argparse.Namespace) -> None:
    """CLI: parse a user reply message."""
    message = args.message
    result = parse_task_reply(message, resolve_id=not args.no_resolve)

    if result is None:
        _cli_output({"ok": True, "data": None})
    else:
        _cli_output({"ok": True, "data": asdict(result)})


def cli_format_review(args: argparse.Namespace) -> None:
    """CLI: format a review notification."""
    bm = _get_bm()
    try:
        task = bm.load_task(args.task_id)
    except FileNotFoundError:
        _cli_output({"ok": False, "error": f"Task {args.task_id} not found"})
        return

    review_id = getattr(args, "review_id", None)
    if review_id:
        try:
            review = bm.load_review(review_id)
        except FileNotFoundError:
            _cli_output({"ok": False, "error": f"Review {review_id} not found"})
            return
    else:
        pending = bm.get_task_pending_reviews(args.task_id)
        if not pending:
            _cli_output({"ok": False, "error": f"No pending reviews for {args.task_id}"})
            return
        review = pending[0]

    text = format_review_notify(task, review)
    short_id = extract_short_id(args.task_id)
    _cli_output({"ok": True, "data": {"text": text, "short_id": short_id}})


def cli_format_status(args: argparse.Namespace) -> None:
    """CLI: format a status change notification."""
    bm = _get_bm()
    try:
        task = bm.load_task(args.task_id)
    except FileNotFoundError:
        _cli_output({"ok": False, "error": f"Task {args.task_id} not found"})
        return

    text = format_status_change(task, args.old_status, args.new_status)
    short_id = extract_short_id(args.task_id)
    _cli_output({"ok": True, "data": {"text": text, "short_id": short_id}})


def cli_format_done(args: argparse.Namespace) -> None:
    """CLI: format a done notification."""
    bm = _get_bm()
    try:
        task = bm.load_task(args.task_id)
    except FileNotFoundError:
        _cli_output({"ok": False, "error": f"Task {args.task_id} not found"})
        return

    artifacts = getattr(args, "artifacts", None) or []
    duration = getattr(args, "duration", "") or ""
    text = format_done_notify(task, duration=duration, artifacts=artifacts)
    short_id = extract_short_id(args.task_id)
    _cli_output({"ok": True, "data": {"text": text, "short_id": short_id}})


def cli_format_error(args: argparse.Namespace) -> None:
    """CLI: format an error notification."""
    bm = _get_bm()
    try:
        task = bm.load_task(args.task_id)
    except FileNotFoundError:
        _cli_output({"ok": False, "error": f"Task {args.task_id} not found"})
        return

    text = format_error_notify(task, args.reason)
    short_id = extract_short_id(args.task_id)
    _cli_output({"ok": True, "data": {"text": text, "short_id": short_id}})


def main() -> None:
    parser = argparse.ArgumentParser(description="Feishu notification formatter & reply parser")
    sub = parser.add_subparsers(dest="command")

    # parse
    p_parse = sub.add_parser("parse", help="Parse a user reply message")
    p_parse.add_argument("message", help="The message text to parse")
    p_parse.add_argument("--no-resolve", action="store_true",
                         help="Don't resolve short ID via brain_manager (use today's date)")

    # format-review
    p_review = sub.add_parser("format-review", help="Format a review notification")
    p_review.add_argument("task_id", help="Task ID (e.g. T-20260330-001)")
    p_review.add_argument("--review-id", help="Specific review ID (optional)")

    # format-status
    p_status = sub.add_parser("format-status", help="Format a status change notification")
    p_status.add_argument("task_id", help="Task ID")
    p_status.add_argument("--old-status", required=True, help="Previous status")
    p_status.add_argument("--new-status", required=True, help="New status")

    # format-done
    p_done = sub.add_parser("format-done", help="Format a done notification")
    p_done.add_argument("task_id", help="Task ID")
    p_done.add_argument("--duration", default="", help="Duration string")
    p_done.add_argument("--artifacts", nargs="*", help="Artifact file paths")

    # format-error
    p_error = sub.add_parser("format-error", help="Format an error notification")
    p_error.add_argument("task_id", help="Task ID")
    p_error.add_argument("--reason", required=True, help="Error reason")

    args = parser.parse_args()

    if args.command == "parse":
        cli_parse(args)
    elif args.command == "format-review":
        cli_format_review(args)
    elif args.command == "format-status":
        cli_format_status(args)
    elif args.command == "format-done":
        cli_format_done(args)
    elif args.command == "format-error":
        cli_format_error(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
