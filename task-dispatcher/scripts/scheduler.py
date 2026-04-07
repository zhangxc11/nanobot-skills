#!/usr/bin/env python3
from __future__ import annotations
"""
scheduler.py — Task Dispatcher Scheduler (v2.0)

Minimal scheduler: pure **tool library** for the Dispatcher agent.

  - Task queue management (sort, capacity, dependency, stale recovery)
  - State transitions (run → executing, mark-done, mark-blocked)
  - Process recording (record-spawn, handle-completion, history)
  - Audit enforcement (mark-done requires auditor pass)

All role flow knowledge lives in pattern documents (e.g. dev-pipeline.md),
NOT in this code. The Dispatcher agent reads patterns and decides next roles.

CLI:
    python3 scheduler.py run [--parent SESSION_ID] [--dry-run]
    python3 scheduler.py record-spawn --task-id T-xxx --role R [--phase P]
    python3 scheduler.py handle-completion --task-id T-xxx
    python3 scheduler.py mark-done --task-id T-xxx
    python3 scheduler.py mark-blocked --task-id T-xxx --reason "..."
    python3 scheduler.py status
"""

SCHEDULER_VERSION = "2.0.0"

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Ensure scripts/ is on sys.path for task_store import ──
_SCRIPTS_DIR = Path(__file__).absolute().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import task_store as bm

# ──────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────

WORKSPACE = Path(__file__).absolute().parent.parent.parent.parent
MAX_CONCURRENT = 3                # Max tasks in 'executing' at any time
MAX_DISPATCH_PER_RUN = 3          # Max NEW tasks per scheduler invocation
STALE_TIMEOUT_HOURS = 4           # Hours before executing task is considered stale
MAX_TIMEOUT_RECOVERY = 3          # After N recoveries → blocked
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
REPORTS_DIR = WORKSPACE / "data" / "brain" / "reports"
FEISHU_NOTIFY_RECIPIENT = "ou_2fba93da1d059fd2520c2f385743f175"

# Pattern map: flow_type → pattern document name
PATTERN_MAP = {
    "standard-dev": "dev-pipeline",
    "cron-auto": "self-check",
}

REPORT_SCHEMA_REQUIRED = {"task_id", "role", "verdict", "summary"}
VALID_VERDICTS = {"pass", "fail", "blocked", "partial"}
VALID_ROLES = frozenset({
    "developer", "tester", "architect", "auditor",
    "architect_review", "retrospective",
})


# ──────────────────────────────────────────
# Task Queue (preserved from v1, simplified)
# ──────────────────────────────────────────

def get_schedulable_tasks() -> list[dict]:
    """Get all 'queued' tasks, sorted by priority."""
    tasks = bm.list_tasks(status_filter={"queued"})
    return sort_by_priority(tasks)


def sort_by_priority(tasks: list[dict]) -> list[dict]:
    """Sort: P0 first, then P1, then P2. Same priority → oldest first."""
    def _key(t: dict):
        prio = PRIORITY_ORDER.get(t.get("priority", "P2"), 2)
        created = t.get("created", "9999")
        return (prio, created)
    return sorted(tasks, key=_key)


def get_executing_count() -> int:
    """Count currently executing tasks."""
    return len(bm.list_tasks(status_filter={"executing"}))


def check_capacity() -> int:
    """Available slots = MAX_CONCURRENT - currently executing."""
    return max(0, MAX_CONCURRENT - get_executing_count())


def check_dependency(task: dict, all_tasks_map: dict[str, dict]) -> bool:
    """True if all dependencies are satisfied (done/cancelled/dropped)."""
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


def is_quick_task(task: dict) -> bool:
    """Quick tasks bypass scheduling — handled inline."""
    tpl = task.get("workgroup", {}).get("template", "") or task.get("template", "")
    return tpl == "quick"


# ──────────────────────────────────────────
# Stale Recovery (preserved from v1, simplified)
# ──────────────────────────────────────────

def check_stale_tasks() -> list[dict]:
    """Find tasks stuck in 'executing' beyond STALE_TIMEOUT_HOURS."""
    stale = []
    now = datetime.now().astimezone()
    timeout = timedelta(hours=STALE_TIMEOUT_HOURS)

    for task in bm.list_tasks(status_filter={"executing"}):
        entered_at = _find_executing_timestamp(task)
        if entered_at is None:
            continue
        if entered_at.tzinfo is None:
            entered_at = entered_at.astimezone()
        elapsed = now - entered_at
        if elapsed > timeout:
            stale.append({
                "task_id": task["id"],
                "title": task.get("title", ""),
                "elapsed_hours": round(elapsed.total_seconds() / 3600, 1),
                "timeout_count": task.get("timeout_count", 0),
            })
    return stale


def _find_executing_timestamp(task: dict) -> datetime | None:
    """Find when task last entered 'executing' state."""
    for entry in reversed(task.get("history", [])):
        if entry.get("action") == "status_change" and "→ executing" in entry.get("detail", ""):
            try:
                return datetime.fromisoformat(entry["timestamp"])
            except (ValueError, KeyError):
                pass
    # Fallback: updated timestamp
    try:
        return datetime.fromisoformat(task.get("updated", ""))
    except (ValueError, KeyError):
        return None


def recover_stale(dry_run: bool = False) -> list[dict]:
    """Recover stale tasks: queued (retry) or blocked (too many retries).

    Also writes stale_recovery entries to orchestration history (P2-3).
    """
    stale_tasks = check_stale_tasks()
    recovered = []

    for s in stale_tasks:
        task_id = s["task_id"]
        count = s["timeout_count"]

        if dry_run:
            action = "would_block" if count + 1 >= MAX_TIMEOUT_RECOVERY else "would_queue"
            recovered.append({**s, "action": action})
            continue

        try:
            task = bm.load_task(task_id)
            new_count = count + 1

            # B-3: Transition first (state change), then write history (recording)
            if new_count >= MAX_TIMEOUT_RECOVERY:
                bm.transition_task(
                    task_id, "blocked", force=True,
                    note=f"超时回收已达 {new_count} 次（阈值 {MAX_TIMEOUT_RECOVERY}），转为 blocked",
                )
                action = "blocked"
            else:
                bm.transition_task(
                    task_id, "queued", force=True,
                    note=f"超时回收: executing 已超 {s['elapsed_hours']}h（第 {new_count} 次回收）",
                )
                action = "queued"

            # Write stale_recovery to orchestration history after successful transition
            task = bm.load_task(task_id)  # reload after transition
            task["timeout_count"] = new_count
            orch = task.setdefault("orchestration", {})
            orch.setdefault("history", []).append({
                "type": "stale_recovery",
                "timeout_count": new_count,
                "elapsed_hours": s["elapsed_hours"],
                "action": action,
                "timestamp": bm.now_iso(),
            })
            bm.save_task(task)

            recovered.append({**s, "action": action, "timeout_count": new_count})
        except Exception as exc:
            recovered.append({"task_id": task_id, "action": "error", "error": str(exc)})

    return recovered


# ──────────────────────────────────────────
# Auto-enqueue
# ──────────────────────────────────────────

def auto_enqueue_pending(dry_run: bool = False) -> list[dict]:
    """Transition all pending tasks to queued (if they have basic fields)."""
    enqueued = []
    for task in bm.list_tasks(status_filter={"pending"}):
        tid = task.get("id", "")
        desc = task.get("description", "") or task.get("title", "")
        if not tid or not desc:
            continue
        if not dry_run:
            try:
                bm.transition_task(tid, "queued", note="auto-enqueue by scheduler")
            except Exception as exc:
                print(f"[scheduler] enqueue failed {tid}: {exc}", flush=True)
                continue
        enqueued.append({"task_id": tid, "title": task.get("title", "")})
    return enqueued


# ──────────────────────────────────────────
# Core: run()
# ──────────────────────────────────────────

def run(parent_session_id: str = "", dry_run: bool = False) -> dict:
    """Scan queue → sort → mark executing → return dispatch list.

    This is the main entry point. The Dispatcher agent calls this,
    then reads pattern docs and spawns roles for each dispatched task.
    """
    # 1. Auto-enqueue pending → queued
    auto_enqueued = auto_enqueue_pending(dry_run=dry_run)

    # 2. Stale recovery
    stale_recovered = recover_stale(dry_run=dry_run)

    # 3. Get schedulable tasks
    tasks = get_schedulable_tasks()
    all_tasks_map = {t["id"]: t for t in bm.list_tasks()}

    # 4. Capacity
    slots = min(check_capacity(), MAX_DISPATCH_PER_RUN)

    dispatched = []
    skipped_dep = []
    skipped_cap = []
    errors = []

    for task in tasks:
        if is_quick_task(task):
            continue

        # Dependency check
        if not check_dependency(task, all_tasks_map):
            skipped_dep.append(task["id"])
            continue

        # Capacity check
        if len(dispatched) >= slots:
            skipped_cap.append(task["id"])
            continue

        # Dispatch: queued → executing
        if not dry_run:
            try:
                bm.transition_task(task["id"], "executing", note="scheduler dispatch")
            except Exception as exc:
                errors.append({"task_id": task["id"], "error": str(exc)})
                continue

        # Determine pattern
        flow_type = task.get("type", "") or task.get("workgroup", {}).get("template", "standard-dev")
        pattern = PATTERN_MAP.get(flow_type, "dev-pipeline")

        dispatched.append({
            "task_id": task["id"],
            "title": task.get("title", ""),
            "priority": task.get("priority", "P2"),
            "description": task.get("description", ""),
            "pattern": pattern,
            "pattern_path": f"skills/role-flow/patterns/{pattern}.md",
        })

    # Refresh briefing
    if dispatched and not dry_run:
        try:
            bm.atomic_write(bm.BRIEFING_FILE, bm.generate_briefing())
        except Exception:
            pass

    return {
        "ok": True,
        "dispatched": dispatched,
        "slots_remaining": max(0, slots - len(dispatched)),
        "auto_enqueued": auto_enqueued,
        "stale_recovered": stale_recovered,
        "skipped_dependency": skipped_dep,
        "skipped_capacity": skipped_cap,
        "errors": errors,
        "dry_run": dry_run,
    }


# ──────────────────────────────────────────
# Process Recording: record_spawn
# ──────────────────────────────────────────

def record_spawn(task_id: str, role: str, phase: str | None = None) -> dict:
    """Record a role spawn event to orchestration history.

    The Dispatcher agent calls this BEFORE using the framework spawn tool.
    """
    task = bm.load_task(task_id)
    orch = task.setdefault("orchestration", {})
    orch.setdefault("history", []).append({
        "type": "spawn",
        "role": role,
        "phase": phase,
        "timestamp": bm.now_iso(),
    })
    orch["current_role"] = role
    bm.save_task(task)
    return {"ok": True, "task_id": task_id, "role": role, "phase": phase}


# ──────────────────────────────────────────
# Process Recording: handle_completion
# ──────────────────────────────────────────

def handle_completion(task_id: str) -> dict:
    """Read role report, record to history, return verdict + prior_context.

    Pure tool function — does NOT make flow decisions.
    The Dispatcher agent reads the return value and decides next steps.
    """
    task = bm.load_task(task_id)
    report = _parse_latest_report(task_id)

    if not report:
        return {"ok": False, "action": "no_report", "reason": "no report found for " + task_id}

    role = report.get("role", "unknown")
    verdict = report.get("verdict", "unknown")

    # Record completion to orchestration history
    orch = task.setdefault("orchestration", {})
    history = orch.setdefault("history", [])

    # B-1: Idempotency — skip if last entry is already the same completion
    if history:
        last = history[-1]
        if (last.get("type") == "completion"
                and last.get("role") == role
                and last.get("verdict") == verdict):
            context = extract_prior_context(report)
            return {
                "ok": True,
                "task_id": task_id,
                "role": role,
                "verdict": verdict,
                "summary": report.get("summary", ""),
                "prior_context": context,
                "history": history,
                "warning": "idempotent: completion already recorded",
            }

    # P2-2: Check for matching spawn entry
    has_spawn = any(
        h.get("type") == "spawn" and h.get("role") == role
        for h in history
    )
    warning = None
    if not has_spawn:
        warning = f"WARNING: no spawn entry for role '{role}' — agent may have skipped record-spawn"

    history.append({
        "type": "completion",
        "role": role,
        "verdict": verdict,
        "summary": report.get("summary", ""),
        "timestamp": bm.now_iso(),
    })
    bm.save_task(task)

    # Extract prior_context for next role
    context = extract_prior_context(report)

    return {
        "ok": True,
        "task_id": task_id,
        "role": role,
        "verdict": verdict,
        "summary": report.get("summary", ""),
        "prior_context": context,
        "history": history,
        "warning": warning,
    }


# ──────────────────────────────────────────
# Prior Context Extraction
# ──────────────────────────────────────────

def extract_prior_context(report: dict) -> str:
    """Extract key fields from a role report for passing to the next role.

    Generic extraction — no per-role hardcoding.
    """
    lines = [f"## 前序角色产出: {report.get('role', '?')}"]
    lines.append(f"Verdict: {report.get('verdict')}")
    lines.append(f"Summary: {report.get('summary', '')}")

    # Generic fields any role might produce
    if output_files := report.get("output_files"):
        lines.append(f"产出文件: {', '.join(output_files)}")
    if issues := report.get("issues"):
        lines.append(f"Issues: {issues}")
    if files_changed := report.get("files_changed"):
        lines.append(f"变更文件: {', '.join(files_changed)}")

    # Specific fields — pass through if present
    if plan := report.get("acceptance_plan"):
        lines.append(f"验收方案: {json.dumps(plan, ensure_ascii=False)}")
    if rule_verdict := report.get("rule_verdict"):
        lines.append(f"规则裁决: {json.dumps(rule_verdict, ensure_ascii=False)}")
    if design := report.get("design_notes"):
        lines.append(f"设计要点: {design}")
    if smoke := report.get("smoke_test"):
        lines.append(f"冒烟测试: {json.dumps(smoke)}")
    if evidence := report.get("test_evidence"):
        lines.append(f"测试证据: {json.dumps(evidence, ensure_ascii=False)}")

    return "\n".join(lines)


# ──────────────────────────────────────────
# Report Parsing (simplified from v1)
# ──────────────────────────────────────────

def _parse_latest_report(task_id: str, role: str | None = None) -> dict | None:
    """Find and parse the most recent report for a task.

    Reports are JSON files in REPORTS_DIR named: {task_id}-{role}-{timestamp}.json
    """
    if not REPORTS_DIR.exists():
        return None

    pattern = f"{task_id}-{role}-*.json" if role else f"{task_id}-*.json"
    report_files = list(REPORTS_DIR.glob(pattern))
    if not report_files:
        return None

    # Verdict severity: prefer fail over pass within same round
    _VERDICT_PRIO = {"blocked": 0, "fail": 1, "partial": 2, "pass": 3}

    def _extract_round(filename: str) -> int:
        m = re.search(r'-[Rr](\d+)-', filename)
        return int(m.group(1)) if m else 1

    valid = []
    for f in report_files:
        try:
            with f.open("r", encoding="utf-8") as fh:
                report = json.load(fh)
            # Validate required fields
            if not REPORT_SCHEMA_REQUIRED.issubset(report.keys()):
                continue
            if report.get("task_id") != task_id:
                continue
            if report.get("role") not in VALID_ROLES:
                continue
            if report.get("verdict") not in VALID_VERDICTS:
                continue
            rnd = _extract_round(f.name)
            vp = _VERDICT_PRIO.get(report["verdict"], 3)
            valid.append((rnd, vp, report))
        except (json.JSONDecodeError, OSError):
            continue

    if not valid:
        return None

    # Latest round, then most severe verdict
    max_round = max(v[0] for v in valid)
    latest = [(vp, rpt) for rnd, vp, rpt in valid if rnd == max_round]
    latest.sort(key=lambda x: x[0])
    return latest[0][1]


# ──────────────────────────────────────────
# Audit Enforcement
# ──────────────────────────────────────────

def check_audit(task: dict) -> bool:
    """Check if auditor has been executed and passed."""
    history = task.get("orchestration", {}).get("history", [])
    return any(
        h.get("type") == "completion"
        and h.get("role") == "auditor"
        and h.get("verdict") == "pass"
        for h in history
    )


# ──────────────────────────────────────────
# State Transitions: mark_done / mark_blocked
# ──────────────────────────────────────────

def mark_done(task_id: str) -> dict:
    """Mark task as done. Requires auditor pass (double-check with task_store guard)."""
    task = bm.load_task(task_id)

    # Scheduler-level audit check (first layer)
    if not check_audit(task):
        return {
            "ok": False,
            "action": "rejected",
            "reason": "auditor not executed or not passed — spawn auditor first",
        }

    # task_store.transition_task has its own guard (second layer)
    try:
        bm.transition_task(task_id, "done", note="completed via scheduler")
    except ValueError as exc:
        return {"ok": False, "action": "rejected", "reason": str(exc)}

    task = bm.load_task(task_id)
    notify(task, "done")
    return {"ok": True, "action": "done", "task_id": task_id}


def mark_blocked(task_id: str, reason: str) -> dict:
    """Mark task as blocked."""
    try:
        bm.transition_task(task_id, "blocked", note=reason)
    except ValueError as exc:
        return {"ok": False, "action": "rejected", "reason": str(exc)}

    task = bm.load_task(task_id)
    notify(task, "blocked", reason=reason)
    return {"ok": True, "action": "blocked", "task_id": task_id, "reason": reason}


# ──────────────────────────────────────────
# Notification (simplified from v1)
# ──────────────────────────────────────────

def notify(task: dict, state: str, reason: str = "") -> bool:
    """Send feishu notification for state changes (done/blocked).

    Failures are logged but never block the scheduler flow.
    """
    task_id = task.get("id", "")
    title = task.get("title", "")
    short_id = task_id.split("-")[-1] if "-" in task_id else task_id

    if state == "done":
        text = f"✅ [{short_id}] {title} — 已完成"
    elif state == "blocked":
        text = f"🚫 [{short_id}] {title} — 已阻塞\n原因: {reason}"
    else:
        return False

    return _send_feishu_notify(text, task_id)


def _send_feishu_notify(text: str, task_id: str = "") -> bool:
    """Send feishu text notification via feishu_messenger.py CLI."""
    import subprocess
    messenger = WORKSPACE / "skills" / "feishu-messenger" / "scripts" / "feishu_messenger.py"
    if not messenger.exists():
        print(f"[scheduler] feishu_messenger.py not found", flush=True)
        return False
    try:
        result = subprocess.run(
            [
                sys.executable, str(messenger),
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


# ──────────────────────────────────────────
# Status
# ──────────────────────────────────────────

def get_status() -> dict:
    """Current scheduler status overview (read-only)."""
    all_tasks = bm.list_tasks()
    counts: dict[str, int] = {}
    for t in all_tasks:
        s = t.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    queued = sort_by_priority(bm.list_tasks(status_filter={"queued"}))
    executing = bm.list_tasks(status_filter={"executing"})
    blocked = bm.list_tasks(status_filter={"blocked"})

    return {
        "ok": True,
        "data": {
            "version": SCHEDULER_VERSION,
            "timestamp": bm.now_iso(),
            "task_counts": counts,
            "queued_tasks": [
                {"id": t["id"], "title": t.get("title", ""), "priority": t.get("priority", "")}
                for t in queued
            ],
            "executing_tasks": [
                {"id": t["id"], "title": t.get("title", ""),
                 "current_role": t.get("orchestration", {}).get("current_role", "")}
                for t in executing
            ],
            "blocked_tasks": [
                {"id": t["id"], "title": t.get("title", "")}
                for t in blocked
            ],
            "available_slots": check_capacity(),
            "max_concurrent": MAX_CONCURRENT,
        },
    }


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Task Dispatcher Scheduler — queue management + state transitions + recording",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Scan queue, dispatch tasks (queued→executing)")
    p_run.add_argument("--parent", default="", help="Parent session ID")
    p_run.add_argument("--dry-run", action="store_true", help="Preview without state changes")

    # record-spawn
    p_spawn = sub.add_parser("record-spawn", help="Record role spawn event to history")
    p_spawn.add_argument("--task-id", required=True, help="Task ID")
    p_spawn.add_argument("--role", required=True, help="Role being spawned")
    p_spawn.add_argument("--phase", default=None, help="Phase (design/code_review/test_review)")

    # handle-completion
    p_comp = sub.add_parser("handle-completion", help="Process role completion, record history")
    p_comp.add_argument("--task-id", required=True, help="Task ID")

    # mark-done
    p_done = sub.add_parser("mark-done", help="Mark task done (requires auditor pass)")
    p_done.add_argument("--task-id", required=True, help="Task ID")

    # mark-blocked
    p_block = sub.add_parser("mark-blocked", help="Mark task blocked")
    p_block.add_argument("--task-id", required=True, help="Task ID")
    p_block.add_argument("--reason", required=True, help="Block reason")

    # status
    sub.add_parser("status", help="Show current scheduler status")

    args = parser.parse_args()

    try:
        if args.command == "run":
            result = run(parent_session_id=args.parent, dry_run=args.dry_run)
        elif args.command == "record-spawn":
            result = record_spawn(args.task_id, args.role, args.phase)
        elif args.command == "handle-completion":
            result = handle_completion(args.task_id)
        elif args.command == "mark-done":
            result = mark_done(args.task_id)
        elif args.command == "mark-blocked":
            result = mark_blocked(args.task_id, args.reason)
        elif args.command == "status":
            result = get_status()
        else:
            parser.print_help()
            sys.exit(1)
    except (FileNotFoundError, ValueError) as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:
        result = {"ok": False, "error": f"unexpected: {type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
