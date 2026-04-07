#!/usr/bin/env python3
"""
test_scheduler_evidence.py - Tests for test evidence validation (T-20260401-009).

Tests cover:
  - Tester pass with valid test_evidence → proceeds normally
  - Tester pass without test_evidence → dispatched back to tester
  - Tester pass with empty test_evidence → dispatched back
  - Tester pass with invalid evidence items (missing type/result) → dispatched back
  - Retry escalation after MAX_EVIDENCE_RETRY → promote_to_review
  - test_results field accepted as alias for test_evidence
  - Feature flag TEST_EVIDENCE_ENABLED=0 disables check
  - Tester fail/blocked/partial → not affected by evidence check
  - _count_evidence_retries counts correctly
  - _generate_tester_guidance includes evidence instructions
"""

import json
import os
import sys
import tempfile
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

    monkeypatch.setenv("TASK_DATA_DIR", str(brain_dir))

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

    import trigger_scheduler as ts
    monkeypatch.setattr(ts, "DISPATCHER_FILE", brain_dir / "dispatcher.json")
    monkeypatch.setattr(ts, "BRAIN_DIR", brain_dir)

    # Ensure TEST_EVIDENCE_ENABLED is on for tests (default)
    monkeypatch.setenv("TEST_EVIDENCE_ENABLED", "1")

    # Reimport scheduler to pick up env var
    if "scheduler" in sys.modules:
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

    yield brain_dir


def _make_task(task_id="T-001", template="standard-dev", priority="P1",
               history=None, iteration=1):
    """Helper to create a task dict for make_decision tests."""
    return {
        "id": task_id,
        "priority": priority,
        "workgroup": {"template": template},
        "orchestration": {
            "iteration": iteration,
            "history": history or [],
        },
    }


def _make_report(task_id="T-001", role="tester", verdict="pass",
                 summary="All tests passed", test_evidence=None, test_results=None):
    """Helper to create a tester report dict."""
    report = {
        "task_id": task_id,
        "role": role,
        "verdict": verdict,
        "summary": summary,
    }
    if test_evidence is not None:
        report["test_evidence"] = test_evidence
    if test_results is not None:
        report["test_results"] = test_results
    return report


# ──────────────────────────────────────────
# Tests: Evidence validation in make_decision
# ──────────────────────────────────────────

class TestTesterEvidenceValidation:
    """Test test_evidence validation in make_decision tester pass branch."""

    def test_pass_with_valid_evidence_proceeds(self):
        """Tester pass + valid test_evidence → normal flow (promote_to_review for L2+)."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_evidence=[
            {"type": "command_output", "command": "pytest tests/", "result": "5 passed, 0 failed"},
            {"type": "manual_test", "description": "Check UI", "result": "OK"},
        ])
        task = _make_task()

        decision = sched.make_decision(report, task)
        # P1 standard-dev → L2+ → promote_to_review
        assert decision.action == "promote_to_review"

    def test_pass_without_evidence_dispatches_back(self):
        """Tester pass + no test_evidence → dispatch back to tester."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report()  # No test_evidence
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"
        assert "test_evidence" in decision.params["context"]
        assert "no test_evidence" in decision.reason

    def test_pass_with_empty_evidence_dispatches_back(self):
        """Tester pass + empty test_evidence=[] → dispatch back to tester."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_evidence=[])
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_pass_with_invalid_evidence_missing_type(self):
        """Evidence item missing 'type' → treated as invalid."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_evidence=[
            {"result": "5 passed"}  # Missing 'type'
        ])
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_pass_with_invalid_evidence_missing_result(self):
        """Evidence item missing 'result' → treated as invalid."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_evidence=[
            {"type": "command_output", "command": "pytest"}  # Missing 'result'
        ])
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_pass_with_evidence_string_not_list(self):
        """test_evidence as string (not list) → treated as invalid."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_evidence="I ran the tests")
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_test_results_alias_accepted(self):
        """test_results field accepted as alias for test_evidence."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(test_results=[
            {"type": "command_output", "command": "pytest", "result": "3 passed"},
        ])
        task = _make_task()

        decision = sched.make_decision(report, task)
        # Should proceed normally (not dispatched back)
        assert decision.action == "promote_to_review"

    def test_fail_not_affected_by_evidence_check(self):
        """Tester fail → dispatches developer regardless of evidence."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(verdict="fail", summary="Tests failed")
        report["issues"] = [{"description": "Bug found"}]
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "developer"

    def test_partial_not_affected_by_evidence_check(self):
        """Tester partial → not affected by evidence check."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report(verdict="partial", summary="Partial progress")
        task = _make_task()

        decision = sched.make_decision(report, task)
        # Partial should go through normal partial handling, not evidence check
        assert decision.action != "dispatch_role" or decision.params.get("role") != "tester" or \
            "test_evidence" not in decision.reason


class TestEvidenceRetryEscalation:
    """Test retry counting and escalation for missing evidence."""

    def test_first_retry_dispatches_back(self):
        """First missing evidence → dispatch back to tester."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report()
        task = _make_task(history=[])  # No prior retries

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_second_retry_dispatches_back(self):
        """Second missing evidence (1 prior retry) → still dispatch back."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report()
        task = _make_task(history=[
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
        ], iteration=2)

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"

    def test_third_retry_escalates_to_review(self):
        """Third missing evidence (2 prior retries) → escalate to promote_to_review."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report()
        task = _make_task(history=[
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
        ], iteration=3)

        decision = sched.make_decision(report, task)
        assert decision.action == "promote_to_review"
        assert "test_evidence" in decision.params["summary"]
        assert "已打回" in decision.params["summary"]


class TestCountEvidenceRetries:
    """Test _count_evidence_retries function."""

    def test_empty_history(self):
        import scheduler_legacy as sched
        task = _make_task(history=[])
        assert sched._count_evidence_retries(task) == 0

    def test_no_evidence_retries(self):
        import scheduler_legacy as sched
        task = _make_task(history=[
            {"role": "developer", "verdict": "pass", "reason": "developer done"},
            {"role": "tester", "verdict": "fail", "reason": "tests failed"},
        ])
        assert sched._count_evidence_retries(task) == 0

    def test_one_evidence_retry(self):
        import scheduler_legacy as sched
        task = _make_task(history=[
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
        ])
        assert sched._count_evidence_retries(task) == 1

    def test_two_evidence_retries(self):
        import scheduler_legacy as sched
        task = _make_task(history=[
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
        ])
        assert sched._count_evidence_retries(task) == 2

    def test_mixed_history(self):
        import scheduler_legacy as sched
        task = _make_task(history=[
            {"role": "developer", "verdict": "pass", "reason": "dev done"},
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
            {"role": "tester", "verdict": "fail", "reason": "tests failed"},
            {"role": "tester", "verdict": "pass", "reason": "tester passed but no test_evidence"},
        ])
        assert sched._count_evidence_retries(task) == 2


class TestFeatureFlag:
    """Test TEST_EVIDENCE_ENABLED feature flag."""

    def test_disabled_skips_evidence_check(self, monkeypatch):
        """When TEST_EVIDENCE_ENABLED=0, tester pass without evidence proceeds normally."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = False

        report = _make_report()  # No test_evidence
        task = _make_task()

        decision = sched.make_decision(report, task)
        # Should proceed to normal flow (promote_to_review for L2+), NOT dispatch back
        assert decision.action == "promote_to_review"

    def test_enabled_enforces_evidence_check(self, monkeypatch):
        """When TEST_EVIDENCE_ENABLED=1, tester pass without evidence is rejected."""
        import scheduler_legacy as sched
        sched.TEST_EVIDENCE_ENABLED = True

        report = _make_report()  # No test_evidence
        task = _make_task()

        decision = sched.make_decision(report, task)
        assert decision.action == "dispatch_role"
        assert decision.params["role"] == "tester"


class TestTesterGuidance:
    """Test _generate_tester_guidance includes evidence instructions."""

    def test_guidance_includes_evidence_instructions(self):
        import scheduler_legacy as sched
        task = _make_task()
        guidance = sched._generate_tester_guidance(task)

        assert "test_evidence" in guidance
        assert "command_output" in guidance
        assert "manual_test" in guidance
        assert "type" in guidance
        assert "result" in guidance
        assert "打回" in guidance
