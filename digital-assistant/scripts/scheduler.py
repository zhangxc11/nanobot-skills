#!/usr/bin/env python3
"""
scheduler.py - Digital Assistant Task Scheduler

Stateless scheduler that produces spawn instructions for a dispatcher session.
All state is persisted in REGISTRY (task YAML files); the scheduler itself
holds no cross-invocation state.

Design constraints (user-confirmed 2026-03-30):
  1. Scheduler is lightweight — only makes decisions (read REGISTRY → sort → output spawn).
  2. Per-invocation cap — dispatch at most MAX_DISPATCH_PER_RUN (3) new tasks.
  3. Workers are spawned as subagents by the dispatcher session.
  4. When a subagent completes, the framework automatically sends a
     [Subagent Result Notification] back to the dispatcher, triggering
     the next scheduling round.

Architecture:
    飞书 Session / CLI  ──┐
                          ├── trigger_scheduler.py ──► Dispatcher Session (fixed)
    Cron (30min fallback) ┘                                │
                                                           ├── spawn worker 1 (subagent)
                                                           ├── spawn worker 2 (subagent)
                                                           └── spawn worker 3 (subagent)
                                                           ↑
                                                    [Subagent Result Notification]
                                                    (framework auto-callback)

Usage (CLI — mainly for testing/debugging):
    python3 skills/digital-assistant/scripts/scheduler.py run [--parent SESSION_ID]
    python3 skills/digital-assistant/scripts/scheduler.py status
    python3 skills/digital-assistant/scripts/scheduler.py dry-run [--parent SESSION_ID]
"""

SCHEDULER_VERSION = '1.1.0'

import argparse
import glob
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure scripts/ is on sys.path for brain_manager import
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import brain_manager as bm
import rule_loader

# ──────────────────────────────────────────
# Constants
# ──────────────────────────────────────────

WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
MAX_CONCURRENT_EXECUTING = 3   # Max tasks in 'executing' state at any time (API rate limit)
MAX_DISPATCH_PER_RUN = 3       # Max NEW tasks to dispatch per scheduler invocation
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# ── Multi-role orchestration constants ──
LEGACY_MODE = os.environ.get("LEGACY_MODE", "") == "1"
MAX_ORCHESTRATION_ITERATIONS = 5      # Total iteration cap for developer↔tester loop
MAX_SAME_ROLE_CONSECUTIVE = 2         # Max consecutive partial dispatches of same role
REPORTS_DIR = WORKSPACE / "data" / "brain" / "reports"

FEISHU_NOTIFY_RECIPIENT = "ou_2fba93da1d059fd2520c2f385743f175"

REPORT_SCHEMA = {
    "required": ["task_id", "role", "verdict", "summary"],
    "optional": ["issues", "files_changed"],
    "valid_roles": ["developer", "tester", "architect"],
    "valid_verdicts": ["pass", "fail", "blocked", "partial"],
}

# ──────────────────────────────────────────
# Verification requirements by task category
# ──────────────────────────────────────────

VERIFICATION_GUIDANCE = {
    "backend_script": {
        "label": "纯后端/脚本",
        "requirements": [
            "单元测试覆盖核心逻辑",
            "集成测试验证端到端流程",
            "实际运行脚本确认输出正确",
        ],
    },
    "api_interface": {
        "label": "API 接口",
        "requirements": [
            "单元测试覆盖核心逻辑",
            "实际调用 API 验证请求/响应格式",
            "错误码和边界条件测试",
        ],
    },
    "web_frontend": {
        "label": "Web 前端/UI",
        "requirements": [
            "单元测试覆盖逻辑层",
            "**浏览器实际打开页面验证**（必须，不能只靠 mock）",
            "截图作为验收证据",
            "使用 browser skill (Playwright) 进行自动化验证",
        ],
    },
    "feishu_integration": {
        "label": "飞书集成",
        "requirements": [
            "实际发送消息/卡片验证",
            "确认消息格式和内容正确",
            "截图或消息 ID 作为验收证据",
        ],
    },
    "data_processing": {
        "label": "数据处理",
        "requirements": [
            "用真实数据（非 mock）验证处理结果",
            "边界条件测试（空数据、大数据量）",
            "输出格式验证",
        ],
    },
}


def detect_task_category(task: dict) -> str:
    """Detect task verification category based on title/description/template keywords."""
    text = f"{task.get('title', '')} {task.get('description', '')}".lower()

    if any(kw in text for kw in ["前端", "ui", "页面", "web", "html", "css", "browser", "界面", "dashboard"]):
        return "web_frontend"
    if any(kw in text for kw in ["飞书", "feishu", "lark", "消息卡片", "messenger"]):
        return "feishu_integration"
    if any(kw in text for kw in ["api", "接口", "endpoint", "http", "rest"]):
        return "api_interface"
    if any(kw in text for kw in ["数据", "data", "etl", "pipeline", "解析", "parse"]):
        return "data_processing"
    return "backend_script"


# ──────────────────────────────────────────
# Dev environment test detection
# ──────────────────────────────────────────

DEV_TEST_KEYWORDS = [
    "nanobot", "gateway", "worker", "webserver", "web-chat", "webchat",
    "scheduler", "dispatcher", "brain_manager", "trigger_scheduler",
    "feishu", "飞书", "cron", "skill", "agent",
]

DEV_ENV_GUIDANCE = """
### Dev 环境实测要求

⚠️ 本任务涉及 nanobot 核心代码变更，**必须在 dev 环境实测通过后才能提交 review**。

**Dev 环境信息**:
- Dev webserver: `http://localhost:9081`
- Dev worker: 端口 `9082`
- Dev workdir: `~/.nanobot/dev-workdir/`
- 启动 dev: `~/.nanobot/workspace/scripts/nanobot-svc.sh dev start`

**实测步骤**:
1. 在 dev 环境部署变更
2. 通过 dev 端口实际调用/验证功能
3. 记录实测结果（命令、输出、截图）到任务 notes 中
4. 确认无回归后再提交 review

> ❌ 仅靠 pytest/单元测试 **不能** 替代 dev 环境实测。
""".strip()


def needs_dev_test(task: dict) -> bool:
    """Detect if task involves nanobot core code changes requiring dev env testing."""
    text = f"{task.get('title', '')} {task.get('description', '')}".lower()
    return any(kw in text for kw in DEV_TEST_KEYWORDS)


# ──────────────────────────────────────────
# Timeout recovery
# ──────────────────────────────────────────

EXECUTING_TIMEOUT_MINUTES = 60  # Task executing > 60 min is considered stale
MAX_TIMEOUT_RECOVERY_COUNT = 3  # After 3 recoveries, transition to blocked instead


def check_stale_executing_tasks() -> list[dict]:
    """Check for tasks stuck in 'executing' state beyond timeout.

    Returns list of stale tasks with info for recovery action.
    """
    from datetime import timedelta

    stale_tasks = []
    now = datetime.now().astimezone()
    timeout_delta = timedelta(minutes=EXECUTING_TIMEOUT_MINUTES)

    for task in bm.list_tasks(status_filter={"executing"}):
        # Find when task entered 'executing' — look at last status_change to executing in history
        entered_executing_at = None
        for entry in reversed(task.get("history", [])):
            detail = entry.get("detail", "")
            if entry.get("action") == "status_change" and "→ executing" in detail:
                try:
                    entered_executing_at = datetime.fromisoformat(entry["timestamp"])
                except (ValueError, KeyError):
                    pass
                break

        if entered_executing_at is None:
            # Fallback: use 'updated' timestamp
            try:
                entered_executing_at = datetime.fromisoformat(task.get("updated", ""))
            except (ValueError, KeyError):
                continue

        # Ensure timezone-aware comparison
        if entered_executing_at.tzinfo is None:
            entered_executing_at = entered_executing_at.astimezone()

        elapsed = now - entered_executing_at
        if elapsed > timeout_delta:
            # Count previous timeout recoveries
            timeout_count = task.get("timeout_count", 0)
            stale_tasks.append({
                "task_id": task["id"],
                "title": task.get("title", ""),
                "priority": task.get("priority", ""),
                "entered_executing_at": entered_executing_at.isoformat(),
                "elapsed_minutes": int(elapsed.total_seconds() / 60),
                "timeout_count": timeout_count,
            })

    return stale_tasks


def recover_stale_tasks(stale_tasks: list[dict], dry_run: bool = False) -> list[dict]:
    """Recover stale executing tasks by transitioning them back to queued.

    If a task has been recovered MAX_TIMEOUT_RECOVERY_COUNT times, transition
    to 'blocked' instead (prevents infinite retry loops).

    Returns list of recovery actions taken.
    """
    recovered = []
    for stale in stale_tasks:
        task_id = stale["task_id"]
        elapsed = stale["elapsed_minutes"]
        timeout_count = stale["timeout_count"]

        if not dry_run:
            try:
                # Load task to update timeout_count
                task = bm.load_task(task_id)
                new_timeout_count = timeout_count + 1
                task["timeout_count"] = new_timeout_count
                bm.save_task(task)

                if new_timeout_count >= MAX_TIMEOUT_RECOVERY_COUNT:
                    # Too many recoveries → blocked for human intervention
                    bm.transition_task(
                        task_id, "blocked", force=True,
                        note=f"超时回收已达 {new_timeout_count} 次（阈值 {MAX_TIMEOUT_RECOVERY_COUNT}），"
                             f"转为 blocked 等待人工处理"
                    )
                    recovered.append({
                        "task_id": task_id,
                        "action": "blocked",
                        "elapsed_minutes": elapsed,
                        "timeout_count": new_timeout_count,
                    })
                else:
                    # Normal recovery → back to queued
                    bm.transition_task(
                        task_id, "queued", force=True,
                        note=f"超时回收: executing 已超 {elapsed} 分钟"
                             f"（阈值 {EXECUTING_TIMEOUT_MINUTES} 分钟，第 {new_timeout_count} 次回收）"
                    )
                    recovered.append({
                        "task_id": task_id,
                        "action": "queued",
                        "elapsed_minutes": elapsed,
                        "timeout_count": new_timeout_count,
                    })
            except Exception as exc:
                recovered.append({"task_id": task_id, "action": "error", "error": str(exc)})
        else:
            next_count = timeout_count + 1
            would_action = "would_block" if next_count >= MAX_TIMEOUT_RECOVERY_COUNT else "would_queue"
            recovered.append({
                "task_id": task_id,
                "action": would_action,
                "elapsed_minutes": elapsed,
                "timeout_count": next_count,
            })
    return recovered


# ──────────────────────────────────────────
# Core scheduling logic
# ──────────────────────────────────────────

def get_schedulable_tasks() -> list[dict]:
    """Get all tasks in 'queued' status, sorted by priority then creation time."""
    tasks = bm.list_tasks(status_filter={"queued"})
    return sort_by_priority(tasks)


def sort_by_priority(tasks: list[dict]) -> list[dict]:
    """Sort tasks: P0 first, then P1, then P2. Same priority by created time (oldest first)."""
    def sort_key(t: dict):
        prio = PRIORITY_ORDER.get(t.get("priority", "P2"), 2)
        created = t.get("created", "9999")
        return (prio, created)
    return sorted(tasks, key=sort_key)


def get_executing_count() -> int:
    """Count currently executing tasks."""
    return len(bm.list_tasks(status_filter={"executing"}))


def check_dependency(task: dict, all_tasks_map: dict[str, dict]) -> bool:
    """Check if task's dependencies are satisfied.

    Returns True if task is ready (no blocking deps).
    Dependencies stored in task['blocked_by'] as list of task IDs.
    """
    blocked_by = task.get("blocked_by", [])
    if not blocked_by:
        return True
    for dep_id in blocked_by:
        dep = all_tasks_map.get(dep_id)
        if dep is None:
            return False
        if dep.get("status") not in ("done", "cancelled", "dropped"):
            return False
    return True


def determine_available_slots() -> int:
    """How many more tasks can start executing (global concurrency limit)."""
    return max(0, MAX_CONCURRENT_EXECUTING - get_executing_count())


def is_quick_task(task: dict) -> bool:
    """Quick tasks bypass scheduling — handled inline by the session."""
    tpl = task.get("workgroup", {}).get("template", "") or task.get("template", "")
    return tpl == "quick"


# ──────────────────────────────────────────
# Phase 1: Design gate & doc triplet checks
# Feature flags for rollback support
# ──────────────────────────────────────────

DESIGN_GATE_ENABLED = os.environ.get("DESIGN_GATE_ENABLED", "1") != "0"
DOC_TRIPLET_CHECK_ENABLED = os.environ.get("DOC_TRIPLET_CHECK_ENABLED", "1") != "0"

# Cross-check Phase feature flags (MF-2: rollback support for cross-check remediation)
# Master flag: controls all cross-check Layer 1 validations
CROSS_CHECK_ENABLED = os.environ.get("CROSS_CHECK_ENABLED", "1") != "0"
# Test evidence validation for tester reports
TEST_EVIDENCE_CHECK_ENABLED = os.environ.get("TEST_EVIDENCE_CHECK_ENABLED", "1") != "0"

# Rollback strategies (documented per MF-2):
# Phase 1 rollback: DESIGN_GATE_ENABLED=0 + DOC_TRIPLET_CHECK_ENABLED=0
#   → removes design gate + doc triplet checks
# Phase 2 rollback: TEST_EVIDENCE_CHECK_ENABLED=0
#   → bypasses test_evidence validation for tester reports
# Phase 3 rollback: stop cron job (`crontab -l | grep -v cross_check_auditor | crontab -`)
#   → disables independent audit process
# Master rollback: CROSS_CHECK_ENABLED=0
#   → disables all cross-check Layer 1 validations at once

# Max times a developer can be sent back for doc completion before escalating
MAX_DOC_RETRY = 2


def check_design_gate(task: dict) -> tuple[bool, str]:
    """Check if a standard-dev task has design documentation before dispatch.

    Returns:
        (pass, reason) — pass=True means task can be dispatched to developer
    """
    if not DESIGN_GATE_ENABLED:
        return True, "design gate disabled via feature flag"

    template = task.get("template",
                task.get("workgroup", {}).get("template", "standard-dev"))

    # quick/cron-auto exempt
    if template in ("quick", "cron-auto"):
        return True, "quick/cron-auto exempt"

    task_id = task["id"]

    # Check 1: explicit design_ref or design_doc
    design_ref = task.get("design_ref") or task.get("design_doc")
    if design_ref:
        return True, f"has design ref: {design_ref}"

    # Check 2: architect report exists (been through architect flow)
    architect_reports = list(REPORTS_DIR.glob(f"{task_id}-architect-*.json"))
    if architect_reports:
        return True, "has architect report"

    # Check 3: task has orchestration history with architect role
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    has_architect = any(h.get("role") == "architect" for h in history)
    if has_architect:
        return True, "has architect in orchestration history"

    # Check 4: emergency exemption
    if task.get("emergency"):
        return True, "emergency exempt (must document post-hoc)"

    # Check 5: task explicitly marked as needs_design=False
    if task.get("needs_design") is False:
        return True, "explicitly marked needs_design=False"

    # Not passed
    return False, "no design document found — must go through architect/brain-trust first"


def check_doc_triplet(task: dict, report: dict | None = None) -> tuple[bool, list[str]]:
    """Check document triplet completeness for a task.

    Checks both report files_changed AND filesystem for DEVLOG/ARCHITECTURE/REQUIREMENTS.

    Returns:
        (complete, missing_docs) — complete=True means docs are sufficient
    """
    if not DOC_TRIPLET_CHECK_ENABLED:
        return True, []

    template = task.get("template",
                task.get("workgroup", {}).get("template", "standard-dev"))

    # quick/cron-auto don't require triplet
    if template in ("quick", "cron-auto"):
        return True, []

    # emergency tasks get doc_debt instead of blocking
    if task.get("emergency"):
        return True, []

    task_id = task["id"]
    has_devlog = False
    has_design = bool(task.get("design_ref") or task.get("design_doc"))

    # Scan reports for this task
    reports = list(REPORTS_DIR.glob(f"{task_id}-*.json"))
    for rp in reports:
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
            files = data.get("files_changed", [])
            for f in files:
                f_upper = f.upper()
                if "DEVLOG" in f_upper:
                    has_devlog = True
                if "ARCHITECTURE" in f_upper or "REQUIREMENTS" in f_upper:
                    has_design = True
        except Exception:
            pass

    # Also check current report if provided
    if report:
        files = report.get("files_changed", [])
        for f in files:
            f_upper = f.upper()
            if "DEVLOG" in f_upper:
                has_devlog = True
            if "ARCHITECTURE" in f_upper or "REQUIREMENTS" in f_upper:
                has_design = True

    # Filesystem fallback: scan common project directories
    project_dirs_to_check = set()
    all_files = []
    for rp in reports:
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
            all_files.extend(data.get("files_changed", []))
        except Exception:
            pass
    if report:
        all_files.extend(report.get("files_changed", []))

    for f in all_files:
        p = Path(f)
        # Look for typical project root indicators
        for parent in p.parents:
            if (parent / ".git").exists():
                project_dirs_to_check.add(parent)
                break

    for proj_dir in project_dirs_to_check:
        if not has_devlog:
            for pattern in ["DEVLOG.md", "devlog.md", "docs/DEVLOG.md"]:
                if (proj_dir / pattern).exists():
                    has_devlog = True
                    break
        if not has_design:
            for pattern in ["ARCHITECTURE.md", "REQUIREMENTS.md",
                            "docs/ARCHITECTURE.md", "docs/REQUIREMENTS.md"]:
                if (proj_dir / pattern).exists():
                    has_design = True
                    break

    missing = []
    if not has_devlog:
        missing.append("DEVLOG.md")
    if not has_design:
        missing.append("ARCHITECTURE.md or design_ref")

    return len(missing) == 0, missing


def _count_doc_retries(task: dict) -> int:
    """Count how many times a developer has been sent back for doc completion."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        reason = h.get("reason", "")
        if "missing docs" in reason or "文档三件套不完整" in reason:
            count += 1
    return count


def _count_evidence_retries(task: dict) -> int:
    """Count how many times a tester has been sent back for test evidence."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        reason = h.get("reason", "")
        if "no test_evidence" in reason or "missing test_evidence" in reason:
            count += 1
    return count


# ──────────────────────────────────────────
# Role determination
# ──────────────────────────────────────────

def get_initial_role(task: dict) -> str:
    """Determine the first Worker role for a task. Conservative strategy.

    - quick/cron-auto: developer (no architect needed)
    - batch-dev: architect (needs design/planning)
    - explicit architect flag: architect
    - standard-dev / long-task / others: developer (static rules suffice)
    """
    template = task.get("template",
                task.get("workgroup", {}).get("template", "standard-dev"))

    if template in ("quick", "cron-auto"):
        return "developer"

    if template == "batch-dev":
        return "architect"

    if task.get("architect") or task.get("needs_design"):
        return "architect"

    return "developer"


# ──────────────────────────────────────────
# Architect report validation
# ──────────────────────────────────────────

def validate_architect_report(report: dict) -> list[str]:
    """Validate architect report format and L0 completeness.

    Returns list of warning messages (empty = valid).
    Does NOT block on warnings — static rules provide L0 fallback.
    """
    warnings = []

    rule_verdict = report.get("rule_verdict", {})
    if not rule_verdict:
        warnings.append("architect report missing 'rule_verdict' field")
    else:
        worker_instructions = rule_verdict.get("worker_instructions", "")
        if not worker_instructions.strip():
            warnings.append("architect report has empty 'worker_instructions'")

    return warnings


# ──────────────────────────────────────────
# Multi-role orchestration
# ──────────────────────────────────────────

@dataclass
class Decision:
    action: str  # "promote_to_review", "dispatch_role", "mark_done", "mark_blocked"
    params: dict = field(default_factory=dict)
    reason: str = ""


def parse_worker_report(task_id: str, role: str = None) -> dict | None:
    """Parse the most recent worker report for a task.

    Args:
        task_id: Task ID to find report for
        role: Optional role filter (developer/tester)

    Returns:
        Parsed report dict or None if not found/invalid
    """
    if not REPORTS_DIR.exists():
        return None

    # Build glob pattern
    if role:
        pattern = f"{task_id}-{role}-*.json"
    else:
        pattern = f"{task_id}-*.json"

    # Find matching files
    report_files = list(REPORTS_DIR.glob(pattern))
    if not report_files:
        return None

    # Sort by mtime, take newest
    report_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    report_file = report_files[0]

    # Parse and validate
    try:
        with report_file.open("r", encoding="utf-8") as f:
            report = json.load(f)

        # Validate required fields
        for field in REPORT_SCHEMA["required"]:
            if field not in report:
                return None

        # Validate task_id matches
        if report.get("task_id") != task_id:
            return None

        # Validate role
        if report.get("role") not in REPORT_SCHEMA["valid_roles"]:
            return None

        # Validate verdict
        if report.get("verdict") not in REPORT_SCHEMA["valid_verdicts"]:
            return None

        return report

    except (json.JSONDecodeError, OSError):
        return None


def get_prior_context(task: dict, max_rounds: int = 2) -> str:
    """Extract prior context from orchestration history.

    Args:
        task: Task dict
        max_rounds: Maximum number of rounds to include

    Returns:
        Formatted context string
    """
    history = task.get("orchestration", {}).get("history", [])
    if not history:
        return ""

    # Take last max_rounds entries
    recent = history[-max_rounds:]

    lines = ["## Prior Iteration Context", ""]
    for i, entry in enumerate(recent, 1):
        role = entry.get("role", "unknown")
        verdict = entry.get("verdict", "unknown")
        summary = entry.get("summary", "")
        lines.append(f"### Round {i}: {role} → {verdict}")
        if summary:
            lines.append(summary)
        lines.append("")

    return "\n".join(lines)


def make_decision(report: dict | None, task: dict) -> Decision:
    """Make orchestration decision based on worker report.

    Args:
        report: Parsed worker report (or None if missing)
        task: Task dict

    Returns:
        Decision object with action and params
    """
    # Get orchestration state
    orch = task.get("orchestration", {})
    iteration = orch.get("iteration", 0)
    history = orch.get("history", [])

    # Cycle control: max iterations
    if iteration >= MAX_ORCHESTRATION_ITERATIONS:
        return Decision(
            action="mark_blocked",
            reason=f"max iterations reached ({MAX_ORCHESTRATION_ITERATIONS})"
        )

    # No report found
    if report is None:
        return Decision(
            action="mark_blocked",
            reason="no worker report found"
        )

    verdict = report.get("verdict")
    role = report.get("role")
    summary = report.get("summary", "")

    # Blocked verdict
    if verdict == "blocked":
        return Decision(
            action="mark_blocked",
            reason=f"{role} blocked: {summary}"
        )

    # Partial verdict — analyze whether to continue or block
    if verdict == "partial":
        # Cycle control: consecutive same role limit only applies to partial
        # (pass/fail have natural role transitions, only partial can loop indefinitely)
        if len(history) >= MAX_SAME_ROLE_CONSECUTIVE:
            recent_roles = [h.get("role") for h in history[-MAX_SAME_ROLE_CONSECUTIVE:]]
            if all(r == role for r in recent_roles):
                return Decision(
                    action="mark_blocked",
                    reason=f"max consecutive {role} partial iterations reached"
                )

        # Check if partial is due to task size (continuable) vs true blocker
        issues = report.get("issues", [])
        summary_lower = summary.lower()
        issue_text = " ".join(str(i) for i in issues).lower() if issues else ""

        # True blockers: need external intervention
        blocker_keywords = [
            "需要权限", "need permission", "api key", "需要人工", "human",
            "需要确认", "need approval", "access denied", "unauthorized",
            "需要用户", "外部依赖", "external dependency",
        ]
        is_true_blocker = any(kw in summary_lower or kw in issue_text for kw in blocker_keywords)

        if is_true_blocker:
            return Decision(
                action="mark_blocked",
                reason=f"{role} partial — true blocker: {summary}"
            )
        else:
            # Continuable: dispatch same role to finish remaining work
            return Decision(
                action="dispatch_role",
                params={
                    "role": role,
                    "context": f"Previous attempt was partial (incomplete). Continue from where you left off:\n{summary}"
                               + (f"\nRemaining issues: {json.dumps(issues)}" if issues else ""),
                },
                reason=f"{role} partial — continuable, dispatching same role to finish"
            )

    # Architect role
    if role == "architect":
        if verdict == "pass":
            # Validate architect report
            arch_warnings = validate_architect_report(report)
            for w in arch_warnings:
                # Log warnings but don't block — static rules provide L0 fallback
                pass  # warnings are informational

            rule_verdict = report.get("rule_verdict", {})
            worker_instructions = rule_verdict.get("worker_instructions", "").strip() if rule_verdict else ""

            if not worker_instructions:
                # Fallback: use static rules
                static = rule_loader.collect_rules(task)
                design = report.get("design_notes", report.get("summary", ""))
                context = f"{static}\n\n### Architect Notes\n{design}" if design else static
            else:
                # Normal path: use architect-provided instructions
                context_parts = [worker_instructions]
                design_notes = report.get("design_notes", "")
                if design_notes:
                    context_parts.append(f"### Architect 设计要点\n\n{design_notes}")
                context = "\n\n".join(context_parts)

            # Store in task.rule_context (R2-009)
            task["rule_context"] = worker_instructions or rule_loader.collect_rules(task)
            bm.save_task(task)

            return Decision(
                action="dispatch_role",
                params={"role": "developer", "context": context},
                reason="architect passed, dispatching developer with rule context"
            )
        elif verdict == "fail":
            return Decision(
                action="mark_blocked",
                reason=f"architect rejected: {summary}"
            )

    # Tester role
    if role == "tester":
        if verdict == "pass":
            # ── Cross-check Phase 2: test_evidence validation ──
            if CROSS_CHECK_ENABLED and TEST_EVIDENCE_CHECK_ENABLED:
                template = task.get("workgroup", {}).get("template", "") or task.get("template", "")
                if template not in ("quick", "cron-auto"):
                    evidence = report.get("test_evidence", []) if report else []
                    if not evidence:
                        retry_count = _count_evidence_retries(task)
                        if retry_count < MAX_DOC_RETRY:
                            return Decision(
                                action="dispatch_role",
                                params={
                                    "role": "tester",
                                    "context": (
                                        "⚠️ 测试报告缺少 test_evidence 字段。请补充测试执行证据后重新提交。\n\n"
                                        "报告中必须包含 test_evidence 字段，格式:\n"
                                        '"test_evidence": [\n'
                                        '    {"type": "command_output", "command": "pytest tests/", "result": "5 passed"},\n'
                                        '    {"type": "manual_test", "description": "验证功能", "result": "OK"}\n'
                                        "]\n\n"
                                        f"之前的测试结果: {summary}"
                                    ),
                                },
                                reason=f"tester passed but no test_evidence (retry {retry_count + 1}/{MAX_DOC_RETRY})"
                            )
                        else:
                            # Exceeded max retries, escalate to human review
                            return Decision(
                                action="promote_to_review",
                                params={"summary": f"⚠️ tester passed but no test_evidence after {retry_count} retries. {summary}"},
                                reason=f"tester passed but missing test_evidence, max retries exceeded, upgrading to manual review"
                            )

            # Check review level
            review_level = bm.determine_review_level(task)
            if review_level in ("L0", "L1"):
                # ── Phase 1: L0/L1 auto-approve must check docs ──
                doc_ok, doc_missing = check_doc_triplet(task, report)
                if not doc_ok:
                    return Decision(
                        action="promote_to_review",
                        params={"summary": f"⚠️ tester passed but docs incomplete ({', '.join(doc_missing)}). {summary}"},
                        reason=f"tester passed but docs missing {doc_missing}, upgrading to manual review"
                    )
                return Decision(
                    action="mark_done",
                    reason=f"tester passed, docs verified, review level {review_level}"
                )
            else:
                return Decision(
                    action="promote_to_review",
                    params={"summary": summary},
                    reason=f"tester passed, promoting to {review_level} review"
                )
        elif verdict == "fail":
            # Dispatch back to developer with fix context
            return Decision(
                action="dispatch_role",
                params={
                    "role": "developer",
                    "context": f"Tester found issues:\n{summary}\n\nIssues: {json.dumps(report.get('issues', []))}",
                },
                reason="tester failed, dispatching developer for fixes"
            )

    # Developer role
    if role == "developer":
        if verdict == "pass":
            # Check template
            template = task.get("workgroup", {}).get("template", "") or task.get("template", "")
            if template in ("quick", "cron-auto"):
                return Decision(
                    action="mark_done",
                    reason=f"developer passed, {template} template needs no tester"
                )
            else:
                # ── Phase 1: doc triplet check before dispatching tester ──
                doc_ok, doc_missing = check_doc_triplet(task, report)
                if not doc_ok:
                    retry_count = _count_doc_retries(task)
                    if retry_count >= MAX_DOC_RETRY:
                        # Escalate to manual review after max retries
                        return Decision(
                            action="promote_to_review",
                            params={"summary": f"⚠️ 文档三件套不完整 (已打回{retry_count}次，升级人工审核): 缺少 {', '.join(doc_missing)}. Developer summary: {summary}"},
                            reason=f"developer passed but missing docs after {retry_count} retries: {doc_missing} — escalating to manual review"
                        )
                    return Decision(
                        action="dispatch_role",
                        params={
                            "role": "developer",
                            "context": (
                                f"⚠️ 文档三件套不完整，缺少: {', '.join(doc_missing)}\n\n"
                                f"请补全以下文档后重新提交报告:\n"
                                f"1. DEVLOG.md — 记录开发过程、Phase、checkbox 任务清单\n"
                                f"2. ARCHITECTURE.md — 方案文档（如已有 design_ref 可跳过）\n\n"
                                f"之前的工作成果: {summary}"
                            ),
                        },
                        reason=f"developer passed but missing docs: {doc_missing} — dispatching back to complete docs"
                    )

                # Docs verified, dispatch tester
                return Decision(
                    action="dispatch_role",
                    params={"role": "tester", "context": f"Developer completed:\n{summary}"},
                    reason="developer passed, docs verified, dispatching tester"
                )
        elif verdict == "fail":
            # Count consecutive developer failures
            recent_devs = [h for h in history if h.get("role") == "developer"]
            if len(recent_devs) < MAX_SAME_ROLE_CONSECUTIVE:
                return Decision(
                    action="dispatch_role",
                    params={"role": "developer", "context": f"Previous attempt failed:\n{summary}"},
                    reason="developer failed, retrying"
                )
            else:
                return Decision(
                    action="mark_blocked",
                    reason=f"developer failed {len(recent_devs)} times, needs human intervention"
                )

    # Fallback
    return Decision(
        action="mark_blocked",
        reason=f"unknown role/verdict combination: {role}/{verdict}"
    )


def _send_feishu_notify(text: str, task_id: str = "") -> bool:
    """Send a feishu text notification to the configured recipient.

    Uses feishu_messenger.py send-text CLI. Failures are logged but never
    block the scheduler flow.

    Args:
        text: Notification text to send
        task_id: Associated task ID for notify-log

    Returns:
        True if message was sent successfully, False otherwise
    """
    import subprocess
    messenger_script = WORKSPACE / "skills" / "feishu-messenger" / "scripts" / "feishu_messenger.py"
    if not messenger_script.exists():
        print(f"[scheduler] feishu_messenger.py not found at {messenger_script}", flush=True)
        return False

    try:
        result = subprocess.run(
            [
                sys.executable, str(messenger_script),
                "send-text",
                "--to", FEISHU_NOTIFY_RECIPIENT,
                "--text", text,
                "--source", "scheduler",
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[scheduler] feishu notify sent for {task_id}", flush=True)
            return True
        else:
            print(f"[scheduler] feishu notify failed: {result.stderr}", flush=True)
            return False
    except Exception as exc:
        print(f"[scheduler] feishu notify exception: {exc}", flush=True)
        return False


def notify_task_state_change(task: dict, new_state: str, reason: str = "") -> bool:
    """Send feishu notification when a task enters review/blocked/done state.

    Args:
        task: Task dict
        new_state: The new state (review, blocked, done)
        reason: Additional context (e.g. blocked reason, review summary)

    Returns:
        True if notification was sent successfully
    """
    try:
        from feishu_notify import (
            format_review_notify,
            format_done_notify,
            format_error_notify,
            extract_short_id,
        )
    except ImportError:
        print("[scheduler] feishu_notify module not available, skipping notification", flush=True)
        return False

    task_id = task.get("id", "")

    if new_state == "review":
        # Try to load the latest pending review for rich formatting
        try:
            pending_reviews = bm.get_task_pending_reviews(task_id)
            if pending_reviews:
                text = format_review_notify(task, pending_reviews[-1])
            else:
                short_id = extract_short_id(task_id)
                title = task.get("title", "")
                text = (
                    f"📋 [{short_id}] {title} — 等待 Review\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Tester 已通过，请确认: {short_id} Go / NoGo"
                )
                if reason:
                    text += f"\n摘要: {reason}"
        except Exception:
            short_id = extract_short_id(task_id)
            title = task.get("title", "")
            text = f"📋 [{short_id}] {title} — 等待 Review\n请确认: {short_id} Go / NoGo"

    elif new_state == "blocked":
        text = format_error_notify(task, reason or "未知原因")

    elif new_state == "done":
        text = format_done_notify(task)

    else:
        return False

    return _send_feishu_notify(text, task_id)


def execute_decision(decision: Decision, task: dict) -> dict:
    """Execute an orchestration decision.

    Args:
        decision: Decision object
        task: Task dict

    Returns:
        Result dict with ok, action, and optional spawn_instruction
    """
    task_id = task["id"]
    action = decision.action

    try:
        # Log decision
        bm.append_decision({
            "timestamp": bm.now_iso(),
            "task_id": task_id,
            "action": action,
            "reason": decision.reason,
            "params": decision.params,
        })

        if action == "promote_to_review":
            # Create review
            task.setdefault("review", {"pending_count": 0, "items": []})
            review_id = bm.next_review_id()
            review_level = bm.determine_review_level(task)
            review_roles = bm.get_review_roles(review_level, task)

            review = {
                "id": review_id,
                "task_id": task_id,
                "level": review_level,
                "roles": review_roles,
                "status": "pending",
                "created": bm.now_iso(),
                "summary": decision.params.get("summary", ""),
            }
            bm.save_review(review)

            # Update task
            task["review"]["pending_count"] = task["review"].get("pending_count", 0) + 1
            task["review"]["items"].append(review_id)
            bm.save_task(task)

            # Transition to review
            bm.transition_task(task_id, "review", note="orchestrator: tester passed, submitting review")

            # Notify user: task awaiting review
            notify_task_state_change(task, "review", reason=decision.params.get("summary", ""))

            return {"ok": True, "action": action, "review_id": review_id}

        elif action == "mark_done":
            bm.transition_task(task_id, "done", note=f"orchestrator: {decision.reason}")

            # Notify user: task completed
            notify_task_state_change(task, "done")

            return {"ok": True, "action": action}

        elif action == "mark_blocked":
            bm.transition_task(task_id, "blocked", note=f"orchestrator: {decision.reason}")

            # Notify user: task blocked, needs decision
            notify_task_state_change(task, "blocked", reason=decision.reason)

            return {"ok": True, "action": action, "reason": decision.reason}

        elif action == "dispatch_role":
            # Update orchestration state
            orch = task.get("orchestration", {})
            orch["iteration"] = orch.get("iteration", 0) + 1
            orch.setdefault("history", []).append({
                "role": decision.params.get("role"),
                "timestamp": bm.now_iso(),
                "context": decision.params.get("context", ""),
            })
            task["orchestration"] = orch
            bm.save_task(task)

            # Generate spawn instruction
            role = decision.params.get("role", "developer")
            prior_context = decision.params.get("context", "")
            spawn_instruction = generate_spawn_instruction_v2(task, role, prior_context)

            return {
                "ok": True,
                "action": action,
                "role": role,
                "spawn_instruction": spawn_instruction,
            }

        else:
            return {"ok": False, "error": f"unknown action: {action}"}

    except Exception as exc:
        return {"ok": False, "error": str(exc), "action": action}


def handle_worker_completion(task_id: str, role: str = None) -> dict:
    """Handle worker completion pipeline.

    Args:
        task_id: Task ID
        role: Optional expected role

    Returns:
        Result dict with decision info and optional spawn_instruction
    """
    try:
        # Load task
        task = bm.load_task(task_id)

        # Parse report
        report = parse_worker_report(task_id, role)

        # Auto-detect role from report if not specified
        if report and not role:
            role = report.get("role")

        # Make decision
        decision = make_decision(report, task)

        # Execute decision
        result = execute_decision(decision, task)

        # Add decision info to result
        result["decision"] = {
            "action": decision.action,
            "reason": decision.reason,
            "params": decision.params,
        }

        return result

    except Exception as exc:
        return {"ok": False, "error": str(exc), "task_id": task_id}


# ──────────────────────────────────────────
# Architect & Tester guidance templates
# ──────────────────────────────────────────

def _generate_architect_guidance() -> str:
    """Generate architect-specific mission guidance."""
    return """### Your Mission (Architect)

你是 Architect Worker，职责：规则裁决 + 方案设计（如需要）。

**Step 1: 识别项目上下文**
- 从任务描述中识别涉及的项目/仓库
- 读取规则文件: skills/digital-assistant/rules/ 下的 Markdown 文件

**Step 2: 规则裁决（必做）**
- 读取 global.md（L0，始终适用）
- 读取对应项目的规则文件（如 nanobot.md）
- 读取对应任务类型的规则文件（如 standard-dev.md）
- 裁决每条规则的适用性：
  - L0: 项目匹配就适用，不可裁剪
  - L1: 默认适用，可根据任务范围判断
  - L2: 根据任务复杂度决定是否推荐

**Step 3: 方案设计（可选，仅复杂任务）**
- 复杂任务：输出设计要点、技术方案、风险评估
- 简单任务：跳过

**Step 4: 输出报告**
报告 JSON 中包含：
- rule_verdict.worker_instructions: 渲染好的规则文本（按 MUST/REQUIRED/RECOMMENDED 分组）
- design_notes: 方案设计要点（可为空字符串）
- suggested_rules: 建议新增的规则（可选，如发现规则库未覆盖的场景）
"""


def _generate_tester_guidance(task: dict) -> str:
    """Generate tester-specific mission guidance with observable rule audit.

    Includes auditable rule subset: code scope, commit format, docs, test coverage.
    Process rules (env, branch) are validated by Dispatcher, not Tester.
    """
    return """### Your Mission (Tester)

1. Review the implementation
2. Run all tests and verify they pass
3. Perform manual testing if needed
4. Check for edge cases and potential issues

**规则审查（可观测规则子集）：**
验证 Developer 实现是否遵守以下可观测规则：
- **代码变更范围**: 只修改与任务相关的文件，无不相关的改动
- **Commit 格式**: 每个 commit 是有意义的独立单元
- **文档更新**: 必要的文档是否按需更新
- **测试覆盖**: 验收基于真实执行结果，非纯 mock

**测试证据要求（MUST）：**
报告中必须包含 `test_evidence` 字段，记录实际执行的测试及结果：
```json
"test_evidence": [
    {"type": "command_output", "command": "pytest tests/", "result": "5 passed"},
    {"type": "manual_test", "description": "验证飞书通知发送", "result": "OK"}
]
```
⚠️ 无 test_evidence 的报告会被打回。每项 evidence 必须基于真实执行。

> 注意：过程性规则（如使用哪个环境、哪个分支）由 Dispatcher 校验，你不需要检查。
> 如果发现规则违反，在报告 issues 中标注。
"""


# ──────────────────────────────────────────
# Worker prompt generation
# ──────────────────────────────────────────

def generate_worker_prompt_v2(task: dict, role: str = "developer", prior_context: str = "") -> str:
    """Generate worker prompt for multi-role orchestration (v2).

    Workers are de-statified: they only write a JSON report file.
    No brain_manager calls, no status management.

    Args:
        task: Task dict
        role: Worker role (developer/tester)
        prior_context: Context from previous iterations

    Returns:
        Worker prompt string
    """
    task_id = task["id"]
    title = task.get("title", "")
    desc = task.get("description", "")
    priority = task.get("priority", "P2")
    template = task.get("workgroup", {}).get("template", "") or task.get("template", "standard-dev")

    # Generate report filename with timestamp
    timestamp = int(time.time())
    report_path = f"{WORKSPACE}/data/brain/reports/{task_id}-{role}-{timestamp}.json"

    lines = [
        f"## Task Execution: {role.title()} Role",
        "",
        f"**Task ID**: {task_id}",
        f"**Title**: {title}",
        f"**Priority**: {priority}",
        f"**Template**: {template}",
        f"**Your Role**: {role}",
        "",
    ]

    if desc:
        lines += ["### Task Description", desc, ""]

    # ── Static rule injection (all roles) ──
    static_rules = rule_loader.collect_rules(task)
    if static_rules:
        lines += [static_rules, ""]

    # ── Architect dynamic context (injected via prior_context after static rules) ──
    if prior_context:
        lines += [prior_context, ""]

    # Role-specific guidance
    if role == "architect":
        lines += [_generate_architect_guidance(), ""]
    elif role == "developer":
        lines += [
            "### Your Mission (Developer)",
            "",
        ]
        # Add design doc references if available
        design_ref = task.get("design_ref") or task.get("design_doc")
        if design_ref:
            lines += [
                f"1. Read the design documents first:",
                f"   - Design doc: `{design_ref}`",
                "2. Implement the required functionality",
                "3. Write tests to verify your implementation",
                "4. Run tests and ensure they pass",
                "5. Document any important decisions or changes",
                "",
            ]
        else:
            lines += [
                "1. Implement the required functionality",
                "2. Write tests to verify your implementation",
                "3. Run tests and ensure they pass",
                "4. Document any important decisions or changes",
                "",
            ]
        # ── Phase 1: hardcode doc triplet requirement in worker prompt ──
        if template not in ("quick", "cron-auto"):
            lines += [
                "**📋 文档三件套（MUST — 不写不算完成）**:",
                "- **DEVLOG.md**: 开发日志，记录 Phase、checkbox 任务清单、关键决策",
                "- **ARCHITECTURE.md**: 方案文档（如任务有 design_ref 则可跳过）",
                "- **REQUIREMENTS.md**: 需求文档（如任务描述已充分则可跳过）",
                "文档路径: 项目根目录或 task 关联目录",
                "⚠️ 调度器会检查文档完整性，缺少文档的报告会被打回。",
                "",
                "**Git Commit 规范**:",
                "- commit message 必须包含 Task ID，格式: `feat(task_id): 描述`",
                "- 例如: `feat(T-20260401-003): add design gate check`",
                "",
            ]
    elif role == "tester":
        lines += [_generate_tester_guidance(task), ""]

    # Report template
    lines += [
        "### Report Submission",
        "",
        "When you complete your work, write a JSON report to:",
        f"**{report_path}**",
        "",
        "Report format:",
        "```json",
        "{",
        f'  "task_id": "{task_id}",',
        f'  "role": "{role}",',
        '  "verdict": "pass|fail|blocked|partial",',
        '  "summary": "Free text describing what was done and key findings",',
        '  "issues": [{"description": "issue description"}],',
        '  "files_changed": ["path/to/file"]',
        "}",
        "```",
        "",
        "**Verdict meanings:**",
        "- `pass`: Work completed successfully, ready for next step",
        "- `fail`: Work attempted but issues found, needs rework",
        "- `blocked`: Cannot proceed due to external dependency",
        "- `partial`: Some work done but incomplete (requires human review)",
        "",
        "After writing the report, return a brief summary of your work.",
        "",
        "⚠️ **重要：无论任务是否完成，你都必须在工作结束前写入报告文件。**",
        "如果你发现自己已经执行了很多步骤但还未完成，请立即写入一份 partial 报告：",
        "- verdict: \"partial\"（表示未完成）",
        "- summary: 说明已完成的部分和未完成的部分",
        "- issues: 列出遇到的问题",
        "",
        "报告文件是调度器了解你工作结果的唯一渠道，不写报告 = 调度器无法继续流程。",
    ]

    return "\n".join(lines)


def _generate_worker_prompt_legacy(task: dict) -> str:
    """Legacy worker prompt (original implementation).

    Used when LEGACY_MODE=1 environment variable is set.
    """
    task_id = task["id"]
    title = task.get("title", "")
    desc = task.get("description", "")
    priority = task.get("priority", "P2")
    template = task.get("workgroup", {}).get("template", "") or task.get("template", "standard-dev")

    review_level = bm.determine_review_level(task)
    review_roles = bm.get_review_roles(review_level, task)
    category = detect_task_category(task)
    verification = VERIFICATION_GUIDANCE.get(category, VERIFICATION_GUIDANCE["backend_script"])

    lines = [
        "## 任务执行指令",
        "",
        f"**任务 ID**: {task_id}",
        f"**标题**: {title}",
        f"**优先级**: {priority}",
        f"**工作组模板**: {template}",
        f"**Review 级别**: {review_level}",
        "",
    ]

    bm_cmd = "python3 skills/digital-assistant/scripts/brain_manager.py"

    if desc:
        lines += ["### 任务描述", desc, ""]

    # ── Template guidance ──
    lines.append("### 执行指引")
    if template == "quick":
        lines += [
            "快速任务，直接执行。",
            "完成后: `brain_manager.py quick log --title ... --result ...`",
            "",
        ]
    elif template == "standard-dev":
        lines += [
            "遵循 dev-workflow 流程：",
            "1. 读取相关文件，理解上下文",
            "2. 设计方案（如需要）",
            "3. 编码实现",
            "4. 编写测试并运行",
            "5. 自验通过后提交 review",
            "",
        ]
    elif template == "batch-dev":
        lines += [
            "批量开发任务，使用 batch-dev-planner skill 编排。",
            "1. 盘点子需求  2. 依赖分析  3. 并行开发  4. 统一验收",
            "",
        ]
    elif template == "long-task":
        lines += [
            "长程问题，迭代推进：",
            "1. 明确边界  2. STATE.md 跟踪  3. 假设-验证  4. 收敛结论",
            "",
        ]
    elif template == "cron-auto":
        lines += ["定时自动任务，按预定逻辑执行。", ""]

    # ── Verification requirements (category-specific) ──
    lines += [
        f"### 验收要求（{verification['label']}）",
        "",
        "完成开发后，必须满足以下验收标准：",
    ]
    for req in verification["requirements"]:
        lines.append(f"- {req}")
    lines += [
        "",
        "> ⚠️ 纯 mock 测试不能算验收通过。涉及 Web 前端的任务必须浏览器实测+截图。",
        "",
    ]

    # ── Dev environment test requirement (conditional) ──
    if needs_dev_test(task):
        lines += [DEV_ENV_GUIDANCE, ""]

    # ── Evidence requirements ──
    lines += [
        "### 验收证据要求",
        "",
        "完成任务后，必须在任务 notes 中记录以下证据：",
        "1. **执行记录**: 实际运行的命令和关键输出",
        "2. **测试结果**: pytest/测试运行的通过数和覆盖率",
        "3. **实测证据**: dev 环境调用记录 / 浏览器截图 / 飞书消息截图（按任务类型）",
        "4. **变更文件清单**: 本次修改的文件列表",
        "",
        f"记录方式: `{bm_cmd} task update {task_id} --note \"验收证据: ...\"`",
        "",
    ]

    # ── Documentation update requirements ──
    lines += [
        "### 文档更新检查",
        "",
        "完成开发后，检查并更新以下文档（如适用）：",
        "- **MEMORY.md**: 如有重要决策/架构变更，更新 `~/.nanobot/workspace/MEMORY.md`",
        "- **SKILL.md**: 如修改了 skill 行为，更新对应 `SKILL.md`",
        "- **HISTORY.md**: 如完成了里程碑事件，追加到 `~/.nanobot/workspace/HISTORY.md`",
        "",
        "> 不需要更新时可跳过，但需要在 notes 中说明「已检查，无需更新」。",
        "",
    ]

    # ── Review requirements ──
    if review_level != "L0":
        lines.append("### Review 要求")
        if review_level == "L1":
            lines += [
                "- 自检：完成后对照 Checklist 自查",
                f"- `python3 skills/digital-assistant/scripts/brain_manager.py review checklist {task_id} --role code_reviewer`",
            ]
        elif review_level in ("L2", "L3"):
            lines += [
                f"- 独立 Review: {', '.join(review_roles)}",
                f"- `brain_manager.py review add {task_id} --summary <摘要> --prompt <提示>`",
                f"- `brain_manager.py task update {task_id} --status review`",
            ]
        lines.append("")

    # ── Status management (dynamic by review level) ──
    if review_level in ("L2", "L3"):
        lines += [
            "### 状态管理",
            f"⚠️ 本任务 Review 级别为 **{review_level}**，完成后 **必须** 提交 review，**不可** 直接标记 done。",
            f"- ✅ 提交 review: `{bm_cmd} task update {task_id} --status review`",
            f"- 遇阻塞: `{bm_cmd} task update {task_id} --status blocked --note \"原因\"`",
            f"- 更新 BRIEFING: `{bm_cmd} briefing update`",
            "",
            f"> 系统会拒绝 executing→done 的直接转换。必须经过 review 状态。",
        ]
    elif review_level == "L1":
        lines += [
            "### 状态管理",
            f"本任务 Review 级别为 L1（自检），完成自检后可直接标 done。",
            f"- 完成: `{bm_cmd} task update {task_id} --status done`",
            f"- 遇阻塞: `{bm_cmd} task update {task_id} --status blocked --note \"原因\"`",
            f"- 更新 BRIEFING: `{bm_cmd} briefing update`",
        ]
    else:  # L0
        lines += [
            "### 状态管理",
            f"- 完成: `{bm_cmd} task update {task_id} --status done`",
            f"- 遇阻塞: `{bm_cmd} task update {task_id} --status blocked --note \"原因\"`",
            f"- 更新 BRIEFING: `{bm_cmd} briefing update`",
        ]

    return "\n".join(lines)


def generate_worker_prompt(task: dict, role: str = "developer", prior_context: str = "") -> str:
    """Generate worker prompt (dispatcher to v2 or legacy).

    Args:
        task: Task dict
        role: Worker role (developer/tester) - ignored in legacy mode
        prior_context: Context from previous iterations - ignored in legacy mode

    Returns:
        Worker prompt string
    """
    if LEGACY_MODE:
        return _generate_worker_prompt_legacy(task)
    return generate_worker_prompt_v2(task, role, prior_context)


def generate_spawn_instruction_v2(task: dict, role: str, prior_context: str = "", parent_session_id: str = "") -> dict:
    """Generate spawn instruction for multi-role orchestration (v2).

    Args:
        task: Task dict
        role: Worker role (developer/tester)
        prior_context: Context from previous iterations
        parent_session_id: Parent session ID (informational)

    Returns:
        Spawn instruction dict
    """
    task_id = task["id"]
    title = task.get("title", "未命名任务")
    role_emoji = {"developer": "🔨", "tester": "🧪", "architect": "📐"}.get(role, "🔨")

    # Role-based iteration limits
    role_iterations = {"developer": 60, "tester": 30, "architect": 25}
    max_iterations = role_iterations.get(role, 60)

    return {
        "task_id": task_id,
        "task_prompt": generate_worker_prompt_v2(task, role, prior_context),
        "title": f"{role_emoji} {task_id}: {title[:30]} [{role}]",
        "template": task.get("workgroup", {}).get("template", ""),
        "priority": task.get("priority", "P2"),
        "role": role,
        "max_iterations": max_iterations,
    }


def generate_spawn_instruction(task: dict, parent_session_id: str = "", role: str = "developer") -> dict:
    """Generate spawn instruction dict for a task.

    Returns info needed by the dispatcher to spawn a worker subagent:
      task_id, task_prompt (full worker instructions), title, priority, template.

    The dispatcher session uses the nanobot `spawn` tool to create a subagent.
    When the subagent completes, the framework automatically sends a
    [Subagent Result Notification] back to the dispatcher session.

    Args:
        task: Task dict
        parent_session_id: Parent session ID for tracking (informational)
        role: Worker role (developer/tester) - used in v2 mode

    Returns:
        Spawn instruction dict
    """
    if LEGACY_MODE or not role:
        # Legacy mode: use old prompt generation
        task_id = task["id"]
        title = task.get("title", "未命名任务")

        return {
            "task_id": task_id,
            "task_prompt": generate_worker_prompt(task),
            "title": f"🔨 {task_id}: {title[:30]}",
            "template": task.get("workgroup", {}).get("template", ""),
            "priority": task.get("priority", "P2"),
        }

    return generate_spawn_instruction_v2(task, role, parent_session_id=parent_session_id)


def check_completed_tasks() -> list[dict]:
    """Tasks in 'review' status with pending reviews — may need follow-up."""
    needs_attention = []
    for task in bm.list_tasks(status_filter={"review"}):
        pending = bm.get_task_pending_reviews(task["id"])
        if pending:
            needs_attention.append({
                "task_id": task["id"],
                "title": task.get("title", ""),
                "pending_reviews": len(pending),
                "review_ids": [r["id"] for r in pending],
            })
    return needs_attention


# ──────────────────────────────────────────
# Report generation
# ──────────────────────────────────────────

def generate_schedule_report(
    queued_tasks: list[dict],
    dispatched: list[dict],
    skipped_dependency: list[dict],
    skipped_cap: list[dict],
    review_pending: list[dict],
    executing_count: int,
    errors: list[dict] | None = None,
    stale_recovered: list[dict] | None = None,
    auto_enqueued: list[dict] | None = None,
) -> dict:
    errors = errors or []
    stale_recovered = stale_recovered or []
    auto_enqueued = auto_enqueued or []
    return {
        "timestamp": bm.now_iso(),
        "summary": {
            "total_queued": len(queued_tasks),
            "dispatched": len(dispatched),
            "skipped_dependency": len(skipped_dependency),
            "skipped_cap": len(skipped_cap),
            "currently_executing": executing_count,
            "review_pending": len(review_pending),
            "errors": len(errors),
            "stale_recovered": len(stale_recovered),
            "auto_enqueued": len(auto_enqueued),
        },
        "dispatched_tasks": [
            {"task_id": d["task_id"], "priority": d.get("priority", "")}
            for d in dispatched
        ],
        "skipped_dependency_tasks": [
            {"task_id": t["id"], "blocked_by": t.get("blocked_by", [])}
            for t in skipped_dependency
        ],
        "skipped_cap_tasks": [
            {"task_id": t["id"], "priority": t.get("priority", "")}
            for t in skipped_cap
        ],
        "review_pending": review_pending,
        "errors": [
            {"task_id": e["task"]["id"], "error": e["error"]}
            for e in errors
        ],
        "stale_recovered": stale_recovered,
    }


# ──────────────────────────────────────────
# Auto-enqueue pending tasks
# ──────────────────────────────────────────

def auto_enqueue_pending_tasks(dry_run: bool = False) -> list[dict]:
    """Scan all 'pending' tasks and transition them to 'queued' if they have basic fields.

    Returns list of tasks that were enqueued.
    """
    pending_tasks = bm.list_tasks(status_filter={"pending"})
    enqueued = []
    for task in pending_tasks:
        tid = task.get("id", "")
        desc = task.get("description", "") or task.get("title", "")
        if not tid or not desc:
            print(f"[scheduler] skip pending task {tid}: missing id or description/title")
            continue
        if not dry_run:
            try:
                bm.transition_task(tid, "queued", note="auto-enqueue by scheduler")
                print(f"[scheduler] auto-enqueued pending task {tid}")
            except Exception as exc:
                print(f"[scheduler] failed to enqueue {tid}: {exc}")
                continue
        enqueued.append(task)
    if enqueued:
        print(f"[scheduler] auto-enqueued {len(enqueued)} pending task(s)")
    return enqueued


# ──────────────────────────────────────────
# Main scheduler entry point
# ──────────────────────────────────────────

def run_scheduler(
    dry_run: bool = False,
    parent_session_id: str = "",
) -> dict:
    """Stateless scheduler — one-shot decision maker.

    Reads REGISTRY → sorts → dispatches up to MAX_DISPATCH_PER_RUN tasks
    (also respecting MAX_CONCURRENT_EXECUTING global limit).

    Workers are spawned as subagents by the dispatcher session.
    When a subagent completes, the framework automatically sends a
    [Subagent Result Notification] back to the dispatcher, triggering
    the next scheduling round.

    Args:
        dry_run: If True, don't update REGISTRY
        parent_session_id: Parent session ID for tracking
    """
    # ── 0a. Auto-enqueue: pending → queued ──
    auto_enqueued = auto_enqueue_pending_tasks(dry_run=dry_run)

    # ── 0b. Timeout recovery (before scheduling new tasks) ──
    stale_tasks = check_stale_executing_tasks()
    stale_recovered = []
    if stale_tasks:
        stale_recovered = recover_stale_tasks(stale_tasks, dry_run=dry_run)

    # 1. Queued tasks sorted by priority (now includes auto-enqueued tasks)
    queued_tasks = get_schedulable_tasks()

    # Build task index for dependency checks
    all_tasks_map = {t["id"]: t for t in bm.list_tasks()}

    # 2. Determine capacity
    global_slots = determine_available_slots()
    per_run_cap = MAX_DISPATCH_PER_RUN
    effective_cap = min(global_slots, per_run_cap)

    executing_count = get_executing_count()
    dispatched: list[dict] = []
    skipped_dependency: list[dict] = []
    skipped_cap: list[dict] = []
    errors: list[dict] = []

    for task in queued_tasks:
        # Quick tasks bypass scheduling
        if is_quick_task(task):
            continue

        # 3. Dependency check
        if not check_dependency(task, all_tasks_map):
            skipped_dependency.append(task)
            continue

        # 4. Cap check (both per-run and global)
        if len(dispatched) >= effective_cap:
            skipped_cap.append(task)
            continue

        # 4.5 Design gate check (Phase 1)
        initial_role = get_initial_role(task)
        gate_pass, gate_reason = check_design_gate(task)
        if not gate_pass:
            # Force architect role instead of developer
            initial_role = "architect"

        # 5. Dispatch
        if not dry_run:
            try:
                note = "scheduler dispatch"
                if not gate_pass:
                    note = f"scheduler dispatch (architect gate: {gate_reason})"
                bm.transition_task(task["id"], "executing", note=note)
            except Exception as exc:
                errors.append({"task": task, "error": str(exc)})
                continue

        dispatched.append(generate_spawn_instruction(task, parent_session_id, role=initial_role))

    # 6. Review follow-up check
    review_pending = check_completed_tasks()

    # 7. Report
    report = generate_schedule_report(
        queued_tasks, dispatched, skipped_dependency, skipped_cap,
        review_pending, executing_count + len(dispatched), errors,
        stale_recovered=stale_recovered,
        auto_enqueued=auto_enqueued,
    )

    # Refresh BRIEFING
    if dispatched and not dry_run:
        try:
            bm.atomic_write(bm.BRIEFING_FILE, bm.generate_briefing())
        except Exception:
            pass

    # Generate Feishu notification text
    notification = None
    try:
        from feishu_notify import format_batch_summary
        # Enrich dispatched with title info for notification
        dispatched_for_notify = []
        for d in dispatched:
            tid = d.get("task_id", "")
            title = ""
            try:
                t = bm.load_task(tid)
                title = t.get("title", "")
            except Exception:
                pass
            dispatched_for_notify.append({
                "task_id": tid,
                "priority": d.get("priority", ""),
                "title": title,
            })
        notification = format_batch_summary(
            dispatched=dispatched_for_notify,
            review_pending=review_pending,
            errors=[
                {"task_id": e["task"]["id"], "error": e["error"]}
                for e in errors
            ],
        )
    except ImportError:
        pass

    result = {
        "ok": True,
        "spawn_instructions": dispatched,
        "report": report,
        "dry_run": dry_run,
    }
    if notification is not None:
        result["notification"] = notification

    return result


def get_status() -> dict:
    """Current scheduler status overview (read-only)."""
    all_tasks = bm.list_tasks()
    counts: dict[str, int] = {}
    for t in all_tasks:
        s = t.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    queued = sort_by_priority(bm.list_tasks(status_filter={"queued"}))
    executing = bm.list_tasks(status_filter={"executing"})
    review = bm.list_tasks(status_filter={"review"})

    return {
        "ok": True,
        "data": {
            "timestamp": bm.now_iso(),
            "task_counts": counts,
            "queued_tasks": [{"id": t["id"], "title": t.get("title", ""), "priority": t.get("priority", "")} for t in queued],
            "executing_tasks": [{"id": t["id"], "title": t.get("title", "")} for t in executing],
            "review_tasks": [{"id": t["id"], "title": t.get("title", "")} for t in review],
            "pending_reviews": len(bm.list_reviews(status_filter="pending")),
            "available_slots": determine_available_slots(),
            "max_concurrent": MAX_CONCURRENT_EXECUTING,
            "max_dispatch_per_run": MAX_DISPATCH_PER_RUN,
        },
    }


# ──────────────────────────────────────────
# Public introspection helpers
# ──────────────────────────────────────────

def get_scheduler_status() -> dict:
    """Return a lightweight status dict exposing key scheduler configuration.

    Returns:
        dict with keys:
          - version (str): current SCHEDULER_VERSION
          - max_concurrent (int): MAX_CONCURRENT_EXECUTING
          - max_iterations_developer (int): developer role's max_iterations
    """
    return {
        "version": SCHEDULER_VERSION,
        "max_concurrent": MAX_CONCURRENT_EXECUTING,
        "max_iterations_developer": 60,  # developer role iteration limit (see _build_spawn_instruction_v2)
    }


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Digital Assistant Task Scheduler")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Dispatch queued tasks")
    p_run.add_argument("--parent", default="", help="Parent session ID")

    p_dry = sub.add_parser("dry-run", help="Show what would be dispatched (no state changes)")
    p_dry.add_argument("--parent", default="", help="Parent session ID")

    sub.add_parser("status", help="Show current status")

    p_handle = sub.add_parser("handle-completion", help="Handle worker completion for a task")
    p_handle.add_argument("--task-id", required=False, help="Task ID")
    p_handle.add_argument("--auto-detect", action="store_true", help="Auto-detect from recent reports")
    p_handle.add_argument("--role", default=None, help="Expected worker role")

    args = parser.parse_args()

    if args.command == "run":
        result = run_scheduler(
            dry_run=False,
            parent_session_id=args.parent,
        )
    elif args.command == "dry-run":
        result = run_scheduler(
            dry_run=True,
            parent_session_id=args.parent,
        )
    elif args.command == "status":
        result = get_status()
    elif args.command == "handle-completion":
        if args.auto_detect:
            # Auto-detect from recent reports (last 5 minutes)
            if not REPORTS_DIR.exists():
                result = {"ok": False, "error": "Reports directory does not exist"}
            else:
                cutoff_time = time.time() - 300  # 5 minutes ago
                recent_reports = [
                    f for f in REPORTS_DIR.glob("*.json")
                    if f.stat().st_mtime > cutoff_time
                ]
                if not recent_reports:
                    result = {"ok": False, "error": "No recent reports found (last 5 minutes)"}
                else:
                    # Take the most recent
                    recent_reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    report_file = recent_reports[0]
                    # Extract task_id from filename: T-xxx-role-timestamp.json
                    match = re.match(r"(T-\d+)-", report_file.name)
                    if match:
                        task_id = match.group(1)
                        result = handle_worker_completion(task_id, args.role)
                    else:
                        result = {"ok": False, "error": f"Could not parse task_id from {report_file.name}"}
        elif args.task_id:
            result = handle_worker_completion(args.task_id, args.role)
        else:
            result = {"ok": False, "error": "Either --task-id or --auto-detect must be specified"}
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
