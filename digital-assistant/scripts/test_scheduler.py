#!/usr/bin/env python3
"""
test_scheduler.py - Tests for scheduler.py and trigger_scheduler.py dispatcher mechanism.

Tests cover:
  - Priority sorting (P0 > P1 > P2, same priority by creation time)
  - Dependency checking (blocked_by resolution)
  - Concurrency control (MAX_CONCURRENT_EXECUTING + MAX_DISPATCH_PER_RUN)
  - Review level integration (determine_review_level used in worker prompt)
  - Quick task bypass (quick template skipped by scheduler)
  - Task category detection and verification guidance
  - Dispatcher state management (load/save/increment)
  - Session alive check
  - Trigger logic (wake-up vs create new)
  - Generation handoff (iteration cap)
  - Scheduler run (dry-run and live)
  - Status reporting
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
# Test: Priority Sorting
# ══════════════════════════════════════════

class TestPrioritySorting:
    def test_p0_before_p1_before_p2(self):
        import scheduler as sch
        tasks = [
            {"id": "T-1", "priority": "P2", "created": "2026-03-30T10:00:00"},
            {"id": "T-2", "priority": "P0", "created": "2026-03-30T11:00:00"},
            {"id": "T-3", "priority": "P1", "created": "2026-03-30T09:00:00"},
        ]
        sorted_tasks = sch.sort_by_priority(tasks)
        assert [t["id"] for t in sorted_tasks] == ["T-2", "T-3", "T-1"]

    def test_same_priority_by_creation_time(self):
        import scheduler as sch
        tasks = [
            {"id": "T-B", "priority": "P1", "created": "2026-03-30T12:00:00"},
            {"id": "T-A", "priority": "P1", "created": "2026-03-30T10:00:00"},
            {"id": "T-C", "priority": "P1", "created": "2026-03-30T11:00:00"},
        ]
        sorted_tasks = sch.sort_by_priority(tasks)
        assert [t["id"] for t in sorted_tasks] == ["T-A", "T-C", "T-B"]

    def test_missing_priority_defaults_to_p2(self):
        import scheduler as sch
        tasks = [
            {"id": "T-1", "priority": "P1", "created": "2026-03-30T10:00:00"},
            {"id": "T-2", "created": "2026-03-30T09:00:00"},  # no priority
        ]
        sorted_tasks = sch.sort_by_priority(tasks)
        assert sorted_tasks[0]["id"] == "T-1"  # P1 before default P2

    def test_empty_list(self):
        import scheduler as sch
        assert sch.sort_by_priority([]) == []


# ══════════════════════════════════════════
# Test: Dependency Checking
# ══════════════════════════════════════════

class TestDependencyCheck:
    def test_no_dependencies_is_ready(self):
        import scheduler as sch
        task = {"id": "T-1", "status": "queued"}
        assert sch.check_dependency(task, {}) is True

    def test_empty_blocked_by_is_ready(self):
        import scheduler as sch
        task = {"id": "T-1", "status": "queued", "blocked_by": []}
        assert sch.check_dependency(task, {}) is True

    def test_dependency_done_is_ready(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-1"]}
        all_tasks = {"T-1": {"id": "T-1", "status": "done"}}
        assert sch.check_dependency(task, all_tasks) is True

    def test_dependency_cancelled_is_ready(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-1"]}
        all_tasks = {"T-1": {"id": "T-1", "status": "cancelled"}}
        assert sch.check_dependency(task, all_tasks) is True

    def test_dependency_dropped_is_ready(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-1"]}
        all_tasks = {"T-1": {"id": "T-1", "status": "dropped"}}
        assert sch.check_dependency(task, all_tasks) is True

    def test_dependency_executing_blocks(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-1"]}
        all_tasks = {"T-1": {"id": "T-1", "status": "executing"}}
        assert sch.check_dependency(task, all_tasks) is False

    def test_dependency_queued_blocks(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-1"]}
        all_tasks = {"T-1": {"id": "T-1", "status": "queued"}}
        assert sch.check_dependency(task, all_tasks) is False

    def test_dependency_not_found_blocks(self):
        import scheduler as sch
        task = {"id": "T-2", "blocked_by": ["T-nonexistent"]}
        assert sch.check_dependency(task, {}) is False

    def test_multiple_deps_all_done(self):
        import scheduler as sch
        task = {"id": "T-3", "blocked_by": ["T-1", "T-2"]}
        all_tasks = {
            "T-1": {"id": "T-1", "status": "done"},
            "T-2": {"id": "T-2", "status": "done"},
        }
        assert sch.check_dependency(task, all_tasks) is True

    def test_multiple_deps_one_pending(self):
        import scheduler as sch
        task = {"id": "T-3", "blocked_by": ["T-1", "T-2"]}
        all_tasks = {
            "T-1": {"id": "T-1", "status": "done"},
            "T-2": {"id": "T-2", "status": "executing"},
        }
        assert sch.check_dependency(task, all_tasks) is False


# ══════════════════════════════════════════
# Test: Concurrency Control
# ══════════════════════════════════════════

class TestConcurrencyControl:
    def test_no_executing_full_slots(self):
        import scheduler as sch
        assert sch.determine_available_slots() == sch.MAX_CONCURRENT_EXECUTING

    def test_some_executing_reduces_slots(self):
        import scheduler as sch
        _create_task("T-20260330-001", status="executing")
        _create_task("T-20260330-002", status="executing")
        assert sch.determine_available_slots() == sch.MAX_CONCURRENT_EXECUTING - 2

    def test_max_executing_zero_slots(self):
        import scheduler as sch
        for i in range(sch.MAX_CONCURRENT_EXECUTING):
            _create_task(f"T-20260330-{i+1:03d}", status="executing")
        assert sch.determine_available_slots() == 0

    def test_per_run_cap_limits_dispatch(self):
        """Even with many slots, per-run cap limits dispatch count."""
        import scheduler as sch
        # Create more queued tasks than MAX_DISPATCH_PER_RUN
        for i in range(5):
            _create_task(f"T-20260330-{i+1:03d}", status="queued", priority="P1")

        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        dispatched = result["spawn_instructions"]
        assert len(dispatched) <= sch.MAX_DISPATCH_PER_RUN

    def test_global_limit_overrides_per_run(self):
        """If only 1 global slot, dispatch at most 1 even if per-run cap is 3."""
        import scheduler as sch
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
        import scheduler as sch
        _create_task("T-20260330-001", status="queued", template="quick")
        _create_task("T-20260330-002", status="queued", template="standard-dev")

        result = sch.run_scheduler(dry_run=True)
        dispatched_ids = [d["task_id"] for d in result["spawn_instructions"]]
        assert "T-20260330-001" not in dispatched_ids
        assert "T-20260330-002" in dispatched_ids

    def test_is_quick_task_detection(self):
        import scheduler as sch
        assert sch.is_quick_task({"workgroup": {"template": "quick"}}) is True
        assert sch.is_quick_task({"workgroup": {"template": "standard-dev"}}) is False
        assert sch.is_quick_task({"template": "quick"}) is True
        assert sch.is_quick_task({}) is False


# ══════════════════════════════════════════
# Test: Task Category Detection
# ══════════════════════════════════════════

class TestTaskCategoryDetection:
    def test_web_frontend(self):
        import scheduler as sch
        task = {"title": "修复 Dashboard 页面 UI 问题", "description": ""}
        assert sch.detect_task_category(task) == "web_frontend"

    def test_feishu_integration(self):
        import scheduler as sch
        task = {"title": "飞书消息卡片优化", "description": ""}
        assert sch.detect_task_category(task) == "feishu_integration"

    def test_api_interface(self):
        import scheduler as sch
        task = {"title": "新增 REST API 接口", "description": ""}
        assert sch.detect_task_category(task) == "api_interface"

    def test_data_processing(self):
        import scheduler as sch
        task = {"title": "数据清洗 pipeline", "description": ""}
        assert sch.detect_task_category(task) == "data_processing"

    def test_default_backend(self):
        import scheduler as sch
        task = {"title": "优化缓存策略", "description": ""}
        assert sch.detect_task_category(task) == "backend_script"

    def test_description_also_checked(self):
        import scheduler as sch
        task = {"title": "新功能", "description": "需要修改前端 html 页面"}
        assert sch.detect_task_category(task) == "web_frontend"


# ══════════════════════════════════════════
# Test: Review Level Integration
# ══════════════════════════════════════════

class TestReviewLevelIntegration:
    def test_worker_prompt_includes_review_level(self, monkeypatch):
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "Review 级别" in prompt

    def test_quick_task_l0(self, monkeypatch):
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="quick")
        prompt = sch.generate_worker_prompt(task)
        assert "L0" in prompt

    def test_p0_task_l3(self, monkeypatch):
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P0")
        prompt = sch.generate_worker_prompt(task)
        assert "L3" in prompt

    def test_verification_guidance_in_prompt(self, monkeypatch):
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="修复 Dashboard 页面", description="前端 bug")
        prompt = sch.generate_worker_prompt(task)
        assert "浏览器" in prompt
        assert "截图" in prompt

    def test_backend_verification_in_prompt(self, monkeypatch):
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 0

    def test_dependency_blocks_dispatch(self):
        import scheduler as sch
        _create_task("T-20260330-001", status="executing")
        _create_task("T-20260330-002", status="queued", blocked_by=["T-20260330-001"])

        result = sch.run_scheduler(dry_run=True)
        assert len(result["spawn_instructions"]) == 0
        assert result["report"]["summary"]["skipped_dependency"] == 1

    def test_priority_ordering_in_dispatch(self):
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
        result = sch.get_status()
        assert result["ok"]
        assert result["data"]["available_slots"] == sch.MAX_CONCURRENT_EXECUTING

    def test_status_with_tasks(self):
        import scheduler as sch
        _create_task("T-20260330-001", status="queued", priority="P0")
        _create_task("T-20260330-002", status="executing")

        result = sch.get_status()
        data = result["data"]
        assert data["task_counts"]["queued"] == 1
        assert data["task_counts"]["executing"] == 1
        assert len(data["queued_tasks"]) == 1
        assert len(data["executing_tasks"]) == 1


# ══════════════════════════════════════════
# Test: Dispatcher State Management
# ══════════════════════════════════════════

class TestDispatcherState:
    def test_load_dispatcher_no_file(self):
        import trigger_scheduler as ts
        assert ts.load_dispatcher() is None

    def test_save_and_load_dispatcher(self, isolated_brain):
        import trigger_scheduler as ts
        data = {
            "session_id": "webchat_dispatch_123_456",
            "session_key": "webchat:dispatch_123_456",
            "created_at": "2026-03-31T00:00:00+08:00",
            "iteration_count": 5,
            "last_triggered_at": "2026-03-31T00:25:00+08:00",
        }
        ts.save_dispatcher(data)

        loaded = ts.load_dispatcher()
        assert loaded is not None
        assert loaded["session_id"] == "webchat_dispatch_123_456"
        assert loaded["session_key"] == "webchat:dispatch_123_456"
        assert loaded["iteration_count"] == 5

    def test_load_dispatcher_corrupted(self, isolated_brain):
        import trigger_scheduler as ts
        ts.DISPATCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts.DISPATCHER_FILE.write_text("not json at all")
        assert ts.load_dispatcher() is None

    def test_load_dispatcher_missing_fields(self, isolated_brain):
        import trigger_scheduler as ts
        ts.DISPATCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts.DISPATCHER_FILE.write_text(json.dumps({"session_id": "", "session_key": ""}))
        assert ts.load_dispatcher() is None

    def test_increment_iteration(self, isolated_brain):
        import trigger_scheduler as ts
        data = _write_dispatcher(isolated_brain, iteration_count=10)
        updated = ts.increment_iteration(data)
        assert updated["iteration_count"] == 11
        assert "last_triggered_at" in updated

        # Verify persisted
        reloaded = ts.load_dispatcher()
        assert reloaded["iteration_count"] == 11

    def test_save_dispatcher_atomic(self, isolated_brain):
        """Save should be atomic (write to tmp then rename)."""
        import trigger_scheduler as ts
        data = {
            "session_id": "webchat_dispatch_test",
            "session_key": "webchat:dispatch_test",
            "created_at": ts._now_iso(),
            "iteration_count": 0,
        }
        ts.save_dispatcher(data)

        # tmp file should not exist after save
        tmp_file = ts.DISPATCHER_FILE.with_suffix(".tmp")
        assert not tmp_file.exists()
        assert ts.DISPATCHER_FILE.exists()

    def test_save_dispatcher_creates_parent_dirs(self, tmp_path):
        import trigger_scheduler as ts
        nested_file = tmp_path / "deep" / "nested" / "dispatcher.json"
        original = ts.DISPATCHER_FILE
        ts.DISPATCHER_FILE = nested_file
        try:
            data = {
                "session_id": "test",
                "session_key": "webchat:test",
                "created_at": ts._now_iso(),
                "iteration_count": 0,
            }
            ts.save_dispatcher(data)
            assert nested_file.exists()
            loaded = json.loads(nested_file.read_text())
            assert loaded["session_id"] == "test"
        finally:
            ts.DISPATCHER_FILE = original


# ══════════════════════════════════════════
# Test: Session Alive Check
# ══════════════════════════════════════════

class TestSessionAliveCheck:
    def test_session_alive_when_recent(self):
        """Mock the API to return a recently active session."""
        import trigger_scheduler as ts

        recent_time = datetime.now().astimezone().isoformat()
        mock_sessions = [
            {
                "id": "webchat_dispatch_123_456",
                "lastActiveAt": recent_time,
                "messageCount": 10,
            }
        ]

        mock_response = mock.MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"sessions": mock_sessions}).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("trigger_scheduler.urlopen", return_value=mock_response):
            result = ts.check_session_alive("webchat_dispatch_123_456")
            assert result["alive"] is True
            assert result["exists"] is True
            assert result["stale"] is False

    def test_session_stale_when_old(self):
        """Session with old lastActiveAt should be considered stale."""
        import trigger_scheduler as ts

        old_time = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        mock_sessions = [
            {
                "id": "webchat_dispatch_123_456",
                "lastActiveAt": old_time,
                "messageCount": 50,
            }
        ]

        mock_response = mock.MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"sessions": mock_sessions}).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("trigger_scheduler.urlopen", return_value=mock_response):
            result = ts.check_session_alive("webchat_dispatch_123_456")
            assert result["alive"] is False
            assert result["exists"] is True
            assert result["stale"] is True

    def test_session_not_found(self):
        """Session not in API response should be considered dead."""
        import trigger_scheduler as ts

        mock_response = mock.MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"sessions": []}).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("trigger_scheduler.urlopen", return_value=mock_response):
            result = ts.check_session_alive("webchat_dispatch_nonexistent")
            assert result["alive"] is False
            assert result["exists"] is False

    def test_session_check_api_error(self):
        """API error should return not alive."""
        import trigger_scheduler as ts

        with mock.patch("trigger_scheduler.urlopen", side_effect=Exception("connection refused")):
            result = ts.check_session_alive("webchat_dispatch_123_456")
            assert result["alive"] is False
            assert "error" in result


# ══════════════════════════════════════════
# Test: Prompt Generation
# ══════════════════════════════════════════

class TestPromptGeneration:
    def test_wake_up_prompt_has_decision_tree(self):
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=True)
        assert "⏰ 调度器唤醒" in prompt
        assert "scheduler.py run" in prompt
        # Should contain proactive handling guidance
        assert "迭代用满" in prompt or "reached the maximum" in prompt
        assert "follow_up" in prompt
        assert "handle-completion" in prompt
        assert "项目经理" in prompt

    def test_full_prompt_has_all_sections(self):
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt
        assert "Step 4" in prompt
        assert "固定 session 模式" in prompt
        assert "后续唤醒" in prompt
        # New: proactive project manager mindset
        assert "项目经理" in prompt
        assert "决策树" in prompt

    def test_full_prompt_has_iteration_exhaustion_handling(self):
        """Full prompt should have guidance for iteration-exhausted workers."""
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        assert "迭代用满" in prompt or "reached the maximum" in prompt
        assert "follow_up" in prompt
        # Should mention max follow_up limit
        assert str(ts.MAX_FOLLOW_UP_ON_EXHAUSTION) in prompt

    def test_full_prompt_has_no_report_handling(self):
        """Full prompt should handle case when worker has no report."""
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        assert "no worker report" in prompt or "无报告" in prompt
        assert "补写报告" in prompt or "补报告" in prompt

    def test_full_prompt_has_partial_handling(self):
        """Full prompt should handle partial verdicts proactively."""
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        assert "partial" in prompt

    def test_full_prompt_discourages_blind_blocked(self):
        """Full prompt should discourage blindly accepting mark_blocked."""
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        assert "不要无脑接受" in prompt or "不要直接接受" in prompt

    def test_dry_run_flag_in_prompt(self):
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(dry_run=True, is_wake_up=True)
        assert "dry-run" in prompt

    def test_parent_in_prompt(self):
        import trigger_scheduler as ts
        prompt = ts.build_scheduler_prompt(parent_session_id="feishu.ST.123", is_wake_up=True)
        assert "feishu.ST.123" in prompt

    def test_no_lock_references_in_prompt(self):
        """The new prompt should not reference lock acquire/release."""
        import trigger_scheduler as ts
        full_prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=False)
        wake_prompt = ts.build_scheduler_prompt(dry_run=False, is_wake_up=True)
        for prompt in [full_prompt, wake_prompt]:
            assert "release_lock" not in prompt
            assert "acquire_lock" not in prompt
            assert "scheduler.lock" not in prompt

    def test_max_follow_up_constant_exists(self):
        """MAX_FOLLOW_UP_ON_EXHAUSTION constant should be defined."""
        import trigger_scheduler as ts
        assert hasattr(ts, "MAX_FOLLOW_UP_ON_EXHAUSTION")
        assert isinstance(ts.MAX_FOLLOW_UP_ON_EXHAUSTION, int)
        assert ts.MAX_FOLLOW_UP_ON_EXHAUSTION >= 1


# ══════════════════════════════════════════
# Test: Trigger Logic
# ══════════════════════════════════════════

class TestTriggerLogic:
    def test_trigger_no_dispatcher_creates_new(self, isolated_brain):
        """When no dispatcher.json exists, should create new session."""
        import trigger_scheduler as ts

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_create.return_value = {
                "ok": True,
                "action": "created_new",
                "session_key": "webchat:dispatch_test_123",
                "session_id": "webchat_dispatch_test_123",
            }
            result = ts.trigger_scheduler()
            assert result["ok"]
            mock_create.assert_called_once()

    def test_trigger_alive_session_sends_wake_up(self, isolated_brain):
        """When dispatcher exists and session is alive, should send wake-up."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=5)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "send_to_session_async") as mock_send:
            mock_alive.return_value = {"alive": True, "exists": True, "stale": False}
            mock_send.return_value = {"ok": True, "started": True, "pid": 12345}

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result["action"] == "wake_up"
            assert result["iteration_count"] == 6  # incremented
            mock_send.assert_called_once()

    def test_trigger_stale_session_creates_new(self, isolated_brain):
        """When dispatcher session is stale, should create new one."""
        import trigger_scheduler as ts

        old_dispatcher = _write_dispatcher(isolated_brain, session_id="webchat_dispatch_old",
                                           iteration_count=10)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_alive.return_value = {"alive": False, "exists": True, "stale": True}
            mock_create.return_value = {
                "ok": True,
                "action": "created_new",
                "session_key": "webchat:dispatch_new_789",
                "session_id": "webchat_dispatch_new_789",
            }

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result.get("previous_session_id") == "webchat_dispatch_old"
            assert result.get("previous_reason") == "stale_or_dead"
            mock_create.assert_called_once()

    def test_trigger_webchat_down(self):
        """When web-chat is down, should return error."""
        import trigger_scheduler as ts

        with mock.patch.object(ts, "check_webchat_health", return_value=False):
            result = ts.trigger_scheduler()
            assert result["ok"] is False
            assert "not running" in result["error"]

    def test_trigger_iteration_cap_creates_successor(self, isolated_brain):
        """When iteration count exceeds MAX_ITERATIONS, should create successor."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=ts.MAX_ITERATIONS)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_create.return_value = {
                "ok": True,
                "action": "created_new",
                "session_key": "webchat:dispatch_new_999",
                "session_id": "webchat_dispatch_new_999",
            }

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result["action"] == "generation_handoff"
            assert result["previous_iterations"] == ts.MAX_ITERATIONS
            mock_create.assert_called_once()

    def test_trigger_send_failure_creates_new(self, isolated_brain):
        """When send to existing session fails, should create new one."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=5)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "send_to_session_async") as mock_send, \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_alive.return_value = {"alive": True, "exists": True, "stale": False}
            mock_send.return_value = {"ok": False, "error": "connection refused"}
            mock_create.return_value = {
                "ok": True,
                "action": "created_new",
                "session_key": "webchat:dispatch_fallback",
                "session_id": "webchat_dispatch_fallback",
            }

            result = ts.trigger_scheduler()
            assert result["ok"]
            mock_create.assert_called_once()


# ══════════════════════════════════════════
# Test: Generation Handoff
# ══════════════════════════════════════════

class TestGenerationHandoff:
    def test_handoff_at_exact_cap(self, isolated_brain):
        """Handoff should trigger at exactly MAX_ITERATIONS."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=ts.MAX_ITERATIONS)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_create.return_value = {"ok": True, "action": "created_new",
                                        "session_key": "new", "session_id": "new"}
            result = ts.trigger_scheduler()
            assert result["action"] == "generation_handoff"

    def test_no_handoff_below_cap(self, isolated_brain):
        """No handoff when below MAX_ITERATIONS."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=ts.MAX_ITERATIONS - 1)

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "send_to_session_async") as mock_send:
            mock_alive.return_value = {"alive": True, "exists": True, "stale": False}
            mock_send.return_value = {"ok": True, "started": True, "pid": 123}

            result = ts.trigger_scheduler()
            assert result["action"] == "wake_up"

    def test_handoff_updates_dispatcher(self, isolated_brain):
        """After handoff, dispatcher.json should have new session info."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, session_id="old_session",
                          iteration_count=ts.MAX_ITERATIONS)

        new_dispatcher = {
            "session_id": "webchat_dispatch_new",
            "session_key": "webchat:dispatch_new",
            "created_at": ts._now_iso(),
            "iteration_count": 1,
            "last_triggered_at": ts._now_iso(),
        }

        def fake_create(**kwargs):
            ts.save_dispatcher(new_dispatcher)
            return {"ok": True, "action": "created_new",
                    "session_key": "webchat:dispatch_new",
                    "session_id": "webchat_dispatch_new",
                    "dispatcher": new_dispatcher}

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "create_dispatcher_session", side_effect=fake_create):
            ts.trigger_scheduler()

        reloaded = ts.load_dispatcher()
        assert reloaded["session_id"] == "webchat_dispatch_new"
        assert reloaded["iteration_count"] == 1


# ══════════════════════════════════════════
# Test: Dispatcher Status
# ══════════════════════════════════════════

class TestDispatcherStatus:
    def test_status_no_dispatcher(self):
        import trigger_scheduler as ts
        result = ts.get_dispatcher_status()
        assert result["ok"]
        assert result["status"] == "no_dispatcher"
        assert result["dispatcher"] is None

    def test_status_active_dispatcher(self, isolated_brain):
        import trigger_scheduler as ts
        _write_dispatcher(isolated_brain, iteration_count=42)

        with mock.patch.object(ts, "check_session_alive") as mock_alive:
            mock_alive.return_value = {"alive": True, "exists": True, "stale": False}
            result = ts.get_dispatcher_status()
            assert result["ok"]
            assert result["status"] == "active"
            assert result["dispatcher"]["iteration_count"] == 42
            assert result["iterations_remaining"] == ts.MAX_ITERATIONS - 42

    def test_status_stale_dispatcher(self, isolated_brain):
        import trigger_scheduler as ts
        _write_dispatcher(isolated_brain, iteration_count=100)

        with mock.patch.object(ts, "check_session_alive") as mock_alive:
            mock_alive.return_value = {"alive": False, "exists": True, "stale": True}
            result = ts.get_dispatcher_status()
            assert result["ok"]
            assert result["status"] == "stale"


# ══════════════════════════════════════════
# Test: Iteration Limit (Dual Detection)
# ══════════════════════════════════════════

class TestCheckIterationLimit:
    def test_below_both_thresholds(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": 100}
        status = {"message_count": 500}
        assert ts.check_iteration_limit(dispatcher, status) is False

    def test_trigger_count_at_cap(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": ts.MAX_ITERATIONS}
        status = {"message_count": 100}
        assert ts.check_iteration_limit(dispatcher, status) is True

    def test_trigger_count_over_cap(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": ts.MAX_ITERATIONS + 10}
        assert ts.check_iteration_limit(dispatcher) is True

    def test_message_count_at_cap(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": 100}
        status = {"message_count": ts.MESSAGE_COUNT_CAP}
        assert ts.check_iteration_limit(dispatcher, status) is True

    def test_message_count_over_cap(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": 100}
        status = {"message_count": ts.MESSAGE_COUNT_CAP + 500}
        assert ts.check_iteration_limit(dispatcher, status) is True

    def test_no_status_only_checks_trigger_count(self):
        """Without session_status, only trigger_count is checked."""
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": 100}
        assert ts.check_iteration_limit(dispatcher, session_status=None) is False

    def test_no_status_trigger_at_cap(self):
        import trigger_scheduler as ts
        dispatcher = {"iteration_count": ts.MAX_ITERATIONS}
        assert ts.check_iteration_limit(dispatcher, session_status=None) is True

    def test_missing_iteration_count_defaults_zero(self):
        import trigger_scheduler as ts
        dispatcher = {}
        status = {"message_count": 100}
        assert ts.check_iteration_limit(dispatcher, status) is False


class TestMessageCountHandoff:
    def test_message_count_triggers_handoff(self, isolated_brain):
        """When message_count exceeds cap but trigger_count is low, should still handoff."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=50,
                          session_id="webchat_dispatch_old")
        recent_time = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "create_dispatcher_session") as mock_create:
            mock_alive.return_value = {
                "alive": True, "exists": True, "stale": False,
                "last_active": recent_time,
                "message_count": ts.MESSAGE_COUNT_CAP + 100,
            }
            mock_create.return_value = {
                "ok": True, "action": "created_new",
                "session_key": "webchat:dispatch_new",
                "session_id": "webchat_dispatch_new",
            }

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result["action"] == "generation_handoff"
            assert result["handoff_reason"] == "message_count_cap"
            mock_create.assert_called_once()


class TestGenerationField:
    def test_first_session_generation_1(self, isolated_brain):
        """First dispatcher session should have generation=1."""
        import trigger_scheduler as ts

        with mock.patch("trigger_scheduler.send_to_session_async") as mock_send:
            mock_send.return_value = {"ok": True, "started": True, "pid": 12345}

            result = ts.create_dispatcher_session()
            assert result["ok"]

            dispatcher = ts.load_dispatcher()
            assert dispatcher["generation"] == 1
            assert dispatcher["previous_session_id"] == ""
            assert dispatcher["version"] == 3

    def test_successor_increments_generation(self, isolated_brain):
        """Successor session should increment generation."""
        import trigger_scheduler as ts

        # Write initial dispatcher with generation=3
        _write_dispatcher(isolated_brain, generation=3,
                          session_id="webchat_dispatch_old")

        with mock.patch("trigger_scheduler.send_to_session_async") as mock_send:
            mock_send.return_value = {"ok": True, "started": True, "pid": 12345}

            result = ts.create_dispatcher_session()
            assert result["ok"]

            dispatcher = ts.load_dispatcher()
            assert dispatcher["generation"] == 4
            assert dispatcher["previous_session_id"] == "webchat_dispatch_old"

    def test_session_key_has_random_suffix(self, isolated_brain):
        """Session key should include a random suffix for uniqueness."""
        import trigger_scheduler as ts

        with mock.patch("trigger_scheduler.send_to_session_async") as mock_send:
            mock_send.return_value = {"ok": True, "started": True, "pid": 12345}

            result = ts.create_dispatcher_session()
            assert result["ok"]
            # Key format: webchat:dispatch_{chat_id}_{ts}_{rand}
            parts = result["session_key"].split("_")
            assert len(parts) >= 4  # at least: webchat:dispatch, chat_id, ts, rand


# ══════════════════════════════════════════
# Test: Concurrency Protection (should_skip_wakeup)
# ══════════════════════════════════════════

class TestShouldSkipWakeup:
    def test_skip_when_recently_active(self):
        """Session active 1 minute ago should be skipped."""
        import trigger_scheduler as ts
        recent_time = (datetime.now().astimezone() - timedelta(seconds=60)).isoformat()
        status = {"last_active": recent_time}
        assert ts.should_skip_wakeup(status) is True

    def test_no_skip_when_idle(self):
        """Session active 10 minutes ago should not be skipped."""
        import trigger_scheduler as ts
        old_time = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()
        status = {"last_active": old_time}
        assert ts.should_skip_wakeup(status) is False

    def test_no_skip_when_no_last_active(self):
        """No last_active should not skip."""
        import trigger_scheduler as ts
        assert ts.should_skip_wakeup({}) is False
        assert ts.should_skip_wakeup({"last_active": ""}) is False

    def test_no_skip_when_invalid_timestamp(self):
        """Invalid timestamp should not skip (fail-open)."""
        import trigger_scheduler as ts
        assert ts.should_skip_wakeup({"last_active": "not-a-date"}) is False

    def test_boundary_at_5_minutes(self):
        """Session active exactly at boundary should not be skipped (>= 300s = not busy)."""
        import trigger_scheduler as ts
        boundary_time = (datetime.now().astimezone() - timedelta(seconds=301)).isoformat()
        status = {"last_active": boundary_time}
        assert ts.should_skip_wakeup(status) is False

    def test_skip_at_just_under_boundary(self):
        """Session active just under 5 minutes should be skipped."""
        import trigger_scheduler as ts
        recent_time = (datetime.now().astimezone() - timedelta(seconds=299)).isoformat()
        status = {"last_active": recent_time}
        assert ts.should_skip_wakeup(status) is True


class TestTriggerSkipsBusy:
    def test_trigger_skips_busy_session(self, isolated_brain):
        """When session is alive but busy, should skip wake-up."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=5)
        recent_time = (datetime.now().astimezone() - timedelta(seconds=60)).isoformat()

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive:
            mock_alive.return_value = {
                "alive": True, "exists": True, "stale": False,
                "last_active": recent_time, "message_count": 10,
            }

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result["action"] == "skipped_busy"
            assert "still executing" in result["reason"]

    def test_trigger_wakes_idle_session(self, isolated_brain):
        """When session is alive and idle, should send wake-up."""
        import trigger_scheduler as ts

        _write_dispatcher(isolated_brain, iteration_count=5)
        old_time = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()

        with mock.patch.object(ts, "check_webchat_health", return_value=True), \
             mock.patch.object(ts, "check_session_alive") as mock_alive, \
             mock.patch.object(ts, "send_to_session_async") as mock_send:
            mock_alive.return_value = {
                "alive": True, "exists": True, "stale": False,
                "last_active": old_time, "message_count": 10,
            }
            mock_send.return_value = {"ok": True, "started": True, "pid": 12345}

            result = ts.trigger_scheduler()
            assert result["ok"]
            assert result["action"] == "wake_up"
            mock_send.assert_called_once()


# ══════════════════════════════════════════
# Test: No Lock Functions Exist
# ══════════════════════════════════════════

class TestNoLockFunctions:
    def test_no_acquire_lock(self):
        """trigger_scheduler should not have acquire_lock function."""
        import trigger_scheduler as ts
        assert not hasattr(ts, "acquire_lock")

    def test_no_release_lock(self):
        """trigger_scheduler should not have release_lock function."""
        import trigger_scheduler as ts
        assert not hasattr(ts, "release_lock")

    def test_no_check_lock(self):
        """trigger_scheduler should not have check_lock function."""
        import trigger_scheduler as ts
        assert not hasattr(ts, "check_lock")

    def test_no_lock_file_constant(self):
        """trigger_scheduler should not have LOCK_FILE constant."""
        import trigger_scheduler as ts
        assert not hasattr(ts, "LOCK_FILE")


# ══════════════════════════════════════════
# Test: Completed Tasks / Review Follow-up
# ══════════════════════════════════════════

class TestCompletedTasks:
    def test_no_review_tasks(self):
        import scheduler as sch
        result = sch.check_completed_tasks()
        assert result == []

    def test_review_task_with_pending_review(self):
        import scheduler as sch
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
        import scheduler as sch
        _create_task("T-20260330-001", status="queued", blocked_by=["T-20260330-002"])
        _create_task("T-20260330-002", status="queued", blocked_by=["T-20260330-001"])

        result = sch.run_scheduler(dry_run=True)
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 0
        assert result["report"]["summary"]["skipped_dependency"] == 2

    def test_three_way_cycle(self):
        """A→B→C→A cycle — none should be scheduled."""
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
        import brain_manager as bm

        _create_task("T-20260330-001", status="queued")

        with mock.patch.object(bm, "transition_task",
                               side_effect=ValueError("Invalid transition: done → executing")):
            result = sch.run_scheduler(dry_run=False)

        assert result["report"]["summary"]["errors"] == 1
        assert len(result["spawn_instructions"]) == 0


# ══════════════════════════════════════════
# Test: transition_task Function (brain_manager)
# ══════════════════════════════════════════

class TestTransitionTask:
    def test_valid_transition(self):
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")
        updated = bm.transition_task("T-20260330-001", "executing")
        assert updated["status"] == "executing"
        # Verify persisted
        reloaded = bm.load_task("T-20260330-001")
        assert reloaded["status"] == "executing"

    def test_invalid_transition_raises(self):
        import brain_manager as bm
        _create_task("T-20260330-001", status="done")
        with pytest.raises(ValueError, match="Invalid transition"):
            bm.transition_task("T-20260330-001", "executing")

    def test_decision_logged(self):
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="test")
        decisions = bm.list_decisions(limit=10)
        assert any(
            d.get("type") == "status_change" and d.get("task_id") == "T-20260330-001"
            for d in decisions
        )

    def test_history_entry_added(self):
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="scheduler dispatch")
        task = bm.load_task("T-20260330-001")
        last_entry = task["history"][-1]
        assert last_entry["action"] == "status_change"
        assert "queued → executing" in last_entry["detail"]
        assert "scheduler dispatch" in last_entry["detail"]

    def test_note_appended_to_context(self):
        import brain_manager as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="my note")
        task = bm.load_task("T-20260330-001")
        assert "my note" in task["context"]["notes"]

    def test_nonexistent_task_raises(self):
        import brain_manager as bm
        with pytest.raises(FileNotFoundError):
            bm.transition_task("T-NONEXISTENT", "executing")


# ══════════════════════════════════════════
# Test: Round 1 Fixes — Timeout Recovery (Fix 4)
# ══════════════════════════════════════════

class TestTimeoutRecovery:
    def test_no_stale_tasks(self):
        import scheduler as sch
        assert sch.check_stale_executing_tasks() == []

    def test_recent_executing_not_stale(self):
        import scheduler as sch
        _create_task("T-20260330-001", status="executing")
        stale = sch.check_stale_executing_tasks()
        assert len(stale) == 0

    def test_old_executing_is_stale(self):
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
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
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" not in prompt
        assert "--status review" in prompt

    def test_L0_prompt_has_status_done(self, monkeypatch):
        """L0 task prompt should contain --status done."""
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="quick", priority="P2")
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" in prompt

    def test_L1_prompt_has_status_done(self, monkeypatch):
        """L1 task prompt should contain --status done."""
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        task["files_changed"] = 1
        import brain_manager as bm
        bm.save_task(task)
        prompt = sch.generate_worker_prompt(task)
        assert "--status done" in prompt

    def test_prompt_has_evidence_requirements(self, monkeypatch):
        """All prompts should have evidence requirements."""
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev", priority="P1")
        prompt = sch.generate_worker_prompt(task)
        assert "验收证据要求" in prompt
        assert "文档更新检查" in prompt

    def test_nanobot_task_has_dev_env_guidance(self, monkeypatch):
        """Task involving nanobot code should have dev env guidance."""
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="修复 scheduler 调度逻辑")
        prompt = sch.generate_worker_prompt(task)
        assert "Dev 环境实测要求" in prompt
        assert "9081" in prompt

    def test_pure_data_task_no_dev_env(self, monkeypatch):
        """Non-nanobot task should NOT have dev env guidance."""
        import scheduler as sch
        monkeypatch.setattr(sch, "LEGACY_MODE", True)
        task = _create_task("T-20260330-001", template="standard-dev",
                            title="更新 README 文档")
        prompt = sch.generate_worker_prompt(task)
        assert "Dev 环境实测要求" not in prompt

    def test_needs_dev_test_detection(self):
        import scheduler as sch
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        result = sched.parse_worker_report("T-999", "developer")
        assert result is None

    def test_parse_multiple_reports_takes_newest(self, tmp_path, monkeypatch):
        import scheduler as sched
        import time
        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        # Write older report
        report1 = reports_dir / "T-001-developer-1000000000.json"
        with report1.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "fail",
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

    def test_parse_with_role_filter(self, tmp_path, monkeypatch):
        import scheduler as sched
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

    def test_tester_pass_l2_promotes_review(self):
        import scheduler as sched
        report = {
            "task_id": "T-001",
            "role": "tester",
            "verdict": "pass",
            "summary": "All tests passed",
        }
        task = {
            "id": "T-001",
            "priority": "P1",
            "workgroup": {"template": "standard-dev"},
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "promote_to_review"

    def test_tester_pass_l0_marks_done(self):
        import scheduler as sched
        report = {
            "task_id": "T-001",
            "role": "tester",
            "verdict": "pass",
            "summary": "Tests passed",
        }
        task = {
            "id": "T-001",
            "priority": "P2",
            "workgroup": {"template": "quick"},  # L0 template
            "orchestration": {"iteration": 1, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_done"

    def test_tester_fail_dispatches_developer(self):
        import scheduler as sched
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

    def test_developer_pass_dispatches_tester(self):
        import scheduler as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Implementation complete",
            "files_changed": ["src/main.py", "DEVLOG.md"],
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
        assert decision.params["role"] == "tester"

    def test_developer_pass_quick_marks_done(self):
        import scheduler as sched
        report = {
            "task_id": "T-001",
            "role": "developer",
            "verdict": "pass",
            "summary": "Quick task done",
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
            "orchestration": {"iteration": 5, "history": []},
        }

        decision = sched.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "max iterations" in decision.reason

    def test_no_report_blocks(self):
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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

    def test_developer_consecutive_pass_dispatches_tester(self):
        """T-004 scenario: developer partial → developer pass → should go to tester, NOT blocked."""
        import scheduler as sched
        report = {
            "task_id": "T-004",
            "role": "developer",
            "verdict": "pass",
            "summary": "Implementation complete after partial",
            "files_changed": ["src/main.py", "DEVLOG.md"],
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
        assert decision.params["role"] == "tester"
        assert "blocked" not in decision.reason.lower()

    def test_developer_consecutive_partial_blocks(self):
        """Developer partial × 2 → 3rd partial → should be blocked (prevents infinite loop)."""
        import scheduler as sched
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

    def test_tester_consecutive_pass_promotes_review(self):
        """Tester pass after consecutive tester rounds → should promote to review, NOT blocked."""
        import scheduler as sched
        report = {
            "task_id": "T-006",
            "role": "tester",
            "verdict": "pass",
            "summary": "All tests passed on retry",
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
        # Should promote to review (L2+) or mark_done (L0/L1), NOT blocked
        assert decision.action in ("promote_to_review", "mark_done")

    def test_tester_fail_dispatches_developer_regardless_of_consecutive(self):
        """Tester fail → developer, even after consecutive tester rounds."""
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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
        import scheduler as sched
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

    def test_full_pipeline_developer_pass_to_tester(self, tmp_path, monkeypatch):
        import scheduler as sched
        import brain_manager as bm

        reports_dir = tmp_path / "brain" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)

        task = _create_task("T-001", status="executing", priority="P1")
        # Add design_ref to satisfy doc triplet check
        task["design_ref"] = "D-test-001"
        bm.save_task(task)

        # Write developer pass report (include DEVLOG in files_changed)
        report_file = reports_dir / "T-001-developer-1234567890.json"
        with report_file.open("w") as f:
            json.dump({
                "task_id": "T-001",
                "role": "developer",
                "verdict": "pass",
                "summary": "Implementation complete",
                "files_changed": ["main.py", "DEVLOG.md"],
            }, f)

        result = sched.handle_worker_completion("T-001", "developer")
        assert result["ok"] is True
        assert result["decision"]["action"] == "dispatch_role"
        assert result["role"] == "tester"

    def test_full_pipeline_tester_pass_to_review(self, tmp_path, monkeypatch):
        import scheduler as sched
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
            }, f)

        result = sched.handle_worker_completion("T-001", "tester")
        assert result["ok"] is True
        assert result["decision"]["action"] == "promote_to_review"

        # Verify task in review status
        reloaded = bm.load_task("T-001")
        assert reloaded["status"] == "review"


class TestGenerateWorkerPromptV2:
    """Test generate_worker_prompt_v2 function."""

    def test_no_brain_manager_in_prompt(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "brain_manager" not in prompt.lower()
        assert "status" not in prompt.lower() or "Report Submission" in prompt

    def test_report_template_in_prompt(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "task_id" in prompt
        assert "verdict" in prompt
        assert "pass|fail|blocked|partial" in prompt

    def test_absolute_path_in_prompt(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "/data/brain/reports/T-001-developer-" in prompt

    def test_role_guidance_developer(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "developer")
        assert "Developer" in prompt
        assert "Implement" in prompt or "implement" in prompt

    def test_role_guidance_tester(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prompt = sched.generate_worker_prompt_v2(task, "tester")
        assert "Tester" in prompt
        assert "verify" in prompt.lower() or "test" in prompt.lower()

    def test_prior_context_included(self):
        import scheduler as sched
        task = _create_task("T-001", status="executing")

        prior_context = "## Previous attempt failed\nBug in line 42"
        prompt = sched.generate_worker_prompt_v2(task, "developer", prior_context)
        assert "Previous attempt failed" in prompt


class TestLegacyMode:
    """Test LEGACY_MODE switch."""

    def test_legacy_mode_uses_old_prompt(self, monkeypatch):
        import scheduler as sched
        monkeypatch.setattr(sched, "LEGACY_MODE", True)

        task = _create_task("T-001", status="executing")
        prompt = sched.generate_worker_prompt(task)

        # Legacy prompt has brain_manager commands
        assert "brain_manager" in prompt.lower()

    def test_non_legacy_uses_new_prompt(self, monkeypatch):
        import scheduler as sched
        monkeypatch.setattr(sched, "LEGACY_MODE", False)

        task = _create_task("T-001", status="executing")
        prompt = sched.generate_worker_prompt(task, "developer")

        # New prompt has report template
        assert "verdict" in prompt.lower()
        assert "brain_manager" not in prompt.lower()


class TestHandleCompletionCLI:
    """Test handle-completion CLI command."""

    def test_cli_handle_completion_with_task_id(self, tmp_path, monkeypatch):
        import scheduler as sched

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
        import scheduler as sched
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
        import scheduler as sched
        task = {"id": "T-001", "template": "quick"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "exempt" in reason

    def test_cron_auto_exempt(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "cron-auto"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_has_design_ref(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "design ref" in reason

    def test_has_architect_report(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        (reports_dir / "T-001-architect-123.json").write_text('{"task_id":"T-001"}')
        task = {"id": "T-001", "template": "standard-dev"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "architect report" in reason

    def test_has_architect_in_history(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "standard-dev",
                "orchestration": {"history": [{"role": "architect", "verdict": "pass"}]}}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_emergency_exempt(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "standard-dev", "emergency": True}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "emergency" in reason

    def test_needs_design_false(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "standard-dev", "needs_design": False}
        ok, reason = sched.check_design_gate(task)
        assert ok is True

    def test_no_design_fails(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        task = {"id": "T-001", "template": "standard-dev",
                "orchestration": {"history": []}}
        ok, reason = sched.check_design_gate(task)
        assert ok is False
        assert "no design document" in reason

    def test_feature_flag_disabled(self, monkeypatch):
        import scheduler as sched
        monkeypatch.setattr(sched, "DESIGN_GATE_ENABLED", False)
        task = {"id": "T-001", "template": "standard-dev"}
        ok, reason = sched.check_design_gate(task)
        assert ok is True
        assert "feature flag" in reason


class TestCheckDocTriplet:
    """Test check_doc_triplet() function."""

    def test_quick_exempt(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "quick"}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True
        assert missing == []

    def test_emergency_exempt(self):
        import scheduler as sched
        task = {"id": "T-001", "template": "standard-dev", "emergency": True}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True

    def test_has_devlog_and_design_ref(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["src/main.py", "DEVLOG.md"]}
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is True

    def test_missing_devlog(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["src/main.py"]}
        task = {"id": "T-001", "template": "standard-dev", "design_ref": "D-001"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is False
        assert "DEVLOG.md" in missing

    def test_missing_design(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"files_changed": ["DEVLOG.md"]}
        task = {"id": "T-001", "template": "standard-dev"}
        ok, missing = sched.check_doc_triplet(task, report)
        assert ok is False
        assert any("ARCHITECTURE" in m for m in missing)

    def test_feature_flag_disabled(self, monkeypatch):
        import scheduler as sched
        monkeypatch.setattr(sched, "DOC_TRIPLET_CHECK_ENABLED", False)
        task = {"id": "T-001", "template": "standard-dev"}
        ok, missing = sched.check_doc_triplet(task)
        assert ok is True


class TestDocRetryEscalation:
    """Test doc retry counter and escalation to manual review."""

    def test_developer_pass_missing_docs_sends_back(self, tmp_path, monkeypatch):
        import scheduler as sched
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
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"task_id": "T-001", "role": "developer", "verdict": "pass",
                  "summary": "Done again", "files_changed": ["main.py"]}
        task = {"id": "T-001", "priority": "P1",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 3, "history": [
                    {"role": "developer", "verdict": "pass", "reason": "developer passed but missing docs: ['DEVLOG.md']"},
                    {"role": "developer", "verdict": "pass", "reason": "developer passed but missing docs: ['DEVLOG.md']"},
                ]}}
        decision = sched.make_decision(report, task)
        assert decision.action == "promote_to_review"

    def test_tester_pass_l0_missing_docs_promotes_review(self, tmp_path, monkeypatch):
        import scheduler as sched
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        monkeypatch.setattr(sched, "REPORTS_DIR", reports_dir)
        report = {"task_id": "T-001", "role": "tester", "verdict": "pass",
                  "summary": "Tests passed", "files_changed": ["test.py"]}
        # Use quick-like template that would get L0 but override to standard-dev
        # Actually, use P2 + standard-dev with monkeypatch on determine_review_level
        import brain_manager as bm
        monkeypatch.setattr(bm, "determine_review_level", lambda t: "L0")
        task = {"id": "T-001", "priority": "P2",
                "workgroup": {"template": "standard-dev"},
                "orchestration": {"iteration": 2, "history": []}}
        decision = sched.make_decision(report, task)
        assert decision.action == "promote_to_review"
        assert "docs" in decision.reason.lower()
