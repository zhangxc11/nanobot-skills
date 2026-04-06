#!/usr/bin/env python3
"""
test_callback_mechanism.py - Tests for spawn-based worker dispatch

Tests the spawn subagent flow:
1. scheduler.py generates spawn instructions (task_prompt, no callback commands)
2. trigger_scheduler.py builds dispatcher prompts that use spawn tool
3. Framework auto-sends [Subagent Result Notification] on completion (not tested here)
"""

import json
import os
import sys
from pathlib import Path

import pytest

# Add scripts dir to path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import task_store as bm
import scheduler_legacy as scheduler
from trigger_scheduler import build_scheduler_prompt


# ──────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────

@pytest.fixture
def temp_brain_dir(tmp_path):
    """Create temporary brain directory for testing."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "tasks").mkdir()
    (brain_dir / "reviews").mkdir()

    original_brain_dir = bm.BRAIN_DIR
    original_tasks_dir = bm.TASKS_DIR
    original_reviews_dir = bm.REVIEWS_DIR
    original_briefing = bm.BRIEFING_FILE

    bm.BRAIN_DIR = brain_dir
    bm.TASKS_DIR = brain_dir / "tasks"
    bm.REVIEWS_DIR = brain_dir / "reviews"
    bm.BRIEFING_FILE = brain_dir / "BRIEFING.md"

    yield brain_dir

    bm.BRAIN_DIR = original_brain_dir
    bm.TASKS_DIR = original_tasks_dir
    bm.REVIEWS_DIR = original_reviews_dir
    bm.BRIEFING_FILE = original_briefing


@pytest.fixture
def sample_task(temp_brain_dir):
    """Create a sample task for testing."""
    task_data = {
        "id": "T-20260331-001",
        "title": "Test Task",
        "description": "Test description",
        "status": "queued",
        "priority": "P1",
        "created": bm.now_iso(),
        "workgroup": {"template": "standard-dev"},
        "history": [{"timestamp": bm.now_iso(), "action": "created", "detail": "test task"}],
        "context": {},
    }
    bm.save_task(task_data)
    return task_data


# ──────────────────────────────────────────
# Test scheduler.py — worker prompt (no callback)
# ──────────────────────────────────────────

def test_worker_prompt_has_no_callback_instruction(sample_task):
    """Worker prompt should NOT contain callback/report_completion instructions."""
    prompt = scheduler.generate_worker_prompt(task=sample_task)

    # Must NOT have old callback artifacts
    assert "report_completion.py" not in prompt
    assert "完成后必须回调调度器" not in prompt
    assert "dispatcher-session" not in prompt
    assert "[Worker Completion Notification]" not in prompt

    # Must still have task details
    assert sample_task["id"] in prompt
    assert sample_task["title"] in prompt

    # v2 mode: should have report submission section (not legacy 状态管理)
    assert "Report Submission" in prompt
    assert "verdict" in prompt


def test_worker_prompt_contains_verification_requirements(sample_task):
    """Worker prompt should include verification or report requirements."""
    prompt = scheduler.generate_worker_prompt(task=sample_task)

    # v2 mode: should have report submission and verdict meanings
    assert "Report Submission" in prompt
    assert "Verdict meanings" in prompt


# ──────────────────────────────────────────
# Test scheduler.py — spawn instruction format
# ──────────────────────────────────────────

def test_spawn_instruction_format(sample_task):
    """Spawn instruction should have task_prompt (not message/session_key)."""
    instruction = scheduler.generate_spawn_instruction(
        task=sample_task,
        parent_session_id="feishu_parent_123",
    )

    # New format fields
    assert "task_id" in instruction
    assert "task_prompt" in instruction
    assert "title" in instruction
    assert "priority" in instruction
    assert "template" in instruction

    # Old format fields must NOT exist
    assert "session_key" not in instruction
    assert "message" not in instruction
    assert "parent" not in instruction
    assert "dispatcher_session_id" not in instruction

    # task_prompt should be the worker prompt (no callback)
    assert "report_completion.py" not in instruction["task_prompt"]
    assert sample_task["id"] in instruction["task_prompt"]


def test_spawn_instruction_no_dispatcher_param(sample_task):
    """generate_spawn_instruction should not accept dispatcher_session_id."""
    import inspect
    sig = inspect.signature(scheduler.generate_spawn_instruction)
    param_names = list(sig.parameters.keys())
    assert "dispatcher_session_id" not in param_names


# ──────────────────────────────────────────
# Test scheduler.py — run_scheduler output
# ──────────────────────────────────────────

def test_run_scheduler_dispatches_tasks(sample_task, temp_brain_dir):
    """run_scheduler should dispatch queued tasks and return spawn_instructions."""
    result = scheduler.run_scheduler(dry_run=False)

    assert result["ok"] is True
    assert len(result["spawn_instructions"]) == 1

    instr = result["spawn_instructions"][0]
    assert instr["task_id"] == sample_task["id"]
    assert "task_prompt" in instr
    assert "report_completion.py" not in instr["task_prompt"]

    # Task should be moved to executing
    task = bm.load_task(sample_task["id"])
    assert task["status"] == "executing"


def test_run_scheduler_no_dispatcher_param():
    """run_scheduler should not accept dispatcher_session_id."""
    import inspect
    sig = inspect.signature(scheduler.run_scheduler)
    param_names = list(sig.parameters.keys())
    assert "dispatcher_session_id" not in param_names


# ──────────────────────────────────────────
# Test trigger_scheduler.py — dispatcher prompt
# ──────────────────────────────────────────

def test_dispatcher_prompt_uses_spawn():
    """Dispatcher prompt should instruct using spawn tool, not create_subsession.sh."""
    prompt = build_scheduler_prompt(dry_run=False, parent_session_id="", is_wake_up=False)

    # Must use spawn
    assert "spawn" in prompt
    assert "dispatched" in prompt  # v2: dispatched list instead of task_prompt
    assert "Subagent Result Notification" in prompt

    # Must NOT reference old mechanisms
    assert "create_subsession" not in prompt
    assert "session_key" not in prompt
    assert "[Worker Completion Notification]" not in prompt
    assert "report_completion.py" not in prompt
    assert "send-completion" not in prompt
    assert "dispatcher_session_id" not in prompt


def test_dispatcher_prompt_no_dispatcher_session_param():
    """build_scheduler_prompt should not accept dispatcher_session_id."""
    import inspect
    sig = inspect.signature(build_scheduler_prompt)
    param_names = list(sig.parameters.keys())
    assert "dispatcher_session_id" not in param_names


def test_wakeup_prompt_uses_spawn():
    """Wake-up prompt should reference spawn, not create_subsession."""
    prompt = build_scheduler_prompt(dry_run=False, parent_session_id="", is_wake_up=True)

    assert "spawn" in prompt
    assert "create_subsession" not in prompt
    assert "dispatcher-session" not in prompt


def test_wakeup_prompt_no_dispatcher_flag():
    """Wake-up prompt should not include --dispatcher-session flag."""
    prompt = build_scheduler_prompt(dry_run=False, parent_session_id="test", is_wake_up=True)

    assert "--dispatcher-session" not in prompt


# ──────────────────────────────────────────
# Test report_completion.py is deleted
# ──────────────────────────────────────────

def test_report_completion_deleted():
    """report_completion.py should no longer exist."""
    report_path = _SCRIPTS_DIR / "report_completion.py"
    assert not report_path.exists(), "report_completion.py should be deleted"


# ──────────────────────────────────────────
# Integration: end-to-end spawn flow
# ──────────────────────────────────────────

def test_end_to_end_spawn_flow(sample_task, temp_brain_dir):
    """Test end-to-end spawn flow (without actual spawn execution)."""
    # 1. Scheduler generates spawn instructions
    result = scheduler.run_scheduler(dry_run=False)
    assert result["ok"]
    assert len(result["spawn_instructions"]) == 1

    instr = result["spawn_instructions"][0]

    # 2. Verify instruction is suitable for spawn tool
    assert "task_prompt" in instr
    prompt = instr["task_prompt"]
    assert len(prompt) > 50  # Should be substantial
    assert sample_task["id"] in prompt
    assert sample_task["title"] in prompt

    # 3. No callback artifacts in the prompt
    assert "report_completion" not in prompt
    assert "dispatcher-session" not in prompt

    # 4. Task is now executing
    task = bm.load_task(sample_task["id"])
    assert task["status"] == "executing"

    # 5. Dispatcher prompt knows how to handle subagent results
    dispatcher_prompt = build_scheduler_prompt(dry_run=False)
    assert "Subagent Result Notification" in dispatcher_prompt
    assert "spawn" in dispatcher_prompt


# ──────────────────────────────────────────
# Run tests
# ──────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
