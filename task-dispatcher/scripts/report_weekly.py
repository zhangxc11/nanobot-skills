#!/usr/bin/env python3
"""
report_weekly.py - 调度器周报生成脚本

独立脚本，import 复用 brain_manager 的数据函数，生成结构化周报 Markdown。
始终从 tasks YAML 直接计算（回溯 7 天），不依赖旧周报 Markdown。
任务类型分布从实际数据动态扫描，不硬编码枚举列表。

Usage:
    python3 report_weekly.py                     # 生成本周周报
    python3 report_weekly.py --week 2026-W14     # 指定 ISO 周
    python3 report_weekly.py --dry-run           # 仅输出到 stdout，不写文件
    python3 report_weekly.py --no-notify         # 不发送飞书通知

输出: data/brain/reports/weekly/YYYY-Www.md
Cron: 0 10 * * 1 (每周一 10:00)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
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
    list_decisions,
    list_quick_log,
    atomic_write,
)

try:
    import yaml  # noqa: E402
except ImportError:
    yaml = None  # type: ignore

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

REPORTS_DIR = BRAIN_DIR / "reports" / "weekly"
FEISHU_MESSENGER_SCRIPT = WORKSPACE_ROOT / "skills" / "feishu-messenger" / "scripts" / "feishu_messenger.py"
NOTIFY_TARGET = os.environ.get("REPORT_NOTIFY_TO", "")

# CIL weekly reports directory (may or may not exist)
CIL_WEEKLY_DIR = BRAIN_DIR / "reports" / "cil-weekly"

# ──────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────


@dataclass
class WeekRange:
    """ISO week range: Monday to Sunday."""
    year: int
    week: int
    start_date: str  # YYYY-MM-DD (Monday)
    end_date: str    # YYYY-MM-DD (Sunday)

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.week:02d}"


@dataclass
class WeeklyTaskSummary:
    id: str
    title: str
    status: str
    priority: str
    task_type: str
    created: str
    updated: str
    duration_str: str = ""


@dataclass
class WeeklyOverview:
    total_created: int = 0
    total_done: int = 0
    total_blocked: int = 0
    total_quick: int = 0
    total_decisions: int = 0


@dataclass
class DayEfficiency:
    date: str
    done_count: int = 0
    created_count: int = 0
    decisions_count: int = 0


@dataclass
class WeeklyReport:
    week_label: str  # e.g. "2026-W14"
    start_date: str
    end_date: str
    # Overview: this week
    this_week: WeeklyOverview = field(default_factory=WeeklyOverview)
    # Overview: last week (for comparison)
    last_week: WeeklyOverview = field(default_factory=WeeklyOverview)
    # Completed tasks this week
    done_tasks: list = field(default_factory=list)
    # Task type distribution (dynamic)
    type_distribution: dict = field(default_factory=dict)
    # Daily efficiency trend
    daily_efficiency: list = field(default_factory=list)
    # Problems / anomalies
    blocked_tasks: list = field(default_factory=list)
    stale_tasks: list = field(default_factory=list)
    # CIL weekly reference
    cil_weekly_path: str = ""


# ──────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────


def iso_week_range(year: int, week: int) -> WeekRange:
    """Calculate the Monday–Sunday date range for a given ISO year+week."""
    # ISO week: Monday is day 1
    # Jan 4 is always in week 1
    jan4 = datetime(year, 1, 4)
    # Monday of week 1
    week1_monday = jan4 - timedelta(days=jan4.isoweekday() - 1)
    # Monday of target week
    target_monday = week1_monday + timedelta(weeks=week - 1)
    target_sunday = target_monday + timedelta(days=6)
    return WeekRange(
        year=year,
        week=week,
        start_date=target_monday.strftime("%Y-%m-%d"),
        end_date=target_sunday.strftime("%Y-%m-%d"),
    )


def parse_week_label(label: str) -> tuple[int, int]:
    """Parse 'YYYY-Www' or 'YYYY-Ww' to (year, week)."""
    m = re.match(r"^(\d{4})-W(\d{1,2})$", label)
    if not m:
        raise ValueError(f"Invalid week label: '{label}'. Expected YYYY-Www.")
    year, week = int(m.group(1)), int(m.group(2))
    if week < 1 or week > 53:
        raise ValueError(f"Week number out of range: {week}")
    return year, week


def current_iso_week() -> tuple[int, int]:
    """Return (year, week) for the current ISO week.

    On Monday, returns the current (new) week.
    """
    today = datetime.now()
    iso = today.isocalendar()
    return iso[0], iso[1]


def previous_iso_week(year: int, week: int) -> tuple[int, int]:
    """Return (year, week) for the previous ISO week."""
    wr = iso_week_range(year, week)
    prev_monday = datetime.strptime(wr.start_date, "%Y-%m-%d") - timedelta(weeks=1)
    iso = prev_monday.isocalendar()
    return iso[0], iso[1]


def date_in_range(date_str: str, start: str, end: str) -> bool:
    """Check if a date string (YYYY-MM-DD prefix) falls within [start, end]."""
    if not date_str:
        return False
    day = date_str[:10]
    return start <= day <= end


def calc_duration(created: str, updated: str) -> str:
    """Calculate human-readable duration between two ISO timestamps."""
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


def _parse_duration_to_minutes(duration_str: str) -> int:
    """Parse a duration string like '2h 15m', '3d 1h', '45m' to total minutes."""
    total = 0
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


def _safe_get(task: dict, key: str, default: str = "") -> str:
    """Safely get a string value from task dict."""
    val = task.get(key, default)
    return str(val) if val is not None else default


# ──────────────────────────────────────────
# Data collection functions
# ──────────────────────────────────────────


def collect_week_overview(start: str, end: str) -> WeeklyOverview:
    """Collect overview stats for a date range [start, end].

    Scans all tasks to count:
    - total_created: tasks created in the range
    - total_done: tasks completed (status=done, updated in range)
    - total_blocked: tasks that were blocked at any point in the range
    Also counts quick tasks and decisions.
    """
    all_tasks = list_tasks()
    overview = WeeklyOverview()

    for t in all_tasks:
        created = _safe_get(t, "created")
        updated = _safe_get(t, "updated")
        status = _safe_get(t, "status")

        if date_in_range(created, start, end):
            overview.total_created += 1

        if status == "done" and date_in_range(updated, start, end):
            overview.total_done += 1

        # Check if task was blocked during this range
        history = t.get("history", []) or []
        for entry in history:
            ts = entry.get("timestamp", "")
            detail = entry.get("detail", "")
            if date_in_range(ts, start, end) and "→ blocked" in detail:
                overview.total_blocked += 1
                break  # count each task only once

    # Quick tasks
    quick_entries = list_quick_log(date_prefix=None)
    for e in quick_entries:
        ts = e.get("timestamp", "")
        if date_in_range(ts, start, end):
            overview.total_quick += 1

    # Decisions
    all_decisions = list_decisions(limit=0)
    for d in all_decisions:
        ts = d.get("timestamp", "")
        if date_in_range(ts, start, end):
            overview.total_decisions += 1

    return overview


def collect_done_tasks(start: str, end: str) -> list[WeeklyTaskSummary]:
    """Collect tasks completed within the date range."""
    all_tasks = list_tasks()
    done: list[WeeklyTaskSummary] = []

    for t in all_tasks:
        status = _safe_get(t, "status")
        updated = _safe_get(t, "updated")
        created = _safe_get(t, "created")

        if status == "done" and date_in_range(updated, start, end):
            ts = WeeklyTaskSummary(
                id=_safe_get(t, "id"),
                title=_safe_get(t, "title"),
                status=status,
                priority=_safe_get(t, "priority"),
                task_type=_safe_get(t, "type"),
                created=created,
                updated=updated,
                duration_str=calc_duration(created, updated),
            )
            done.append(ts)

    # Sort by updated date
    done.sort(key=lambda x: x.updated)
    return done


def collect_type_distribution(start: str, end: str) -> dict[str, int]:
    """Dynamically scan task types for tasks active in the date range.

    Includes tasks created in range OR completed in range.
    """
    all_tasks = list_tasks()
    dist: dict[str, int] = {}

    for t in all_tasks:
        created = _safe_get(t, "created")
        updated = _safe_get(t, "updated")
        status = _safe_get(t, "status")

        in_range = False
        if date_in_range(created, start, end):
            in_range = True
        if status == "done" and date_in_range(updated, start, end):
            in_range = True

        if in_range:
            task_type = _safe_get(t, "type") or "unknown"
            dist[task_type] = dist.get(task_type, 0) + 1

    return dist


def collect_daily_efficiency(start: str, end: str) -> list[DayEfficiency]:
    """Compute per-day efficiency metrics for the week."""
    all_tasks = list_tasks()
    all_decisions = list_decisions(limit=0)

    # Generate all dates in range
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    days: list[str] = []
    current = start_dt
    while current <= end_dt:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    # Pre-compute per-day counts
    done_by_day: dict[str, int] = {d: 0 for d in days}
    created_by_day: dict[str, int] = {d: 0 for d in days}
    decisions_by_day: dict[str, int] = {d: 0 for d in days}

    for t in all_tasks:
        created = _safe_get(t, "created")[:10]
        updated = _safe_get(t, "updated")[:10]
        status = _safe_get(t, "status")

        if created in created_by_day:
            created_by_day[created] += 1
        if status == "done" and updated in done_by_day:
            done_by_day[updated] += 1

    for d in all_decisions:
        ts = d.get("timestamp", "")[:10]
        if ts in decisions_by_day:
            decisions_by_day[ts] += 1

    return [
        DayEfficiency(
            date=day,
            done_count=done_by_day[day],
            created_count=created_by_day[day],
            decisions_count=decisions_by_day[day],
        )
        for day in days
    ]


def collect_blocked_tasks(start: str, end: str) -> list[dict]:
    """Collect tasks that are currently blocked or were blocked during the range."""
    all_tasks = list_tasks()
    blocked: list[dict] = []

    for t in all_tasks:
        status = _safe_get(t, "status")
        task_id = _safe_get(t, "id")
        title = _safe_get(t, "title")

        # Currently blocked
        if status == "blocked":
            reason = _extract_block_reason(t)
            blocked.append({
                "id": task_id,
                "title": title,
                "reason": reason,
                "current": True,
            })
            continue

        # Was blocked during the range (but recovered)
        history = t.get("history", []) or []
        was_blocked = False
        for entry in history:
            ts = entry.get("timestamp", "")
            detail = entry.get("detail", "")
            if date_in_range(ts, start, end) and "→ blocked" in detail:
                was_blocked = True
                break
        if was_blocked:
            blocked.append({
                "id": task_id,
                "title": title,
                "reason": "(已恢复)",
                "current": False,
            })

    return blocked


def _extract_block_reason(task: dict) -> str:
    """Extract block reason from task history."""
    history = task.get("history", []) or []
    for entry in reversed(history):
        detail = entry.get("detail", "")
        if "blocked" in detail.lower():
            paren_start = detail.find("(")
            paren_end = detail.rfind(")")
            if paren_start != -1 and paren_end > paren_start:
                return detail[paren_start + 1:paren_end]
            return detail
    # Fallback: notes
    notes = task.get("context", {}).get("notes", "") or ""
    if notes:
        for line in reversed(notes.strip().split("\n")):
            if "block" in line.lower() or "阻塞" in line or "等待" in line:
                if line.startswith("[") and "]" in line:
                    return line[line.index("]") + 1:].strip()
                return line.strip()
    return ""


def find_cil_weekly(week_label: str) -> str:
    """Look for a CIL weekly report file matching the week.

    Checks common naming patterns:
    - CIL_WEEKLY_DIR / YYYY-Www.md
    - CIL_WEEKLY_DIR / YYYY-Www-*.md
    Returns the relative path if found, empty string otherwise.
    """
    if not CIL_WEEKLY_DIR.exists():
        return ""

    # Try exact match
    exact = CIL_WEEKLY_DIR / f"{week_label}.md"
    if exact.exists():
        try:
            return str(exact.relative_to(WORKSPACE_ROOT))
        except ValueError:
            return str(exact)

    # Try prefix match
    for f in sorted(CIL_WEEKLY_DIR.glob(f"{week_label}*.md")):
        try:
            return str(f.relative_to(WORKSPACE_ROOT))
        except ValueError:
            return str(f)

    return ""


# ──────────────────────────────────────────
# Report generation
# ──────────────────────────────────────────


def generate_weekly_report(year: int, week: int) -> WeeklyReport:
    """Aggregate all data into a WeeklyReport for the given ISO week."""
    wr = iso_week_range(year, week)
    start, end = wr.start_date, wr.end_date

    # This week overview
    this_week = collect_week_overview(start, end)

    # Last week overview
    prev_year, prev_week = previous_iso_week(year, week)
    prev_wr = iso_week_range(prev_year, prev_week)
    last_week = collect_week_overview(prev_wr.start_date, prev_wr.end_date)

    # Done tasks
    done_tasks = collect_done_tasks(start, end)

    # Type distribution (dynamic scan)
    type_dist = collect_type_distribution(start, end)

    # Daily efficiency
    daily_eff = collect_daily_efficiency(start, end)

    # Blocked / problem tasks
    blocked = collect_blocked_tasks(start, end)

    # CIL weekly reference
    cil_path = find_cil_weekly(wr.label)

    report = WeeklyReport(
        week_label=wr.label,
        start_date=start,
        end_date=end,
        this_week=this_week,
        last_week=last_week,
        done_tasks=done_tasks,
        type_distribution=type_dist,
        daily_efficiency=daily_eff,
        blocked_tasks=blocked,
        cil_weekly_path=cil_path,
    )

    return report


# ──────────────────────────────────────────
# Markdown rendering
# ──────────────────────────────────────────


def _change_indicator(this_val: int, last_val: int) -> str:
    """Return a change indicator string: ↑n / ↓n / →."""
    diff = this_val - last_val
    if diff > 0:
        return f"↑{diff}"
    elif diff < 0:
        return f"↓{abs(diff)}"
    else:
        return "→"


def render_weekly_markdown(report: WeeklyReport) -> str:
    """Render a WeeklyReport into Markdown."""
    lines: list[str] = []
    tw = report.this_week
    lw = report.last_week

    # ── Header ──
    lines.append(f"# 📊 周报 {report.week_label}")
    lines.append(f"> {report.start_date} ~ {report.end_date}")
    lines.append("")

    # ── 1. 总览对比表 ──
    lines.append("## 1. 📋 总览对比")
    lines.append("")
    lines.append("| 指标 | 本周 | 上周 | 变化 |")
    lines.append("|------|------|------|------|")
    lines.append(f"| 完成任务 | {tw.total_done} | {lw.total_done} | {_change_indicator(tw.total_done, lw.total_done)} |")
    lines.append(f"| 新建任务 | {tw.total_created} | {lw.total_created} | {_change_indicator(tw.total_created, lw.total_created)} |")
    lines.append(f"| 阻塞事件 | {tw.total_blocked} | {lw.total_blocked} | {_change_indicator(tw.total_blocked, lw.total_blocked)} |")
    lines.append(f"| 快速任务 | {tw.total_quick} | {lw.total_quick} | {_change_indicator(tw.total_quick, lw.total_quick)} |")
    lines.append(f"| 调度决策 | {tw.total_decisions} | {lw.total_decisions} | {_change_indicator(tw.total_decisions, lw.total_decisions)} |")
    lines.append("")

    # ── 2. 完成任务列表 ──
    lines.append("## 2. ✅ 完成任务")
    lines.append("")
    if report.done_tasks:
        lines.append(f"共完成 **{len(report.done_tasks)}** 项任务：")
        lines.append("")
        for t in report.done_tasks:
            dur = f" ({t.duration_str})" if t.duration_str else ""
            day = t.updated[:10] if t.updated else ""
            lines.append(f"- [{t.id}] **{t.title}** {t.priority}{dur} _{day}_")
    else:
        lines.append("_本周无完成任务_")
    lines.append("")

    # ── 3. 任务类型分布 ──
    lines.append("## 3. 📊 任务类型分布")
    lines.append("")
    if report.type_distribution:
        lines.append("| 类型 | 数量 | 占比 |")
        lines.append("|------|------|------|")
        total = sum(report.type_distribution.values())
        for ttype, count in sorted(report.type_distribution.items(), key=lambda x: -x[1]):
            pct = f"{count / total * 100:.0f}%" if total > 0 else "0%"
            lines.append(f"| {ttype} | {count} | {pct} |")
    else:
        lines.append("_无数据_")
    lines.append("")

    # ── 4. 效率趋势 ──
    lines.append("## 4. ⚡ 效率趋势（日粒度）")
    lines.append("")
    if report.daily_efficiency:
        lines.append("| 日期 | 完成 | 新建 | 决策 |")
        lines.append("|------|------|------|------|")
        for de in report.daily_efficiency:
            weekday = _weekday_name(de.date)
            lines.append(f"| {de.date} ({weekday}) | {de.done_count} | {de.created_count} | {de.decisions_count} |")
        # Summary row
        total_done = sum(d.done_count for d in report.daily_efficiency)
        total_created = sum(d.created_count for d in report.daily_efficiency)
        total_decisions = sum(d.decisions_count for d in report.daily_efficiency)
        lines.append(f"| **合计** | **{total_done}** | **{total_created}** | **{total_decisions}** |")
    else:
        lines.append("_无数据_")
    lines.append("")

    # ── 5. 问题汇总 ──
    lines.append("## 5. 🚨 问题汇总")
    lines.append("")
    has_issues = False
    current_blocked = [b for b in report.blocked_tasks if b.get("current")]
    recovered_blocked = [b for b in report.blocked_tasks if not b.get("current")]

    if current_blocked:
        has_issues = True
        lines.append("**当前阻塞:**")
        for b in current_blocked:
            reason = f" — {b['reason']}" if b.get("reason") else ""
            lines.append(f"- [{b['id']}] {b['title']}{reason}")
        lines.append("")

    if recovered_blocked:
        has_issues = True
        lines.append(f"**本周阻塞后恢复:** {len(recovered_blocked)} 项")
        for b in recovered_blocked[:5]:
            lines.append(f"- [{b['id']}] {b['title']}")
        if len(recovered_blocked) > 5:
            lines.append(f"- _...及其他 {len(recovered_blocked) - 5} 项_")
        lines.append("")

    if not has_issues:
        lines.append("_本周无异常_")
        lines.append("")

    # ── 6. CIL 周报引用 ──
    if report.cil_weekly_path:
        lines.append("## 6. 🔗 CIL 周报")
        lines.append("")
        lines.append(f"- [{report.cil_weekly_path}]({report.cil_weekly_path})")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"_由 report_weekly.py 自动生成 | {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines) + "\n"


def _weekday_name(date_str: str) -> str:
    """Return Chinese weekday name for a date string."""
    names = ["一", "二", "三", "四", "五", "六", "日"]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"周{names[dt.weekday()]}"
    except (ValueError, IndexError):
        return ""


# ──────────────────────────────────────────
# Notification
# ──────────────────────────────────────────


def _build_notification_summary(report: WeeklyReport) -> str:
    """Build a ≤10 line notification summary."""
    lines: list[str] = []
    tw = report.this_week
    lines.append(f"📊 周报 {report.week_label} ({report.start_date} ~ {report.end_date})")
    lines.append(f"完成: {tw.total_done} | 新建: {tw.total_created} | 快速: {tw.total_quick}")
    if tw.total_blocked > 0:
        lines.append(f"🚨 阻塞事件: {tw.total_blocked}")
    lines.append(f"调度决策: {tw.total_decisions}")
    # Comparison
    lw = report.last_week
    if lw.total_done > 0 or tw.total_done > 0:
        lines.append(f"vs 上周: 完成 {lw.total_done}→{tw.total_done}, 新建 {lw.total_created}→{tw.total_created}")
    if report.done_tasks:
        top3 = ", ".join(t.id for t in report.done_tasks[:3])
        lines.append(f"✅ {top3}")
    if report.type_distribution:
        top_type = max(report.type_distribution.items(), key=lambda x: x[1])
        lines.append(f"📊 主要类型: {top_type[0]} ({top_type[1]})")
    return "\n".join(lines[:10])


def send_notification(report: WeeklyReport) -> bool:
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
        prog="report_weekly.py",
        description="生成调度器周报",
    )
    parser.add_argument(
        "--week",
        default=None,
        help="ISO 周标签 YYYY-Www (默认: 上一周，即周一运行时回顾刚结束的一周)",
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

    # Determine week
    if args.week:
        try:
            year, week = parse_week_label(args.week)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Default: previous week (since this runs on Monday morning)
        year, week = current_iso_week()
        year, week = previous_iso_week(year, week)

    # Generate report
    report = generate_weekly_report(year, week)
    markdown = render_weekly_markdown(report)

    # Output
    if args.dry_run:
        print(markdown)
    else:
        report_path = REPORTS_DIR / f"{report.week_label}.md"
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
