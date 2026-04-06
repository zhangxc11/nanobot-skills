#!/usr/bin/env python3
"""
report_daily.py - 调度器日报生成脚本

独立脚本，import 复用 brain_manager 的数据函数，生成结构化日报 Markdown。

Usage:
    python3 report_daily.py                     # 生成当日日报
    python3 report_daily.py --date 2026-03-30   # 回溯指定日期
    python3 report_daily.py --dry-run           # 仅输出到 stdout，不写文件
    python3 report_daily.py --no-notify         # 不发送飞书通知

输出: data/brain/reports/daily/YYYY-MM-DD.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Ensure brain_manager is importable (same directory) ──
sys.path.insert(0, str(Path(__file__).resolve().parent))

import task_store as brain_manager  # noqa: E402  # backward compat alias

# Reuse from brain_manager
from task_store import (  # noqa: E402
    BRAIN_DIR,
    WORKSPACE_ROOT,
    list_tasks,
    list_reviews,
    list_quick_log,
    list_decisions,
    atomic_write,
)

try:
    import yaml  # noqa: E402
except ImportError:
    yaml = None  # type: ignore

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

REPORTS_DIR = BRAIN_DIR / "reports" / "daily"
FEISHU_MESSENGER_SCRIPT = WORKSPACE_ROOT / "skills" / "feishu-messenger" / "scripts" / "feishu_messenger.py"
NOTIFY_TARGET = os.environ.get("REPORT_NOTIFY_TO", "")

# ──────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────


@dataclass
class TaskSummary:
    id: str
    title: str
    status: str
    priority: str
    task_type: str
    updated: str
    block_reason: str = ""
    duration_str: str = ""


@dataclass
class ReviewSummary:
    id: str
    task_id: str
    summary: str
    status: str
    created: str
    wait_str: str = ""


@dataclass
class QuickSummary:
    id: str
    title: str
    result: str
    timestamp: str


@dataclass
class DailyReport:
    date: str
    # Overview
    total_tasks: int = 0
    done_count: int = 0
    executing_count: int = 0
    queued_count: int = 0
    blocked_count: int = 0
    review_count: int = 0
    quick_count: int = 0
    # Detail lists
    done_tasks: list = field(default_factory=list)
    executing_tasks: list = field(default_factory=list)
    blocked_tasks: list = field(default_factory=list)
    review_tasks: list = field(default_factory=list)
    queued_tasks: list = field(default_factory=list)
    quick_tasks: list = field(default_factory=list)
    # Decisions stats
    decisions_total: int = 0
    decisions_by_type: dict = field(default_factory=dict)
    # Stale/recovered
    stale_recovered: list = field(default_factory=list)
    # Efficiency
    avg_duration_str: str = ""
    type_distribution: dict = field(default_factory=dict)
    # Pending reviews
    pending_reviews: list = field(default_factory=list)
    overdue_reviews: list = field(default_factory=list)


# ──────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────


def calc_duration(created: str, updated: str) -> str:
    """Calculate human-readable duration between two ISO timestamps.

    Returns e.g. '2h 15m', '3d 1h', '45m'.
    Returns '' if either timestamp is missing or unparseable.
    """
    if not created or not updated:
        return ""
    try:
        t1 = datetime.fromisoformat(created)
        t2 = datetime.fromisoformat(updated)
        delta = t2 - t1
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return ""
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return ""


def extract_block_reason(task: dict) -> str:
    """Extract block reason from task context notes or history.

    Looks for the most recent 'blocked' status change note.
    """
    # Check context notes for block-related info
    notes = task.get("context", {}).get("notes", "") or ""
    history = task.get("history", []) or []

    # Search history in reverse for the most recent blocked transition
    for entry in reversed(history):
        detail = entry.get("detail", "")
        if "blocked" in detail.lower():
            # Extract note part if present: "status: xxx → blocked (reason)"
            paren_start = detail.find("(")
            paren_end = detail.rfind(")")
            if paren_start != -1 and paren_end > paren_start:
                return detail[paren_start + 1:paren_end]
            return detail

    # Fallback: last line of notes mentioning block
    if notes:
        for line in reversed(notes.strip().split("\n")):
            if "block" in line.lower() or "阻塞" in line or "等待" in line:
                # Strip timestamp prefix like [2026-03-31T...]
                if line.startswith("[") and "]" in line:
                    return line[line.index("]") + 1:].strip()
                return line.strip()

    return ""


def _safe_get(task: dict, key: str, default: str = "") -> str:
    """Safely get a string value from task dict."""
    val = task.get(key, default)
    return str(val) if val is not None else default


# ──────────────────────────────────────────
# Data collection functions
# ──────────────────────────────────────────


def collect_tasks(date: str) -> dict:
    """Collect task data for a given date (YYYY-MM-DD).

    Returns dict with categorized task lists.
    """
    all_tasks = list_tasks()

    done_tasks: list[TaskSummary] = []
    executing_tasks: list[TaskSummary] = []
    blocked_tasks: list[TaskSummary] = []
    review_tasks: list[TaskSummary] = []
    queued_tasks: list[TaskSummary] = []

    for t in all_tasks:
        status = _safe_get(t, "status")
        updated = _safe_get(t, "updated")
        created = _safe_get(t, "created")

        ts = TaskSummary(
            id=_safe_get(t, "id"),
            title=_safe_get(t, "title"),
            status=status,
            priority=_safe_get(t, "priority"),
            task_type=_safe_get(t, "type"),
            updated=updated,
        )

        if status == "done" and updated.startswith(date):
            ts.duration_str = calc_duration(created, updated)
            done_tasks.append(ts)
        elif status == "executing":
            executing_tasks.append(ts)
        elif status == "blocked":
            ts.block_reason = extract_block_reason(t)
            blocked_tasks.append(ts)
        elif status in ("review", "revision"):
            review_tasks.append(ts)
        elif status == "queued":
            queued_tasks.append(ts)

    return {
        "done": done_tasks,
        "executing": executing_tasks,
        "blocked": blocked_tasks,
        "review": review_tasks,
        "queued": queued_tasks,
    }


def collect_dispatcher_meta() -> dict:
    """Collect dispatcher metadata from dispatcher.json."""
    dispatcher_file = BRAIN_DIR / "dispatcher.json"
    if not dispatcher_file.exists():
        return {}
    try:
        with dispatcher_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def collect_decisions_stats(date: str) -> dict:
    """Collect decision statistics for a given date.

    Returns dict with total count and breakdown by type.
    """
    all_decisions = list_decisions(limit=0)  # get all
    day_decisions = [
        d for d in all_decisions
        if d.get("timestamp", "").startswith(date)
    ]
    by_type: dict[str, int] = {}
    for d in day_decisions:
        dtype = d.get("type", "unknown")
        by_type[dtype] = by_type.get(dtype, 0) + 1

    return {
        "total": len(day_decisions),
        "by_type": by_type,
    }


def collect_stale_recovered(date: str) -> list[dict]:
    """Find tasks that were recovered from stale/blocked state on the given date.

    Looks for status changes from blocked→queued or blocked→executing in history.
    """
    all_tasks = list_tasks()
    recovered: list[dict] = []

    for t in all_tasks:
        history = t.get("history", []) or []
        for entry in history:
            ts = entry.get("timestamp", "")
            if not ts.startswith(date):
                continue
            detail = entry.get("detail", "")
            # Match patterns like "status: blocked → queued" or "status: blocked → executing"
            if "blocked" in detail and ("→ queued" in detail or "→ executing" in detail):
                recovered.append({
                    "id": _safe_get(t, "id"),
                    "title": _safe_get(t, "title"),
                    "detail": detail,
                    "timestamp": ts,
                })

    return recovered


def collect_quick_tasks(date: str) -> list[QuickSummary]:
    """Collect quick tasks for a given date."""
    entries = list_quick_log(date_prefix=date)
    return [
        QuickSummary(
            id=e.get("id", ""),
            title=e.get("title", ""),
            result=e.get("result", ""),
            timestamp=e.get("timestamp", ""),
        )
        for e in entries
    ]


def collect_pending_reviews(date: str) -> tuple[list[ReviewSummary], list[ReviewSummary]]:
    """Collect pending and overdue reviews.

    Returns (pending_reviews, overdue_reviews).
    Overdue = pending for > 48 hours.
    """
    reviews = list_reviews(status_filter="pending")
    now_dt = datetime.now().astimezone()

    pending: list[ReviewSummary] = []
    overdue: list[ReviewSummary] = []

    for r in reviews:
        created_str = r.get("created", "")
        wait_str = ""
        is_overdue = False

        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str)
                delta = now_dt - created_dt
                total_hours = int(delta.total_seconds() // 3600)
                if total_hours >= 48:
                    is_overdue = True
                if total_hours >= 24:
                    wait_str = f"{total_hours // 24}d {total_hours % 24}h"
                elif total_hours >= 1:
                    wait_str = f"{total_hours}h"
                else:
                    wait_str = f"{int(delta.total_seconds() // 60)}m"
            except (ValueError, TypeError):
                pass

        rs = ReviewSummary(
            id=r.get("id", ""),
            task_id=r.get("task_id", ""),
            summary=r.get("summary", ""),
            status=r.get("status", ""),
            created=created_str,
            wait_str=wait_str,
        )
        pending.append(rs)
        if is_overdue:
            overdue.append(rs)

    return pending, overdue


# ──────────────────────────────────────────
# Report generation
# ──────────────────────────────────────────


def generate_daily_report(date: str) -> DailyReport:
    """Aggregate all data into a DailyReport for the given date."""
    tasks_data = collect_tasks(date)
    decisions_data = collect_decisions_stats(date)
    stale_recovered = collect_stale_recovered(date)
    quick_tasks = collect_quick_tasks(date)
    pending_reviews, overdue_reviews = collect_pending_reviews(date)

    done = tasks_data["done"]
    executing = tasks_data["executing"]
    blocked = tasks_data["blocked"]
    review = tasks_data["review"]
    queued = tasks_data["queued"]

    # Type distribution (dynamic scan, not hardcoded)
    type_dist: dict[str, int] = {}
    for task_list in [done, executing, blocked, review, queued]:
        for t in task_list:
            tt = t.task_type or "unknown"
            type_dist[tt] = type_dist.get(tt, 0) + 1

    # Average duration for done tasks
    durations: list[int] = []
    for t in done:
        if t.duration_str:
            # Parse back to minutes for averaging
            mins = _parse_duration_to_minutes(t.duration_str)
            if mins > 0:
                durations.append(mins)
    avg_duration_str = ""
    if durations:
        avg_mins = sum(durations) // len(durations)
        if avg_mins >= 60:
            avg_duration_str = f"{avg_mins // 60}h {avg_mins % 60}m"
        else:
            avg_duration_str = f"{avg_mins}m"

    report = DailyReport(
        date=date,
        total_tasks=len(done) + len(executing) + len(blocked) + len(review) + len(queued),
        done_count=len(done),
        executing_count=len(executing),
        queued_count=len(queued),
        blocked_count=len(blocked),
        review_count=len(review),
        quick_count=len(quick_tasks),
        done_tasks=[asdict(t) for t in done],
        executing_tasks=[asdict(t) for t in executing],
        blocked_tasks=[asdict(t) for t in blocked],
        review_tasks=[asdict(t) for t in review],
        queued_tasks=[asdict(t) for t in queued],
        quick_tasks=[asdict(t) for t in quick_tasks],
        decisions_total=decisions_data["total"],
        decisions_by_type=decisions_data["by_type"],
        stale_recovered=stale_recovered,
        avg_duration_str=avg_duration_str,
        type_distribution=type_dist,
        pending_reviews=[asdict(r) for r in pending_reviews],
        overdue_reviews=[asdict(r) for r in overdue_reviews],
    )

    return report


def _parse_duration_to_minutes(duration_str: str) -> int:
    """Parse a duration string like '2h 15m', '3d 1h', '45m' to total minutes."""
    total = 0
    import re
    d_match = re.search(r"(\d+)d", duration_str)
    h_match = re.search(r"(\d+)h", duration_str)
    m_match = re.search(r"(\d+)m", duration_str)
    if d_match:
        total += int(d_match.group(1)) * 1440
    if h_match:
        total += int(h_match.group(1)) * 60
    if m_match:
        total += int(m_match.group(1))
    return total


# ──────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────


def render_daily_markdown(report: DailyReport) -> str:
    """Render a DailyReport into Markdown (≤200 lines target)."""
    lines: list[str] = []

    # ── 1. 概览 ──
    lines.append(f"# 📊 日报 {report.date}")
    lines.append("")
    lines.append("## 1. 概览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 完成任务 | {report.done_count} |")
    lines.append(f"| 进行中 | {report.executing_count} |")
    lines.append(f"| 排队中 | {report.queued_count} |")
    lines.append(f"| 阻塞 | {report.blocked_count} |")
    lines.append(f"| 待审 | {report.review_count} |")
    lines.append(f"| 快速任务 | {report.quick_count} |")
    lines.append(f"| 调度决策 | {report.decisions_total} |")
    lines.append("")

    # ── 2. 完成 ──
    lines.append("## 2. ✅ 已完成")
    lines.append("")
    if report.done_tasks:
        for t in report.done_tasks:
            dur = f" ({t['duration_str']})" if t.get("duration_str") else ""
            lines.append(f"- [{t['id']}] **{t['title']}** {t['priority']}{dur}")
    else:
        lines.append("_无_")
    if report.quick_tasks:
        lines.append("")
        lines.append(f"**快速任务** ({report.quick_count} 条):")
        for q in report.quick_tasks[:5]:
            result = f" → {q['result']}" if q.get("result") else ""
            lines.append(f"- [{q['id']}] {q['title']}{result}")
        if len(report.quick_tasks) > 5:
            lines.append(f"- _...及其他 {len(report.quick_tasks) - 5} 条_")
    lines.append("")

    # ── 3. 进行中 ──
    lines.append("## 3. 🔵 进行中")
    lines.append("")
    if report.executing_tasks:
        for t in report.executing_tasks:
            lines.append(f"- [{t['id']}] {t['title']} ({t['priority']})")
    else:
        lines.append("_无_")
    lines.append("")

    # ── 4. 异常 ──
    lines.append("## 4. 🚨 异常")
    lines.append("")
    has_anomaly = False
    if report.blocked_tasks:
        has_anomaly = True
        lines.append("**阻塞任务:**")
        for t in report.blocked_tasks:
            reason = f" — {t['block_reason']}" if t.get("block_reason") else ""
            lines.append(f"- [{t['id']}] {t['title']}{reason}")
    if report.overdue_reviews:
        has_anomaly = True
        lines.append("**超时待审 (>48h):**")
        for r in report.overdue_reviews:
            lines.append(f"- [{r['id']}] {r['summary']} (等待 {r['wait_str']})")
    if report.stale_recovered:
        has_anomaly = True
        lines.append("**今日恢复:**")
        for s in report.stale_recovered:
            lines.append(f"- [{s['id']}] {s['title']} — {s['detail']}")
    if not has_anomaly:
        lines.append("_无异常_")
    lines.append("")

    # ── 5. 效率 ──
    lines.append("## 5. ⚡ 效率")
    lines.append("")
    if report.avg_duration_str:
        lines.append(f"- 平均完成耗时: {report.avg_duration_str}")
    else:
        lines.append("- 平均完成耗时: N/A")
    if report.type_distribution:
        dist_parts = [f"{k}: {v}" for k, v in sorted(report.type_distribution.items())]
        lines.append(f"- 任务类型分布: {', '.join(dist_parts)}")
    if report.decisions_by_type:
        dec_parts = [f"{k}: {v}" for k, v in sorted(report.decisions_by_type.items())]
        lines.append(f"- 决策分布: {', '.join(dec_parts)}")
    lines.append("")

    # ── 6. 待办 ──
    lines.append("## 6. 📋 待办")
    lines.append("")
    if report.queued_tasks:
        for t in report.queued_tasks[:10]:
            lines.append(f"- [{t['id']}] {t['title']} ({t['priority']})")
        if len(report.queued_tasks) > 10:
            lines.append(f"- _...及其他 {len(report.queued_tasks) - 10} 条_")
    else:
        lines.append("_无_")
    if report.pending_reviews:
        lines.append("")
        lines.append("**待审项:**")
        for r in report.pending_reviews[:5]:
            lines.append(f"- [{r['id']}] {r['summary']} (等待 {r['wait_str']})")
        if len(report.pending_reviews) > 5:
            lines.append(f"- _...及其他 {len(report.pending_reviews) - 5} 条_")
    lines.append("")

    # ── 7. 预期 ──
    lines.append("## 7. 🔮 预期")
    lines.append("")
    tomorrow_outlook: list[str] = []
    if report.queued_count > 0:
        tomorrow_outlook.append(f"排队中 {report.queued_count} 条任务待执行")
    if report.blocked_count > 0:
        tomorrow_outlook.append(f"{report.blocked_count} 条阻塞任务需关注")
    if report.overdue_reviews:
        tomorrow_outlook.append(f"{len(report.overdue_reviews)} 条超时待审需处理")
    if not tomorrow_outlook:
        tomorrow_outlook.append("无明显待处理事项")
    for item in tomorrow_outlook:
        lines.append(f"- {item}")
    lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"_由 report_daily.py 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────
# Notification
# ──────────────────────────────────────────


def _build_notification_summary(report: DailyReport) -> str:
    """Build a ≤10 line notification summary."""
    lines: list[str] = []
    lines.append(f"📊 日报 {report.date}")
    lines.append(f"完成: {report.done_count} | 进行中: {report.executing_count} | 排队: {report.queued_count}")
    if report.quick_count:
        lines.append(f"快速任务: {report.quick_count}")
    if report.blocked_count:
        lines.append(f"🚨 阻塞: {report.blocked_count}")
    if report.overdue_reviews:
        lines.append(f"⏰ 超时待审: {len(report.overdue_reviews)}")
    if report.done_tasks:
        lines.append("✅ " + ", ".join(t["id"] for t in report.done_tasks[:3]))
    if report.avg_duration_str:
        lines.append(f"⚡ 平均耗时: {report.avg_duration_str}")
    if report.queued_count:
        lines.append(f"📋 待办: {report.queued_count} 条")
    # Ensure ≤10 lines
    return "\n".join(lines[:10])


def send_notification(report: DailyReport) -> bool:
    """Send notification via feishu_messenger.py.

    Returns True if sent successfully, False otherwise.
    Failures are logged but never block report generation.
    """
    if not NOTIFY_TARGET:
        return False

    if not FEISHU_MESSENGER_SCRIPT.exists():
        print(f"[notify] feishu_messenger.py not found at {FEISHU_MESSENGER_SCRIPT}", file=sys.stderr)
        return False

    text = _build_notification_summary(report)

    try:
        cmd = [
            sys.executable,
            str(FEISHU_MESSENGER_SCRIPT),
            "--app", "ST",
            "send-text",
            "--to", NOTIFY_TARGET,
            "--text", text,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"[notify] Sent to {NOTIFY_TARGET}", file=sys.stderr)
            return True
        else:
            print(f"[notify] Failed (rc={result.returncode}): {result.stderr[:200]}", file=sys.stderr)
            return False
    except subprocess.TimeoutExpired:
        print("[notify] Timeout sending notification", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[notify] Error: {e}", file=sys.stderr)
        return False


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="report_daily.py",
        description="生成调度器日报",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="报告日期 YYYY-MM-DD (默认: 今天)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅输出到 stdout，不写文件",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="不发送飞书通知",
    )

    args = parser.parse_args()

    # Determine date
    if args.date:
        # Validate date format
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"Error: Invalid date format '{args.date}'. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)
        date = args.date
    else:
        date = datetime.now().strftime("%Y-%m-%d")

    # Generate report
    report = generate_daily_report(date)
    markdown = render_daily_markdown(report)

    # Output
    if args.dry_run:
        print(markdown)
    else:
        # Write to file
        report_path = REPORTS_DIR / f"{date}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(report_path, markdown)
        print(f"Report written to: {report_path}", file=sys.stderr)

    # Notification
    if not args.no_notify and not args.dry_run:
        send_notification(report)
    elif args.dry_run and not args.no_notify and NOTIFY_TARGET:
        print(f"\n[dry-run] Would notify: {NOTIFY_TARGET}", file=sys.stderr)
        print(f"[dry-run] Summary:\n{_build_notification_summary(report)}", file=sys.stderr)


if __name__ == "__main__":
    main()
