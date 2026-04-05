#!/usr/bin/env python3
"""
Tests for scheduler.py new features:
  - Phase 2: Static rule injection in generate_worker_prompt_v2
  - Phase 3: Architect role integration (get_initial_role, make_decision, spawn)
  - Phase 4: Cross-check (tester guidance with rule audit, validate_architect_report)
"""

import json
import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure scripts dir is on path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import scheduler
import rule_loader


# ──────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────

@pytest.fixture
def mock_task():
    """Standard dev task for testing."""
    return {
        "id": "T-20260331-TEST",
        "title": "Test task for scheduler",
        "description": "A test task",
        "priority": "P1",
        "template": "standard-dev",
        "status": "queued",
    }


@pytest.fixture
def nanobot_task():
    """Nanobot-related task for testing project detection."""
    return {
        "id": "T-20260331-NANO",
        "title": "Fix nanobot scheduler bug",
        "description": "Fix a bug in the scheduler module",
        "priority": "P1",
        "template": "standard-dev",
        "status": "queued",
    }


@pytest.fixture
def batch_task():
    """Batch dev task."""
    return {
        "id": "T-20260331-BATCH",
        "title": "Batch development task",
        "description": "Multiple features to implement",
        "priority": "P1",
        "template": "batch-dev",
        "status": "queued",
    }


@pytest.fixture
def quick_task():
    """Quick task."""
    return {
        "id": "T-20260331-QUICK",
        "title": "Quick question",
        "description": "Answer this",
        "priority": "P2",
        "template": "quick",
        "status": "queued",
    }


@pytest.fixture
def architect_task():
    """Task with explicit architect flag."""
    return {
        "id": "T-20260331-ARCH",
        "title": "Design new architecture",
        "description": "Needs architect review",
        "priority": "P1",
        "template": "standard-dev",
        "architect": True,
        "status": "queued",
    }


# ──────────────────────────────────────────
# Phase 2: Static rule injection tests
# ──────────────────────────────────────────

class TestStaticRuleInjection:
    """Tests for rule injection in generate_worker_prompt_v2."""

    def test_prompt_contains_rules_header(self, mock_task):
        """Worker prompt should contain rules section."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "developer")
        assert "执行规则" in prompt

    def test_prompt_contains_global_rules(self, mock_task):
        """Worker prompt should contain global L0 rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "developer")
        assert "G-001" in prompt
        assert "G-002" in prompt

    def test_nanobot_task_has_project_rules(self, nanobot_task):
        """Nanobot task prompt should include nanobot-specific rules."""
        prompt = scheduler.generate_worker_prompt_v2(nanobot_task, "developer")
        assert "NANO-001" in prompt
        assert "NANO-002" in prompt

    def test_standard_dev_has_template_rules(self, mock_task):
        """Standard-dev task should include STD rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "developer")
        assert "STD-001" in prompt

    def test_batch_dev_has_batch_rules(self, batch_task):
        """Batch-dev task should include BAT rules."""
        prompt = scheduler.generate_worker_prompt_v2(batch_task, "developer")
        assert "BAT-001" in prompt

    def test_rules_before_role_guidance(self, mock_task):
        """Rules should appear before role-specific guidance."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "developer")
        rules_pos = prompt.find("执行规则")
        mission_pos = prompt.find("Your Mission")
        assert rules_pos < mission_pos, "Rules should appear before mission guidance"

    def test_tester_also_gets_rules(self, mock_task):
        """Tester role should also receive rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "tester")
        assert "执行规则" in prompt
        assert "G-001" in prompt

    def test_architect_also_gets_rules(self, mock_task):
        """Architect role should also receive rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "architect")
        assert "执行规则" in prompt
        assert "G-001" in prompt

    def test_prior_context_after_rules(self, mock_task):
        """Prior context should appear after rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "developer", prior_context="PRIOR_CONTEXT_MARKER")
        rules_pos = prompt.find("执行规则")
        context_pos = prompt.find("PRIOR_CONTEXT_MARKER")
        assert rules_pos < context_pos, "Rules should appear before prior context"


# ──────────────────────────────────────────
# Phase 3: Architect role integration tests
# ──────────────────────────────────────────

class TestGetInitialRole:
    """Tests for get_initial_role() conservative strategy."""

    def test_quick_returns_developer(self, quick_task):
        assert scheduler.get_initial_role(quick_task) == "developer"

    def test_cron_auto_returns_developer(self):
        task = {"template": "cron-auto"}
        assert scheduler.get_initial_role(task) == "developer"

    def test_batch_dev_returns_architect(self, batch_task):
        assert scheduler.get_initial_role(batch_task) == "architect"

    def test_standard_dev_returns_developer(self, mock_task):
        assert scheduler.get_initial_role(mock_task) == "developer"

    def test_explicit_architect_flag(self, architect_task):
        assert scheduler.get_initial_role(architect_task) == "architect"

    def test_needs_design_flag(self):
        task = {"template": "standard-dev", "needs_design": True}
        assert scheduler.get_initial_role(task) == "architect"

    def test_long_task_returns_developer(self):
        task = {"template": "long-task"}
        assert scheduler.get_initial_role(task) == "developer"

    def test_default_returns_developer(self):
        task = {}
        assert scheduler.get_initial_role(task) == "developer"

    def test_template_from_workgroup(self):
        task = {"workgroup": {"template": "batch-dev"}}
        assert scheduler.get_initial_role(task) == "architect"


class TestArchitectPromptGuidance:
    """Tests for architect-specific prompt guidance."""

    def test_architect_guidance_in_prompt(self, mock_task):
        """Architect prompt should include architect-specific guidance."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "architect")
        assert "Architect" in prompt
        assert "规则裁决" in prompt
        assert "方案设计" in prompt

    def test_architect_guidance_has_steps(self, mock_task):
        """Architect guidance should have structured steps."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "architect")
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt
        assert "Step 4" in prompt

    def test_architect_report_template(self, mock_task):
        """Architect prompt should include report submission section."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "architect")
        assert "Report Submission" in prompt
        assert '"role": "architect"' in prompt


class TestArchitectSpawnInstruction:
    """Tests for architect spawn instruction generation."""

    def test_architect_emoji(self, batch_task):
        """Architect spawn instruction should use 📐 emoji."""
        instruction = scheduler.generate_spawn_instruction_v2(batch_task, "architect")
        assert "📐" in instruction["title"]

    def test_architect_max_iterations(self, batch_task):
        """Architect should have max_iterations=25."""
        instruction = scheduler.generate_spawn_instruction_v2(batch_task, "architect")
        assert instruction["max_iterations"] == 25

    def test_developer_max_iterations(self, mock_task):
        """Developer should still have max_iterations=60."""
        instruction = scheduler.generate_spawn_instruction_v2(mock_task, "developer")
        assert instruction["max_iterations"] == 60

    def test_tester_max_iterations(self, mock_task):
        """Tester should still have max_iterations=30."""
        instruction = scheduler.generate_spawn_instruction_v2(mock_task, "tester")
        assert instruction["max_iterations"] == 30

    def test_architect_role_in_instruction(self, batch_task):
        """Spawn instruction should contain role=architect."""
        instruction = scheduler.generate_spawn_instruction_v2(batch_task, "architect")
        assert instruction["role"] == "architect"


class TestValidateArchitectReport:
    """Tests for validate_architect_report()."""

    def test_valid_report(self):
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
            "rule_verdict": {
                "worker_instructions": "Some rules here"
            },
        }
        warnings = scheduler.validate_architect_report(report)
        assert warnings == []

    def test_missing_rule_verdict(self):
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
        }
        warnings = scheduler.validate_architect_report(report)
        assert len(warnings) == 1
        assert "rule_verdict" in warnings[0]

    def test_empty_worker_instructions(self):
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
            "rule_verdict": {"worker_instructions": ""},
        }
        warnings = scheduler.validate_architect_report(report)
        assert len(warnings) == 1
        assert "empty" in warnings[0]

    def test_empty_rule_verdict_dict(self):
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
            "rule_verdict": {},
        }
        warnings = scheduler.validate_architect_report(report)
        assert len(warnings) == 1


class TestMakeDecisionArchitect:
    """Tests for make_decision() architect branch."""

    def test_architect_pass_dispatches_developer(self):
        """Architect pass should dispatch developer."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Rules reviewed",
            "rule_verdict": {"worker_instructions": "Follow these rules..."},
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        with patch("brain_manager.save_task"):
            decision = scheduler.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"
        assert "Follow these rules" in decision.params["context"]

    def test_architect_pass_with_design_notes(self):
        """Architect pass with design notes should include them in context."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
            "rule_verdict": {"worker_instructions": "Rules here"},
            "design_notes": "Important design decision",
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        with patch("brain_manager.save_task"):
            decision = scheduler.make_decision(report, task)
        assert "Important design decision" in decision.params["context"]

    def test_architect_pass_empty_instructions_fallback(self):
        """Empty worker_instructions should fallback to static rules."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Rules reviewed but no instructions",
            "rule_verdict": {"worker_instructions": ""},
        }
        task = {
            "id": "T-001",
            "title": "nanobot task",
            "description": "",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        with patch("brain_manager.save_task"):
            decision = scheduler.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"
        # Should contain static rules as fallback
        assert "G-001" in decision.params["context"] or "Architect Notes" in decision.params["context"]

    def test_architect_pass_stores_rule_context(self):
        """Architect pass should store rule_context in task."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "pass",
            "summary": "Done",
            "rule_verdict": {"worker_instructions": "Custom rules"},
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        with patch("brain_manager.save_task"):
            scheduler.make_decision(report, task)
        assert task.get("rule_context") == "Custom rules"

    def test_architect_fail_blocks(self):
        """Architect fail should block the task."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "fail",
            "summary": "Cannot proceed with this design",
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        decision = scheduler.make_decision(report, task)
        assert decision.action == "mark_blocked"
        assert "architect rejected" in decision.reason

    def test_architect_blocked_verdict(self):
        """Architect blocked verdict should block the task."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "blocked",
            "summary": "Need external dependency",
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        decision = scheduler.make_decision(report, task)
        assert decision.action == "mark_blocked"

    def test_architect_partial_continues(self):
        """Architect partial should continue with same role."""
        report = {
            "task_id": "T-001",
            "role": "architect",
            "verdict": "partial",
            "summary": "Still working on analysis",
        }
        task = {
            "id": "T-001",
            "template": "batch-dev",
            "orchestration": {"iteration": 0, "history": []},
        }
        decision = scheduler.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "architect"

    def test_architect_in_valid_roles(self):
        """REPORT_SCHEMA should include architect in valid_roles."""
        assert "architect" in scheduler.REPORT_SCHEMA["valid_roles"]


# ──────────────────────────────────────────
# Phase 4: Cross-check tests
# ──────────────────────────────────────────

class TestTesterGuidance:
    """Tests for tester guidance with rule audit."""

    def test_tester_guidance_has_rule_audit(self, mock_task):
        """Tester prompt should include rule audit instructions."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "tester")
        assert "规则审查" in prompt

    def test_tester_guidance_observable_rules(self, mock_task):
        """Tester guidance should mention observable rules."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "tester")
        assert "代码变更范围" in prompt
        assert "Commit" in prompt
        assert "文档更新" in prompt
        assert "测试覆盖" in prompt

    def test_tester_guidance_excludes_process_rules(self, mock_task):
        """Tester guidance should note that process rules are Dispatcher's job."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "tester")
        assert "Dispatcher" in prompt

    def test_tester_still_has_report_section(self, mock_task):
        """Tester prompt should still have report submission section."""
        prompt = scheduler.generate_worker_prompt_v2(mock_task, "tester")
        assert "Report Submission" in prompt


# ──────────────────────────────────────────
# Integration: run_scheduler with get_initial_role
# ──────────────────────────────────────────

class TestRunSchedulerIntegration:
    """Tests for run_scheduler using get_initial_role."""

    def test_batch_task_dispatched_as_architect(self, batch_task, monkeypatch):
        """Batch-dev task should be dispatched with architect role."""
        monkeypatch.setattr("brain_manager.list_tasks", lambda **kw: (
            [batch_task] if kw.get("status_filter") == {"queued"} else
            [] if kw.get("status_filter") == {"executing"} else
            [] if kw.get("status_filter") == {"review"} else
            [batch_task]
        ))
        monkeypatch.setattr("brain_manager.transition_task", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.append_decision", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.atomic_write", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.generate_briefing", lambda: "")
        monkeypatch.setattr("brain_manager.BRIEFING_FILE", Path("/tmp/test_briefing.md"))

        result = scheduler.run_scheduler(dry_run=False, parent_session_id="test")
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 1
        instruction = result["spawn_instructions"][0]
        assert instruction["role"] == "architect"
        assert "📐" in instruction["title"]
        assert instruction["max_iterations"] == 25

    def test_standard_task_dispatched_as_developer(self, mock_task, monkeypatch):
        """Standard-dev task should be dispatched with developer role."""
        monkeypatch.setattr("brain_manager.list_tasks", lambda **kw: (
            [mock_task] if kw.get("status_filter") == {"queued"} else
            [] if kw.get("status_filter") == {"executing"} else
            [] if kw.get("status_filter") == {"review"} else
            [mock_task]
        ))
        monkeypatch.setattr("brain_manager.transition_task", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.append_decision", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.atomic_write", lambda *a, **kw: None)
        monkeypatch.setattr("brain_manager.generate_briefing", lambda: "")
        monkeypatch.setattr("brain_manager.BRIEFING_FILE", Path("/tmp/test_briefing.md"))

        result = scheduler.run_scheduler(dry_run=False, parent_session_id="test")
        assert result["ok"]
        assert len(result["spawn_instructions"]) == 1
        instruction = result["spawn_instructions"][0]
        assert instruction["role"] == "developer"
        assert "🔨" in instruction["title"]


# ──────────────────────────────────────────
# v1 compatibility
# ──────────────────────────────────────────

class TestV1Compatibility:
    """Ensure v1 legacy mode still works."""

    def test_legacy_mode_ignores_rules(self, mock_task, monkeypatch):
        """Legacy mode should not include rule injection."""
        monkeypatch.setattr(scheduler, "LEGACY_MODE", True)
        # Legacy mode uses _generate_worker_prompt_legacy which doesn't call rule_loader
        prompt = scheduler.generate_worker_prompt(mock_task)
        # Legacy prompt uses Chinese headers
        assert "任务执行指令" in prompt

    def test_non_legacy_includes_rules(self, mock_task, monkeypatch):
        """Non-legacy mode should include rule injection."""
        monkeypatch.setattr(scheduler, "LEGACY_MODE", False)
        prompt = scheduler.generate_worker_prompt(mock_task, "developer")
        assert "执行规则" in prompt
