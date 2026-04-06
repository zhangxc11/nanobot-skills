#!/usr/bin/env python3
"""
task_store.py - Task Dispatcher Data Store CLI
Phase 1.0 + 1.1 + 1.2

Architecture notes:
- BRIEFING.md and REGISTRY.md are derived views; never store canonical state in them
- All state mutations go through save_task() / save_review() for consistency
- Atomic writes via tempfile + os.rename prevent partial-write corruption
- review resolve accepts both Task ID (T-xxx) and Review ID (R-xxx) for flexibility
- urgent list in BRIEFING uses ordered-dict dedup: P0 tasks and pending-review tasks
  may overlap; we preserve insertion order while eliminating duplicates
- Template YAML files live in skills/task-dispatcher/templates/
- Quick tasks are stored in data/brain/quick-log.jsonl (no YAML file created)
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:
    print(json.dumps({"ok": False, "error": "PyYAML not installed. Run: pip install pyyaml"}))
    sys.exit(1)

# ──────────────────────────────────────────
# Paths  (resolved at import time so tests can monkey-patch before calling handlers)
# ──────────────────────────────────────────

# Script lives at:  <workspace>/skills/task-dispatcher/scripts/task_store.py
# So workspace root is 4 levels up.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_task_data_dir_env = os.environ.get("TASK_DATA_DIR") or os.environ.get("BRAIN_DIR")
TASK_DATA_DIR  = Path(_task_data_dir_env) if _task_data_dir_env else WORKSPACE_ROOT / "data" / "tasks"
BRAIN_DIR      = TASK_DATA_DIR  # backward compat alias
TASKS_DIR      = TASK_DATA_DIR / "tasks"
REVIEWS_DIR    = TASK_DATA_DIR / "reviews"
BRIEFING_FILE  = TASK_DATA_DIR / "BRIEFING.md"
REGISTRY_FILE  = TASK_DATA_DIR / "REGISTRY.md"
TEMPLATES_DIR  = Path(__file__).resolve().parent.parent / "templates"
QUICK_LOG      = TASK_DATA_DIR / "quick-log.jsonl"
QUICK_ARCHIVE_DIR = TASK_DATA_DIR / "archive" / "quick"
DECISIONS_LOG  = TASK_DATA_DIR / "decisions.jsonl"
CHECKLISTS_DIR = WORKSPACE_ROOT / "data" / "tasks" / "checklists"
REVIEW_RESULTS_DIR = TASK_DATA_DIR / "review-results"

# ──────────────────────────────────────────
# Domain constants
# ──────────────────────────────────────────

VALID_TRANSITIONS: dict[str, set] = {
    "pending":   {"queued", "dropped", "cancelled"},  # auto-enqueue by scheduler
    "queued":    {"executing", "blocked", "dropped", "cancelled"},
    "executing": {"review", "done", "blocked", "dropped", "cancelled", "queued"},  # queued: timeout recovery
    "blocked":   {"queued", "executing", "dropped", "cancelled"},
    "review":    {"executing", "revision", "done", "dropped", "cancelled"},  # DEPRECATED(v10): review state — replaced by role-flow patterns
    "revision":  {"executing", "dropped", "cancelled"},  # DEPRECATED(v10): revision state — replaced by role-flow patterns
    "done":      set(),        # terminal state
    "dropped":   {"queued"},   # re-activation allowed
    "cancelled": {"queued"},   # legacy alias for dropped, re-activation allowed
}

VALID_TYPES      = {"quick", "standard-dev", "batch-dev", "long-task", "cron-auto"}
VALID_PRIORITIES = {"P0", "P1", "P2"}
VALID_STATUSES   = set(VALID_TRANSITIONS.keys())

BRIEFING_MAX_LINES = 50

# ──────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def today_iso_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def atomic_write(path: Path, content: str) -> None:
    """Write content atomically: write to temp file then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)   # os.replace is atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_yaml_write(path: Path, data: dict) -> None:
    atomic_write(path, yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False))


def ok(data=None) -> dict:
    result: dict = {"ok": True}
    if data is not None:
        result["data"] = data
    return result


def err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def output(d: dict) -> None:
    print(json.dumps(d, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────
# ID generation
# ──────────────────────────────────────────

def _next_id(prefix_letter: str, directory: Path) -> str:
    """Generate next sequential ID of format X-YYYYMMDD-NNN."""
    date = today_str()
    glob_pattern = f"{prefix_letter}-{date}-*.yaml"
    existing: list[int] = []
    if directory.exists():
        for f in directory.glob(glob_pattern):
            try:
                existing.append(int(f.stem.split("-")[-1]))
            except ValueError:
                pass
    seq = max(existing, default=0) + 1
    return f"{prefix_letter}-{date}-{seq:03d}"


def next_task_id() -> str:
    import task_store as _m
    return _next_id("T", _m.TASKS_DIR)


def next_review_id() -> str:
    import task_store as _m
    return _next_id("R", _m.REVIEWS_DIR)


# ──────────────────────────────────────────
# Task I/O
# ──────────────────────────────────────────

def _task_path(task_id: str) -> Path:
    import task_store as _m
    return _m.TASKS_DIR / f"{task_id}.yaml"


def load_task(task_id: str) -> dict:
    path = _task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"Task {task_id} not found")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_task(task: dict) -> None:
    atomic_yaml_write(_task_path(task["id"]), task)


def transition_task(task_id: str, new_status: str, *, note: str | None = None,
                    force: bool = False) -> dict:
    """Transition a task to *new_status* with FSM validation and decision logging.

    This is the **only** sanctioned way to change task status programmatically.
    It mirrors the validation logic of ``cmd_task_update --status`` but is
    callable from Python (e.g. scheduler.py) without going through CLI/argparse.

    Args:
        task_id: Task ID to transition.
        new_status: Target status.
        note: Optional note appended to context and history.
        force: If True, bypass review level gate (emergency override).

    Returns the updated task dict on success.
    Raises ValueError for invalid transitions, FileNotFoundError if task missing.
    """
    task = load_task(task_id)
    current = task["status"]
    allowed = VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        allowed_str = ", ".join(sorted(allowed)) if allowed else "none (terminal state)"
        raise ValueError(
            f"Invalid transition: {current} → {new_status}. Allowed: {allowed_str}"
        )

    # ── Auditor gate: executing→done requires auditor pass (v10 改造) ──
    # 替代旧版 review_level guard — 与 scheduler_v2.py mark_done() 双重保障
    # review 状态和转换保留不删，其他流程仍可使用 review 路径
    if current == "executing" and new_status == "done" and not force:
        orch = task.get("orchestration", {})
        history = orch.get("history", [])
        auditor_passed = any(
            h.get("type") == "completion"
            and h.get("role") == "auditor"
            and h.get("verdict") == "pass"
            for h in history
        )
        if not auditor_passed:
            raise ValueError(
                f"任务 {task_id} 未通过 Auditor 审计，不允许标记 done。"
                f"请先执行 Auditor 角色。"
            )

    # ── executing→queued protection: only allowed via force (timeout recovery) ──
    if current == "executing" and new_status == "queued" and not force:
        raise ValueError(
            f"任务 {task_id} 不允许从 executing 直接退回 queued。"
            f"如遇困难请用 --status blocked。超时回收由调度器自动执行。"
        )

    # ── Audit logging for force overrides ──
    if force:
        append_decision({
            "type": "force_override",
            "task_id": task_id,
            "from": current,
            "to": new_status,
            "note": note or "",
        })

    task["status"] = new_status
    ts = now_iso()
    task["updated"] = ts

    detail = f"status: {current} → {new_status}"
    if note:
        detail += f" ({note})"
    task["history"].append({"timestamp": ts, "action": "status_change", "detail": detail})

    if note:
        existing = task.get("context", {}).get("notes", "") or ""
        task.setdefault("context", {})["notes"] = (existing + f"\n[{ts}] {note}").strip()

    save_task(task)
    append_decision({"type": "status_change", "task_id": task_id, "from": current, "to": new_status})
    return task


def list_tasks(status_filter: set | None = None) -> list:
    import task_store as _m
    tasks: list = []
    if not _m.TASKS_DIR.exists():
        return tasks
    for f in sorted(_m.TASKS_DIR.glob("T-*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            t = yaml.safe_load(fh)
        if status_filter is None or t.get("status") in status_filter:
            tasks.append(t)
    return tasks


def get_brain_stats() -> dict:
    """Return a summary dict with task counts by status.

    Keys:
      - total_tasks     (int): total number of tasks
      - queued_count    (int): tasks with status == 'queued'
      - executing_count (int): tasks with status == 'executing'
      - done_count      (int): tasks with status == 'done'
    """
    all_tasks = list_tasks()
    return {
        "total_tasks":     len(all_tasks),
        "queued_count":    sum(1 for t in all_tasks if t.get("status") == "queued"),
        "executing_count": sum(1 for t in all_tasks if t.get("status") == "executing"),
        "done_count":      sum(1 for t in all_tasks if t.get("status") == "done"),
    }


# ──────────────────────────────────────────
# Review I/O
# ──────────────────────────────────────────

def _review_path(review_id: str) -> Path:
    import task_store as _m
    return _m.REVIEWS_DIR / f"{review_id}.yaml"


def load_review(review_id: str) -> dict:
    path = _review_path(review_id)
    if not path.exists():
        raise FileNotFoundError(f"Review {review_id} not found")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_review(review: dict) -> None:
    atomic_yaml_write(_review_path(review["id"]), review)


def list_reviews(status_filter: str | None = None) -> list:
    import task_store as _m
    reviews: list = []
    if not _m.REVIEWS_DIR.exists():
        return reviews
    for f in sorted(_m.REVIEWS_DIR.glob("R-*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            r = yaml.safe_load(fh)
        if status_filter is None or r.get("status") == status_filter:
            reviews.append(r)
    return reviews


def get_task_pending_reviews(task_id: str) -> list:
    return [r for r in list_reviews() if r.get("task_id") == task_id and r.get("status") == "pending"]


# ──────────────────────────────────────────
# BRIEFING generator
# ──────────────────────────────────────────

def generate_briefing() -> str:
    tasks   = list_tasks()
    reviews = list_reviews(status_filter="pending")
    pending_task_ids = {r["task_id"] for r in reviews}

    # Buckets (use dict for ordered dedup on urgent)
    urgent_map: dict[str, dict] = {}
    executing: list = []
    waiting:   list = []
    done_today: list = []
    queued:    list = []

    today = today_iso_prefix()

    for t in tasks:
        tid    = t["id"]
        status = t.get("status", "")
        prio   = t.get("priority", "P2")

        if status in ("cancelled", "dropped"):
            continue

        if status == "done":
            if t.get("updated", "").startswith(today):
                done_today.append(t)
            continue

        if prio == "P0" or tid in pending_task_ids:
            urgent_map[tid] = t          # dedup by task id

        if status == "executing":
            executing.append(t)

        if tid in pending_task_ids:
            waiting.append(t)

        if status == "queued":
            queued.append(t)

    urgent = list(urgent_map.values())

    lines = [
        "# 🧠 Daily Briefing",
        f"> 最后更新: {now_iso()}",
        "",
        "## 🔴 紧急事项",
    ]
    if urgent:
        for t in urgent[:5]:
            lines.append(f"- [{t['id']}] **{t['title']}** ({t.get('priority', '?')}) — {t.get('status', '?')}")
    else:
        lines.append("_无_")

    lines += ["", "## 🔵 进行中"]
    if executing:
        for t in executing[:5]:
            lines.append(f"- [{t['id']}] {t['title']}")
    else:
        lines.append("_无_")

    lines += ["", "## ⏳ 等待用户输入"]
    if waiting:
        for t in waiting[:5]:
            task_reviews = [r for r in reviews if r["task_id"] == t["id"]]
            for r in task_reviews[:2]:
                lines.append(f"- [{r['id']}] {r['summary']} (task: {t['id']})")
    else:
        lines.append("_无_")

    lines += ["", "## ✅ 今日已完成"]
    if done_today:
        for t in done_today[:5]:
            lines.append(f"- [{t['id']}] {t['title']}")
    else:
        lines.append("_无_")

    # Quick tasks today
    quick_today = list_quick_log(date_prefix=today)
    if quick_today:
        lines.append(f"- (快速通道) 今日共完成 {len(quick_today)} 条快速任务")

    lines += ["", "## 📋 近期规划"]
    if queued:
        for t in queued[:5]:
            lines.append(f"- [{t['id']}] {t['title']} ({t.get('priority', '?')})")
    else:
        lines.append("_无_")

    lines += ["", "---", "_由 task_store 自动生成_"]

    # Enforce max line limit (truncate low-priority sections)
    if len(lines) > BRIEFING_MAX_LINES:
        lines = lines[:BRIEFING_MAX_LINES - 2] + ["", "_由 task_store 自动生成 (截断)_"]

    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────
# REGISTRY generator
# ──────────────────────────────────────────

def generate_registry() -> str:
    tasks = list_tasks()
    active_statuses = {"queued", "executing", "review", "blocked", "revision"}

    active: list    = [t for t in tasks if t.get("status") in active_statuses]
    recent_done: list = []

    for t in tasks:
        if t.get("status") != "done":
            continue
        updated = t.get("updated", "")
        if not updated:
            continue
        try:
            updated_date = datetime.strptime(updated[:10], "%Y-%m-%d")
            delta = (datetime.now() - updated_date).days
            if delta <= 7:
                recent_done.append(t)
        except ValueError:
            pass

    lines = [
        "# 📋 Task Registry",
        f"> 最后更新: {now_iso()}",
        "",
        "## Active Tasks",
        "| ID | Title | Type | Status | Priority | Updated |",
        "|----|-------|------|--------|----------|---------|",
    ]

    for t in active:
        updated = (t.get("updated") or "")[:19]
        lines.append(
            f"| {t['id']} | {t.get('title', '')} | {t.get('type', '')} | "
            f"{t.get('status', '')} | {t.get('priority', '')} | {updated} |"
        )

    lines += [
        "",
        "## Recently Completed (Last 7 Days)",
        "| ID | Title | Completed |",
        "|----|-------|-----------|",
    ]

    for t in recent_done:
        completed = (t.get("updated") or "")[:19]
        lines.append(f"| {t['id']} | {t.get('title', '')} | {completed} |")

    lines += ["", "---", "_由 task_store 自动生成_"]

    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────
# Template I/O & matching
# ──────────────────────────────────────────

def load_all_templates() -> list[dict]:
    """Load all template YAML files from TEMPLATES_DIR."""
    import task_store as _m
    templates: list[dict] = []
    if not _m.TEMPLATES_DIR.exists():
        return templates
    for f in sorted(_m.TEMPLATES_DIR.glob("*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            t = yaml.safe_load(fh)
        if t:
            templates.append(t)
    return templates


def _detect_traits(text: str) -> dict:
    """Heuristically detect traits from text."""
    lower = text.lower()
    involves_code = bool(re.search(
        r"代码|开发|实现|bug|fix|refactor|function|class|api|编码|重构|优化代码|添加功能",
        lower
    ))
    is_exploratory = bool(re.search(
        r"调查|分析|排查|为什么|不清楚|root cause|根因|investigate|explore|探索|研究",
        lower
    ))
    is_cron = bool(re.search(
        r"定时|cron|日报|周报|定期|scheduled|recurring",
        lower
    ))
    # 已移除 "自动任务" — 太泛化，导致误分类 (T-008)
    # Detect numeric todo count from description, e.g. "5条需求"
    todo_match = re.search(r"(\d+)\s*条", lower)
    related_todos = int(todo_match.group(1)) if todo_match else 0
    return {
        "involves_code":    involves_code,
        "is_exploratory":   is_exploratory,
        "is_cron_triggered": is_cron,
        "related_todos":    related_todos,
    }


def match_template(title: str, desc: str = "") -> dict:
    """Match best template based on title + description.

    Algorithm:
    1. Load all template YAML files.
    2. For each template, compute score:
       a. Keyword match score: count matching keywords in title+desc (case-insensitive).
       b. Trait bonus: +1 per matching required trait.
       c. Weighted final score = keyword_score * 2 + trait_bonus + priority_weight.
    3. Sort by score descending.
    4. Return best match with confidence and reasoning.
    """
    templates = load_all_templates()
    if not templates:
        return {
            "template":   "standard-dev",
            "confidence": 0.0,
            "reason":     "no templates found; defaulting to standard-dev",
            "all_scores": [],
        }

    combined = (title + " " + desc).lower()
    detected  = _detect_traits(combined)
    scores: list[dict] = []

    for tmpl in templates:
        name     = tmpl.get("name", "")
        matching = tmpl.get("matching", {})
        keywords = [k.lower() for k in (matching.get("keywords") or [])]
        traits   = matching.get("traits", {}) or {}
        priority_weight = matching.get("priority", 1)

        # Keyword score
        matched_kw = [kw for kw in keywords if kw in combined]
        kw_score   = len(matched_kw)

        # Trait bonus
        trait_bonus = 0
        if traits.get("involves_code") and detected["involves_code"]:
            trait_bonus += 1
        if traits.get("is_exploratory") and detected["is_exploratory"]:
            trait_bonus += 2   # exploratory is a strong signal
        if traits.get("is_cron_triggered") and detected["is_cron_triggered"]:
            trait_bonus += 3   # cron is very specific
        related_min = traits.get("related_todos_min", 0)
        if detected["related_todos"] >= related_min and related_min >= 5:
            trait_bonus += 2   # batch indicator

        final = kw_score * 2 + trait_bonus + (priority_weight / 10)
        scores.append({
            "template":      name,
            "score":         round(final, 2),
            "kw_score":      kw_score,
            "trait_bonus":   trait_bonus,
            "matched_kw":    matched_kw,
            "priority_weight": priority_weight,
        })

    scores.sort(key=lambda x: x["score"], reverse=True)

    best = scores[0]

    # Confidence: relative share of total score (avoids dependence on corpus size)
    has_signal = best["kw_score"] > 0 or best["trait_bonus"] > 0
    total_score = sum(s["score"] for s in scores) or 1.0
    if has_signal:
        confidence = round(min(best["score"] / total_score, 1.0), 2)
    else:
        confidence = 0.1   # no keyword or trait match at all

    reason_parts = []
    if best["matched_kw"]:
        reason_parts.append(f"matched keywords: {', '.join(best['matched_kw'])}")
    for trait, val in detected.items():
        if val:
            reason_parts.append(f"{trait}={val}")
    reason = "; ".join(reason_parts) if reason_parts else "no strong signal; using priority default"

    # Fall back to standard-dev only when there is genuinely no signal
    result_template = best["template"]
    if not has_signal:
        result_template = "standard-dev"
        reason += " [no signal; defaulted to standard-dev]"

    # ── cron-auto 准入门卫 (T-008) ──
    # 如果最终结果是 cron-auto，做额外准入检查：必须有 cron 强信号才允许
    if result_template == "cron-auto":
        cron_strong_signals = ["cron", "定时", "定期", "scheduled", "recurring", "计划任务", "日报", "周报"]
        has_strong_signal = any(sig in combined for sig in cron_strong_signals)
        if not has_strong_signal:
            result_template = "standard-dev"
            reason += " [cron-auto gate: no strong cron signal, fallback to standard-dev]"
            confidence = max(confidence - 0.2, 0.1)

    return {
        "template":   result_template,
        "confidence": confidence,
        "reason":     reason,
        "all_scores": scores,
    }


# ──────────────────────────────────────────
# Quick-log I/O
# ──────────────────────────────────────────

def _quick_log_path() -> Path:
    import task_store as _m
    return _m.QUICK_LOG


def _quick_archive_dir() -> Path:
    import task_store as _m
    return _m.QUICK_ARCHIVE_DIR


def _next_quick_id() -> str:
    """Generate next sequential Quick ID of format Q-YYYYMMDD-NNN."""
    path  = _quick_log_path()
    date  = today_str()
    prefix = f"Q-{date}-"
    seq   = 0
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    eid   = entry.get("id", "")
                    if eid.startswith(prefix):
                        n = int(eid.split("-")[-1])
                        seq = max(seq, n)
                except (json.JSONDecodeError, ValueError):
                    pass
    return f"{prefix}{seq + 1:03d}"


def append_quick_log(entry: dict) -> None:
    """Append one JSON line to the quick log (direct append for concurrency safety)."""
    path = _quick_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)



def _decisions_log_path() -> Path:
    import task_store as _m
    return _m.DECISIONS_LOG


def append_decision(entry: dict) -> None:
    """Append one JSON line to the decisions log."""
    path = _decisions_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if "timestamp" not in entry:
        entry["timestamp"] = now_iso()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def list_decisions(limit: int = 20) -> list[dict]:
    """Read recent decision entries."""
    path = _decisions_log_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:] if limit else entries


def list_quick_log(date_prefix: str | None = None) -> list[dict]:
    """Read quick log entries, optionally filtered by date prefix (YYYY-MM-DD)."""
    path = _quick_log_path()
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if date_prefix is None or entry.get("timestamp", "").startswith(date_prefix):
                entries.append(entry)
    return entries


# ──────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────

def cmd_task_create(args: argparse.Namespace) -> None:
    task_id = next_task_id()
    ts = now_iso()

    # Template resolution
    template_name = getattr(args, "template", None)
    match_info: dict | None = None
    if template_name:
        match_info = {"template": template_name, "confidence": 1.0, "reason": "manually specified"}
    else:
        match_info = match_template(args.title, args.desc or "")
        template_name = match_info["template"]

    task = {
        "id":             task_id,
        "title":          args.title,
        "type":           args.type,
        "status":         "queued",
        "priority":       args.priority,
        "created":        ts,
        "updated":        ts,
        "source_session": None,
        "todo_id":        None,
        "description":    args.desc or "",
        "workgroup": {
            "template":   template_name,
            "match_info": match_info,
        },
        "context": {
            "sessions": [],
            "files":    [],
            "notes":    "",
        },
        "review": {
            "items":         [],
            "pending_count": 0,
        },
        "history": [
            {
                "timestamp": ts,
                "action":    "created",
                "detail":    "从用户请求创建",
            }
        ],
    }
    save_task(task)
    output(ok({"id": task_id, "task": task}))


def cmd_task_update(args: argparse.Namespace) -> None:
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return

    changes: dict         = {}
    history_parts: list   = []

    status_changed = False

    if args.status:
        new_stat = args.status
        force = getattr(args, 'force', False)
        try:
            transition_task(args.task_id, new_stat, note=args.note, force=force)
            # Reload task after transition (transition_task already saved it)
            task = load_task(args.task_id)
            changes["status"] = new_stat
            status_changed = True
        except ValueError as exc:
            output(err(str(exc)))
            return

    if args.title:
        task["title"]        = args.title
        changes["title"]     = args.title
        history_parts.append("title updated")

    if args.priority:
        task["priority"]     = args.priority
        changes["priority"]  = args.priority
        history_parts.append(f"priority: {args.priority}")

    if args.note:
        existing = task["context"].get("notes", "") or ""
        task["context"]["notes"] = (existing + f"\n[{now_iso()}] {args.note}").strip()
        history_parts.append("note added")

    if not status_changed and not history_parts:
        output(err("No changes specified"))
        return

    # Only write additional history + save if there are non-status changes
    # (transition_task already recorded the status change and saved)
    if history_parts:
        ts = now_iso()
        task["updated"] = ts
        task["history"].append({
            "timestamp": ts,
            "action":    "updated",
            "detail":    "; ".join(history_parts),
        })
        save_task(task)

    output(ok({"id": task["id"], "changes": changes}))


def cmd_task_list(args: argparse.Namespace) -> None:
    if args.status == "active":
        status_filter: set | None = {"queued", "executing", "review", "blocked", "revision"}
    elif args.status:
        status_filter = {args.status}
    else:
        status_filter = None

    tasks = list_tasks(status_filter)
    summary = [
        {
            "id":       t["id"],
            "title":    t["title"],
            "type":     t.get("type"),
            "status":   t.get("status"),
            "priority": t.get("priority"),
            "updated":  t.get("updated"),
        }
        for t in tasks
    ]
    output(ok({"count": len(summary), "tasks": summary}))


def cmd_task_show(args: argparse.Namespace) -> None:
    try:
        task = load_task(args.task_id)
        output(ok(task))
    except FileNotFoundError as e:
        output(err(str(e)))


def cmd_task_delete(args: argparse.Namespace) -> None:
    path = _task_path(args.task_id)
    if not path.exists():
        output(err(f"Task {args.task_id} not found"))
        return
    path.unlink()
    output(ok({"deleted": args.task_id}))


def cmd_template_match(args: argparse.Namespace) -> None:
    result = match_template(args.title, args.desc or "")
    output(ok(result))


def cmd_template_list(args: argparse.Namespace) -> None:
    templates = load_all_templates()
    summary = [
        {
            "name":           t.get("name"),
            "display_name":   t.get("display_name"),
            "description":    t.get("description"),
            "autonomy_level": t.get("autonomy_level"),
            "priority":       (t.get("matching") or {}).get("priority"),
        }
        for t in templates
    ]
    output(ok({"count": len(summary), "templates": summary}))


def cmd_template_show(args: argparse.Namespace) -> None:
    import task_store as _m
    path = _m.TEMPLATES_DIR / f"{args.name}.yaml"
    if not path.exists():
        output(err(f"Template '{args.name}' not found"))
        return
    with path.open("r", encoding="utf-8") as f:
        tmpl = yaml.safe_load(f)
    output(ok(tmpl))


def cmd_quick_log(args: argparse.Namespace) -> None:
    entry_id = _next_quick_id()
    entry = {
        "id":               entry_id,
        "title":            args.title,
        "result":           args.result or "",
        "timestamp":        now_iso(),
        "duration_minutes": None,
    }
    append_quick_log(entry)
    output(ok({"id": entry_id, "entry": entry}))


def cmd_quick_list(args: argparse.Namespace) -> None:
    today = today_iso_prefix()
    entries = list_quick_log(date_prefix=today)
    output(ok({"count": len(entries), "entries": entries}))


def cmd_quick_archive(args: argparse.Namespace) -> None:
    """Move quick log entries older than today to YYYY-MM.jsonl archive files."""
    import task_store as _m
    today = today_iso_prefix()
    all_entries = list_quick_log()
    to_keep:    list[dict] = []
    to_archive: list[dict] = []

    for entry in all_entries:
        ts = entry.get("timestamp", "")
        if ts.startswith(today):
            to_keep.append(entry)
        else:
            to_archive.append(entry)

    if not to_archive:
        output(ok({"archived": 0, "message": "nothing to archive"}))
        return

    # Group by YYYY-MM
    by_month: dict[str, list[dict]] = {}
    for entry in to_archive:
        ts = entry.get("timestamp", "")
        month_key = ts[:7] if len(ts) >= 7 else "unknown"
        by_month.setdefault(month_key, []).append(entry)

    archive_dir = _m.QUICK_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived_count = 0
    for month_key, entries in by_month.items():
        archive_file = archive_dir / f"{month_key}.jsonl"
        existing = archive_file.read_text(encoding="utf-8") if archive_file.exists() else ""
        new_lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        atomic_write(archive_file, existing + new_lines)
        archived_count += len(entries)

    # Rewrite quick log with only today's entries
    kept_content = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in to_keep)
    atomic_write(_m.QUICK_LOG, kept_content)

    output(ok({"archived": archived_count, "kept": len(to_keep)}))


def cmd_briefing_update(args: argparse.Namespace) -> None:
    import task_store as _m
    content = generate_briefing()
    atomic_write(_m.BRIEFING_FILE, content)
    output(ok({"path": str(_m.BRIEFING_FILE), "lines": len(content.splitlines())}))


def cmd_review_add(args: argparse.Namespace) -> None:
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return

    review_id = next_review_id()
    ts        = now_iso()
    review    = {
        "id":       review_id,
        "task_id":  args.task_id,
        "summary":  args.summary,
        "prompt":   args.prompt,
        "status":   "pending",
        "created":  ts,
        "resolved": None,
        "decision": None,
        "note":     None,
    }
    save_review(review)

    # Keep task review metadata in sync
    task["review"]["pending_count"] = task["review"].get("pending_count", 0) + 1
    task["review"]["items"].append(review_id)
    task["updated"] = ts
    task["history"].append({
        "timestamp": ts,
        "action":    "review_added",
        "detail":    f"Review {review_id} added: {args.summary}",
    })
    save_task(task)
    output(ok({"id": review_id, "review": review}))


def cmd_review_notify(args: argparse.Namespace) -> None:
    """Generate notification content for a pending review item."""
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return

    review_id = getattr(args, "review_id", None)

    if review_id:
        try:
            review = load_review(review_id)
        except FileNotFoundError as e:
            output(err(str(e)))
            return
        if review.get("task_id") != args.task_id:
            output(err(f"Review {review_id} does not belong to task {args.task_id}"))
            return
        if review.get("status") != "pending":
            output(err(f"Review {review_id} is not pending (status: {review.get('status')})"))
            return
    else:
        pending = get_task_pending_reviews(args.task_id)
        if not pending:
            output(err(f"No pending reviews found for task {args.task_id}"))
            return
        review = pending[0]
        review_id = review["id"]

    task_title    = task.get("title", "Unknown Task")
    review_summary = review.get("summary", "")
    review_prompt  = review.get("prompt", "")

    # ── New format via feishu_notify ──
    try:
        from feishu_notify import format_review_notify, extract_short_id
        feishu_card_content = format_review_notify(task, review)
        short_id = extract_short_id(args.task_id)
        reply_hint = f"{short_id} Go / {short_id} NoGo / {short_id} 修改意见"
    except ImportError:
        # Fallback to legacy format if feishu_notify not available
        feishu_card_content = (
            f"📋 **{task_title} — 需要你 Review**\n\n"
            f"{review_prompt}\n\n"
            f"**续接提示词（复制到新对话即可）：**\n"
            f"`查看待审项 {review_id}，任务是 {task_title}，{review_summary}`"
        )
        short_id = None
        reply_hint = None

    # Preserve activation_prompt for backward compatibility
    activation_prompt = f"查看待审项 {review_id}，任务是 {task_title}，{review_summary}"
    summary = f"{task_title} 的 review {review_id} 需要你处理：{review_summary}"

    result = {
        "review_id":           review_id,
        "task_id":             args.task_id,
        "feishu_card_content": feishu_card_content,
        "activation_prompt":   activation_prompt,
        "summary":             summary,
    }
    if short_id is not None:
        result["short_id"] = short_id
    if reply_hint is not None:
        result["reply_hint"] = reply_hint

    output(ok(result))


def _review_wait_str(created_str: str) -> str:
    """Return human-readable wait duration from an ISO timestamp string."""
    if not created_str:
        return "unknown"
    try:
        created_dt = datetime.fromisoformat(created_str)
        delta = datetime.now().astimezone() - created_dt
        hours = int(delta.total_seconds() // 3600)
        if hours < 1:
            return f"{int(delta.total_seconds() // 60)}m"
        if hours < 24:
            return f"{hours}h"
        return f"{hours // 24}d"
    except (ValueError, TypeError):
        return "unknown"


def cmd_review_list(args: argparse.Namespace) -> None:
    fmt     = getattr(args, "format", None) or "default"
    reviews = list_reviews(status_filter="pending")

    if fmt == "default":
        output(ok({"count": len(reviews), "reviews": reviews}))
        return

    if fmt == "brief":
        items = [
            {
                "id":      r["id"],
                "task_id": r.get("task_id"),
                "summary": r.get("summary"),
                "waiting": _review_wait_str(r.get("created", "")),
            }
            for r in reviews
        ]
        output(ok({"count": len(items), "reviews": items}))
        return

    if fmt == "detail":
        items = []
        for r in reviews:
            task_info: dict = {"id": r.get("task_id")}
            try:
                t = load_task(r["task_id"])
                task_info = {
                    "id":       t["id"],
                    "title":    t.get("title"),
                    "status":   t.get("status"),
                    "priority": t.get("priority"),
                    "files":    t.get("context", {}).get("files", []),
                }
            except FileNotFoundError:
                task_info["error"] = "task not found"
            items.append({
                "id":                r["id"],
                "task_id":           r.get("task_id"),
                "summary":           r.get("summary"),
                "prompt":            r.get("prompt"),
                "created":           r.get("created"),
                "waiting":           _review_wait_str(r.get("created", "")),
                "task":              task_info,
                "suggested_actions": ["approve", "reject", "defer"],
            })
        output(ok({"count": len(items), "reviews": items}))
        return

    output(err(f"Unknown format: {fmt}. Valid options: default, brief, detail"))


def cmd_review_resolve(args: argparse.Namespace) -> None:
    """Resolve a review item.

    The positional argument accepts:
      - A Review ID (R-YYYYMMDD-NNN): resolve that specific review.
      - A Task ID  (T-YYYYMMDD-NNN): resolve the first pending review for that task.
    """
    ref = args.review_id   # may be R-xxx or T-xxx

    review: dict | None = None

    if ref.startswith("R-"):
        try:
            review = load_review(ref)
        except FileNotFoundError:
            pass
    elif ref.startswith("T-"):
        pending = get_task_pending_reviews(ref)
        if pending:
            review = pending[0]

    if review is None:
        output(err(f"No pending review found for '{ref}'"))
        return

    if review.get("status") == "resolved":
        output(err(f"Review {review['id']} is already resolved"))
        return

    ts = now_iso()
    review["status"]   = "resolved"
    review["resolved"] = ts
    review["decision"] = args.decision
    review["note"]     = args.note
    save_review(review)

    # Log decision
    append_decision({"type": "review_resolve", "review_id": review["id"], "task_id": review["task_id"], "decision": args.decision, "note": args.note, "resolved_by": "agent"})

    # Update task pending count, check for auto-transition
    task_status_changed = False
    new_task_status: str | None = None
    try:
        task = load_task(review["task_id"])
        task["review"]["pending_count"] = len(get_task_pending_reviews(review["task_id"]))
        task["updated"] = ts
        task["history"].append({
            "timestamp": ts,
            "action":    "review_resolved",
            "detail":    f"Review {review['id']} resolved: {args.decision}",
        })

        # Auto-transition: review → executing when all reviews approved
        all_review_ids   = task["review"].get("items", [])
        all_task_reviews = []
        for rid in all_review_ids:
            try:
                all_task_reviews.append(load_review(rid))
            except FileNotFoundError:
                pass

        if (
            all_task_reviews
            and all(r.get("status") == "resolved" for r in all_task_reviews)
            and all(r.get("decision") == "approved" for r in all_task_reviews)
            and task.get("status") == "review"
        ):
            task["status"] = "executing"
            task_status_changed = True
            new_task_status = "executing"
            task["history"].append({
                "timestamp": ts,
                "action":    "auto_status_change",
                "detail":    "All reviews approved; status: review → executing",
            })

        # Auto-transition: review → revision when rejected
        if args.decision == "rejected" and task.get("status") == "review":
            task["status"] = "revision"
            task_status_changed = True
            new_task_status = "revision"
            task["history"].append({
                "timestamp": ts,
                "action":    "auto_status_change",
                "detail":    "Review rejected; status: review → revision",
            })

        save_task(task)

        # Always refresh BRIEFING.md after a resolve
        import task_store as _m
        atomic_write(_m.BRIEFING_FILE, generate_briefing())

    except FileNotFoundError:
        pass   # Task may have been deleted; review still gets resolved

    output(ok({
        "id":                  review["id"],
        "task_id":             review["task_id"],
        "decision":            args.decision,
        "task_status_changed": task_status_changed,
        "new_task_status":     new_task_status,
    }))





# ──────────────────────────────────────────
# Cross-Check: Review Level, Checklist, Structured Results
# DEPRECATED(v10): The entire cross-check review system below is superseded by
# role-flow patterns (dev-pipeline.md). Kept for backward compatibility only.
# ──────────────────────────────────────────

REVIEW_LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}  # DEPRECATED(v10)

ROLE_CHECKLIST_MAP = {  # DEPRECATED(v10)
    "code_reviewer": "code_review.yaml",
    "test_verifier": "test_verify.yaml",
    "safety_checker": "safety_check.yaml",
}

VALID_VERDICTS = {"go", "no_go", "conditional_go"}  # DEPRECATED(v10)
VALID_SEVERITIES = {"critical", "major", "minor"}  # DEPRECATED(v10)
VALID_CHECKLIST_RESULTS = {"pass", "fail", "conditional", "na"}  # DEPRECATED(v10)


def determine_review_level(task: dict) -> str:
    """Determine review level (L0/L1/L2/L3) based on task characteristics.

    .. deprecated:: v10
        Superseded by role-flow patterns (dev-pipeline.md). Kept for backward compatibility.

    Priority order (highest first):
      1. L3 triggers: P0, architecture change, external publish, financial logic, batch-dev
      2. L0 triggers: quick/cron-auto template (only if no L3 trigger)
      3. L1: simple change (<=2 files, no interface change)
      4. L2: default

    Two-phase design:
    - Pre-judge at creation: only template/priority available, files_changed defaults to 99.
    - Final-judge at review: actual change info available for precise determination.
    - If final > pre, auto-upgrade to higher level.
    """
    template = task.get("workgroup", {}).get("template", "") or task.get("template", "quick")

    # ── L3 检查最先：高风险条件不可被覆盖 ──
    l3_triggers = [
        task.get("involves_architecture_change"),
        task.get("involves_external_publish"),
        task.get("involves_financial_logic"),
        template == "batch-dev",
        task.get("priority") == "P0",
    ]
    if any(l3_triggers):
        return "L3"

    # ── L0: quick/cron-auto（仅在无 L3 触发时生效）──
    if template in ("quick", "cron-auto"):
        return "L0"

    # ── L1: simple change (<=2 files, no interface change) ──
    files_changed = task.get("files_changed", 99)
    if template == "standard-dev" and files_changed <= 2:
        if not task.get("involves_interface_change"):
            return "L1"

    # ── L2: default ──
    return "L2"


def get_review_roles(level: str, task: dict) -> list[str]:
    """Return recommended reviewer roles for a given review level.

    .. deprecated:: v10
        Superseded by role-flow patterns.
    """
    if level == "L0":
        return []
    if level == "L1":
        return []  # self-check, no external reviewer
    if level == "L2":
        template = task.get("workgroup", {}).get("template", "") or task.get("template", "")
        if template == "long-task":
            return ["test_verifier"]
        return ["code_reviewer"]
    if level == "L3":
        roles = ["code_reviewer", "test_verifier"]
        if task.get("involves_financial_logic") or task.get("involves_external_publish"):
            roles.append("safety_checker")
        return roles
    return ["code_reviewer"]


def load_checklist(role: str) -> dict:
    """Load checklist YAML for a reviewer role.

    .. deprecated:: v10
        Superseded by role-flow patterns.
    """
    import task_store as _m
    filename = ROLE_CHECKLIST_MAP.get(role)
    if not filename:
        raise ValueError(f"Unknown reviewer role: {role}. Valid: {', '.join(ROLE_CHECKLIST_MAP.keys())}")
    path = _m.CHECKLISTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Checklist file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate_task_checklist(task: dict, role: str) -> dict:
    """Generate a task-specific checklist combining template checklist with task context.

    .. deprecated:: v10
        Superseded by role-flow patterns.
    """
    checklist = load_checklist(role)
    level = determine_review_level(task)
    roles = get_review_roles(level, task)
    return {
        "task_id": task.get("id", ""),
        "task_title": task.get("title", ""),
        "review_level": level,
        "reviewer_role": role,
        "recommended_roles": roles,
        "checklist_name": checklist.get("name", ""),
        "checklist_version": checklist.get("version", ""),
        "items": checklist.get("items", []),
        "task_context": {
            "template": task.get("workgroup", {}).get("template", ""),
            "priority": task.get("priority", ""),
            "description": task.get("description", ""),
            "files": task.get("context", {}).get("files", []),
        },
    }


def validate_review_result(result: dict) -> tuple[bool, list[str]]:
    """Validate a structured review result against the expected schema.

    .. deprecated:: v10
        Superseded by role-flow patterns.

    Returns (is_valid, list_of_error_messages).
    """
    errors: list[str] = []
    # Required top-level fields
    for field in ("review_id", "reviewer_role", "verdict"):
        if field not in result:
            errors.append(f"Missing required field: {field}")
    # Verdict value
    verdict = result.get("verdict", "")
    if verdict and verdict not in VALID_VERDICTS:
        errors.append(f"Invalid verdict '{verdict}'. Must be one of: {', '.join(sorted(VALID_VERDICTS))}")
    # checklist_results
    cr = result.get("checklist_results")
    if cr is None:
        errors.append("Missing required field: checklist_results")
    elif not isinstance(cr, list):
        errors.append("checklist_results must be a list")
    else:
        for i, item in enumerate(cr):
            if not isinstance(item, dict):
                errors.append(f"checklist_results[{i}] must be a dict")
                continue
            if "id" not in item:
                errors.append(f"checklist_results[{i}] missing 'id'")
            r = item.get("result", "")
            if r and r not in VALID_CHECKLIST_RESULTS:
                errors.append(f"checklist_results[{i}] invalid result '{r}'")
    # issues
    issues = result.get("issues")
    if issues is None:
        errors.append("Missing required field: issues")
    elif not isinstance(issues, list):
        errors.append("issues must be a list")
    else:
        for i, issue in enumerate(issues):
            if not isinstance(issue, dict):
                errors.append(f"issues[{i}] must be a dict")
                continue
            sev = issue.get("severity", "")
            if sev and sev not in VALID_SEVERITIES:
                errors.append(f"issues[{i}] invalid severity '{sev}'")
    return (len(errors) == 0, errors)


def auto_judge_review(results: list[dict]) -> dict:
    """Auto-determine Go/NoGo from a list of structured review results.

    .. deprecated:: v10
        Superseded by role-flow patterns.

    Rules:
    1. All go → go
    2. Any critical issue → no_go
    3. Major issues >= 2 (deduped by description) → no_go
    4. Otherwise → needs_arbitration
    """
    if not results:
        return {"verdict": "needs_arbitration", "reason": "No review results provided"}

    all_go = all(r.get("verdict") == "go" for r in results)
    if all_go:
        return {"verdict": "go", "reason": "All reviewers approved"}

    # Collect all issues across results
    all_issues: list[dict] = []
    for r in results:
        all_issues.extend(r.get("issues", []))

    critical_issues = [i for i in all_issues if i.get("severity") == "critical"]
    if critical_issues:
        descs = [i.get("description", "unknown") for i in critical_issues]
        return {"verdict": "no_go", "reason": f"Critical issues found: {'; '.join(descs[:3])}"}

    # Dedup major issues by description
    major_descs = set()
    for i in all_issues:
        if i.get("severity") == "major":
            major_descs.add(i.get("description", f"issue-{len(major_descs)}"))
    if len(major_descs) >= 2:
        return {"verdict": "no_go", "reason": f"Multiple major issues ({len(major_descs)}): {'; '.join(list(major_descs)[:3])}"}

    # Mixed results
    verdicts = [r.get("verdict", "") for r in results]
    all_no_go = all(v == "no_go" for v in verdicts)
    if all_no_go:
        return {"verdict": "no_go", "reason": "All reviewers rejected"}

    return {"verdict": "needs_arbitration", "reason": f"Mixed verdicts: {', '.join(verdicts)}"}


def _save_review_result(task_id: str, result: dict) -> Path:
    """Save a structured review result to the review-results directory."""
    import task_store as _m
    results_dir = _m.REVIEW_RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    review_id = result.get("review_id", f"R-{today_str()}-unknown")
    path = results_dir / f"{review_id}.yaml"
    atomic_yaml_write(path, result)
    return path


def _load_task_review_results(task_id: str) -> list[dict]:
    """Load all structured review results for a task."""
    import task_store as _m
    results_dir = _m.REVIEW_RESULTS_DIR
    if not results_dir.exists():
        return []
    results: list[dict] = []
    for f in sorted(results_dir.glob("*.yaml")):
        with f.open("r", encoding="utf-8") as fh:
            r = yaml.safe_load(fh)
        if r and r.get("task_id") == task_id:
            results.append(r)
    return results


def cmd_review_level(args: argparse.Namespace) -> None:
    """CLI: task_store review level <task_id>

    .. deprecated:: v10
        Superseded by role-flow patterns.
    """
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return
    level = determine_review_level(task)
    roles = get_review_roles(level, task)
    output(ok({
        "task_id": args.task_id,
        "review_level": level,
        "recommended_roles": roles,
        "task_template": task.get("workgroup", {}).get("template", ""),
        "task_priority": task.get("priority", ""),
    }))


def cmd_review_checklist(args: argparse.Namespace) -> None:
    """CLI: task_store review checklist <task_id> --role <role>

    .. deprecated:: v10
        Superseded by role-flow patterns.
    """
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return
    try:
        checklist = generate_task_checklist(task, args.role)
    except (ValueError, FileNotFoundError) as e:
        output(err(str(e)))
        return
    output(ok(checklist))


def cmd_review_submit(args: argparse.Namespace) -> None:
    """CLI: task_store review submit <task_id> --result-file <path>

    .. deprecated:: v10
        Superseded by role-flow patterns.

    Design note — submit vs resolve:
      - submit: Receives a structured review result (YAML) and runs auto-judge
        to produce a verdict recommendation. Does NOT change task status.
        Multiple reviewers can submit independently; results accumulate.
      - resolve: A human/process decision that triggers task state transitions
        (e.g. review→executing on approved, review→revision on rejected).
    Submit feeds data into the system; resolve acts on it.
    """
    try:
        task = load_task(args.task_id)
    except FileNotFoundError as e:
        output(err(str(e)))
        return

    result_path = Path(args.result_file)
    if not result_path.exists():
        output(err(f"Result file not found: {args.result_file}"))
        return

    with result_path.open("r", encoding="utf-8") as f:
        result = yaml.safe_load(f)

    if not isinstance(result, dict):
        output(err("Result file must contain a YAML mapping"))
        return

    # Validate schema
    valid, validation_errors = validate_review_result(result)
    if not valid:
        output(err(f"Schema validation failed: {'; '.join(validation_errors)}"))
        return

    # Inject task_id
    result["task_id"] = args.task_id
    result["submitted_at"] = now_iso()

    # Save structured result
    saved_path = _save_review_result(args.task_id, result)

    # Update task metadata
    ts = now_iso()
    if "structured_results" not in task["review"]:
        task["review"]["structured_results"] = []
    task["review"]["structured_results"].append(result.get("review_id", ""))
    task["updated"] = ts
    task["history"].append({
        "timestamp": ts,
        "action": "structured_review_submitted",
        "detail": f"Structured review from {result.get('reviewer_role', 'unknown')}: {result.get('verdict', 'unknown')}",
    })
    save_task(task)

    # Auto-judge
    all_results = _load_task_review_results(args.task_id)
    judgment = auto_judge_review(all_results)

    output(ok({
        "task_id": args.task_id,
        "review_id": result.get("review_id", ""),
        "verdict": result.get("verdict", ""),
        "saved_path": str(saved_path),
        "auto_judgment": judgment,
        "total_results": len(all_results),
    }))


def cmd_decisions_list(args: argparse.Namespace) -> None:
    limit = getattr(args, "limit", 20) or 20
    entries = list_decisions(limit=limit)
    output(ok({"count": len(entries), "entries": entries}))


def cmd_registry_update(args: argparse.Namespace) -> None:
    import task_store as _m
    content = generate_registry()
    atomic_write(_m.REGISTRY_FILE, content)
    output(ok({"path": str(_m.REGISTRY_FILE), "lines": len(content.splitlines())}))


# ──────────────────────────────────────────
# Daily Maintenance & Report
# ──────────────────────────────────────────

def list_overdue_reviews(threshold_hours: int = 48) -> list[dict]:
    """Return pending reviews that have been waiting longer than threshold_hours."""
    reviews = list_reviews(status_filter="pending")
    now_dt = datetime.now().astimezone()
    overdue: list[dict] = []
    for r in reviews:
        created_str = r.get("created", "")
        if not created_str:
            continue
        try:
            created_dt = datetime.fromisoformat(created_str)
            delta = now_dt - created_dt
            if delta.total_seconds() > threshold_hours * 3600:
                r["overdue_hours"] = int(delta.total_seconds() // 3600)
                overdue.append(r)
        except (ValueError, TypeError):
            continue
    return overdue


def _archive_quick_entries_daily() -> dict:
    """Archive quick-log entries older than today to YYYY-MM-DD.jsonl daily files.

    Returns dict with 'archived' count and 'kept' count.
    """
    import task_store as _m
    today = today_iso_prefix()
    all_entries = list_quick_log()
    to_keep:    list[dict] = []
    to_archive: list[dict] = []

    for entry in all_entries:
        ts = entry.get("timestamp", "")
        if ts[:10] == today:
            to_keep.append(entry)
        else:
            to_archive.append(entry)

    if not to_archive:
        return {"archived": 0, "kept": len(to_keep)}

    # Group by YYYY-MM-DD
    by_day: dict[str, list[dict]] = {}
    for entry in to_archive:
        ts = entry.get("timestamp", "")
        day_key = ts[:10] if len(ts) >= 10 else "unknown"
        by_day.setdefault(day_key, []).append(entry)

    archive_dir = _m.QUICK_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived_count = 0
    for day_key, entries in by_day.items():
        archive_file = archive_dir / f"{day_key}.jsonl"
        existing = archive_file.read_text(encoding="utf-8") if archive_file.exists() else ""
        new_lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        atomic_write(archive_file, existing + new_lines)
        archived_count += len(entries)

    # Rewrite quick log with only today's entries
    kept_content = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in to_keep)
    atomic_write(_m.QUICK_LOG, kept_content)

    return {"archived": archived_count, "kept": len(to_keep)}


def cmd_daily_maintenance(args: argparse.Namespace) -> None:
    """Run full daily maintenance: archive, overdue check, briefing update, report."""
    import task_store as _m

    # 1. Quick task archive (daily granularity)
    archive_result = _archive_quick_entries_daily()

    # 2. Overdue review check
    overdue = list_overdue_reviews(threshold_hours=48)
    overdue_summaries = [
        {
            "id":            r["id"],
            "task_id":       r.get("task_id"),
            "summary":       r.get("summary"),
            "overdue_hours": r.get("overdue_hours"),
        }
        for r in overdue
    ]

    # 3. BRIEFING update
    content = generate_briefing()
    atomic_write(_m.BRIEFING_FILE, content)
    briefing_lines = len(content.splitlines())

    # 4. Generate daily report stats
    today = today_iso_prefix()
    all_tasks = list_tasks()
    active_statuses = {"queued", "executing", "review", "blocked", "revision"}

    tasks_done_today = sum(
        1 for t in all_tasks
        if t.get("status") == "done" and t.get("updated", "").startswith(today)
    )
    tasks_active = sum(1 for t in all_tasks if t.get("status") in active_statuses)
    reviews_pending = len(list_reviews(status_filter="pending"))
    quick_today = list_quick_log(date_prefix=today)

    stats = {
        "tasks_done_today":  tasks_done_today,
        "tasks_active":      tasks_active,
        "reviews_pending":   reviews_pending,
        "reviews_overdue":   len(overdue),
        "quick_tasks_today": len(quick_today),
        "quick_archived":    archive_result["archived"],
    }

    # 5. Log decision
    append_decision({
        "type":  "daily_maintenance",
        "stats": stats,
    })

    output(ok({
        "stats":           stats,
        "overdue_reviews": overdue_summaries,
        "briefing_lines":  briefing_lines,
        "briefing_path":   str(_m.BRIEFING_FILE),
    }))


def cmd_daily_report(args: argparse.Namespace) -> None:
    """Generate a read-only daily report summary. No mutations."""
    today = today_iso_prefix()
    all_tasks = list_tasks()
    active_statuses = {"queued", "executing", "review", "blocked", "revision"}

    tasks_done_today = sum(
        1 for t in all_tasks
        if t.get("status") == "done" and t.get("updated", "").startswith(today)
    )
    tasks_active = sum(1 for t in all_tasks if t.get("status") in active_statuses)
    tasks_queued = sum(1 for t in all_tasks if t.get("status") == "queued")
    tasks_executing = sum(1 for t in all_tasks if t.get("status") == "executing")
    tasks_review = sum(1 for t in all_tasks if t.get("status") == "review")
    tasks_blocked = sum(1 for t in all_tasks if t.get("status") == "blocked")

    reviews_pending = len(list_reviews(status_filter="pending"))
    overdue = list_overdue_reviews(threshold_hours=48)
    quick_today = list_quick_log(date_prefix=today)

    stats = {
        "tasks_done_today":  tasks_done_today,
        "tasks_active":      tasks_active,
        "tasks_queued":      tasks_queued,
        "tasks_executing":   tasks_executing,
        "tasks_review":      tasks_review,
        "tasks_blocked":     tasks_blocked,
        "reviews_pending":   reviews_pending,
        "reviews_overdue":   len(overdue),
        "quick_tasks_today": len(quick_today),
    }

    overdue_summaries = [
        {
            "id":            r["id"],
            "task_id":       r.get("task_id"),
            "summary":       r.get("summary"),
            "overdue_hours": r.get("overdue_hours"),
        }
        for r in overdue
    ]

    output(ok({
        "stats":           stats,
        "overdue_reviews": overdue_summaries,
    }))


# ──────────────────────────────────────────
# CLI parser
# ──────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task_store.py",
        description="Task Dispatcher Brain Management CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── task ──────────────────────────────
    task_p = sub.add_parser("task", help="Task management")
    task_s = task_p.add_subparsers(dest="subcommand", required=True)

    tc = task_s.add_parser("create", help="Create a new task")
    tc.add_argument("--title",    required=True, help="Task title")
    tc.add_argument("--type",     required=True, choices=sorted(VALID_TYPES), help="Task type")
    tc.add_argument("--priority", required=True, choices=sorted(VALID_PRIORITIES), help="Priority (P0/P1/P2)")
    tc.add_argument("--desc",     default="", help="Task description")
    tc.add_argument("--template", choices=sorted(VALID_TYPES), help="Workgroup template (auto-matched if omitted)")

    tu = task_s.add_parser("update", help="Update a task field or status")
    tu.add_argument("task_id",   help="Task ID, e.g. T-20260330-001")
    tu.add_argument("--status",   choices=sorted(VALID_STATUSES), help="New status")
    tu.add_argument("--title",    help="New title")
    tu.add_argument("--priority", choices=sorted(VALID_PRIORITIES), help="New priority")
    tu.add_argument("--note",     help="Append a note to context")
    tu.add_argument("--force",    action="store_true", help="Force transition, bypass review level check")

    tl = task_s.add_parser("list", help="List tasks (optionally filtered)")
    tl.add_argument("--status", help="Filter: 'active' or any specific status")

    ts = task_s.add_parser("show", help="Show full task detail")
    ts.add_argument("task_id", help="Task ID")

    td = task_s.add_parser("delete", help="Delete a task file")
    td.add_argument("task_id", help="Task ID")

    # ── briefing ──────────────────────────
    br_p = sub.add_parser("briefing", help="Briefing management")
    br_s = br_p.add_subparsers(dest="subcommand", required=True)
    br_s.add_parser("update", help="Regenerate BRIEFING.md from current task state")

    # ── review ────────────────────────────
    rv_p = sub.add_parser("review", help="Review item management")
    rv_s = rv_p.add_subparsers(dest="subcommand", required=True)

    ra = rv_s.add_parser("add", help="Add a review item to a task")
    ra.add_argument("task_id",   help="Task ID to attach this review to")
    ra.add_argument("--summary", required=True, help="Short summary of what needs review")
    ra.add_argument("--prompt",  required=True, help="Detailed prompt for the reviewer")

    rl = rv_s.add_parser("list", help="List all pending review items")
    rl.add_argument("--format", dest="format",
                    choices=["default", "brief", "detail"], default="default",
                    help="Output format (default: full records, brief: one-line, detail: with task context)")

    rn = rv_s.add_parser("notify", help="Generate notification content for a pending review")
    rn.add_argument("task_id",     help="Task ID")
    rn.add_argument("--review-id", dest="review_id", default=None,
                    help="Specific Review ID (defaults to first pending review for the task)")

    rr = rv_s.add_parser("resolve", help="Resolve a review item")
    rr.add_argument("review_id",  help="Review ID (R-xxx) or Task ID (T-xxx, resolves first pending)")
    rr.add_argument("--decision", required=True, choices=["approved", "rejected", "deferred"],
                    help="Resolution decision")
    rr.add_argument("--note",     help="Optional resolution note")

    rlv = rv_s.add_parser("level", help="Determine review level for a task")
    rlv.add_argument("task_id", help="Task ID")

    rcl = rv_s.add_parser("checklist", help="Generate review checklist for a task")
    rcl.add_argument("task_id", help="Task ID")
    rcl.add_argument("--role", required=True,
                     choices=sorted(ROLE_CHECKLIST_MAP.keys()),
                     help="Reviewer role")

    rsub = rv_s.add_parser("submit", help="Submit structured review result")
    rsub.add_argument("task_id", help="Task ID")
    rsub.add_argument("--result-file", required=True, dest="result_file",
                      help="Path to review result YAML file")

    # ── registry ──────────────────────────
    reg_p = sub.add_parser("registry", help="Registry management")
    reg_s = reg_p.add_subparsers(dest="subcommand", required=True)
    reg_s.add_parser("update", help="Regenerate REGISTRY.md from current task state")

    # ── decisions ─────────────────────────
    dec_p = sub.add_parser("decisions", help="Decision log management")
    dec_s = dec_p.add_subparsers(dest="subcommand", required=True)
    dl = dec_s.add_parser("list", help="List recent decisions")
    dl.add_argument("--limit", type=int, default=20, help="Max entries to return")

    # ── template ──────────────────────────
    tpl_p = sub.add_parser("template", help="Workgroup template management")
    tpl_s = tpl_p.add_subparsers(dest="subcommand", required=True)

    tm = tpl_s.add_parser("match", help="Match best template for a given title/description")
    tm.add_argument("--title", required=True, help="Task title to match")
    tm.add_argument("--desc",  default="",   help="Optional task description")

    tpl_s.add_parser("list", help="List all available templates")

    tsh = tpl_s.add_parser("show", help="Show full template detail")
    tsh.add_argument("name", help="Template name (e.g. standard-dev)")

    # ── quick ─────────────────────────────
    qk_p = sub.add_parser("quick", help="Quick task log management")
    qk_s = qk_p.add_subparsers(dest="subcommand", required=True)

    ql = qk_s.add_parser("log", help="Record a quick task result")
    ql.add_argument("--title",  required=True, help="Quick task title")
    ql.add_argument("--result", default="",   help="Result or outcome")

    qk_s.add_parser("list", help="List today's quick tasks")

    qk_s.add_parser("archive", help="Archive quick tasks older than today")

    # ── daily ─────────────────────────────
    dy_p = sub.add_parser("daily", help="Daily maintenance and report")
    dy_s = dy_p.add_subparsers(dest="subcommand", required=True)

    dy_s.add_parser("maintenance", help="Run full daily maintenance (archive, overdue check, briefing update, stats)")
    dy_s.add_parser("report", help="Generate read-only daily report summary")

    return parser


# ──────────────────────────────────────────
# Dispatch table
# ──────────────────────────────────────────

DISPATCH = {
    ("task",     "create"):  cmd_task_create,
    ("task",     "update"):  cmd_task_update,
    ("task",     "list"):    cmd_task_list,
    ("task",     "show"):    cmd_task_show,
    ("task",     "delete"):  cmd_task_delete,
    ("briefing", "update"):  cmd_briefing_update,
    ("review",   "add"):     cmd_review_add,
    ("review",   "list"):    cmd_review_list,
    ("review",   "notify"):  cmd_review_notify,
    ("review",   "resolve"): cmd_review_resolve,
    ("review",   "level"):   cmd_review_level,
    ("review",   "checklist"): cmd_review_checklist,
    ("review",   "submit"):  cmd_review_submit,
    ("registry", "update"):  cmd_registry_update,
    ("decisions", "list"):   cmd_decisions_list,
    ("template", "match"):   cmd_template_match,
    ("template", "list"):    cmd_template_list,
    ("template", "show"):    cmd_template_show,
    ("quick",    "log"):     cmd_quick_log,
    ("quick",    "list"):    cmd_quick_list,
    ("quick",    "archive"): cmd_quick_archive,
    ("daily",    "maintenance"): cmd_daily_maintenance,
    ("daily",    "report"):      cmd_daily_report,
}


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    try:
        key     = (args.command, args.subcommand)
        handler = DISPATCH.get(key)
        if handler:
            handler(args)
        else:
            output(err(f"Unknown command: {args.command} {args.subcommand}"))
    except Exception as e:
        output(err(f"Unexpected error: {type(e).__name__}: {e}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
