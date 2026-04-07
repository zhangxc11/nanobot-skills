#!/usr/bin/env python3
"""
test_trigger_scheduler.py - Tests for trigger_scheduler.py and scheduler.py (v2) dispatcher mechanism.

Tests cover:
  - Priority sorting (P0 > P1 > P2, same priority by creation time)
  - Dependency checking (blocked_by resolution)
  - Dispatcher state management (load/save/increment)
  - Session alive check
  - Trigger logic (wake-up vs create new)
  - Generation handoff (iteration cap)
  - Dispatcher status
  - Iteration limit check
  - Message count handoff
  - Generation field
  - Concurrency protection (should_skip_wakeup)
  - No lock functions
  - transition_task (brain_manager)
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
# Fixture: isolated TASK_DATA_DIR
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
    monkeypatch.setenv("TASK_DATA_DIR", str(brain_dir))

    # Patch brain_manager module-level paths
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import task_store as bm
    monkeypatch.setattr(bm, "TASK_DATA_DIR", brain_dir)
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
    monkeypatch.setattr(ts, "TASK_DATA_DIR", brain_dir)

    yield brain_dir


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _create_task(task_id: str, status: str = "queued", priority: str = "P1",
                 template: str = "standard-dev", title: str = "",
                 created: str = "", blocked_by: list = None,
                 description: str = "") -> dict:
    """Create and save a task, return the task dict."""
    import task_store as bm

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
# Test: transition_task Function (brain_manager)
# ══════════════════════════════════════════

class TestTransitionTask:
    def test_valid_transition(self):
        import task_store as bm
        _create_task("T-20260330-001", status="queued")
        updated = bm.transition_task("T-20260330-001", "executing")
        assert updated["status"] == "executing"
        # Verify persisted
        reloaded = bm.load_task("T-20260330-001")
        assert reloaded["status"] == "executing"

    def test_invalid_transition_raises(self):
        import task_store as bm
        _create_task("T-20260330-001", status="done")
        with pytest.raises(ValueError, match="Invalid transition"):
            bm.transition_task("T-20260330-001", "executing")

    def test_decision_logged(self):
        import task_store as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="test")
        decisions = bm.list_decisions(limit=10)
        assert any(
            d.get("type") == "status_change" and d.get("task_id") == "T-20260330-001"
            for d in decisions
        )

    def test_history_entry_added(self):
        import task_store as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="scheduler dispatch")
        task = bm.load_task("T-20260330-001")
        last_entry = task["history"][-1]
        assert last_entry["action"] == "status_change"
        assert "queued → executing" in last_entry["detail"]
        assert "scheduler dispatch" in last_entry["detail"]

    def test_note_appended_to_context(self):
        import task_store as bm
        _create_task("T-20260330-001", status="queued")
        bm.transition_task("T-20260330-001", "executing", note="my note")
        task = bm.load_task("T-20260330-001")
        assert "my note" in task["context"]["notes"]

    def test_nonexistent_task_raises(self):
        import task_store as bm
        with pytest.raises(FileNotFoundError):
            bm.transition_task("T-NONEXISTENT", "executing")


# ══════════════════════════════════════════
# Test: generate_report_path Function
# ══════════════════════════════════════════

class TestGenerateReportPath:
    """Tests for scheduler.generate_report_path()."""

    def test_basic_path_format(self, tmp_path, monkeypatch):
        """Path contains correct task_id, role, round and ends with .json."""
        import scheduler
        monkeypatch.setattr(scheduler, "REPORTS_DIR", tmp_path / "reports")

        path = scheduler.generate_report_path("T-test-001", "tester", 1)
        assert path.startswith("/"), "Should return absolute path"
        assert path.endswith(".json"), "Should end with .json"
        assert "/T-test-001-tester-R1-" in path

    def test_round_parameter(self, tmp_path, monkeypatch):
        """Round parameter correctly appears as -R{n}- in path."""
        import scheduler
        monkeypatch.setattr(scheduler, "REPORTS_DIR", tmp_path / "reports")

        path = scheduler.generate_report_path("T-test-002", "architect", 3)
        assert "-R3-" in path
        assert "architect" in path

    def test_default_round(self, tmp_path, monkeypatch):
        """Default round is 1 when not specified."""
        import scheduler
        monkeypatch.setattr(scheduler, "REPORTS_DIR", tmp_path / "reports")

        path = scheduler.generate_report_path("T-test-003", "developer")
        assert "-R1-" in path

    def test_glob_pattern_compatible(self, tmp_path, monkeypatch):
        """Generated filename matches _parse_latest_report glob pattern."""
        import re
        import scheduler
        monkeypatch.setattr(scheduler, "REPORTS_DIR", tmp_path / "reports")

        path = scheduler.generate_report_path("T-test-001", "tester", 1)
        filename = Path(path).name
        # _parse_latest_report uses glob: {task_id}-{role}-*.json
        assert re.match(r"T-test-001-tester-R\d+-\d{14}\.json", filename), \
            f"Filename does not match expected pattern: {filename}"

    def test_uses_reports_dir(self, tmp_path, monkeypatch):
        """Path uses the REPORTS_DIR constant."""
        import scheduler
        custom_dir = tmp_path / "custom" / "reports"
        monkeypatch.setattr(scheduler, "REPORTS_DIR", custom_dir)

        path = scheduler.generate_report_path("T-test-004", "auditor")
        assert str(custom_dir) in path, \
            f"Path should use REPORTS_DIR ({custom_dir}), got: {path}"
