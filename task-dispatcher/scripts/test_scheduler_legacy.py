#!/usr/bin/env python3
"""
test_scheduler_legacy.py - Tests for scheduler_legacy.py (deprecated v1 scheduler APIs).

Tests cover:
  - Concurrency control (MAX_CONCURRENT_EXECUTING + MAX_DISPATCH_PER_RUN)
  - Quick task bypass (quick template skipped by scheduler)
  - Task category detection and verification guidance
  - Spawn instruction generation
  - Completed tasks / review follow-up
  - Review level integration (determine_review_level used in worker prompt)
  - Scheduler run (dry-run and live)
  - Status reporting
  - Circular dependency
  - transition_task failure handling
  - Timeout recovery
  - Worker prompt fixes
  - Report schema
  - Parse worker report
  - Make decision
  - Execute decision
  - Handle worker completion
  - Generate worker prompt v2
  - Legacy mode
  - Handle completion CLI
  - Design gate check
  - Doc triplet check
  - Doc retry escalation
  - V6.1 role cross-check
  - Assert audit completed
  - V6.1 prompt generation
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# ──────────────────────────────────────────
# Fixture: isolated BRAIN_DIR
# ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_brain(tmp_path, monkeypatch):
    """Create an isolated brain directory for each test."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "tasks").mkdir()
    (brain_dir / "reviews").mkdir()
    (brain_dir / "review-results").mkdir()

    # Point brain_manager to tmp
    monkeypatch.setenv("BRAIN_DIR", str(brain_dir))

    # Patch brain_manager module-level paths
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import brain_manager as bm
    monkeypatch.setattr(bm, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(bm, "TASKS_DIR", brain_dir / "tasks")
    monkeypatch.setattr(bm, "REVIEWS_DIR", brain_dir / "reviews")
    monkeypatch.setattr(bm, "BRIEFING_FILE", brain_dir / "BRIEFING.md")
    monkeypatch.setattr(bm, "REGISTRY_FILE", brain_dir / "REGISTRY.md")
    monkeypatch.setattr(bm, "QUICK_LOG", brain_dir / "quick-log.jsonl")
    monkeypatch.setattr(bm, "QUICK_ARCHIVE_DIR", brain_dir / "archive" / "quick")
    monkeypatch.setattr(bm, "DECISIONS_LOG", brain_dir / "decisions.jsonl")
    monkeypatch.setattr(bm, "REVIEW_RESULTS_DIR", brain_dir / "review-results")

    # Patch trigger_scheduler paths
    import trigger_scheduler as ts
    monkeypatch.setattr(ts, "DISPATCHER_FILE", brain_dir / "dispatcher.json")
    monkeypatch.setattr(ts, "BRAIN_DIR", brain_dir)

    yield brain_dir


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _create_task(task_id: str, status: str = "queued", priority: str = "P1",
                 template: str = "standard-dev", title: str = "",
                 created: str = "", blocked_by: list = None,
                 description: str = "") -> dict:
    """Create and save a task, return the task dict."""
    import brain_manager as bm

    ts = created or bm.now_iso()
    task = {
        "id": task_id,
        "title": title or f"Task {task_id}",
        "type": "standard-dev",
        "status": status,
        "priority": priority,
        "created": ts,
        "updated": ts,
        "description": description,
        "workgroup": {
            "template": template,
            "match_info": {"template": template, "confidence": 1.0},
        },
        "context": {"sessions": [], "files": [], "notes": ""},
        "review": {"items": [], "pending_count": 0},
        "history": [{"timestamp": ts, "action": "created", "detail": "test"}],
    }
    if blocked_by:
        task["blocked_by"] = blocked_by
    bm.save_task(task)
    return task


def _write_dispatcher(brain_dir: Path, **kwargs) -> dict:
    """Write a dispatcher.json file with given fields."""
    import trigger_scheduler as ts
    data = {
        "session_id": kwargs.get("session_id", "webchat_dispatch_123_456"),
        "session_key": kwargs.get("session_key", "webchat:dispatch_123_456"),
        "created_at": kwargs.get("created_at", ts._now_iso()),
        "iteration_count": kwargs.get("iteration_count", 0),
        "last_triggered_at": kwargs.get("last_triggered_at", ts._now_iso()),
    }
    data.update(kwargs)
    ts.save_dispatcher(data)
    return data


# ══════════════════════════════════════════
# Test: Concurrency Control
# ══════════════════════════════════════════

class TestConcurrencyControl:
    def test_no_executing_full_slots(self):
        import scheduler_legacy as sch
        assert sch.determine_available_slots() == sch.MAX_CONCURRENT_EXECUTING

    def test_some_executing_reduces_slots(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="executing")
        _create_task("T-20260330-002", status="executing")
        assert sch.determine_available_slots() == sch.MAX_CONCURRENT_EXECUTING - 2

    def test_max_executing_zero_slots(self):
        import scheduler_legacy as sch
        for i in range(sch.MAX_CONCURRENT_EXECUTING):
            _create_task(f"T-20260330-{i+1:03d}", status="executing")
        assert sch.determine_available_slots() == 0

    def test_per_run_cap_limits_dispatch(self):
        """Even with many slots, per-run cap limits dispatch count."""
        import scheduler_legacy as sch
        # Create more queued tasks than MAX_DISPATCH_PER_RUN
        for i in range(5):
            _create_task(f"T-20260330-{i+1:03d}", status="queued", priority="P1")

        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        dispatched = result["spawn_instructions"]
        assert len(dispatched) <= sch.MAX_DISPATCH_PER_RUN

    def test_global_limit_overrides_per_run(self):
        """If only 1 global slot, dispatch at most 1 even if per-run cap is 3."""
        import scheduler_legacy as sch
        # Fill up to MAX-1 executing
        for i in range(sch.MAX_CONCURRENT_EXECUTING - 1):
            _create_task(f"T-20260330-{i+1:03d}", status="executing")

        # Add 3 queued tasks
        for i in range(3):
            _create_task(f"T-20260330-{i+10:03d}", status="queued", priority="P1")

        result = sch.run_scheduler(dry_run=True)
        assert len(result["spawn_instructions"]) == 1


# ══════════════════════════════════════════
# Test: Quick Task Bypass
# ══════════════════════════════════════════

class TestQuickTaskBypass:
    def test_quick_tasks_skipped(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued", template="quick")
        _create_task("T-20260330-002", status="queued", template="standard-dev")

        result = sch.run_scheduler(dry_run=True)
        dispatched_ids = [d["task_id"] for d in result["spawn_instructions"]]
        assert "T-20260330-001" not in dispatched_ids
        assert "T-20260330-002" in dispatched_ids

    def test_is_quick_task_detection(self):
        import scheduler_legacy as sch
        assert sch.is_quick_task({"workgroup": {"template": "quick"}}) is True
        assert sch.is_quick_task({"workgroup": {"template": "standard-dev"}}) is False
        assert sch.is_quick_task({"template": "quick"}) is True
        assert sch.is_quick_task({}) is False


# ══════════════════════════════════════════
# Test: Task Category Detection
# ══════════════════════════════════════════

class TestTaskCategoryDetection:
    def test_web_frontend(self):
        import scheduler_legacy as sch
        task = {"title": "修复 Dashboard 页面 UI 问题", "description": ""}
        assert sch.detect_task_category(task) == "web_frontend"

    def test_feishu_integration(self):
        import scheduler_legacy as sch
        task = {"title": "飞书消息卡片优化", "description": ""}
        assert sch.detect_task_category(task) == "feishu_integration"

    def test_api_interface(self):
        import scheduler_legacy as sch
        task = {"title": "新增 REST API 接口", "description": ""}
        assert sch.detect_task_category(task) == "api_interface"

    def test_data_processing(self):
        import scheduler_legacy as sch
        task = {"title": "数据清洗 pipeline", "description": ""}
        assert sch.detect_task_category(task) == "data_processing"

    def test_default_backend(self):
        import scheduler_legacy as sch
        task = {"title": "优化缓存策略", "description": ""}
        assert sch.detect_task_category(task) == "backend_script"

    def test_description_also_checked(self):
        import scheduler_legacy as sch
        task = {"title": "新功能", "description": "需要修改前端 html 页面"}
        assert sch.detect_task_category(task) == "web_frontend"


# ══════════════════════════════════════════
# Test: Review Level Integration
# ══════════════════════════════════════════

class TestReviewLevelIntegration:
    def test_worker_prompt_includes_review_level(self, monkeypatch):
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "Review 级别" in prompt

    def test_quick_task_l0(self, monkeypatch):
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="quick")
        prompt = sch.generate_worker_prompt(task)
        assert "L0" in prompt

    def test_p0_task_l3(self, monkeypatch):
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P0")
        prompt = sch.generate_worker_prompt(task)
        assert "L3" in prompt

    def test_verification_guidance_in_prompt(self, monkeypatch):
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="修复 Dashboard 页面", description="前端 bug")
        prompt = sch.generate_worker_prompt(task)
        assert "浏览器" in prompt
        assert "截图" in prompt

    def test_backend_verification_in_prompt(self, monkeypatch):
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="优化缓存逻辑")
        prompt = sch.generate_worker_prompt(task)
        assert "单元测试" in prompt
        assert "集成测试" in prompt


# ══════════════════════════════════════════
# Test: Spawn Instruction Generation
# ══════════════════════════════════════════

class TestSpawnInstruction:
    def test_spawn_instruction_fields(self):
        import scheduler_legacy as sch
        task = _create_task("T-20260330-001")
        instr = sch.generate_spawn_instruction(task, parent_session_id="web_123")
        assert instr["task_id"] == "T-20260330-001"
        assert "task_prompt" in instr
        assert "title" in instr
        assert "priority" in instr
        assert "template" in instr
        # Old fields should NOT exist
        assert "session_key" not in instr
        assert "message" not in instr
        assert "parent" not in instr
        assert "dispatcher_session_id" not in instr

    def test_spawn_instruction_prompt_content(self):
        import scheduler_legacy as sch
        task = _create_task("T-20260330-001")
        instr = sch.generate_spawn_instruction(task, parent_session_id="webchat_dispatch_123_456")
        # task_prompt should contain task details
        assert "T-20260330-001" in instr["task_prompt"]
        # No callback artifacts
        assert "report_completion" not in instr["task_prompt"]
        assert "dispatcher-session" not in instr["task_prompt"]


# ══════════════════════════════════════════
# Test: Scheduler Run (Integration)
# ══════════════════════════════════════════

class TestSchedulerRun:
    def test_dry_run_no_state_change(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")

        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert result["dry_run"] is True
        assert len(result["spawn_instructions"]) == 1

        # Task should still be queued
        task = bm.load_task("T-20260330-001")
        assert task["status"] == "queued"

    def test_live_run_updates_status(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")

        result = sch.run_scheduler(dry_run=False)
        assert result["ok"]
        assert result["dry_run"] is False
        assert len(result["spawn_instructions"]) == 1

        # Task should now be executing
        task = bm.load_task("T-20260330-001")
        assert task["status"] == "executing"

    def test_no_queued_tasks_empty_dispatch(self):
        import scheduler_legacy as sch
        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 0

    def test_dependency_blocks_dispatch(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="executing")
        _create_task("T-20260330-002", status="queued", blocked_by=["T-20260330-001"])

        result = sch.run_scheduler(dry_run=True)
        assert len(result["spawn_instructions"]) == 0
        assert result["report"]["summary"]["skipped_dependency"] == 1

    def test_priority_ordering_in_dispatch(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued", priority="P2",
                     created="2026-03-30T10:00:00+08:00")
        _create_task("T-20260330-002", status="queued", priority="P0",
                     created="2026-03-30T11:00:00+08:00")
        _create_task("T-20260330-003", status="queued", priority="P1",
                     created="2026-03-30T09:00:00+08:00")

        result = sch.run_scheduler(dry_run=True)
        ids = [d["task_id"] for d in result["spawn_instructions"]]
        assert ids == ["T-20260330-002", "T-20260330-003", "T-20260330-001"]

    def test_report_structure(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued")
        result = sch.run_scheduler(dry_run=True)
        report = result["report"]
        assert "timestamp" in report
        assert "summary" in report
        assert "dispatched_tasks" in report
        assert "skipped_dependency_tasks" in report
        assert "skipped_cap_tasks" in report
        assert "review_pending" in report


# ══════════════════════════════════════════
# Test: Status
# ══════════════════════════════════════════

class TestStatus:
    def test_status_empty(self):
        import scheduler_legacy as sch
        result = sch.get_status()
        assert result["ok"]
        assert result["data"]["available_slots"] == sch.MAX_CONCURRENT_EXECUTING

    def test_status_with_tasks(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued", priority="P0")
        _create_task("T-20260330-002", status="executing")

        result = sch.get_status()
        data = result["data"]
        assert data["task_counts"]["queued"] == 1
        assert data["task_counts"]["executing"] == 1
        assert len(data["queued_tasks"]) == 1
        assert len(data["executing_tasks"]) == 1


# ══════════════════════════════════════════
# Test: Completed Tasks / Review Follow-up
# ══════════════════════════════════════════

class TestCompletedTasks:
    def test_no_review_tasks(self):
        import scheduler_legacy as sch
        result = sch.check_completed_tasks()
        assert result == []

    def test_review_task_with_pending_review(self):
        import scheduler_legacy as sch
        import brain_manager as bm

        _create_task("T-20260330-001", status="review")
        # Create a pending review
        review = {
            "id": "R-20260330-001",
            "task_id": "T-20260330-001",
            "status": "pending",
            "summary": "Code review needed",
            "prompt": "Please review",
            "created": bm.now_iso(),
        }
        bm.save_review(review)

        result = sch.check_completed_tasks()
        assert len(result) == 1
        assert result[0]["task_id"] == "T-20260330-001"
        assert result[0]["pending_reviews"] == 1


# ══════════════════════════════════════════
# Test: Circular Dependency (S-4)
# ══════════════════════════════════════════

class TestCircularDependency:
    def test_mutual_dependency_neither_dispatched(self):
        """A depends on B, B depends on A — neither should be scheduled."""
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued", blocked_by=["T-20260330-002"])
        _create_task("T-20260330-002", status="queued", blocked_by=["T-20260330-001"])

        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 0
        assert result["report"]["summary"]["skipped_dependency"] == 2

    def test_three_way_cycle(self):
        """A→B→C→A cycle — none should be scheduled."""
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="queued", blocked_by=["T-20260330-003"])
        _create_task("T-20260330-002", status="queued", blocked_by=["T-20260330-001"])
        _create_task("T-20260330-003", status="queued", blocked_by=["T-20260330-002"])

        result = sch.run_scheduler(dry_run=True)
        assert len(result["spawn_instructions"]) == 0
        assert result["report"]["summary"]["skipped_dependency"] == 3


# ══════════════════════════════════════════
# Test: transition_task Failure Handling (S-5)
# ══════════════════════════════════════════

class TestTransitionTaskFailure:
    def test_save_failure_recorded_as_error(self):
        """When transition_task raises, the task should appear in errors, not skipped_dependency."""
        import scheduler_legacy as sch
        import brain_manager as bm

        _create_task("T-20260330-001", status="queued")

        # Mock transition_task to raise an exception
        with mock.patch.object(bm, "transition_task", side_effect=RuntimeError("disk full")):
            result = sch.run_scheduler(dry_run=False)

        assert result["ok"]
        # Should NOT be in dispatched
        assert len(result["spawn_instructions"]) == 0
        # Should be in errors, not skipped_dependency
        assert result["report"]["summary"]["errors"] == 1
        assert result["report"]["summary"]["skipped_dependency"] == 0
        assert result["report"]["errors"][0]["task_id"] == "T-20260330-001"
        assert "disk full" in result["report"]["errors"][0]["error"]

    def test_invalid_transition_recorded_as_error(self):
        """transition_task raises ValueError for invalid FSM transition."""
        import scheduler_legacy as sch
        import brain_manager as bm

        _create_task("T-20260330-001", status="queued")

        with mock.patch.object(bm, "transition_task",
                               side_effect=ValueError("Invalid transition: done → executing")):
            result = sch.run_scheduler(dry_run=False)

        assert result["report"]["summary"]["errors"] == 1
        assert len(result["spawn_instructions"]) == 0


# ══════════════════════════════════════════
# Test: Round 1 Fixes — Timeout Recovery (Fix 4)
# ══════════════════════════════════════════

class TestTimeoutRecovery:
    def test_no_stale_tasks(self):
        import scheduler_legacy as sch
        assert sch.check_stale_executing_tasks() == []

    def test_recent_executing_not_stale(self):
        import scheduler_legacy as sch
        _create_task("T-20260330-001", status="executing")
        stale = sch.check_stale_executing_tasks()
        assert len(stale) == 0

    def test_old_executing_is_stale(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        from datetime import timedelta
        old_time = (datetime.now().astimezone() - timedelta(minutes=90)).replace(microsecond=0).isoformat()
        task = _create_task("T-20260330-001", status="executing")
        task["history"].append({
            "timestamp": old_time,
            "action": "status_change",
            "detail": "status: queued → executing",
        })
        task["updated"] = old_time
        bm.save_task(task)

        stale = sch.check_stale_executing_tasks()
        assert len(stale) == 1
        assert stale[0]["task_id"] == "T-20260330-001"
        assert stale[0]["elapsed_minutes"] >= 89

    def test_recover_stale_to_queued(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        from datetime import timedelta
        old_time = (datetime.now().astimezone() - timedelta(minutes=90)).replace(microsecond=0).isoformat()
        task = _create_task("T-20260330-001", status="executing")
        task["history"].append({
            "timestamp": old_time,
            "action": "status_change",
            "detail": "status: queued → executing",
        })
        task["updated"] = old_time
        bm.save_task(task)
        stale = sch.check_stale_executing_tasks()
        recovered = sch.recover_stale_tasks(stale)
        assert len(recovered) == 1
        assert recovered[0]["action"] == "queued"
        reloaded = bm.load_task("T-20260330-001")
        assert reloaded["status"] == "queued"

    def test_recover_blocked_after_max_retries(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        from datetime import timedelta
        old_time = (datetime.now().astimezone() - timedelta(minutes=90)).replace(microsecond=0).isoformat()
        task = _create_task("T-20260330-001", status="executing")
        task["timeout_count"] = sch.MAX_TIMEOUT_RECOVERY_COUNT - 1
        task["history"].append({
            "timestamp": old_time,
            "action": "status_change",
            "detail": "status: queued → executing",
        })
        task["updated"] = old_time
        bm.save_task(task)
        stale = sch.check_stale_executing_tasks()
        recovered = sch.recover_stale_tasks(stale)
        assert len(recovered) == 1
        assert recovered[0]["action"] == "blocked"
        reloaded = bm.load_task("T-20260330-001")
        assert reloaded["status"] == "blocked"

    def test_dry_run_no_state_change(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        from datetime import timedelta
        old_time = (datetime.now().astimezone() - timedelta(minutes=90)).replace(microsecond=0).isoformat()
        task = _create_task("T-20260330-001", status="executing")
        task["history"].append({
            "timestamp": old_time,
            "action": "status_change",
            "detail": "status: queued → executing",
        })
        task["updated"] = old_time
        bm.save_task(task)
        stale = sch.check_stale_executing_tasks()
        recovered = sch.recover_stale_tasks(stale, dry_run=True)
        assert len(recovered) == 1
        assert recovered[0]["action"] == "would_queue"
        reloaded = bm.load_task("T-20260330-001")
        assert reloaded["status"] == "executing"

    def test_stale_recovered_in_report(self):
        import scheduler_legacy as sch
        import brain_manager as bm
        from datetime import timedelta
        old_time = (datetime.now().astimezone() - timedelta(minutes=90)).replace(microsecond=0).isoformat()
        task = _create_task("T-20260330-001", status="executing")
        task["history"].append({
            "timestamp": old_time,
            "action": "status_change",
            "detail": "status: queued → executing",
        })
        task["updated"] = old_time
        bm.save_task(task)
        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert result["report"]["summary"]["stale_recovered"] >= 1


# ══════════════════════════════════════════
# Test: Round 1 Fixes — Worker Prompt (Fix 1C + Fix 3)
# ══════════════════════════════════════════

class TestWorkerPromptFixes:
    def test_L2_prompt_no_status_done(self, monkeypatch):
        """L2 task prompt should NOT contain --status done."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" not in prompt
        assert "--status review" in prompt

    def test_L0_prompt_has_status_done(self, monkeypatch):
        """L0 task prompt should contain --status done."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="quick", priority="P2")
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" in prompt

    def test_L1_prompt_has_status_done(self, monkeypatch):
        """L1 task prompt should contain --status done."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        task["files_changed"] = 1
        import brain_manager as bm
        bm.save_task(task)
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" in prompt

    def test_prompt_has_evidence_requirements(self, monkeypatch):
        """All prompts should have evidence requirements."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "验收证据要求" in prompt
        assert "文档更新检查" in prompt

    def test_nanobot_task_has_dev_env_guidance(self, monkeypatch):
        """Task involving nanobot code should have dev env guidance."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="修复 scheduler 调度逻辑")
        prompt = sch.generate_worker_prompt(task)
        assert "Dev 环境实测要求" in prompt
        assert "9081" in prompt

    def test_pure_data_task_no_dev_env(self, monkeypatch):
        """Non-nanobot task should NOT have dev env guidance."""
        import scheduler_legacy as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="更新 README 文档")
        prompt = sch.generate_worker_prompt(task)
        assert "Dev 环境实测要求" not in prompt

    def test_needs_dev_test_detection(self):
        import scheduler_legacy as sch
        assert sch.needs_dev_test({"title": "修复 scheduler 调度逻辑"}) is True
        assert sch.needs_dev_test({"title": "更新 README 文档"}) is False
        assert sch.needs_dev_test({"title": "飞书消息格式优化"}) is True
        assert sch.needs_dev_test({"title": "优化 gateway 性能"}) is True


# ──────────────────────────────────────────
# Multi-role orchestration tests (Phase 1 MVP)
# ──────────────────────────────────────────

class TestReportSchema:
    """Test report schema constants."""

    def test_report_schema_constants_defined(self):
        import scheduler_legacy as sched
        assert hasattr(sched, "REPORT_SCHEMA")
        assert "required" in sched.REPORT_SCHEMA
        assert "valid_roles" in sched.REPORT_SCHEMA
        assert "valid_verdicts" in sched.REPORT_SCHEMA
        assert "developer" in sched.REPORT_SCHEMA["valid_roles"]
        assert "tester" in sched.REPORT_SCHEMA["valid_roles"]
        assert "pass" in sched.REPORT_SCHEMA["valid_verdicts"]


class TestParseWorkerReport:
    """Test parse_worker_report function."""

    def test_parse_valid_report(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Write valid report
        report_data = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "All tests passed",
            "issues": [],
            "files_changed": ["test.py"],
        }
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump(report_data, f)

        result = sched.parse_worker_report("T-001", "developer")
        assert result is not None
        assert result["task_id"] == "T-001"
        assert result["verdict"] == "pass"

    def test_parse_invalid_json(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Write invalid JSON
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            f.write("{invalid json")

        result = sched.parse_worker_report("T-001", "developer")
        assert result is None

    def test_parse_missing_required_field(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Missing verdict field
        report_data = {
            "task_id": "T-001",
            "role": "developer",
            "summary": "Test",
        }
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump(report_data, f)

        result = sched.parse_worker_report("T-001", "developer")
        assert result is None

    def test_parse_task_id_mismatch(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # task_id in file doesn't match requested
        report_data = {
            "task_id": "T-002",
            "role": "developer",
            "verdict": "pass",
            "summary": "Test",
        }
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump(report_data, f)

        result = sched.parse_worker_report("T-001", "developer")
        assert result is None

    def test_parse_no_report_file(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        result = sched.parse_worker_report("T-999", "developer")
        assert result is None

    def test_parse_multiple_reports_takes_newest(self, tmp_path, monkeypatch):
        """When multiple reports have the same verdict, newest wins."""
        import scheduler_legacy as sched
        import time
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Write older report (same verdict as newer)
        report1 = reports_dir / "T-001-developer-1000000000.json"
        with report1.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Old report",
            }, f)

        time.sleep(0.01)

        # Write newer report
        report2 = reports_dir / "T-001-developer-2000000000.json"
        with report2.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "New report",
            }, f)

        result = sched.parse_worker_report("T-001", "developer")
        assert result is not None
        assert result["summary"] == "New report"

    def test_parse_multiple_reports_fail_first_priority(self, tmp_path, monkeypatch):
        """Fail-first: an older 'fail' verdict beats a newer 'pass' (P0-3 fix)."""
        import scheduler_legacy as sched
        import time
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Older report with fail
        report1 = reports_dir / "T-001-developer-1000000000.json"
        with report1.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "fail",
                "summary": "Failed report",
            }, f)

        time.sleep(0.01)

        # Newer report with pass
        report2 = reports_dir / "T-001-developer-2000000000.json"
        with report2.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Passed report",
            }, f)

        result = sched.parse_worker_report("T-001", "developer")
        assert result is not None
        # fail-first: the fail verdict should be preferred
        assert result["verdict"] == "fail"
        assert result["summary"] == "Failed report"

    def test_parse_with_role_filter(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Write developer report
        dev_report = reports_dir / "T-001-developer-1234567890.json"
        with dev_report.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Dev report",
            }, f)

        # Write tester report
        test_report = reports_dir / "T-001-tester-1234567891.json"
        with test_report.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "tester",
                "verdict": "fail",
                "summary": "Test report",
            }, f)

        # Filter by role
        result = sched.parse_worker_report("T-001", "tester")
        assert result is not None
        assert result["role"] == "tester"
        assert result["summary"] == "Test report"


class TestMakeDecision:
    """Test make_decision function."""

    def test_tester_pass_l2_dispatches_test_review(self):
        """V6.1: PL2 tester pass → test_review (was promote_to_review)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "tester",
            "verdict": "pass",
            "summary": "All tests passed",
            "test_evidence": [{"type": "command_output", "command": "pytest", "result": "5 passed"}],
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "test_review"

    def test_developer_pass_pl0_marks_done(self):
        """PL0 (quick): developer pass → mark_done directly."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Quick task done",
            "smoke_test": {"status": "pass", "output": "OK"},
        }
        task = {
            "id": "T-001",
            "priority": "P2",
            "workgroup": {"template": "quick"},  # PL0 template
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_done"

    def test_tester_fail_dispatches_developer(self):
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "tester",
            "verdict": "fail",
            "summary": "Tests failed",
            "issues": [{"description": "Bug found"}],
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_developer_pass_dispatches_code_review(self):
        """V6.1: PL2 developer pass → code_review (was tester)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Implementation complete",
            "files_changed": ["src/main.py", "DEVLOG.md"],
            "smoke_test": {"status": "pass", "output": "import OK, 5 tests passed"},
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
            "design_ref": "D-test-001",  # satisfies doc triplet design check
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "code_review"

    def test_developer_pass_quick_marks_done(self):
        """PL0 (quick): developer pass → mark_done. Needs smoke_test for code tasks."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Quick task done",
            "smoke_test": {"status": "pass", "output": "OK"},
        }
        task = {
            "id": "T-001",
            "priority": "P2",
            "workgroup": {"template": "quick"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_done"

    def test_developer_fail_retries(self):
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "fail",
            "summary": "Implementation failed",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_developer_fail_max_consecutive_blocks(self):
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "fail",
            "summary": "Still failing",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {
                "iteration": 3,
                "history": [
                    {"role": "developer", "verdict": "fail"},
                    {"role": "developer", "verdict": "fail"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"

    def test_max_iterations_blocks(self):
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Done",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 10, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "max iterations" in decision.reason

    def test_no_report_blocks(self):
        import scheduler_legacy as sched
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(None, task)
        assert decision.action == "mark_blocked"
        assert "no worker report" in decision.reason

    def test_blocked_verdict_blocks(self):
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "blocked",
            "summary": "Dependency missing",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"

    def test_partial_verdict_continuable_dispatches_same_role(self):
        """Partial verdict without blocker keywords should continue with same role."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "partial",
            "summary": "Partially done, need more time to finish remaining tests",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params.get("role") == "developer"
        assert "partial" in decision.reason.lower()
        assert "continuable" in decision.reason.lower()

    def test_partial_verdict_true_blocker_blocks(self):
        """Partial verdict with blocker keywords should mark blocked."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "partial",
            "summary": "Need API key to access the service, cannot proceed",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "blocker" in decision.reason.lower()

    def test_partial_verdict_blocker_in_issues(self):
        """Partial verdict with blocker keywords in issues should mark blocked."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "partial",
            "summary": "Some work done",
            "issues": [{"description": "需要权限才能访问生产环境"}],
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"

    def test_partial_verdict_tester_continuable(self):
        """Partial verdict from tester should also continue with same role."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-001",
            "role": "tester",
            "verdict": "partial",
            "summary": "Ran 5 of 10 test cases, need more iterations",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params.get("role") == "tester"

    # --- Consecutive role limit: only blocks partial, not pass/fail ---

    def test_developer_consecutive_pass_dispatches_code_review(self):
        """T-004 scenario: developer partial → developer pass → should go to code_review, NOT blocked.
        V6.1: was tester, now code_review."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-004",
            "role": "developer",
            "verdict": "pass",
            "summary": "Implementation complete after partial",
            "files_changed": ["src/main.py", "DEVLOG.md"],
            "smoke_test": {"status": "pass", "output": "OK"},
        }
        task = {
            "id": "T-004",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "design_ref": "D-test-004",  # satisfies doc triplet design check
            "orchestration": {
                "iteration": 2,
                "history": [
                    {"role": "developer", "verdict": "partial"},
                    {"role": "developer", "verdict": "pass"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "code_review"
        assert "blocked" not in decision.reason.lower()

    def test_developer_consecutive_partial_blocks(self):
        """Developer partial × 2 → 3rd partial → should be blocked (prevents infinite loop)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-005",
            "role": "developer",
            "verdict": "partial",
            "summary": "Still not done",
        }
        task = {
            "id": "T-005",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {
                "iteration": 3,
                "history": [
                    {"role": "developer", "verdict": "partial"},
                    {"role": "developer", "verdict": "partial"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "consecutive" in decision.reason.lower()

    def test_tester_consecutive_pass_dispatches_test_review(self):
        """Tester pass after consecutive tester rounds → should dispatch test_review, NOT blocked.
        V6.1: was promote_to_review/mark_done, now test_review."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-006",
            "role": "tester",
            "verdict": "pass",
            "summary": "All tests passed on retry",
            "test_evidence": [{"type": "manual_test", "description": "retry test", "result": "OK"}],
        }
        task = {
            "id": "T-006",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {
                "iteration": 3,
                "history": [
                    {"role": "tester", "verdict": "partial"},
                    {"role": "tester", "verdict": "pass"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        # V6.1: tester pass → test_review (dispatch_role)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "test_review"

    def test_tester_fail_dispatches_developer_regardless_of_consecutive(self):
        """Tester fail → developer, even after consecutive tester rounds."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-007",
            "role": "tester",
            "verdict": "fail",
            "summary": "Tests failed",
            "issues": [{"description": "Regression bug"}],
        }
        task = {
            "id": "T-007",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {
                "iteration": 3,
                "history": [
                    {"role": "tester", "verdict": "partial"},
                    {"role": "tester", "verdict": "fail"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_tester_consecutive_partial_blocks(self):
        """Tester partial × 2 → 3rd partial → should be blocked."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-008",
            "role": "tester",
            "verdict": "partial",
            "summary": "Still running tests",
        }
        task = {
            "id": "T-008",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {
                "iteration": 3,
                "history": [
                    {"role": "tester", "verdict": "partial"},
                    {"role": "tester", "verdict": "partial"},
                ],
            },
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "consecutive" in decision.reason.lower()


class TestExecuteDecision:
    """Test execute_decision function."""

    def test_promote_to_review_creates_review(self):
        import scheduler_legacy as sched
        import brain_manager as bm

        task = _create_task("T-001", status="executing", priority="P1")
        decision = sched.Decision(
            action="promote_to_review",
            params={"summary": "Ready for review"},
            reason="tester passed"
        )

        result = sched.execute_decision(decision, task)
        assert result["ok"] is True
        assert "review_id" in result

        # Verify task transitioned to review
        reloaded = bm.load_task("T-001")
        assert reloaded["status"] == "review"

    def test_mark_done_transitions(self):
        import scheduler_legacy as sched
        import brain_manager as bm

        # Use quick template for L0 review (can go directly to done)
        task = _create_task("T-001", status="executing", priority="P2", template="quick")
        decision = sched.Decision(
            action="mark_done",
            reason="tester passed, L0 review"
        )

        result = sched.execute_decision(decision, task)
        if not result["ok"]:
            print(f"Error: {result}")
        assert result["ok"] is True

        reloaded = bm.load_task("T-001")
        assert reloaded["status"] == "done"

    def test_mark_blocked_transitions(self):
        import scheduler_legacy as sched
        import brain_manager as bm

        task = _create_task("T-001", status="executing")
        decision = sched.Decision(
            action="mark_blocked",
            reason="max iterations reached"
        )

        result = sched.execute_decision(decision, task)
        assert result["ok"] is True

        reloaded = bm.load_task("T-001")
        assert reloaded["status"] == "blocked"

    def test_dispatch_role_updates_orchestration(self):
        import scheduler_legacy as sched
        import brain_manager as bm

        task = _create_task("T-001", status="executing")
        decision = sched.Decision(
            action="dispatch_role",
            params={"role": "tester", "context": "Dev completed"},
            reason="developer passed"
        )

        result = sched.execute_decision(decision, task)
        assert result["ok"] is True
        assert "spawn_instruction" in result
        assert result["role"] == "tester"

        # Verify orchestration state updated
        reloaded = bm.load_task("T-001")
        assert "orchestration" in reloaded
        assert reloaded["orchestration"]["iteration"] == 1
        assert len(reloaded["orchestration"]["history"]) == 1


class TestHandleWorkerCompletion:
    """Test handle_worker_completion end-to-end pipeline."""

    def test_full_pipeline_tester_fail_to_developer(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        import brain_manager as bm

        # Setup reports dir
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Create task
        task = _create_task("T-001", status="executing", priority="P1")

        # Write tester fail report
        report_file = reports_dir / "T-001-tester-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "tester",
                "verdict": "fail",
                "summary": "Tests failed",
                "issues": [{"description": "Bug found"}],
            }, f)

        # Handle completion
        result = sched.handle_worker_completion("T-001", "tester")
        assert result["ok"] is True
        assert result["decision"]["action"] == "dispatch_role"
        assert "spawn_instruction" in result

        # Verify task still executing
        reloaded = bm.load_task("T-001")
        assert reloaded["status"] == "executing"
        assert "orchestration" in reloaded

    def test_full_pipeline_developer_pass_to_code_review(self, tmp_path, monkeypatch):
        """V6.1: developer pass → code_review (was tester)."""
        import scheduler_legacy as sched
        import brain_manager as bm

        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        task = _create_task("T-001", status="executing", priority="P1")
        # Add design_ref to satisfy doc triplet check
        task["design_ref"] = "D-test-001"
        bm.save_task(task)

        # Write developer pass report (include DEVLOG in files_changed + smoke_test)
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Implementation complete",
                "files_changed": ["main.py", "DEVLOG.md"],
                "smoke_test": {"status": "pass", "output": "OK"},
            }, f)

        result = sched.handle_worker_completion("T-001", "developer")
        assert result["ok"] is True
        assert result["decision"]["action"] == "dispatch_role"
        assert result["role"] == "code_review"

    def test_full_pipeline_tester_pass_to_test_review(self, tmp_path, monkeypatch):
        """V6.1: tester pass → test_review (was promote_to_review)."""
        import scheduler_legacy as sched
        import brain_manager as bm

        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        task = _create_task("T-001", status="executing", priority="P1")

        # Write tester pass report
        report_file = reports_dir / "T-001-tester-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "tester",
                "verdict": "pass",
                "summary": "All tests passed",
                "test_evidence": [{"type": "command_output", "command": "pytest", "result": "5 passed"}],
            }, f)

        result = sched.handle_worker_completion("T-001", "tester")
        assert result["ok"] is True
        assert result["decision"]["action"] == "dispatch_role"
        assert result["role"] == "test_review"


class TestGenerateWorkerPromptV2:
    """Test generate_worker_prompt_v2 function."""

    def test_no_brain_manager_in_prompt(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "brain_manager" not in prompt.lower()
        assert "status" not in prompt.lower() or "Report Submission" in prompt

    def test_report_template_in_prompt(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "task_id" in prompt
        assert "verdict" in prompt
        assert "pass|fail|blocked|partial" in prompt

    def test_absolute_path_in_prompt(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "/data/brain/reports/T-001-developer-" in prompt

    def test_role_guidance_developer(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "Developer" in prompt
        assert "Implement" in prompt or "implement" in prompt

    def test_role_guidance_tester(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "tester")
        assert "Tester" in prompt
        assert "verify" in prompt.lower() or "test" in prompt.lower()

    def test_prior_context_included(self):
        import scheduler_legacy as sched
        task = _create_task("T-001", status="executing")

        prior_context = "## Previous attempt failed\nBug in line 42"
        prompt = sched.generate_worker_prompt_v2(task, "developer", prior_context)
        assert "Previous attempt failed" in prompt


class TestLegacyMode:
    """Test LEGACY_MODE switch."""

    def test_legacy_mode_uses_old_prompt(self, monkeypatch):
        import scheduler_legacy as sched
        monkeypatch.setattr(sched, "LEGACY_MODE", True)

        task = _create_task("T-001", status="executing")
        prompt = sched.generate_worker_prompt(task)

        # Legacy prompt has brain_manager commands
        assert "brain_manager" in prompt.lower()

    def test_non_legacy_uses_new_prompt(self, monkeypatch):
        import scheduler_legacy as sched
        monkeypatch.setattr(sched, "LEGACY_MODE", False)

        task = _create_task("T-001", status="executing")
        prompt = sched.generate_worker_prompt(task, "developer")

        # New prompt has report template
        assert "verdict" in prompt.lower()
        assert "brain_manager" not in prompt.lower()


class TestHandleCompletionCLI:
    """Test handle-completion CLI command."""

    def test_cli_handle_completion_with_task_id(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched

        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        task = _create_task("T-001", status="executing", priority="P2")

        # Write report
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Done",
            }, f)

        # Simulate CLI call
        result = sched.handle_worker_completion("T-001")
        assert result["ok"] is True

    def test_cli_handle_completion_auto_detect(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        import time

        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        task = _create_task("T-001", status="executing", priority="P2")

        # Write recent report
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Done",
            }, f)

        # Touch file to make it recent
        os.utime(report_file, (time.time(), time.time()))

        # Auto-detect should find it
        result = sched.parse_worker_report("T-001")
        assert result is not None


# ──────────────────────────────────────────
# Phase 1: Design gate & doc triplet tests
# ──────────────────────────────────────────

class TestCheckDesignGate:
    """Test check_design_gate() function."""

    def test_quick_template_exempt(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "quick"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "exempt" in reason

    def test_cron_auto_exempt(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "cron-auto"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_has_design_ref(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "design ref" in reason

    def test_has_architect_report(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        (reports_dir / "T-001-architect-123.json").write_text('{"task_id":"T-001"}')
        task = {"id": "T-001", "template": "standard-dev"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "architect report" in reason

    def test_has_architect_in_history(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "standard-dev",
                "orchestration": {"history": [{"role": "architect", "verdict": "pass"}]}}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_emergency_exempt(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "standard-dev", "emergency": True}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "emergency" in reason

    def test_needs_design_false(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "standard-dev", "needs_design": False}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_no_design_fails(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        task = {"id": "T-001", "template": "standard-dev",
                "orchestration": {"history": []}}
        ok, reason = sched.check_design_gate(task)
        assert ok is False
        assert "no design document" in reason

    def test_feature_flag_disabled(self, monkeypatch):
        import scheduler_legacy as sched
        monkeypatch.setattr(sched, "DESIGN_GATE_ENABLED", False)
        task = {"id": "T-001", "template": "standard-dev"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "feature flag" in reason


class TestCheckDocTriplet:
    """Test check_doc_triplet() function."""

    def test_quick_exempt(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "quick"}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True
        assert missing == []

    def test_emergency_exempt(self):
        import scheduler_legacy as sched
        task = {"id": "T-001", "template": "standard-dev", "emergency": True}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True

    def test_has_devlog_and_design_ref(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["src/main.py", "DEVLOG.md"]}
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is True

    def test_missing_devlog(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["src/main.py"]}
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is False
        assert "DEVLOG.md" in missing

    def test_missing_design(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["DEVLOG.md"]}
        task = {"id": "T-001", "template": "standard-dev"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is False
        assert any("ARCHITECTURE" in m for m in missing)

    def test_feature_flag_disabled(self, monkeypatch):
        import scheduler_legacy as sched
        monkeypatch.setattr(sched, "DOC_TRIPLET_CHECK_ENABLED", False)
        task = {"id": "T-001", "template": "standard-dev"}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True


class TestDocRetryEscalation:
    """Test doc retry counter and escalation to manual review."""

    def test_developer_pass_missing_docs_sends_back(self, tmp_path, monkeypatch):
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"task_id": "T-001", "role": "developer", "verdict": "pass",
                  "summary": "Done", "files_changed": ["main.py"]}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 1, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_developer_pass_missing_docs_escalates_after_max_retry(self, tmp_path, monkeypatch):
        """Developer pass with missing docs escalates to review after MAX_DOC_RETRY retries."""
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"task_id": "T-001", "role": "developer", "verdict": "pass",
                  "summary": "Done again", "files_changed": ["main.py"],
                  "smoke_test": {"status": "pass", "output": "OK"}}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 3, "history": [
                    {"role": "developer", "verdict": "pass", "context": "⚠️ 文档三件套不完整，缺少: missing docs"},
                    {"role": "developer", "verdict": "pass", "context": "⚠️ 文档三件套不完整，缺少: missing docs"},
                ]}}
        decision = sched.make_decision(report, task)
        assert decision.action == "promote_to_review"

    def test_tester_pass_dispatches_test_review(self, tmp_path, monkeypatch):
        """V6.1: PL2 tester pass → test_review (doc check happens at review_check after retrospective)."""
        import scheduler_legacy as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "Tests passed", "files_changed": ["test.py"],
                  "test_evidence": [{"type": "command_output", "command": "pytest", "result": "OK"}]}
        import brain_manager as bm
        monkeypatch.setattr(bm, "determine_review_level", lambda t: "L0")
        task = {"id": "T-001", "priority": "P2",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 2, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "test_review"


# ──────────────────────────────────────────
# V6.1 Tests: 8-role cross-check flow
# ──────────────────────────────────────────

class TestV61RoleCrossCheck:
    """Tests for V6.1 8-role cross-check flow (code_review, test_review, architect_review repositioned)."""

    def test_pl2_architect_pass_dispatches_architect_review(self):
        """PL2: architect pass → architect_review (V6.1: review architecture before development)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Design complete",
            "acceptance_plan": [{"step_id": "T1", "description": "test", "category": "e2e", "expected_result": "ok"}],
        }
        task = {
            "id": "T-V61-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "architect_review"

    def test_pl2_architect_review_pass_dispatches_developer(self):
        """PL2: architect_review pass → developer."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-002",
            "role": "architect_review",
            "verdict": "pass",
            "summary": "Architecture looks good",
        }
        task = {
            "id": "T-V61-002",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 2, "history": [
                {"role": "architect", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_pl2_architect_review_fail_dispatches_architect(self):
        """PL2: architect_review fail → architect (send back to redesign)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-003",
            "role": "architect_review",
            "verdict": "fail",
            "summary": "Design has gaps",
            "issues": ["Missing error handling design"],
        }
        task = {
            "id": "T-V61-003",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 2, "history": [
                {"role": "architect", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "architect"

    def test_pl2_code_review_pass_dispatches_tester(self):
        """PL2: code_review pass → tester."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-004",
            "role": "code_review",
            "verdict": "pass",
            "summary": "Code matches design, tests adequate",
        }
        task = {
            "id": "T-V61-004",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 3, "history": [
                {"role": "architect", "verdict": "pass"},
                {"role": "architect_review", "verdict": "pass"},
                {"role": "developer", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_pl2_code_review_fail_dispatches_developer(self):
        """PL2: code_review fail → developer (D11: check role fail sends back to execution role)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-005",
            "role": "code_review",
            "verdict": "fail",
            "summary": "Unit tests insufficient",
            "issues": ["Missing edge case tests"],
        }
        task = {
            "id": "T-V61-005",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 3, "history": [
                {"role": "architect", "verdict": "pass"},
                {"role": "architect_review", "verdict": "pass"},
                {"role": "developer", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_pl2_test_review_pass_dispatches_retrospective(self):
        """PL2: test_review pass → retrospective."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-006",
            "role": "test_review",
            "verdict": "pass",
            "summary": "Tests are thorough and evidence is credible",
        }
        task = {
            "id": "T-V61-006",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 5, "history": [
                {"role": "architect", "verdict": "pass"},
                {"role": "architect_review", "verdict": "pass"},
                {"role": "developer", "verdict": "pass"},
                {"role": "code_review", "verdict": "pass"},
                {"role": "tester", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "retrospective"

    def test_pl2_test_review_fail_dispatches_tester(self):
        """PL2: test_review fail → tester (D11: check role fail sends back to execution role)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-007",
            "role": "test_review",
            "verdict": "fail",
            "summary": "E2E tests are mocked, not real",
            "issues": ["Tests use mock instead of real service"],
        }
        task = {
            "id": "T-V61-007",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 5, "history": [
                {"role": "architect", "verdict": "pass"},
                {"role": "architect_review", "verdict": "pass"},
                {"role": "developer", "verdict": "pass"},
                {"role": "code_review", "verdict": "pass"},
                {"role": "tester", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_pl3_test_review_pass_dispatches_auditor(self):
        """PL3: test_review pass → auditor (not retrospective like PL2)."""
        import scheduler_legacy as sched
        report = {
            "task_id": "T-V61-008",
            "role": "test_review",
            "verdict": "pass",
            "summary": "Tests are thorough",
        }
        task = {
            "id": "T-V61-008",
            "priority": "P0",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 6, "history": [
                {"role": "architect", "verdict": "pass"},
                {"role": "architect_review", "verdict": "pass"},
                {"role": "developer", "verdict": "pass"},
                {"role": "code_review", "verdict": "pass"},
                {"role": "tester", "verdict": "pass"},
            ]},
        }
        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "auditor"

    def test_valid_roles_has_8_roles(self):
        """V6.1: VALID_ROLES should have 8 roles."""
        import scheduler_legacy as sched
        assert len(sched.VALID_ROLES) == 8
        assert "code_review" in sched.VALID_ROLES
        assert "test_review" in sched.VALID_ROLES

    def test_flow_transitions_count(self):
        """V6.1: FLOW_TRANSITIONS should have 34 entries (PL0:2 + PL1:2 + PL2:14 + PL3:16)."""
        import scheduler_legacy as sched
        assert len(sched.FLOW_TRANSITIONS) == 34
        # Count per PL
        pl_counts = {}
        for (pl, _, _) in sched.FLOW_TRANSITIONS:
            pl_counts[pl] = pl_counts.get(pl, 0) + 1
        assert pl_counts["PL0"] == 2
        assert pl_counts["PL1"] == 2
        assert pl_counts["PL2"] == 14
        assert pl_counts["PL3"] == 16

    def test_expected_flow_pl2(self):
        """V6.1: PL2 expected flow includes all 7 roles."""
        import scheduler_legacy as sched
        flow = sched._get_expected_flow("PL2")
        assert flow == ["architect", "architect_review", "developer", "code_review",
                        "tester", "test_review", "retrospective"]

    def test_expected_flow_pl3(self):
        """V6.1: PL3 expected flow includes all 8 roles."""
        import scheduler_legacy as sched
        flow = sched._get_expected_flow("PL3")
        assert flow == ["architect", "architect_review", "developer", "code_review",
                        "tester", "test_review", "auditor", "retrospective"]


class TestAssertAuditCompleted:
    """Tests for _assert_audit_completed pre-condition assertion (D34)."""

    def test_pl0_no_audit_required(self):
        """PL0: no audit required, should not raise."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P2", "workgroup": {"template": "quick"},
                "orchestration": {"history": []}}
        sched._assert_audit_completed(task)  # should not raise

    def test_pl1_no_audit_required(self):
        """PL1: no audit required, should not raise."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P2", "workgroup": {"template": "standard-dev"},
                "title": "文档整理", "description": "整理项目文档",
                "orchestration": {"history": []}}
        assert sched.determine_process_level(task) == "PL1"
        sched._assert_audit_completed(task)  # should not raise

    def test_pl2_retrospective_passed(self):
        """PL2: retrospective passed → should not raise."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "orchestration": {"history": [
                    {"role": "retrospective", "verdict": "pass"},
                ]}}
        sched._assert_audit_completed(task)  # should not raise

    def test_pl2_no_retrospective_raises(self):
        """PL2: no retrospective → should raise ValueError."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "orchestration": {"history": [
                    {"role": "tester", "verdict": "pass"},
                ]}}
        with pytest.raises(ValueError, match="retrospective"):
            sched._assert_audit_completed(task)

    def test_pl3_auditor_passed(self):
        """PL3: auditor passed → should not raise."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P0", "workgroup": {"template": "standard-dev"},
                "orchestration": {"history": [
                    {"role": "auditor", "verdict": "pass"},
                ]}}
        sched._assert_audit_completed(task)  # should not raise

    def test_pl3_no_auditor_raises(self):
        """PL3: no auditor → should raise ValueError."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P0", "workgroup": {"template": "standard-dev"},
                "orchestration": {"history": [
                    {"role": "retrospective", "verdict": "pass"},
                ]}}
        with pytest.raises(ValueError, match="auditor"):
            sched._assert_audit_completed(task)


class TestV61PromptGeneration:
    """Tests for V6.1 prompt generation (code_review, test_review guidance)."""

    def test_code_review_guidance_in_prompt(self):
        """code_review role should have specific guidance in prompt."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "title": "Test task", "description": "Test"}
        prompt = sched.generate_worker_prompt_v2(task, "code_review")
        assert "Code Review" in prompt
        assert "架构一致性" in prompt
        assert "测试覆盖" in prompt

    def test_test_review_guidance_in_prompt(self):
        """test_review role should have specific guidance in prompt."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "title": "Test task", "description": "Test"}
        prompt = sched.generate_worker_prompt_v2(task, "test_review")
        assert "Test Review" in prompt
        assert "测试真实性" in prompt
        assert "测试盲区" in prompt

    def test_code_review_emoji(self):
        """code_review should have 🔎 emoji in spawn instruction."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "title": "Test task", "description": "Test"}
        instruction = sched.generate_spawn_instruction_v2(task, "code_review")
        assert "🔎" in instruction["title"]

    def test_test_review_emoji(self):
        """test_review should have 📝 emoji in spawn instruction."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P1", "workgroup": {"template": "standard-dev"},
                "title": "Test task", "description": "Test"}
        instruction = sched.generate_spawn_instruction_v2(task, "test_review")
        assert "📝" in instruction["title"]

    def test_auditor_guidance_has_session_jsonl(self):
        """Auditor guidance should mention dispatcher session log when session ID is available."""
        import scheduler_legacy as sched
        task = {"id": "T-001", "priority": "P0", "workgroup": {"template": "standard-dev"},
                "title": "Test", "description": "Test",
                "orchestration": {"dispatcher_session_id": "test-session-123", "history": []}}
        prompt = sched.generate_worker_prompt_v2(task, "auditor")
        assert "test-session-123" in prompt
        assert "jsonl" in prompt.lower()
