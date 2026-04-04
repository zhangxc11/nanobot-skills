#!/usr/bin/env python3
from __future__ import annotations
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

SCHEDULER_VERSION = '2.1.0'

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
MAX_ORCHESTRATION_ITERATIONS = 20     # Absolute ceiling — no task may exceed this

# PL-based iteration baselines (A-02)
_PL_BASE_ITERATIONS = {
    "PL0": 3,
    "PL1": 5,
    "PL2": 10,
    "PL3": 18,
}
MAX_SAME_ROLE_CONSECUTIVE = 2         # Max consecutive partial dispatches of same role
REPORTS_DIR = WORKSPACE / "data" / "brain" / "reports"

FEISHU_NOTIFY_RECIPIENT = "ou_2fba93da1d059fd2520c2f385743f175"

REPORT_SCHEMA = {
    "required": ["task_id", "role", "verdict", "summary"],
    "optional": ["issues", "files_changed"],
    "valid_roles": ["developer", "tester", "architect", "auditor", "architect_review", "code_review", "test_review", "retrospective"],
    "valid_verdicts": ["pass", "fail", "blocked", "partial"],
}

# ── Phase 2: Retrospective & Auditor constants ──
MAX_RETRO_RETRY = 1  # Max times retrospective can trigger a re-route before force-passing
BRAIN_DIR = WORKSPACE / "data" / "brain"
VALID_ROLES = frozenset({"developer", "tester", "architect", "auditor", "architect_review", "code_review", "test_review", "retrospective"})

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
    "doc_only": {
        "label": "📄 纯文档/调研/配置",
        "requirements": [
            "文档内容完整",
            "格式规范",
            "与任务描述匹配",
        ],
    },
}




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
TEST_EVIDENCE_ENABLED = os.environ.get("TEST_EVIDENCE_ENABLED", "1") != "0"

# ── Cross-check master flag (T-006) ──
CROSS_CHECK_ENABLED = os.environ.get("CROSS_CHECK_ENABLED", "1") != "0"
# Template confirmation (T-006)
TEMPLATE_CONFIRM_ENABLED = os.environ.get("TEMPLATE_CONFIRM_ENABLED", "1") != "0"
TEMPLATE_CONFIDENCE_THRESHOLD = float(os.environ.get("TEMPLATE_CONFIDENCE_THRESHOLD", "0.3"))

# Notification validation & irreversible operation confirmation (T-010)
NOTIFICATION_VALIDATE_ENABLED = os.environ.get("NOTIFICATION_VALIDATE_ENABLED", "1") != "0"
IRREVERSIBLE_CONFIRM_ENABLED = os.environ.get("IRREVERSIBLE_CONFIRM_ENABLED", "1") != "0"
IRREVERSIBLE_ACTIONS = frozenset({"cancel", "reject"})

# Max times a developer can be sent back for doc completion before escalating
MAX_DOC_RETRY = 2
# Max times a tester can be sent back for missing test_evidence before escalating
MAX_EVIDENCE_RETRY = 2
# Max times architect can be sent back for acceptance_plan before auto-generating fallback
MAX_ACCEPTANCE_PLAN_RETRY = 2
# Max times tester can be sent back for coverage before escalating to review
MAX_COVERAGE_RETRY = 2
# Max tester↔developer ping-pong round-trips before escalating (m-6 protection)
MAX_TESTER_DEVELOPER_PINGPONG = 3

# ── State machine feature flag ──
STATE_MACHINE_ENABLED = os.environ.get("STATE_MACHINE_ENABLED", "1") != "0"

# ── Flow Type Templates ──
FLOW_TEMPLATES = {
    "standard-dev": {
        "roles": ["architect", "architect_review", "developer", "code_review",
                   "tester", "test_review"],
        "description": "Standard development flow",
        "has_auditor": True,
        "has_retrospective": True,
    },
    "cron-auto": {
        "roles": ["developer"],
        "description": "Cron automatic task, single-step execution",
        "has_auditor": False,
        "has_retrospective": False,
    },
}


def resolve_flow_type(task: dict) -> str:
    """Read the task's flow type. No semantic guessing."""
    # 1. Explicit flow_type field
    ft = task.get("flow_type")
    if ft and ft in FLOW_TEMPLATES:
        return ft
    # 2. Backward compat: process_level → flow_type mapping
    pl = task.get("process_level")
    PL_TO_FLOW = {"PL0": "cron-auto", "PL1": "standard-dev", "PL2": "standard-dev", "PL3": "standard-dev"}
    if pl and pl in PL_TO_FLOW:
        return PL_TO_FLOW[pl]
    # 3. Default
    return "standard-dev"


# ── State transition table: (flow_type, current_role, verdict) → next_action ──
# next_action can be:
#   ("role", "xxx")       — dispatch to next role
#   ("done",)             — mark task done
#   ("review",)           — escalate to manual review
#   ("blocked",)          — mark task blocked
#   ("review_check",)     — check review_level to decide done vs review
#
# Gate checks (plan/e2e/coverage/doc) run BEFORE table lookup.
# If a gate fails, it returns a Decision directly (retry/escalate).

FLOW_TRANSITIONS = {
    # cron-auto: developer → framework_closeout
    ("cron-auto", "developer", "pass"):  ("framework_closeout",),
    ("cron-auto", "developer", "fail"):  ("retry", "developer"),

    # standard-dev: full flow ending at framework_closeout
    ("standard-dev", "architect", "pass"):           ("role", "architect_review"),
    ("standard-dev", "architect", "fail"):           ("blocked",),
    ("standard-dev", "architect_review", "pass"):    ("role", "developer"),
    ("standard-dev", "architect_review", "fail"):    ("role", "architect"),
    ("standard-dev", "developer", "pass"):           ("role", "code_review"),
    ("standard-dev", "developer", "fail"):           ("retry", "developer"),
    ("standard-dev", "code_review", "pass"):         ("role", "tester"),
    ("standard-dev", "code_review", "fail"):         ("role", "developer"),
    ("standard-dev", "tester", "pass"):              ("role", "test_review"),
    ("standard-dev", "tester", "fail"):              ("role", "developer"),
    ("standard-dev", "test_review", "pass"):         ("framework_closeout",),
    ("standard-dev", "test_review", "fail"):         ("role", "tester"),
}


# ──────────────────────────────────────────
# T-006: Template assignment cross-check
# ──────────────────────────────────────────

def confirm_template_assignment(task: dict) -> tuple[str, str, bool]:
    """二次确认模板分配是否合理。

    三层验证：
      Layer A: confidence 阈值 — match_template 返回的 confidence < threshold 则标记可疑
      Layer B: 规则交叉验证 — 用独立规则集对比当前模板
      Layer C: (未来) LLM 语义分类 — 高风险场景兜底

    Args:
        task: 任务字典

    Returns:
        (confirmed_template, reason, was_changed)
        - confirmed_template: 确认后的模板名
        - reason: 确认/变更原因
        - was_changed: 是否发生了模板变更
    """
    current_template = task.get("template",
                        task.get("workgroup", {}).get("template", "standard-dev"))

    if not TEMPLATE_CONFIRM_ENABLED or not CROSS_CHECK_ENABLED:
        return current_template, "template confirmation disabled", False

    title = task.get("title", "")
    desc = task.get("description", "")
    combined = (title + " " + desc).lower()

    # ── Layer A: Confidence check ──
    match_info = task.get("workgroup", {}).get("match_info", {})
    confidence = match_info.get("confidence", None)

    if confidence is None:
        # No stored match_info — re-compute
        try:
            confidence = bm.match_template(title, desc).get("confidence", 0.0)
        except Exception:
            confidence = 0.0

    low_confidence = confidence < TEMPLATE_CONFIDENCE_THRESHOLD

    # ── Layer B: 独立规则交叉验证 ──
    rule_suggestion = _rule_based_template_check(combined, current_template)

    # ── 决策逻辑 ──
    if not low_confidence and rule_suggestion == current_template:
        # 双重确认一致 → 通过
        return current_template, f"confirmed (confidence={confidence:.2f}, rule agrees)", False

    if not low_confidence and rule_suggestion != current_template:
        # confidence OK 但规则不同意 → 记录告警，保持原模板（保守策略）
        _log_template_decision(task, current_template, rule_suggestion,
                               "rule_disagree", confidence)
        return current_template, (f"kept original (confidence={confidence:.2f}), "
                                  f"but rule suggests '{rule_suggestion}' — logged for audit"), False

    if low_confidence and rule_suggestion != current_template:
        # 低 confidence + 规则不同意 → 采纳规则建议
        _log_template_decision(task, current_template, rule_suggestion,
                               "corrected", confidence)
        return rule_suggestion, (f"corrected: {current_template} → {rule_suggestion} "
                                 f"(low confidence={confidence:.2f}, rule override)"), True

    if low_confidence and rule_suggestion == current_template:
        # 低 confidence 但规则同意 → 通过，但记录
        _log_template_decision(task, current_template, rule_suggestion,
                               "low_confidence_confirmed", confidence)
        return current_template, f"confirmed with low confidence={confidence:.2f} (rule agrees)", False

    # Fallthrough (should not reach here)
    return current_template, "fallthrough", False


def _rule_based_template_check(combined_text: str, current_template: str) -> str:
    """独立规则引擎：基于硬编码规则判断模板类型。

    这套规则独立于 brain_manager.match_template()，作为交叉验证。
    设计原则：宁可保守（返回 standard-dev），不可激进降级（不轻易返回 quick）。
    """
    # 强信号规则（高置信度，直接判定）
    STRONG_SIGNALS = {
        "quick": [
            # 必须同时满足：短文本 + 查询类动词 + 无开发信号
            lambda t: (len(t) < 50
                       and any(k in t for k in ["查", "看看", "搜索", "天气", "日程"])
                       and not any(k in t for k in ["开发", "实现", "修复", "bug", "重构"])),
        ],
        "cron-auto": [
            lambda t: any(k in t for k in ["cron", "定时任务", "定期执行", "每天自动", "每周自动"]),
        ],
        "batch-dev": [
            lambda t: any(k in t for k in ["批量开发", "多个需求", "并行开发", "统筹开发"]),
        ],
    }

    for template, rules in STRONG_SIGNALS.items():
        if any(rule(combined_text) for rule in rules):
            return template

    # 弱信号规则（需要多个信号叠加）
    dev_signals = sum(1 for k in ["开发", "实现", "修复", "fix", "bug", "feature", "功能",
                                   "重构", "refactor", "优化", "添加", "新增", "编写代码"]
                      if k in combined_text)

    if dev_signals >= 1:
        return "standard-dev"

    # 无强信号 → 保守返回 standard-dev（宁可过度流程，不可跳过流程）
    return "standard-dev"


def _log_template_decision(task: dict, original: str, suggested: str,
                           action: str, confidence: float):
    """记录模板确认决策到 decisions.jsonl（供 Layer 2 审计探针使用）。"""
    decision = {
        "timestamp": bm.now_iso(),
        "type": "template_confirmation",
        "task_id": task.get("id", ""),
        "original_template": original,
        "suggested_template": suggested,
        "action": action,  # confirmed | rule_disagree | corrected | low_confidence_confirmed
        "confidence": confidence,
    }
    try:
        bm.append_decision(decision)
    except Exception:
        pass  # 日志写入失败不阻塞调度


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
            # Check design_ref in report
            if data.get("design_ref") or data.get("design_doc"):
                has_design = True
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
        if report.get("design_ref") or report.get("design_doc"):
            has_design = True
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
        if h.get("role") != "developer":
            continue
        ctx = h.get("context", "")
        if "missing docs" in ctx or "文档三件套不完整" in ctx or "文档不完整" in ctx or "文档三件套" in ctx:
            count += 1
    return count


def _count_evidence_retries(task: dict) -> int:
    """Count how many times a tester has been sent back for missing test_evidence."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        if h.get("role") != "tester":
            continue
        ctx = h.get("context", "")
        if "no test_evidence" in ctx or "test_evidence" in ctx or "测试证据" in ctx:
            count += 1
    return count


def _count_acceptance_plan_retries(task: dict) -> int:
    """Count how many times architect has been sent back for acceptance_plan."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        if h.get("role") != "architect":
            continue
        ctx = h.get("context", "")
        if "acceptance_plan" in ctx:
            count += 1
    return count


def _count_coverage_retries(task: dict) -> int:
    """Count how many times tester has been sent back for coverage."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        if h.get("role") != "tester":
            continue
        ctx = h.get("context", "")
        if "覆盖率" in ctx or "coverage" in ctx:
            count += 1
    return count


def _count_tester_developer_pingpong(task: dict) -> int:
    """Count tester↔developer round-trips (m-6 ping-pong protection).

    A round-trip = tester fail → developer → tester again.
    We count how many times tester dispatched developer due to fail.
    """
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for i, h in enumerate(history):
        if h.get("role") == "developer" and i > 0:
            prev = history[i - 1]
            if prev.get("role") == "tester":
                count += 1
    return count


def _count_retro_retries(task: dict) -> int:
    """Count how many times retrospective has triggered a re-route (fail verdict)."""
    orch = task.get("orchestration", {})
    history = orch.get("history", [])
    count = 0
    for h in history:
        if h.get("role") == "retrospective":
            ctx = h.get("context", "")
            if "流程复盘发现缺失环节" in ctx or "retrospective" in ctx:
                count += 1
    return count


# ──────────────────────────────────────────
# Phase 2: Dispatcher advice (LLM suggestion layer)
# ──────────────────────────────────────────


# [V6.1] _evaluate_dispatcher_advice and _record_concern removed (D27: dispatcher 不做语义判断)
# Audit is now handled by auditor worker reading dispatcher session jsonl.


def _generate_default_acceptance_plan(task: dict, level: str = "standard") -> list:
    """Auto-generate acceptance_plan for tasks without architect.

    Args:
        task: Task dict
        level: "minimal" (PL1) or "standard" (PL2 fallback)

    Returns:
        list[dict] — each dict has step_id, description, category, expected_result, source, generated_at
    """
    timestamp = bm.now_iso()

    if level == "minimal":
        return [
            {"step_id": "A1", "description": "确认任务产出物存在",
             "category": "check", "expected_result": "文件/输出已生成",
             "source": "auto", "generated_at": timestamp},
            {"step_id": "A2", "description": "检查内容完整性",
             "category": "check", "expected_result": "内容非空且与任务描述匹配",
             "source": "auto", "generated_at": timestamp},
        ]

    # Standard: default verification steps
    return [
        {"step_id": "A1", "description": "单元测试覆盖核心逻辑",
         "category": "unit", "expected_result": "测试通过",
         "source": "auto", "generated_at": timestamp},
        {"step_id": "A2", "description": "集成测试验证端到端流程",
         "category": "e2e", "expected_result": "端到端验证通过",
         "source": "auto", "generated_at": timestamp},
        {"step_id": "A3", "description": "实际运行确认输出正确",
         "category": "e2e", "expected_result": "功能正常工作",
         "source": "auto", "generated_at": timestamp},
    ]


# ──────────────────────────────────────────
# Role determination
# ──────────────────────────────────────────

def get_initial_role(task: dict) -> str:
    """Determine the first Worker role for a task based on process level.

    PL0 (quick/cron-auto): developer — no architect needed
    PL1 (doc/research):    developer — no architect needed
    PL2 (standard dev):    architect — needs rule verdict + acceptance plan
    PL3 (high risk):       architect — needs full design + acceptance plan

    Explicit architect flag still forces architect regardless of PL.
    """
    # Explicit override
    if task.get("architect") or task.get("needs_design"):
        return "architect"

    ft = resolve_flow_type(task)
    roles = FLOW_TEMPLATES.get(ft, {}).get("roles", [])
    return roles[0] if roles else "developer"


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
    action: str  # "promote_to_review", "dispatch_role", "follow_up_worker", "mark_done", "mark_blocked"
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

    # ── P0-3 fix: fail-first priority ──
    # When multiple reports exist for the same role, a blocked/fail verdict
    # must NOT be overridden by a later pass. We scan all reports and prefer
    # the most severe negative verdict over any pass.
    _VERDICT_PRIORITY = {"blocked": 0, "fail": 1, "partial": 2, "pass": 3}
    best_report = None
    best_priority = 999  # lower = more severe

    for report_file in report_files:
        try:
            with report_file.open("r", encoding="utf-8") as f:
                report = json.load(f)

            # Validate required fields
            valid = True
            for field in REPORT_SCHEMA["required"]:
                if field not in report:
                    valid = False
                    break
            if not valid:
                continue

            # Validate task_id matches
            if report.get("task_id") != task_id:
                continue

            # Validate role
            if report.get("role") not in REPORT_SCHEMA["valid_roles"]:
                continue

            # Validate verdict
            verdict = report.get("verdict")
            if verdict not in REPORT_SCHEMA["valid_verdicts"]:
                continue

            vp = _VERDICT_PRIORITY.get(verdict, 3)
            if best_report is None:
                # First valid report (newest by mtime)
                best_report = report
                best_priority = vp
            elif vp < best_priority:
                # More severe verdict found in an older report — prefer it
                best_report = report
                best_priority = vp

        except (json.JSONDecodeError, OSError):
            continue

    return best_report


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


# ──────────────────────────────────────────
# A-02: Dynamic iteration limit helpers
# ──────────────────────────────────────────

def _compute_max_iterations(task: dict) -> int:
    """Compute dynamic iteration limit for a task.

    Priority:
    1. Architect's expected_steps (if set) + buffer(5)
    2. PL-based baseline from _PL_BASE_ITERATIONS
    3. Fallback to MAX_ORCHESTRATION_ITERATIONS

    The result is always capped at MAX_ORCHESTRATION_ITERATIONS.
    """
    orch = task.get("orchestration", {})
    expected_steps = orch.get("expected_steps")
    if expected_steps and isinstance(expected_steps, int) and expected_steps > 0:
        return min(expected_steps + 5, MAX_ORCHESTRATION_ITERATIONS)

    ft = resolve_flow_type(task)
    # cron-auto: minimal iterations; standard-dev: full flow
    _FT_BASE_ITERATIONS = {"cron-auto": 3, "standard-dev": 18}
    base = _FT_BASE_ITERATIONS.get(ft, 12)
    return min(base, MAX_ORCHESTRATION_ITERATIONS)


def register_worker_session(task_id: str, role: str, session_id: str,
                            max_iterations: int = None) -> bool:
    """Register a spawned worker's session ID for future follow_up.

    Called by Dispatcher after successful spawn.
    """
    task = bm.load_task(task_id)
    orch = task.setdefault("orchestration", {})
    workers = orch.setdefault("active_workers", {})
    workers[role] = {
        "session_id": session_id,
        "iterations_used": 0,
        "max_iterations": max_iterations or _get_role_iteration_limit(role, task),
        "spawned_at": bm.now_iso(),
    }
    bm.save_task(task)
    return True


def _can_follow_up(task: dict, role: str) -> bool:
    """Check if a role's active worker can be followed up.

    Returns True if the role has an active worker with remaining iterations.
    """
    workers = task.get("orchestration", {}).get("active_workers", {})
    worker = workers.get(role)
    if not worker or not worker.get("session_id"):
        return False
    return worker.get("iterations_used", 0) < worker.get("max_iterations", 0)


def _get_follow_up_session(task: dict, role: str) -> str | None:
    """Get the session_id for follow_up, or None if unavailable."""
    workers = task.get("orchestration", {}).get("active_workers", {})
    worker = workers.get(role)
    if worker and worker.get("session_id"):
        return worker["session_id"]
    return None


def _build_iteration_info(task: dict) -> dict:
    """Build iteration info dict for Dispatcher consumption (A-16).

    Returns current/max/remaining counts plus phase_advances and retries breakdown.
    """
    orch = task.get("orchestration", {})
    detail = orch.get("iteration_detail", {})
    total = detail.get("total", orch.get("iteration", 0))
    max_iter = _compute_max_iterations(task)
    return {
        "current": total,
        "max": max_iter,
        "remaining": max_iter - total,
        "phase_advances": detail.get("phase_advances", 0),
        "retries": detail.get("retries", 0),
    }


# A-06: Role iteration budget defaults and helpers
_DEFAULT_ROLE_ITERATIONS = {
    "developer": 60,
    "tester": 30,
    "architect": 25,
    "auditor": 20,
    "architect_review": 20,
    "code_review": 20,
    "test_review": 20,
    "retrospective": 15,
}


def _get_role_iteration_limit(role: str, task: dict) -> int:
    """Get iteration limit for a role, respecting Architect's budget suggestion.

    Priority:
    1. Architect's role_budgets (from task)
    2. Default per-role limit
    Cap: 1.5x default (prevent runaway budgets)
    """
    default = _DEFAULT_ROLE_ITERATIONS.get(role, 60)
    max_cap = int(default * 1.5)

    budgets = task.get("role_budgets", {})
    if role in budgets:
        suggested = budgets[role]
        if isinstance(suggested, int) and suggested > 0:
            return min(suggested, max_cap)

    return default


def _budget_warning_text(iterations_used: int, max_iterations: int) -> str:
    """Generate budget warning text if usage >= 80%.

    Appended to follow_up_message when worker is running low on iterations.
    """
    if max_iterations <= 0:
        return ""
    usage_pct = iterations_used / max_iterations
    if usage_pct >= 0.8:
        remaining = max_iterations - iterations_used
        return (
            f"\n\n⚠️ **预算告警**: 你已使用 {iterations_used}/{max_iterations} iterations "
            f"({usage_pct:.0%})，剩余 {remaining} 次。\n"
            f"请优先完成核心任务，非关键项可标注为后续改进。\n"
        )
    return ""


# ──────────────────────────────────────────
# State machine: gate checks + transition execution
# ──────────────────────────────────────────

def _gate_architect_plan(task: dict, report: dict, ft: str):
    """Gate: check acceptance_plan when architect passes (standard-dev only).

    Returns Decision if gate fails, None if gate passes.
    """
    if ft != "standard-dev":
        return None

    acceptance_plan = report.get("acceptance_plan")

    # Gate 1: plan existence
    if not acceptance_plan or not isinstance(acceptance_plan, list) or len(acceptance_plan) == 0:
        retry_count = _count_acceptance_plan_retries(task)
        if retry_count >= MAX_ACCEPTANCE_PLAN_RETRY:
            # Exceeded retry limit → auto-generate fallback plan and continue
            acceptance_plan = _generate_default_acceptance_plan(task, level="standard")
            task["acceptance_plan"] = acceptance_plan
            bm.save_task(task)
            return None  # pass through with fallback plan
        return Decision(
            action="dispatch_role",
            params={"role": "architect", "context": (
                "⚠️ PL2/PL3 任务必须产出 acceptance_plan（验收方案）。\n\n"
                "请在报告 JSON 中添加 acceptance_plan 字段，格式：\n"
                '"acceptance_plan": [\n'
                '    {"step_id": "T1", "description": "...", "category": "e2e", "expected_result": "..."},\n'
                '    {"step_id": "T2", "description": "...", "category": "unit", "expected_result": "..."}\n'
                "]\n\n"
                "每个步骤必须包含 step_id, description, category, expected_result。\n"
                "代码任务必须包含至少一个 category='e2e' 的步骤。"
            )},
            reason="architect passed but missing acceptance_plan for standard-dev task"
        )

    # Gate 2: e2e step check
    has_e2e = any(
        isinstance(s, dict) and s.get("category") == "e2e"
        for s in acceptance_plan
    )
    if not has_e2e:
        retry_count = _count_acceptance_plan_retries(task)
        if retry_count >= MAX_ACCEPTANCE_PLAN_RETRY:
            # Exceeded retry limit → log warning, pass through
            return None
        return Decision(
            action="dispatch_role",
            params={"role": "architect", "context": (
                "⚠️ 代码任务的 acceptance_plan 必须包含至少一个 category='e2e' 的步骤。\n\n"
                "e2e 步骤应描述具体的端到端验证方式，例如：\n"
                '{"step_id": "T1", "description": "在 dev 环境部署并验证功能", '
                '"category": "e2e", "expected_result": "功能正常工作"}\n\n'
                "请补充 e2e 步骤后重新提交。"
            )},
            reason="architect acceptance_plan missing e2e steps for code task"
        )

    # All gates passed → persist plan
    task["acceptance_plan"] = acceptance_plan

    # Persist Architect's expected_steps and role_budgets (A-02, A-06)
    expected_steps = report.get("expected_steps")
    if expected_steps and isinstance(expected_steps, int) and expected_steps > 0:
        task.setdefault("orchestration", {})["expected_steps"] = expected_steps

    role_budgets = report.get("role_budgets")
    if role_budgets and isinstance(role_budgets, dict):
        task["role_budgets"] = role_budgets

    bm.save_task(task)
    return None


def _check_devlog_on_filesystem(task: dict, report: dict) -> bool:
    """Check if DEVLOG exists on filesystem (fallback for files_changed miss).

    Searches common dev-workdir paths for DEVLOG.md.
    """
    dev_dir = Path(os.environ.get("DEV_WORKDIR", str(WORKSPACE / "dev-workdir")))
    if not dev_dir.exists():
        return False
    for pattern in ["**/DEVLOG.md", "**/devlog.md"]:
        if list(dev_dir.glob(pattern)):
            return True
    return False


def _gate_developer_docs(task: dict, report: dict, ft: str):
    """Gate: check docs when developer passes (standard-dev only).

    Layer 1: DEVLOG existence check (new, stricter).
    Layer 2: Original doc triplet check (preserved).
    cron-auto skips this gate entirely.
    Returns Decision if gate fails, None if gate passes.
    """
    if ft == "cron-auto":
        return None

    # --- Layer 1: DEVLOG existence ---
    files_changed = report.get("files_changed", [])
    has_devlog = any("DEVLOG" in f.upper() or "devlog" in f for f in files_changed)

    if not has_devlog:
        has_devlog = _check_devlog_on_filesystem(task, report)

    if not has_devlog:
        retry_count = _count_doc_retries(task)
        if retry_count >= MAX_DOC_RETRY:
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ Developer 多次未提供 DEVLOG (已打回{retry_count}次)"},
                reason=f"developer no DEVLOG after {retry_count} retries"
            )
        if _can_follow_up(task, "developer"):
            return Decision(
                action="follow_up_worker",
                params={
                    "role": "developer",
                    "context": (
                        "⚠️ 代码实现已完成，但缺少 DEVLOG.md。\n\n"
                        "请在项目目录中创建/更新 DEVLOG.md，记录：\n"
                        "1. 本次改动的 Phase 和 checkbox 任务清单\n"
                        "2. 关键设计决策和 trade-off\n"
                        "3. 已知问题和后续计划\n\n"
                        "完成后重新提交报告，确保 files_changed 包含 DEVLOG.md。"
                    ),
                },
                reason="developer passed but missing DEVLOG — follow_up to complete"
            )
        return Decision(
            action="dispatch_role",
            params={
                "role": "developer",
                "context": "⚠️ 缺少 DEVLOG.md，请补全后重新提交。",
            },
            reason="developer passed but missing DEVLOG — dispatching back"
        )

    # --- Layer 2: Original doc triplet check ---
    doc_ok, doc_missing = check_doc_triplet(task, report)
    if not doc_ok:
        retry_count = _count_doc_retries(task)
        if retry_count >= MAX_DOC_RETRY:
            summary = report.get("summary", "")
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ 文档三件套不完整 (已打回{retry_count}次，升级人工审核): 缺少 {', '.join(doc_missing)}. Developer summary: {summary}"},
                reason=f"developer passed but missing docs after {retry_count} retries: {doc_missing} — escalating to manual review"
            )
        summary = report.get("summary", "")
        if _can_follow_up(task, "developer"):
            return Decision(
                action="follow_up_worker",
                params={"role": "developer", "context": f"⚠️ 文档不完整: {', '.join(doc_missing)}"},
                reason=f"developer docs incomplete — follow_up"
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
    return None


def _gate_tester_coverage(task: dict, report: dict, ft: str):
    """Gate: check acceptance_plan coverage when tester passes.

    Backward-compatible: tasks without acceptance_plan skip this gate.
    Returns Decision if gate fails, None if gate passes.
    """
    acceptance_plan = task.get("acceptance_plan")
    if not acceptance_plan or not isinstance(acceptance_plan, list) or len(acceptance_plan) == 0:
        return None  # no plan → skip coverage check (backward compat)

    # Calculate coverage
    evidence = report.get("test_evidence") or []
    covered_ids = {e.get("step_id") for e in evidence if isinstance(e, dict) and e.get("step_id")}
    plan_ids = {s.get("step_id") for s in acceptance_plan if isinstance(s, dict) and s.get("step_id")}
    coverage = len(covered_ids.intersection(plan_ids)) / len(plan_ids) if plan_ids else 1.0
    uncovered = plan_ids - covered_ids

    # Dynamic threshold by flow type (standard-dev uses 0.8, cron-auto skips)
    coverage_threshold = 0.8

    if coverage < coverage_threshold:
        retry_count = _count_coverage_retries(task)
        if retry_count >= MAX_COVERAGE_RETRY:
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ tester 覆盖率 {coverage:.0%} 不足，已打回{retry_count}次。未覆盖: {uncovered}"},
                reason=f"tester coverage {coverage:.0%} after {retry_count} retries — escalating"
            )
        return Decision(
            action="dispatch_role",
            params={
                "role": "tester",
                "context": (
                    f"⚠️ 验收方案覆盖率不足 ({coverage:.0%})。\n"
                    f"未覆盖步骤: {', '.join(sorted(uncovered))}\n\n"
                    f"请按验收方案逐项执行并在 test_evidence 中标注 step_id。\n"
                    f"每条 evidence 必须包含 step_id 字段对应验收方案中的步骤。"
                ),
            },
            reason=f"tester evidence coverage {coverage:.0%} < {coverage_threshold:.0%}, uncovered: {uncovered}"
        )

    return None  # coverage OK


def _validate_state_transition(task: dict, target_state: str) -> tuple:
    """Validate a state transition at the scheduler (business) level.

    This is on top of brain_manager's VALID_TRANSITIONS (which handles
    basic state machine validity). This function checks business rules.

    Args:
        task: Task dict
        target_state: Target state string (e.g. "done", "review", "executing")

    Returns:
        (valid, reason) — valid=True means transition is allowed
    """
    current = task.get("status", "pending")
    ft = resolve_flow_type(task)
    tpl = FLOW_TEMPLATES.get(ft, {})
    orch = task.get("orchestration", {})
    history = orch.get("history", [])

    # Rule 1: tasks with auditor must go through auditor/retrospective before done
    if target_state == "done" and tpl.get("has_auditor"):
        has_review = any(h.get("role") in ("auditor", "retrospective") for h in history)
        if not has_review and current != "review":
            return False, f"{ft} tasks must go through auditor/retrospective before done"

    # Rule 2: standard-dev tasks should have tester in history before done/review
    if target_state in ("done", "review") and ft == "standard-dev":
        has_tester = any(h.get("role") == "tester" for h in history)
        if not has_tester:
            return False, f"{ft} tasks must have tester before {target_state}"

    # Rule 3: Cannot go to executing if already executing (double dispatch guard)
    if target_state == "executing" and current == "executing":
        return False, "task already executing — possible double dispatch"

    return True, "ok"



def _assert_audit_completed(task: dict) -> None:
    """Pre-condition assertion: audit step must have been executed before mark_done.

    For standard-dev with has_retrospective: retrospective must have passed.
    For standard-dev with has_auditor: auditor must have passed.
    cron-auto: no audit required.

    This is a data-existence check (like a DB CHECK constraint), not a semantic
    judgment. It does not violate cross-check principles (D34).

    Raises:
        ValueError: if required audit step was not completed
    """
    ft = resolve_flow_type(task)
    tpl = FLOW_TEMPLATES.get(ft, {})

    if not tpl.get("has_auditor") and not tpl.get("has_retrospective"):
        return  # No audit required (e.g. cron-auto)

    history = task.get("orchestration", {}).get("history", [])

    if tpl.get("has_auditor"):
        auditor_passed = any(
            h.get("role") == "auditor" and h.get("verdict") == "pass"
            for h in history
        )
        if not auditor_passed:
            raise ValueError(
                f"Task {task.get('id')} cannot be marked done: "
                f"auditor not completed"
            )
    elif tpl.get("has_retrospective"):
        retro_passed = any(
            h.get("role") == "retrospective" and h.get("verdict") == "pass"
            for h in history
        )
        if not retro_passed:
            raise ValueError(
                f"Task {task.get('id')} cannot be marked done: "
                f"retrospective (audit) not completed"
            )


def _assert_audit_completed_safe(task: dict):
    """Safe wrapper: returns Decision(mark_blocked) instead of raising."""
    try:
        _assert_audit_completed(task)
        return None  # OK
    except ValueError as e:
        return Decision(
            action="mark_blocked",
            params={"reason": str(e)},
            reason=f"audit assertion failed (safe degradation): {e}"
        )


def _run_gate_checks(task: dict, report: dict, role: str, verdict: str, ft: str):
    """Run role-specific gate checks before state transition.

    Returns None if all gates pass, or a Decision if a gate fails.
    """
    if role == "architect" and verdict == "pass":
        return _gate_architect_plan(task, report, ft)

    if role == "developer" and verdict == "pass":
        # Check blocker keywords in issues first (P0-4 fix)
        dev_issues = report.get("issues", [])
        if dev_issues:
            _BLOCKER_KW = ["not implemented", "not yet", "缺失", "未实现", "未完成",
                           "todo", "没有实现", "无法实现", "not done", "missing",
                           "stub", "placeholder", "后端没", "backend not"]
            issue_text = " ".join(
                (i.get("description", "") if isinstance(i, dict) else str(i))
                for i in dev_issues
            ).lower()
            if any(kw in issue_text for kw in _BLOCKER_KW):
                return Decision(
                    action="dispatch_role",
                    params={
                        "role": "developer",
                        "context": (
                            "⚠️ 你的报告 verdict=pass 但 issues 中包含未完成/未实现的内容:\n"
                            f"{json.dumps(dev_issues, ensure_ascii=False, indent=2)}\n\n"
                            "请完成所有必要实现后再提交 pass 报告。如果确实无法实现，"
                            "请提交 blocked 报告并说明原因。"
                        ),
                    },
                    reason="developer passed but issues contain blocker keywords — dispatching back"
                )
        # Smoke test gate (A-12): code tasks must include smoke_test result
        smoke_test = report.get("smoke_test")
        if not smoke_test or not isinstance(smoke_test, dict):
            if _can_follow_up(task, "developer"):
                return Decision(
                    action="follow_up_worker",
                    params={
                        "role": "developer",
                        "context": (
                            "⚠️ 报告缺少 smoke_test 字段。\n\n"
                            "请在完成代码后执行基本冒烟测试并记录结果：\n"
                            '在报告中添加: "smoke_test": {"command": "python -c \\"import module\\"", "status": "pass", "output": "..."}\n\n'
                            "至少验证：(1) 代码可 import (2) 主入口可执行无报错"
                        ),
                    },
                    reason="developer passed but no smoke_test"
                )
            return Decision(
                action="dispatch_role",
                params={"role": "developer", "context": "⚠️ 缺少 smoke_test，请补充。"},
                reason="developer passed but no smoke_test — dispatching back"
            )
        if smoke_test.get("status") != "pass":
            if _can_follow_up(task, "developer"):
                return Decision(
                    action="follow_up_worker",
                    params={
                        "role": "developer",
                        "context": (
                            f"⚠️ 冒烟测试未通过：\n"
                            f"命令: {smoke_test.get('command', 'N/A')}\n"
                            f"输出: {smoke_test.get('output', 'N/A')}\n\n"
                            f"请修复后重新提交。"
                        ),
                    },
                    reason=f"developer smoke_test failed: {smoke_test.get('output', '')[:100]}"
                )
            return Decision(
                action="dispatch_role",
                params={"role": "developer", "context": "⚠️ 冒烟测试失败，请修复。"},
                reason="developer smoke_test failed — dispatching back"
            )
        # Doc triplet gate
        return _gate_developer_docs(task, report, ft)

    if role == "tester" and verdict == "pass":
        # test_evidence format validation
        if TEST_EVIDENCE_ENABLED:
            evidence = report.get("test_evidence") or report.get("test_results") or []
            has_valid_evidence = (
                isinstance(evidence, list)
                and len(evidence) > 0
                and all(
                    isinstance(e, dict) and e.get("type") and e.get("result")
                    for e in evidence
                )
            )
            if not has_valid_evidence:
                retry_count = _count_evidence_retries(task)
                if retry_count >= MAX_EVIDENCE_RETRY:
                    summary = report.get("summary", "")
                    return Decision(
                        action="promote_to_review",
                        params={"summary": f"⚠️ tester passed 但多次未提供 test_evidence (已打回{retry_count}次). {summary}"},
                        reason=f"tester passed without test_evidence after {retry_count} retries — escalating"
                    )
                summary = report.get("summary", "")
                return Decision(
                    action="dispatch_role",
                    params={
                        "role": "tester",
                        "context": (
                            "⚠️ 测试报告缺少 test_evidence 字段。请补充测试执行证据。\n\n"
                            "报告中必须包含 test_evidence 字段，格式如下：\n"
                            '"test_evidence": [\n'
                            '    {"type": "command_output", "command": "pytest tests/", "result": "5 passed"},\n'
                            '    {"type": "manual_test", "description": "验证功能X", "result": "OK"}\n'
                            "]\n\n"
                            "⚠️ 无 test_evidence 的 pass 报告会被打回。\n\n"
                            f"之前的测试结论: {summary}"
                        ),
                    },
                    reason="tester passed but no test_evidence"
                )

        # Coverage gate
        return _gate_tester_coverage(task, report, pl)

    if role == "tester" and verdict == "fail":
        # m-6: ping-pong protection before dispatching developer
        pingpong_count = _count_tester_developer_pingpong(task)
        if pingpong_count >= MAX_TESTER_DEVELOPER_PINGPONG:
            summary = report.get("summary", "")
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ tester↔developer 已往返 {pingpong_count} 次，升级人工审核。最近问题: {summary}"},
                reason=f"tester↔developer ping-pong {pingpong_count} times — escalating to review"
            )

    return None  # no gate for this combination


def _build_handoff_context(from_role: str, to_role: str, report: dict,
                           task: dict, summary: str) -> str:
    """Build context string for role handoff transitions.

    Extracted from _execute_transition to keep it concise and testable.
    V6.1: Updated for 8-role flow (architect_review before dev, code_review after dev, test_review after tester).
    """
    # ── architect → architect_review (V6.1: 开发前评审架构) ──
    if from_role == "architect" and to_role == "architect_review":
        design_notes = report.get("design_notes", report.get("summary", ""))
        acceptance_plan = report.get("acceptance_plan", [])
        # Store rule_context from architect report for later use by architect_review → developer handoff
        rule_verdict = report.get("rule_verdict", {})
        worker_instructions = rule_verdict.get("worker_instructions", "").strip() if rule_verdict else ""
        task["rule_context"] = worker_instructions or rule_loader.collect_rules(task)
        bm.save_task(task)
        context = f"Architect has produced the design. Please review for completeness and feasibility.\n\nArchitect summary:\n{summary}"
        if design_notes:
            context += f"\n\n**Design Notes:**\n{design_notes}"
        if acceptance_plan:
            context += f"\n\n**Acceptance Plan ({len(acceptance_plan)} steps):**\n{json.dumps(acceptance_plan, ensure_ascii=False, indent=2)}"
        return context

    # ── architect → developer (kept for backward compat / direct path) ──
    elif from_role == "architect" and to_role == "developer":
        rule_verdict = report.get("rule_verdict", {})
        worker_instructions = rule_verdict.get("worker_instructions", "").strip() if rule_verdict else ""
        if not worker_instructions:
            static = rule_loader.collect_rules(task)
            design = report.get("design_notes", report.get("summary", ""))
            context = f"{static}\n\n### Architect Notes\n{design}" if design else static
        else:
            context_parts = [worker_instructions]
            design_notes = report.get("design_notes", "")
            if design_notes:
                context_parts.append(f"### Architect 设计要点\n\n{design_notes}")
            context = "\n\n".join(context_parts)
        # Store rule_context for later use by architect_review → developer handoff
        task["rule_context"] = worker_instructions or rule_loader.collect_rules(task)
        bm.save_task(task)
        return context

    # ── architect_review → developer (V6.1: 架构评审通过，进入开发) ──
    elif from_role == "architect_review" and to_role == "developer":
        # Retrieve stored architect context from task (saved during architect completion)
        rule_context = task.get("rule_context", "")
        review_notes = f"Architecture review passed:\n{summary}"
        if rule_context:
            return f"{rule_context}\n\n### Architecture Review Notes\n{review_notes}"
        # Fallback: try to build context from architect report stored in task
        return f"Architecture review passed. Proceed with implementation.\n\nReview summary:\n{summary}"

    # ── architect_review → architect (V6.1: 架构评审失败，打回) ──
    elif from_role == "architect_review" and to_role == "architect":
        return f"Architecture review found issues:\n{summary}\n\nIssues: {json.dumps(report.get('issues', []), ensure_ascii=False)}"

    # ── developer → code_review (V6.1: 代码+测试覆盖检查) ──
    elif from_role == "developer" and to_role == "code_review":
        dev_issues = report.get("issues", [])
        context = f"Developer completed implementation. Please review for design consistency and test coverage.\n\nDeveloper summary:\n{summary}"
        if dev_issues:
            context += f"\n\n**Developer reported issues:**\n{json.dumps(dev_issues, ensure_ascii=False, indent=2)}"
        return context

    # ── developer → tester (kept for backward compat) ──
    elif from_role == "developer" and to_role == "tester":
        dev_issues = report.get("issues", [])
        context = f"Developer completed:\n{summary}"
        if dev_issues:
            context += f"\n\n**Developer reported issues (请审查):**\n{json.dumps(dev_issues, ensure_ascii=False, indent=2)}"
        return context

    # ── code_review → tester (V6.1: 代码审查通过) ──
    elif from_role == "code_review" and to_role == "tester":
        return f"Code review passed:\n{summary}"

    # ── code_review → developer (V6.1: 代码审查失败，打回 developer) ──
    elif from_role == "code_review" and to_role == "developer":
        return f"Code review found issues:\n{summary}\n\nIssues: {json.dumps(report.get('issues', []), ensure_ascii=False)}"

    # ── tester → test_review (V6.1: 测试语义审查) ──
    elif from_role == "tester" and to_role == "test_review":
        test_evidence = report.get("test_evidence", report.get("test_results", []))
        context = f"Tester completed testing. Please review the test process and report quality.\n\nTester summary:\n{summary}"
        if test_evidence:
            context += f"\n\n**Test Evidence:**\n{json.dumps(test_evidence, ensure_ascii=False, indent=2)}"
        return context

    # ── tester → developer (test fail → developer fix) ──
    elif from_role == "tester" and to_role == "developer":
        return f"Tester found issues:\n{summary}\n\nIssues: {json.dumps(report.get('issues', []), ensure_ascii=False)}"

    # ── test_review → retrospective (PL2: 测试审查通过 → 复盘) ──
    elif from_role == "test_review" and to_role == "retrospective":
        return f"Test review passed. Please review the orchestration flow completeness.\n\nTest review summary:\n{summary}"

    # ── test_review → auditor (PL3: 测试审查通过 → 审计) ──
    elif from_role == "test_review" and to_role == "auditor":
        return f"Test review passed. Please audit the full orchestration flow.\n\nTest review summary:\n{summary}"

    # ── test_review → tester (V6.1: 测试审查失败，打回 tester) ──
    elif from_role == "test_review" and to_role == "tester":
        return f"Test review found issues with the testing:\n{summary}\n\nIssues: {json.dumps(report.get('issues', []), ensure_ascii=False)}"

    # ── tester → auditor (kept for backward compat) ──
    elif from_role == "tester" and to_role == "auditor":
        return f"Tester passed. Please audit the full orchestration flow.\n\nTester summary:\n{summary}"

    # ── auditor → retrospective ──
    elif from_role == "auditor" and to_role == "retrospective":
        return f"Auditor passed. Please perform final flow retrospective.\n\nAuditor summary:\n{summary}"

    else:
        return summary


def _execute_transition(task: dict, report: dict, transition: tuple, ft: str,
                        dispatcher_advice: dict | None = None) -> Decision:
    """Execute a state transition action."""
    action_type = transition[0]
    summary = report.get("summary", "")
    role = report.get("role", "")
    history = task.get("orchestration", {}).get("history", [])

    if action_type == "framework_closeout":
        # Framework closeout chain: auditor → retrospective → done/review
        tpl = FLOW_TEMPLATES.get(ft, {})
        if tpl.get("has_auditor"):
            context = _build_handoff_context(role, "auditor", report, task, summary)
            if _can_follow_up(task, "auditor"):
                return Decision(
                    action="follow_up_worker",
                    params={"role": "auditor", "context": context},
                    reason=f"{ft} framework_closeout: dispatching auditor (follow_up)"
                )
            return Decision(
                action="dispatch_role",
                params={"role": "auditor", "context": context},
                reason=f"{ft} framework_closeout: dispatching auditor"
            )
        elif tpl.get("has_retrospective"):
            context = _build_handoff_context(role, "retrospective", report, task, summary)
            if _can_follow_up(task, "retrospective"):
                return Decision(
                    action="follow_up_worker",
                    params={"role": "retrospective", "context": context},
                    reason=f"{ft} framework_closeout: dispatching retrospective (follow_up)"
                )
            return Decision(
                action="dispatch_role",
                params={"role": "retrospective", "context": context},
                reason=f"{ft} framework_closeout: dispatching retrospective"
            )
        else:
            return _closeout_done_or_review(task, report, ft)

    elif action_type == "done":
        safe_result = _assert_audit_completed_safe(task)
        if safe_result is not None:
            return safe_result
        return Decision(action="mark_done", reason=f"{ft} flow complete — {role} passed")

    elif action_type == "role":
        next_role = transition[1]
        context = _build_handoff_context(role, next_role, report, task, summary)

        # Check if target role has reusable active worker (A-01)
        if _can_follow_up(task, next_role):
            return Decision(
                action="follow_up_worker",
                params={"role": next_role, "context": context},
                reason=f"{ft} transition: {role} → {next_role} (follow_up existing worker)"
            )
        return Decision(
            action="dispatch_role",
            params={"role": next_role, "context": context},
            reason=f"{ft} transition: {role} → {next_role}"
        )

    elif action_type == "retry":
        retry_role = transition[1]
        # Check max retry (developer fail retries)
        recent_devs = [h for h in history if h.get("role") == retry_role]
        if len(recent_devs) >= MAX_SAME_ROLE_CONSECUTIVE:
            return Decision(
                action="mark_blocked",
                reason=f"{retry_role} failed {len(recent_devs)} times, needs human intervention"
            )
        # Prefer follow_up if active worker exists (A-01)
        if _can_follow_up(task, retry_role):
            return Decision(
                action="follow_up_worker",
                params={"role": retry_role, "context": f"Previous attempt result:\n{summary}"},
                reason=f"{retry_role} retry via follow_up (preserving context)"
            )
        return Decision(
            action="dispatch_role",
            params={"role": retry_role, "context": f"Previous attempt failed:\n{summary}"},
            reason=f"{retry_role} failed, retrying (new spawn)"
        )

    elif action_type == "review_check":
        # PL2: check review_level to decide done vs review
        # Also check doc triplet for auto-approve path
        review_level = bm.determine_review_level(task)
        if review_level in ("L0", "L1"):
            doc_ok, doc_missing = check_doc_triplet(task, report)
            if not doc_ok:
                return Decision(
                    action="promote_to_review",
                    params={"summary": f"⚠️ tester passed but docs incomplete ({', '.join(doc_missing)}). {summary}"},
                    reason=f"tester passed but docs missing {doc_missing}, upgrading to manual review"
                )
            _assert_audit_completed_safe(task)  # Pre-condition assertion (D34) — safe version
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

    elif action_type == "review":
        return Decision(
            action="promote_to_review",
            params={"summary": summary},
            reason=f"{ft} requires mandatory review"
        )

    elif action_type == "blocked":
        return Decision(
            action="mark_blocked",
            reason=f"{role} rejected task: {summary}"
        )

    elif action_type == "auditor_route":
        # Phase 2: Auditor fail → route to appropriate role
        return _handle_auditor_route(task, report, ft, dispatcher_advice)

    elif action_type == "retro_route":
        # Phase 2: Retrospective fail → route to missing role
        return _handle_retro_route(task, report, ft)

    # Unknown transition type — should not happen
    return Decision(
        action="promote_to_review",
        params={"summary": f"Unknown transition: {transition}"},
        reason=f"unknown transition type: {action_type}"
    )


# ──────────────────────────────────────────
# Phase 2: Auditor route + Retrospective route
# ──────────────────────────────────────────

def _handle_auditor_route(task: dict, report: dict, ft: str,
                          dispatcher_advice: dict | None = None) -> Decision:
    """When auditor fails, determine who to send back to.

    Three possible targets based on report analysis:
    - developer: code issues (bugs, missing implementation)
    - architect: test gaps → 补测试方案 → 然后 tester 补测执行
    - tester: test execution gaps (plan exists but not executed properly)

    Uses report.suggested_target, otherwise defaults to developer.
    [V6.1] dispatcher_advice parameter kept for backward compat but no longer used.
    """
    target = "developer"  # safe default

    # 报告中的 suggested_target
    if report.get("suggested_target"):
        target = report["suggested_target"]

    # Validate target
    if target not in ("developer", "tester", "architect"):
        target = "developer"

    # Build context from auditor feedback
    context = _build_auditor_feedback(report, target)

    # If routing to architect, it means test plan gap — add explicit guidance
    if target == "architect":
        context += (
            "\n\n⚠️ Auditor 发现测试方案存在盲区。"
            "请补充测试方案（acceptance_plan），覆盖 Auditor 指出的 gap。"
            "\n不需要重新设计整个方案，只需补充缺失的测试步骤。"
        )

    return Decision(
        action="dispatch_role",
        params={"role": target, "context": context},
        reason=f"auditor failed — routing to {target}"
    )


def _build_auditor_feedback(report: dict, target: str) -> str:
    """Build context string from auditor report for the target role."""
    summary = report.get("summary", "")
    issues = report.get("issues", [])

    lines = [f"⚠️ Auditor 审计发现问题（打回至 {target}）:"]
    if summary:
        lines.append(f"\n审计摘要:\n{summary}")
    if issues:
        lines.append(f"\n具体问题:")
        for issue in issues:
            if isinstance(issue, dict):
                desc = issue.get("description", str(issue))
            else:
                desc = str(issue)
            lines.append(f"- {desc}")

    return "\n".join(lines)


def _handle_retro_route(task: dict, report: dict, ft: str) -> Decision:
    """When retrospective fails (missing steps found), route back to fill gaps.

    If retrospective has already retried MAX_RETRO_RETRY times, force pass
    to prevent infinite loops.
    """
    # Check retry limit
    retro_retries = _count_retro_retries(task)
    if retro_retries >= MAX_RETRO_RETRY:
        # Exceeded retry limit → record issue and pass through
        _record_retro_issue(task, report)
        if FLOW_TEMPLATES.get(ft, {}).get("has_auditor"):
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ 流程复盘超过重试限制 ({MAX_RETRO_RETRY})，升级人工 review。"},
                reason=f"retrospective retry limit reached ({retro_retries}), escalating to review"
            )
        else:
            return Decision(
                action="mark_done",
                reason=f"retrospective retry limit reached ({retro_retries}), force passing"
            )

    missing_role = report.get("missing_role")
    if not missing_role or missing_role not in VALID_ROLES:
        # Cannot determine missing role → record issue and pass through
        _record_retro_issue(task, report)
        if FLOW_TEMPLATES.get(ft, {}).get("has_auditor"):
            return Decision(
                action="promote_to_review",
                params={"summary": "⚠️ 流程复盘发现问题但无法确定缺失角色，升级人工 review。"},
                reason="retrospective fail but unclear target"
            )
        else:
            return Decision(
                action="mark_done",
                reason="retrospective fail but unclear target"
            )

    missing_reason = report.get("missing_reason", "未说明原因")
    return Decision(
        action="dispatch_role",
        params={
            "role": missing_role,
            "context": f"⚠️ 流程复盘发现缺失环节:\n{missing_reason}\n\n请补完此环节。"
        },
        reason=f"retrospective found missing {missing_role} step"
    )


def _dispatch_retrospective(task: dict, report: dict, ft: str) -> Decision:
    """Dispatch the retrospective role after auditor passes."""
    summary = report.get("summary", "")
    context = _build_handoff_context("auditor", "retrospective", report, task, summary)
    if _can_follow_up(task, "retrospective"):
        return Decision(
            action="follow_up_worker",
            params={"role": "retrospective", "context": context},
            reason=f"{ft} framework_closeout: auditor passed → retrospective (follow_up)"
        )
    return Decision(
        action="dispatch_role",
        params={"role": "retrospective", "context": context},
        reason=f"{ft} framework_closeout: auditor passed → retrospective"
    )


def _closeout_done_or_review(task: dict, report: dict, ft: str) -> Decision:
    """Final closeout: check review_level, doc_triplet, audit assertion, then done or review."""
    summary = report.get("summary", "")
    review_level = bm.determine_review_level(task)
    if review_level in ("L0", "L1"):
        doc_ok, doc_missing = check_doc_triplet(task, report)
        if not doc_ok:
            return Decision(
                action="promote_to_review",
                params={"summary": f"⚠️ docs incomplete ({', '.join(doc_missing)}). {summary}"},
                reason=f"closeout: docs missing {doc_missing}, upgrading to manual review"
            )
        safe_result = _assert_audit_completed_safe(task)
        if safe_result is not None:
            return safe_result
        return Decision(
            action="mark_done",
            reason=f"{ft} flow complete, docs verified, review level {review_level}"
        )
    else:
        return Decision(
            action="promote_to_review",
            params={"summary": summary},
            reason=f"{ft} flow complete, promoting to {review_level} review"
        )


def _record_retro_issue(task: dict, report: dict):
    """Record process improvement suggestions from retrospective to persistent log."""
    issues = report.get("issues", [])
    if not issues:
        # Even if no structured issues, record the summary as a general issue
        summary = report.get("summary", "")
        if summary:
            issues = [{"description": summary, "source": "retrospective_summary"}]
        else:
            return

    log_path = BRAIN_DIR / "reports" / "process-improvements.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for issue in issues:
                entry = {
                    "timestamp": bm.now_iso(),
                    "task_id": task.get("id", ""),
                    "flow_type": resolve_flow_type(task),
                    "issue": issue if isinstance(issue, dict) else {"description": str(issue)},
                    "source": "retrospective",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # best-effort logging


def make_decision(report: dict | None, task: dict,
                  dispatcher_advice: dict | None = None) -> Decision:
    """Make orchestration decision based on worker report.

    When STATE_MACHINE_ENABLED=True (default), uses FLOW_TRANSITIONS table
    + gate checks. When False, falls back to the original if-else logic.

    Args:
        report: Parsed worker report (or None if missing)
        task: Task dict
        dispatcher_advice: Optional dispatcher LLM judgment, e.g.:
            {
                "verdict": "pass" | "fail" | "concern",
                "reason": "报告中 E10 声称端到端但实际只是函数调用",
                "concerns": ["e2e 测试缺少 session 级证据"],
                "suggested_target": "tester",  # 建议打回谁
            }

    Returns:
        Decision object with action and params
    """
    # Get orchestration state
    orch = task.get("orchestration", {})
    iteration = orch.get("iteration", 0)
    history = orch.get("history", [])

    # ── Step 0: Global checks (role-independent) ──

    # Cycle control: max iterations
    max_iter = _compute_max_iterations(task)
    if iteration >= max_iter:
        return Decision(
            action="mark_blocked",
            reason=f"max iterations reached ({iteration}/{max_iter}, ft={resolve_flow_type(task)})"
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

    # ── State machine path (default) ──
    if STATE_MACHINE_ENABLED:
        ft = resolve_flow_type(task)

        # Framework closeout chain: auditor → retrospective → done/review
        if role == "auditor":
            if verdict == "pass":
                return _dispatch_retrospective(task, report, ft)
            else:
                return _handle_auditor_route(task, report, ft, dispatcher_advice)

        if role == "retrospective":
            if verdict == "pass":
                return _closeout_done_or_review(task, report, ft)
            else:
                return _handle_retro_route(task, report, ft)

        # Step 1: Role-specific gate checks (before table lookup) — hard gate
        gate_result = _run_gate_checks(task, report, role, verdict, ft)
        if gate_result is not None:
            return gate_result

        # [V6.1] Step 1.5 removed: dispatcher_advice/semantic judgment deleted (D27)
        # Audit is now handled by auditor worker reading dispatcher session jsonl.

        # Step 2: Look up state transition table
        transition = FLOW_TRANSITIONS.get((ft, role, verdict))
        if transition is None:
            return Decision(
                action="promote_to_review",
                params={"summary": f"未知状态转移: ft={ft}, role={role}, verdict={verdict}"},
                reason=f"no transition defined for ({ft}, {role}, {verdict})"
            )

        # Step 3: Execute transition
        return _execute_transition(task, report, transition, ft)

    # ── Legacy if-else path (STATE_MACHINE_ENABLED=False) ──
    return _make_decision_legacy(report, task, role, verdict, summary, history)


def _make_decision_legacy(report: dict, task: dict, role: str, verdict: str,
                          summary: str, history: list) -> Decision:
    """Legacy if-else decision logic (fallback when STATE_MACHINE_ENABLED=False)."""

    # Architect role
    if role == "architect":
        if verdict == "pass":
            arch_warnings = validate_architect_report(report)
            for w in arch_warnings:
                pass  # warnings are informational

            ft = resolve_flow_type(task)
            if ft == "standard-dev":
                acceptance_plan = report.get("acceptance_plan")
                if not acceptance_plan or not isinstance(acceptance_plan, list) or len(acceptance_plan) == 0:
                    retry_count = _count_acceptance_plan_retries(task)
                    if retry_count >= MAX_ACCEPTANCE_PLAN_RETRY:
                        # Exceeded retry limit → auto-generate fallback plan and continue
                        acceptance_plan = _generate_default_acceptance_plan(task, level="standard")
                        task["acceptance_plan"] = acceptance_plan
                        bm.save_task(task)
                    else:
                        return Decision(
                            action="dispatch_role",
                            params={"role": "architect", "context": (
                                "⚠️ PL2/PL3 任务必须产出 acceptance_plan（验收方案）。\n\n"
                                "请在报告 JSON 中添加 acceptance_plan 字段，格式：\n"
                                '"acceptance_plan": [\n'
                                '    {"step_id": "T1", "description": "...", "category": "e2e", "expected_result": "..."},\n'
                                '    {"step_id": "T2", "description": "...", "category": "unit", "expected_result": "..."}\n'
                                "]\n\n"
                                "每个步骤必须包含 step_id, description, category, expected_result。\n"
                                "代码任务必须包含至少一个 category='e2e' 的步骤。"
                            )},
                            reason="architect passed but missing acceptance_plan for standard-dev task"
                        )

                if acceptance_plan and isinstance(acceptance_plan, list) and len(acceptance_plan) > 0:
                    has_e2e = any(
                        isinstance(s, dict) and s.get("category") == "e2e"
                        for s in acceptance_plan
                    )
                    if not has_e2e:
                        retry_count = _count_acceptance_plan_retries(task)
                        if retry_count >= MAX_ACCEPTANCE_PLAN_RETRY:
                            pass  # Exceeded retry limit → pass through
                        else:
                            return Decision(
                                action="dispatch_role",
                                params={"role": "architect", "context": (
                                    "⚠️ 代码任务的 acceptance_plan 必须包含至少一个 category='e2e' 的步骤。\n\n"
                                    "e2e 步骤应描述具体的端到端验证方式，例如：\n"
                                    '{"step_id": "T1", "description": "在 dev 环境部署并验证功能", '
                                    '"category": "e2e", "expected_result": "功能正常工作"}\n\n'
                                    "请补充 e2e 步骤后重新提交。"
                                )},
                                reason="architect acceptance_plan missing e2e steps for code task"
                            )

                task["acceptance_plan"] = acceptance_plan
                bm.save_task(task)

            rule_verdict = report.get("rule_verdict", {})
            worker_instructions = rule_verdict.get("worker_instructions", "").strip() if rule_verdict else ""

            if not worker_instructions:
                static = rule_loader.collect_rules(task)
                design = report.get("design_notes", report.get("summary", ""))
                context = f"{static}\n\n### Architect Notes\n{design}" if design else static
            else:
                context_parts = [worker_instructions]
                design_notes = report.get("design_notes", "")
                if design_notes:
                    context_parts.append(f"### Architect 设计要点\n\n{design_notes}")
                context = "\n\n".join(context_parts)

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
            if TEST_EVIDENCE_ENABLED:
                evidence = report.get("test_evidence") or report.get("test_results") or []
                has_valid_evidence = (
                    isinstance(evidence, list)
                    and len(evidence) > 0
                    and all(
                        isinstance(e, dict) and e.get("type") and e.get("result")
                        for e in evidence
                    )
                )
                if not has_valid_evidence:
                    retry_count = _count_evidence_retries(task)
                    if retry_count >= MAX_EVIDENCE_RETRY:
                        return Decision(
                            action="promote_to_review",
                            params={"summary": f"⚠️ tester passed 但多次未提供 test_evidence (已打回{retry_count}次). {summary}"},
                            reason=f"tester passed without test_evidence after {retry_count} retries — escalating"
                        )
                    return Decision(
                        action="dispatch_role",
                        params={
                            "role": "tester",
                            "context": (
                                "⚠️ 测试报告缺少 test_evidence 字段。请补充测试执行证据。\n\n"
                                "报告中必须包含 test_evidence 字段，格式如下：\n"
                                '"test_evidence": [\n'
                                '    {"type": "command_output", "command": "pytest tests/", "result": "5 passed"},\n'
                                '    {"type": "manual_test", "description": "验证功能X", "result": "OK"}\n'
                                "]\n\n"
                                "⚠️ 无 test_evidence 的 pass 报告会被打回。\n\n"
                                f"之前的测试结论: {summary}"
                            ),
                        },
                        reason="tester passed but no test_evidence"
                    )

            # acceptance_plan coverage check
            acceptance_plan = task.get("acceptance_plan")
            if acceptance_plan and isinstance(acceptance_plan, list) and len(acceptance_plan) > 0:
                plan_step_ids = {s.get("step_id") for s in acceptance_plan if isinstance(s, dict) and s.get("step_id")}
                evidence_for_coverage = report.get("test_evidence") or []
                covered_ids = {
                    e.get("step_id") for e in evidence_for_coverage
                    if isinstance(e, dict) and e.get("step_id")
                }

                uncovered = plan_step_ids - covered_ids
                coverage = len(covered_ids & plan_step_ids) / max(len(plan_step_ids), 1)

                # Dynamic threshold by flow type (aligned with state machine)
                ft_legacy = resolve_flow_type(task)
                coverage_threshold = 1.0 if FLOW_TEMPLATES.get(ft_legacy, {}).get("has_auditor") else 0.8

                if coverage < coverage_threshold:
                    retry_count = _count_coverage_retries(task)
                    if retry_count >= MAX_COVERAGE_RETRY:
                        return Decision(
                            action="promote_to_review",
                            params={"summary": f"⚠️ tester 覆盖率 {coverage:.0%} 不足，已打回{retry_count}次。未覆盖: {uncovered}"},
                            reason=f"tester coverage {coverage:.0%} after {retry_count} retries — escalating"
                        )
                    return Decision(
                        action="dispatch_role",
                        params={
                            "role": "tester",
                            "context": (
                                f"⚠️ 验收方案覆盖率不足 ({coverage:.0%})。\n"
                                f"未覆盖步骤: {', '.join(sorted(uncovered))}\n\n"
                                f"请按验收方案逐项执行并在 test_evidence 中标注 step_id。\n"
                                f"每条 evidence 必须包含 step_id 字段对应验收方案中的步骤。"
                            ),
                        },
                        reason=f"tester evidence coverage {coverage:.0%} < {coverage_threshold:.0%}, uncovered: {uncovered}"
                    )

            review_level = bm.determine_review_level(task)
            if review_level in ("L0", "L1"):
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
            # Ping-pong protection (aligned with state machine)
            pingpong_count = _count_tester_developer_pingpong(task)
            if pingpong_count >= MAX_TESTER_DEVELOPER_PINGPONG:
                return Decision(
                    action="promote_to_review",
                    params={"summary": f"⚠️ tester↔developer 已往返 {pingpong_count} 次，升级人工审核。最近问题: {summary}"},
                    reason=f"tester↔developer ping-pong {pingpong_count} times — escalating to review"
                )
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
            # P0-4 fix: check issues for blocker keywords
            dev_issues = report.get("issues", [])
            if dev_issues:
                _BLOCKER_KW = ["not implemented", "not yet", "缺失", "未实现", "未完成",
                               "todo", "没有实现", "无法实现", "not done", "missing",
                               "stub", "placeholder", "后端没", "backend not"]
                issue_text = " ".join(
                    (i.get("description", "") if isinstance(i, dict) else str(i))
                    for i in dev_issues
                ).lower()
                if any(kw in issue_text for kw in _BLOCKER_KW):
                    return Decision(
                        action="dispatch_role",
                        params={
                            "role": "developer",
                            "context": (
                                "⚠️ 你的报告 verdict=pass 但 issues 中包含未完成/未实现的内容:\n"
                                f"{json.dumps(dev_issues, ensure_ascii=False, indent=2)}\n\n"
                                "请完成所有必要实现后再提交 pass 报告。如果确实无法实现，"
                                "请提交 blocked 报告并说明原因。"
                            ),
                        },
                        reason="developer passed but issues contain blocker keywords — dispatching back"
                    )

            # Check flow type — cron-auto tasks skip tester
            ft_legacy = resolve_flow_type(task)
            if ft_legacy == "cron-auto":
                return Decision(
                    action="mark_done",
                    reason=f"developer passed, {ft_legacy} task — no tester needed"
                )
            else:
                doc_ok, doc_missing = check_doc_triplet(task, report)
                if not doc_ok:
                    retry_count = _count_doc_retries(task)
                    if retry_count >= MAX_DOC_RETRY:
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

                dev_issues = report.get("issues", [])
                tester_context = f"Developer completed:\n{summary}"
                if dev_issues:
                    tester_context += f"\n\n**Developer reported issues (请审查):**\n{json.dumps(dev_issues, ensure_ascii=False, indent=2)}"
                return Decision(
                    action="dispatch_role",
                    params={"role": "tester", "context": tester_context},
                    reason="developer passed, docs verified, dispatching tester"
                )
        elif verdict == "fail":
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
                "--app", "ST",
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


def validate_notification(task: dict, new_state: str, notification_text: str) -> tuple[bool, list[str]]:
    """Layer 1: 验证通知内容与 task 实际状态一致。

    独立于通知生成逻辑，直接从 task 数据交叉验证通知文本。

    Checks:
      1. 通知文本包含与 new_state 匹配的关键词
      2. 通知文本包含 task_id（full 或 short 形式）
      3. 通知文本包含任务标题（至少部分匹配）
      4. review 通知必须包含操作指引（Go/NoGo）

    Args:
        task: Task dict
        new_state: The target state (review, blocked, done)
        notification_text: Generated notification text to validate

    Returns:
        (valid, issues) — valid=True if all checks pass, issues=list of problem descriptions
    """
    if not NOTIFICATION_VALIDATE_ENABLED or not CROSS_CHECK_ENABLED:
        return True, []

    issues = []
    task_id = task.get("id", "")
    title = task.get("title", "")
    text_lower = notification_text.lower()

    # Check 1: State keyword consistency
    state_keywords = {
        "review": ["review", "等待", "确认", "📋"],
        "done":   ["完成", "done", "✅"],
        "blocked": ["blocked", "阻塞", "🚨"],
    }
    expected = state_keywords.get(new_state, [])
    if expected and not any(kw in text_lower for kw in expected):
        issues.append(f"通知内容缺少状态关键词: expected one of {expected} for state '{new_state}'")

    # Check 2: Task ID presence (full or short form)
    if task_id:
        parts = task_id.split("-")
        short_id = f"{parts[0]}-{parts[-1]}" if len(parts) == 3 else task_id
        if task_id not in notification_text and short_id not in notification_text:
            issues.append(f"通知内容缺少 task_id: {task_id} 或 {short_id}")

    # Check 3: Title presence (at least first 10 chars if title is long)
    if title:
        title_check = title[:10] if len(title) > 10 else title
        if title_check not in notification_text:
            issues.append(f"通知内容缺少任务标题: '{title_check}...'")

    # Check 4: Review notifications must have action guidance
    if new_state == "review":
        if "go" not in text_lower and "nogo" not in text_lower:
            issues.append("review 通知缺少 Go/NoGo 操作指引")

    valid = len(issues) == 0
    return valid, issues


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

    # ── Layer 1: 发送前通知内容校验 (T-010) ──
    valid, validation_issues = validate_notification(task, new_state, text)
    if not valid:
        print(f"[scheduler] notification validation issues for {task_id}: {validation_issues}", flush=True)
        bm.append_decision({
            "type": "notification_validation_failed",
            "task_id": task_id,
            "timestamp": bm.now_iso(),
            "state": new_state,
            "issues": validation_issues,
        })
        # 不阻塞发送 — 记录告警但仍发送（避免用户完全收不到通知）

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
            # State transition validation (A-20)
            valid, reason = _validate_state_transition(task, "review")
            if not valid:
                bm.append_decision({
                    "timestamp": bm.now_iso(),
                    "task_id": task_id,
                    "action": "state_validation_warning",
                    "reason": reason,
                })
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

            return {"ok": True, "action": action, "review_id": review_id, "iteration_info": _build_iteration_info(task)}

        elif action == "mark_done":
            # State transition validation (A-20)
            valid, reason = _validate_state_transition(task, "done")
            if not valid:
                bm.append_decision({
                    "timestamp": bm.now_iso(),
                    "task_id": task_id,
                    "action": "state_validation_warning",
                    "reason": reason,
                })
            bm.transition_task(task_id, "done", note=f"orchestrator: {decision.reason}")

            # Notify user: task completed
            notify_task_state_change(task, "done")

            return {"ok": True, "action": action, "iteration_info": _build_iteration_info(task)}

        elif action == "mark_blocked":
            bm.transition_task(task_id, "blocked", note=f"orchestrator: {decision.reason}")

            # Notify user: task blocked, needs decision
            notify_task_state_change(task, "blocked", reason=decision.reason)

            return {"ok": True, "action": action, "reason": decision.reason, "iteration_info": _build_iteration_info(task)}

        elif action == "dispatch_role":
            # Update orchestration state with iteration_detail (A-04)
            orch = task.get("orchestration", {})
            detail = orch.setdefault("iteration_detail", {"total": 0, "phase_advances": 0, "retries": 0})
            detail["total"] += 1

            # Determine if this is a phase advance or retry
            target_role = decision.params.get("role")
            last_role = orch.get("history", [{}])[-1].get("role") if orch.get("history") else None
            if target_role == last_role:
                detail["retries"] += 1
            else:
                detail["phase_advances"] += 1

            orch["iteration"] = detail["total"]  # backward compat
            orch.setdefault("history", []).append({
                "role": target_role,
                "timestamp": bm.now_iso(),
                "context": decision.params.get("context", ""),
                "type": "new_spawn",
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
                "iteration_info": _build_iteration_info(task),
            }

        elif action == "follow_up_worker":
            role = decision.params.get("role")
            orch = task.get("orchestration", {})

            # Update iteration counters (A-04)
            detail = orch.setdefault("iteration_detail", {"total": 0, "phase_advances": 0, "retries": 0})
            detail["total"] += 1
            detail["retries"] += 1  # follow_up only increments retries
            orch["iteration"] = detail["total"]

            # Record history
            orch.setdefault("history", []).append({
                "role": role,
                "timestamp": bm.now_iso(),
                "context": decision.params.get("context", ""),
                "type": "follow_up",
            })
            task["orchestration"] = orch
            bm.save_task(task)

            session_id = _get_follow_up_session(task, role)
            follow_up_message = decision.params.get("context", "")

            # Append budget warning if applicable (A-06)
            workers = task.get("orchestration", {}).get("active_workers", {})
            worker = workers.get(role, {})
            if worker:
                follow_up_message += _budget_warning_text(
                    worker.get("iterations_used", 0),
                    worker.get("max_iterations", 0)
                )

            return {
                "ok": True,
                "action": "follow_up_worker",
                "role": role,
                "session_id": session_id,
                "follow_up_message": follow_up_message,
                "iteration_info": _build_iteration_info(task),
            }

        else:
            return {"ok": False, "error": f"unknown action: {action}"}

    except Exception as exc:
        return {"ok": False, "error": str(exc), "action": action}


def handle_worker_completion(task_id: str, role: str = None,
                             dispatcher_advice: dict | None = None) -> dict:
    """Handle worker completion pipeline.

    Args:
        task_id: Task ID
        role: Optional expected role
        dispatcher_advice: Optional dispatcher judgment (see make_decision)

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

        # Make decision (with optional dispatcher advice)
        decision = make_decision(report, task, dispatcher_advice=dispatcher_advice)

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

你是 Architect Worker，职责：规则裁决 + 方案设计（如需要）+ 验收方案设计。

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

**Step 3.5: 验收方案设计（PL2/PL3 必做）**
- 基于设计方案，产出结构化的 acceptance_plan
- 每个步骤必须包含：step_id, description, category("e2e"|"unit"|"review"|"doc"), expected_result
- 代码开发任务的 acceptance_plan 中必须包含至少一个 category="e2e" 的步骤
- e2e 步骤必须描述具体的端到端验证方式（如"在 dev 环境启动服务并访问 /api/xxx"）

**Step 4: 输出报告**
报告 JSON 中包含：
- rule_verdict.worker_instructions: 渲染好的规则文本（按 MUST/REQUIRED/RECOMMENDED 分组）
- design_notes: 方案设计要点（可为空字符串）
- suggested_rules: 建议新增的规则（可选，如发现规则库未覆盖的场景）
- acceptance_plan: 验收方案（PL2/PL3 必须），格式如下：

```json
"acceptance_plan": [
    {"step_id": "T1", "description": "在 dev 环境部署并启动服务", "category": "e2e", "expected_result": "服务正常启动，无报错"},
    {"step_id": "T2", "description": "浏览器访问页面验证功能", "category": "e2e", "expected_result": "页面正常显示，功能可用"},
    {"step_id": "T3", "description": "单元测试覆盖核心逻辑", "category": "unit", "expected_result": "所有测试通过"},
    {"step_id": "T4", "description": "代码 review 检查规范", "category": "review", "expected_result": "符合编码规范"}
]
```

⚠️ 无 acceptance_plan 的 PL2/PL3 报告会被调度器自动打回。
⚠️ 代码任务的 acceptance_plan 必须包含至少一个 category="e2e" 的步骤。
"""


def _generate_tester_guidance(task: dict) -> str:
    """Generate tester-specific mission guidance — routes to plan-based or free mode.

    If the task has an acceptance_plan, generates plan-execution guidance.
    Otherwise, generates free-test guidance (original behavior).
    """
    acceptance_plan = task.get("acceptance_plan")
    if acceptance_plan and isinstance(acceptance_plan, list) and len(acceptance_plan) > 0:
        return _generate_tester_guidance_with_plan(task, acceptance_plan)
    return _generate_tester_guidance_free(task)


def _generate_tester_guidance_with_plan(task: dict, acceptance_plan: list) -> str:
    """Plan-execution mode: tester follows the acceptance_plan step by step."""

    dev_env_section = ""
    if needs_dev_test(task):
        dev_env_section = f"\n\n{DEV_ENV_GUIDANCE}"

    category_block = dev_env_section

    # Build plan steps text
    plan_lines = []
    for step in acceptance_plan:
        sid = step.get("step_id", "?")
        desc = step.get("description", "")
        cat = step.get("category", "")
        expected = step.get("expected_result", "")
        plan_lines.append(f"- **{sid}** [{cat}]: {desc} → 预期: {expected}")
    plan_text = "\n".join(plan_lines)

    return f"""### Your Mission (Tester) — 方案执行模式

**⚠️ 本任务有预定义验收方案，你必须按方案逐项执行。**

#### 验收方案步骤
{plan_text}

#### 执行要求
1. **逐项执行**: 按 step_id 顺序执行每个验收步骤
2. **记录证据**: 每条 test_evidence 必须包含 step_id 字段对应上述步骤
3. **覆盖率**: 至少覆盖 80% 的步骤（PL3 需 100%）
4. **e2e 步骤**: 必须实际执行，不能只靠 mock
{category_block}

**规则审查（可观测规则子集）：**
- **代码变更范围**: 只修改与任务相关的文件
- **Commit 格式**: 每个 commit 是有意义的独立单元
- **文档更新**: 必要的文档是否按需更新
- **测试覆盖**: 验收基于真实执行结果

**测试证据要求（MUST）**:
```json
"test_evidence": [
    {{"type": "command_output", "command": "pytest tests/", "result": "5 passed", "step_id": "T3"}},
    {{"type": "manual_test", "description": "验证功能X", "result": "OK", "step_id": "T1"}}
]
```
⚠️ 无 test_evidence 的 pass 报告会被打回。
⚠️ 未覆盖的步骤会被自动检测并打回（覆盖率需 ≥80%）。
"""


def _generate_tester_guidance_free(task: dict) -> str:
    """Free-test mode: original tester guidance (no acceptance_plan).

    Includes auditable rule subset: code scope, commit format, docs, test coverage.
    Process rules (env, branch) are validated by Dispatcher, not Tester.
    """
    # ── Dev environment test requirement (conditional) ──
    dev_env_section = ""
    if needs_dev_test(task):
        dev_env_section = f"\n\n{DEV_ENV_GUIDANCE}"

    category_block = dev_env_section

    return f"""### Your Mission (Tester)

1. Review the implementation
2. Run all tests and verify they pass
3. Perform manual testing if needed
4. Check for edge cases and potential issues
{category_block}

**规则审查（可观测规则子集）：**
验证 Developer 实现是否遵守以下可观测规则：
- **代码变更范围**: 只修改与任务相关的文件，无不相关的改动
- **Commit 格式**: 每个 commit 是有意义的独立单元
- **文档更新**: 必要的文档是否按需更新
- **测试覆盖**: 验收基于真实执行结果，非纯 mock

> 注意：过程性规则（如使用哪个环境、哪个分支）由 Dispatcher 校验，你不需要检查。
> 如果发现规则违反，在报告 issues 中标注。

**Developer 报告的 issues（如有，请重点审查）：**
> Developer 的 issues 会通过 prior_context 传递给你，请检查这些 issues 是否已解决。
> 如果 issues 中存在未解决的阻断问题，应报告 fail 而非 pass。

**测试证据要求（MUST）**:
报告中必须包含 test_evidence 字段，记录你实际执行的测试：
```json
"test_evidence": [
    {{"type": "command_output", "command": "pytest tests/", "result": "5 passed, 0 failed"}},
    {{"type": "manual_test", "description": "验证功能X", "result": "OK"}}
]
```
支持的 type: command_output, manual_test, code_review, integration_test, screenshot, browser_test
每个 evidence 必须有 type 和 result 字段。
⚠️ 无 test_evidence 的 pass 报告会被打回。
⚠️ test_evidence 不满足任务分类要求的 pass 报告会被打回。
"""


# ──────────────────────────────────────────
# Phase 2: Auditor, Architect Review, Retrospective guidance templates
# ──────────────────────────────────────────

def _generate_auditor_guidance(task: dict) -> str:
    """Generate auditor guidance focused on FLOW auditing + test quality checking.

    Auditor has dual responsibilities:
    1. Flow audit: check orchestration path integrity, test coverage gaps
    2. Test quality: identify test deficiencies and gaps that Tester missed

    These are complementary to retrospective (which checks process-level issues).
    [V6.1] Enhanced: inject dispatcher session jsonl path for flow verification (D31).
    """
    history = task.get("orchestration", {}).get("history", [])
    actual_flow = [h.get("role") for h in history]

    # V6.1: Inject dispatcher session jsonl path for audit verification (D31)
    session_jsonl_section = ""
    dispatcher_session = task.get("orchestration", {}).get("dispatcher_session_id", "")
    if dispatcher_session:
        jsonl_path = Path.home() / ".nanobot" / "sessions" / f"{dispatcher_session}.jsonl"
        session_jsonl_section = f"""

### Dispatcher Session Log（用于检查流程执行）
请读取此文件检查 dispatcher 的实际调度行为:
`{jsonl_path}`

使用 `read_file` 工具读取，关注：
- spawn subagent 的记录（检查每个角色是否被正确调度）
- 角色执行顺序是否符合模板定义
- 打回后是否有修复+再确认
"""

    return f"""### Your Mission (Auditor — Flow Auditor + Test Quality)

你的职责是审计整个调度链路的合理性，**并检查测试质量**（发现测试缺陷/盲区）。
{session_jsonl_section}
### 审计维度

**A. 调度路径审计（Flow Audit）**
1. **调度路径合理性** — 任务经过了哪些角色？是否有遗漏的环节？打回是否合理？
   - 当前实际流程: {actual_flow}
2. **验收方案覆盖率** — Tester 是否真正覆盖了 acceptance_plan 中的所有步骤？
3. **设计-实现一致性** — Developer 的实现是否偏离了 Architect 的设计？
4. **已知问题回归** — 之前发现的问题是否在本次修复中被正确处理？

**B. 测试质量检查（Test Quality）**
5. **测试真实性** — E2E 测试是否真正端到端（有 session 级/进程级证据）？还是只是函数调用伪装的？
6. **测试盲区** — Tester 的测试是否遗漏了重要的边界条件、异常路径、回归场景？
7. **测试深度** — 测试是否只覆盖了 happy path？负面测试是否充分？
8. **证据可信度** — test_evidence 中的结果是否可复现、可验证？

### 不做什么
- ❌ 不做代码风格审查（那是 code_review 的事）
- ❌ 不重新运行测试（那是 Tester 的事）
- ❌ 不评估设计方案好坏（那是 Plan Reviewer 的事）
- ❌ 不检查流程跳步/环节缺失（那是 retrospective 的事）
- ❌ 不重新评判已通过 test_review 的测试内容质量

### 输出要求
- verdict: pass / fail
- 如果 fail，**必须**明确指出 suggested_target（该打回谁: developer / tester / architect）
  - **developer**: 代码有 bug、实现缺失
  - **architect**: 测试方案有盲区（需要补测试方案，然后 tester 补测）
  - **tester**: 测试执行不到位（方案有但没执行到位）
- 提供具体的问题描述和修复建议
"""


def _generate_architect_review_guidance(task: dict) -> str:
    """Generate guidance for architect_review role (architecture review before development)."""
    return """### Your Mission (Architect Review — 架构评审)

你的职责是评审 Architect 的设计方案是否完整、可行，**在开发开始之前**确认架构质量。

### 审查维度
1. **架构完整性** — 设计方案是否覆盖了所有需求？有无遗漏的场景？
2. **技术可行性** — 方案是否在当前技术栈和约束下可实现？
3. **评测方案可行性** — acceptance_plan 是否覆盖关键验证场景？
4. **风险评估** — 是否识别了关键风险并有缓解措施？
5. **向后兼容** — 方案是否考虑了对现有系统的影响？

### 不做什么
- ❌ 不重新设计方案（那是 architect 的事）
- ❌ 不做代码实现（那是 developer 的事）
- ❌ 不做测试执行（那是 tester 的事）

### 输出
- verdict: pass / fail
- 如果 fail，列出具体的架构问题（会打回 architect 修改）
"""


def _generate_code_review_guidance(task: dict) -> str:
    """Generate guidance for code_review role (code + test coverage review after development).

    V6.1 新角色：检查代码架构一致性 + developer 单元测试覆盖和合理性。
    """
    return """### Your Mission (Code Review — 代码审查)

你的职责是检查 Developer 的代码实现和单元测试质量，**不是重新做设计或执行测试**。

### 审查维度
1. **架构一致性** — 代码实现是否符合 Architect 的设计方案？有无偏离设计的地方？
2. **接口契约** — 函数签名、返回类型、数据结构是否与设计一致？
3. **测试覆盖** — Developer 是否编写了充分的单元测试？
4. **测试合理性** — 单元测试是否覆盖了关键路径和边界情况？
   - 关键路径：核心业务逻辑是否有对应测试？
   - 边界情况：空值、极端值、错误输入是否覆盖？
   - 测试质量：测试是否真正验证了行为（不是只检查不报错）？
5. **代码质量** — 可读性、错误处理、边界条件处理

### 不做什么
- ❌ 不重新设计方案（那是 architect 的事）
- ❌ 不执行测试（那是 tester 的事）
- ❌ 不检查流程合规性（那是 auditor 的事）
- ❌ 不做代码风格审查（只看设计一致性和功能正确性）

### 输出
- verdict: pass — 代码符合设计，测试覆盖充分
- verdict: fail — 发现需要 Developer 修复的问题（会打回 developer）
  - 在 issues 中列出具体问题
"""


def _generate_test_review_guidance(task: dict) -> str:
    """Generate guidance for test_review role (semantic review of test process and report).

    V6.1 新角色：基于语义的测试过程和测试报告审查。
    """
    return """### Your Mission (Test Review — 测试审查)

你的职责是审查 Tester 的测试过程和报告质量，**不是重新执行测试**。

### 审查维度
1. **测试真实性** — E2E 测试是否真正端到端执行？（非 mock/模拟代替真实执行）
2. **测试盲区** — 是否遗漏了边界情况、异常路径、并发场景？
3. **测试深度** — 不仅 happy path，还有 error path 和 edge case？
4. **证据可信度** — 测试输出是否可复现、可验证？证据是否充分？
5. **报告完整性** — test_evidence 是否完整记录了测试过程和结果？

### 不做什么
- ❌ 不重新执行测试（那是 tester 的事）
- ❌ 不审查代码（那是 code_review 的事）
- ❌ 不检查流程合规性（那是 auditor 的事）
- ❌ 不重新评判已通过的代码审查结论

### 输出
- verdict: pass — 测试过程严谨、覆盖充分、证据可信
- verdict: fail — 发现测试不足（会打回 tester 补充测试）
  - 在 issues 中列出具体的测试不足之处
"""


def _generate_retrospective_guidance(task: dict) -> str:
    """Generate retrospective guidance for flow completeness review.

    Retrospective checks for process-level issues:
    - Missing steps, skipped roles, bypassed gates
    - Complementary to Auditor (which checks test quality + flow audit)
    [V6.1] Enhanced: inject dispatcher session jsonl path for flow verification (D31).
    """
    ft = resolve_flow_type(task)
    history = task.get("orchestration", {}).get("history", [])

    # Get expected flow for this flow type
    expected_flow = _get_expected_flow(ft)
    actual_flow = [h.get("role") for h in history]

    # V6.1: Inject dispatcher session jsonl path for audit verification (D31)
    session_jsonl_section = ""
    dispatcher_session = task.get("orchestration", {}).get("dispatcher_session_id", "")
    if dispatcher_session:
        jsonl_path = Path.home() / ".nanobot" / "sessions" / f"{dispatcher_session}.jsonl"
        session_jsonl_section = f"""
### Dispatcher Session Log（用于检查流程执行）
请读取此文件检查 dispatcher 的实际调度行为:
`{jsonl_path}`

使用 `read_file` 工具读取，关注 spawn subagent 的记录。
"""

    return f"""### Your Mission (Retrospective — 流程复盘)

你的职责是复盘整个调度流程的**完整性和规范性**，检查是否有环节被跳过或缺失。
{session_jsonl_section}
### 流程信息
- **流程级别**: {pl}
- **应有流程**: {expected_flow}
- **实际流程**: {actual_flow}

### 检查清单（流程缺陷检查）
1. **环节完整性** — 对照 {pl} 应有流程，检查实际流程是否有遗漏
   - 注意：打回重试导致的重复角色是正常的，只检查是否有应有但从未出现的角色
2. **打回合理性** — 每次打回是否有正当理由？打回后是否回到了正确的角色？
3. **Gate 触发记录** — 代码 gate 是否正常工作？有无被绕过的情况？
4. **跳步检测** — 是否有角色被直接跳过（不是因为打回，而是流程设计遗漏）？

### 不做什么
- ❌ 不检查测试质量（那是 Auditor 的事）
- ❌ 不检查代码实现质量（那是 code_review 的事）
- ❌ 不重新运行测试（那是 Tester 的事）

### 输出要求
- **verdict: pass** — 流程完整，无缺失环节。如有流程设计改进建议，写入 issues
- **verdict: fail** — 发现缺失环节，**必须**指明：
  - missing_role: 缺失的角色（如 "tester", "architect" 等）
  - missing_reason: 为什么认为缺失
  - 状态机会根据 missing_role 打回补齐

### 两种产出
1. **即时修复（verdict=fail）**: 发现缺失环节 → 指明 missing_role → 状态机打回补齐
2. **长期改进（verdict=pass + issues）**: 流程设计缺陷 → 记录到改进日志（不阻塞当前流程）
"""


def _get_expected_flow(ft: str) -> list[str]:
    """Get the expected role sequence for a given flow type."""
    tpl = FLOW_TEMPLATES.get(ft)
    if tpl:
        roles = list(tpl["roles"])
        if tpl.get("has_auditor"):
            roles.append("auditor")
        if tpl.get("has_retrospective"):
            roles.append("retrospective")
        return roles
    return ["developer"]  # fallback


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
                "**冒烟测试要求（MUST — 代码任务必做）**:",
                "完成代码后，必须执行基本冒烟测试：",
                "1. `python -c \"import <your_module>\"` — 验证代码可导入",
                "2. 主入口执行（如 `python script.py --help`）— 验证无启动报错",
                "",
                "在报告中记录：",
                '```json',
                '"smoke_test": {',
                '    "command": "python -c \\"import scheduler\\"",',
                '    "status": "pass",',
                '    "output": "no errors"',
                '}',
                '```',
                "",
                "**Git Commit 规范**:",
                "- commit message 必须包含 Task ID，格式: `feat(task_id): 描述`",
                "- 例如: `feat(T-20260401-003): add design gate check`",
                "",
            ]
    elif role == "tester":
        lines += [_generate_tester_guidance(task), ""]

        # ── Inject acceptance_plan if available ──
        acceptance_plan = task.get("acceptance_plan")
        if acceptance_plan and isinstance(acceptance_plan, list) and len(acceptance_plan) > 0:
            lines += [
                "### 预定义验收方案（MUST 按此执行）",
                "",
                "以下是 Architect 产出的验收方案，你必须逐项执行并在 test_evidence 中对应每一步：",
                "",
            ]
            for step in acceptance_plan:
                sid = step.get("step_id", "?")
                desc_text = step.get("description", "")
                cat = step.get("category", "")
                expected = step.get("expected_result", "")
                lines.append(f"- **{sid}** [{cat}]: {desc_text} → 预期: {expected}")
            lines += [
                "",
                "⚠️ test_evidence 中的每条记录必须包含 step_id 字段，对应上述步骤。",
                "⚠️ 未覆盖的步骤会被自动检测并打回（覆盖率需 ≥80%）。",
                "",
            ]

    elif role == "auditor":
        lines += [_generate_auditor_guidance(task), ""]

    elif role == "architect_review":
        lines += [_generate_architect_review_guidance(task), ""]

    elif role == "code_review":
        lines += [_generate_code_review_guidance(task), ""]

    elif role == "test_review":
        lines += [_generate_test_review_guidance(task), ""]

    elif role == "retrospective":
        lines += [_generate_retrospective_guidance(task), ""]

    # Role-specific report field hints
    role_report_extra = ""
    if role == "auditor":
        role_report_extra = (
            '\n  "suggested_target": "developer|tester|architect",'
            '  // REQUIRED if verdict=fail — who to send back to'
        )
    elif role == "retrospective":
        role_report_extra = (
            '\n  "missing_role": "role_name",'
            '  // REQUIRED if verdict=fail — which role was skipped'
            '\n  "missing_reason": "why this role was needed"'
            '  // REQUIRED if verdict=fail'
        )

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
        f'  "files_changed": ["path/to/file"]{role_report_extra}',
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
    verification = VERIFICATION_GUIDANCE["backend_script"]

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
    role_emoji = {"developer": "🔨", "tester": "🧪", "architect": "📐",
                  "auditor": "🔍", "architect_review": "📋", "code_review": "🔎",
                  "test_review": "📝", "retrospective": "🔄"}.get(role, "🔨")

    # Role-based iteration limits (A-06: dynamic per Architect budget)
    max_iterations = _get_role_iteration_limit(role, task)

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

        # ★ 4.4 Template confirmation (T-006)
        confirmed_tpl, confirm_reason, tpl_changed = confirm_template_assignment(task)
        if tpl_changed:
            # Update task template in memory (for subsequent role/gate checks)
            if "workgroup" in task:
                task["workgroup"]["template"] = confirmed_tpl
            else:
                task["template"] = confirmed_tpl
            # Persist template change
            if not dry_run:
                try:
                    bm.save_task(task)
                except Exception:
                    pass

        # Template corrected to quick → skip (extremely rare due to conservative rules)
        if confirmed_tpl == "quick":
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

        # M3: Write initial orchestration history record after first dispatch
        if not dry_run:
            try:
                orch = task.setdefault("orchestration", {})
                orch.setdefault("history", []).append({
                    "role": initial_role,
                    "timestamp": bm.now_iso(),
                    "context": "initial dispatch",
                    "type": "initial_spawn",
                })
                orch["current_role"] = initial_role
                detail = orch.setdefault("iteration_detail", {})
                detail["total"] = detail.get("total", 0) + 1
                detail["phase_advances"] = detail.get("phase_advances", 0) + 1
                bm.save_task(task)
            except Exception:
                pass

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
