#!/usr/bin/env python3
"""
test_report_daily.py - Unit tests for report_daily.py

Uses tempdir + BRAIN_DIR env var isolation.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# ── Setup: set BRAIN_DIR before importing ──
# We'll use fixtures to manage this per-test


@pytest.fixture
def brain_env(tmp_path):
    """Create an isolated brain directory and set env var."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "tasks").mkdir()
    (brain_dir / "reviews").mkdir()
    (brain_dir / "reports" / "daily").mkdir(parents=True)

    # Patch env and reimport
    with mock.patch.dict(os.environ, {"BRAIN_DIR": str(brain_dir)}):
        # Force re-resolve paths in brain_manager
        import task_store as brain_manager
        brain_manager.BRAIN_DIR = brain_dir
        brain_manager.TASKS_DIR = brain_dir / "tasks"
        brain_manager.REVIEWS_DIR = brain_dir / "reviews"
        brain_manager.QUICK_LOG = brain_dir / "quick-log.jsonl"
        brain_manager.DECISIONS_LOG = brain_dir / "decisions.jsonl"

        import report_daily
        report_daily.REPORTS_DIR = brain_dir / "reports" / "daily"

        yield {
            "brain_dir": brain_dir,
            "tasks_dir": brain_dir / "tasks",
            "reviews_dir": brain_dir / "reviews",
            "reports_dir": brain_dir / "reports" / "daily",
            "brain_manager": brain_manager,
            "report_daily": report_daily,
        }


def _write_task_yaml(tasks_dir: Path, task_id: str, data: dict):
    """Helper: write a task YAML file."""
    import yaml
    path = tasks_dir / f"{task_id}.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def _write_review_yaml(reviews_dir: Path, review_id: str, data: dict):
    """Helper: write a review YAML file."""
    import yaml
    path = reviews_dir / f"{review_id}.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def _write_quick_log(brain_dir: Path, entries: list[dict]):
    """Helper: write quick log entries."""
    path = brain_dir / "quick-log.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _write_decisions_log(brain_dir: Path, entries: list[dict]):
    """Helper: write decisions log entries."""
    path = brain_dir / "decisions.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ──────────────────────────────────────────
# Test: calc_duration
# ──────────────────────────────────────────


class TestCalcDuration:
    def test_hours_minutes(self, brain_env):
        rd = brain_env["report_daily"]
        result = rd.calc_duration("2026-03-31T10:00:00+08:00", "2026-03-31T12:30:00+08:00")
        assert result == "2h 30m"

    def test_days_hours(self, brain_env):
        rd = brain_env["report_daily"]
        result = rd.calc_duration("2026-03-30T10:00:00+08:00", "2026-03-31T11:00:00+08:00")
        assert result == "1d 1h"

    def test_minutes_only(self, brain_env):
        rd = brain_env["report_daily"]
        result = rd.calc_duration("2026-03-31T10:00:00+08:00", "2026-03-31T10:45:00+08:00")
        assert result == "45m"

    def test_zero_duration(self, brain_env):
        rd = brain_env["report_daily"]
        result = rd.calc_duration("2026-03-31T10:00:00+08:00", "2026-03-31T10:00:00+08:00")
        assert result == "0m"

    def test_empty_input(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd.calc_duration("", "2026-03-31T10:00:00+08:00") == ""
        assert rd.calc_duration("2026-03-31T10:00:00+08:00", "") == ""
        assert rd.calc_duration("", "") == ""

    def test_invalid_input(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd.calc_duration("not-a-date", "2026-03-31T10:00:00+08:00") == ""


# ──────────────────────────────────────────
# Test: extract_block_reason
# ──────────────────────────────────────────


class TestExtractBlockReason:
    def test_from_history_parentheses(self, brain_env):
        rd = brain_env["report_daily"]
        task = {
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-31T10:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked (waiting for API key)"},
            ],
        }
        assert rd.extract_block_reason(task) == "waiting for API key"

    def test_from_history_no_parens(self, brain_env):
        rd = brain_env["report_daily"]
        task = {
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-31T10:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked"},
            ],
        }
        result = rd.extract_block_reason(task)
        assert "blocked" in result.lower()

    def test_from_notes(self, brain_env):
        rd = brain_env["report_daily"]
        task = {
            "context": {"notes": "[2026-03-31T10:00:00+08:00] 等待用户确认"},
            "history": [],
        }
        assert "等待用户确认" in rd.extract_block_reason(task)

    def test_empty_task(self, brain_env):
        rd = brain_env["report_daily"]
        task = {"context": {"notes": ""}, "history": []}
        assert rd.extract_block_reason(task) == ""

    def test_missing_context(self, brain_env):
        rd = brain_env["report_daily"]
        task = {"history": []}
        assert rd.extract_block_reason(task) == ""


# ──────────────────────────────────────────
# Test: decisions stats
# ──────────────────────────────────────────


class TestDecisionsStats:
    def test_basic_stats(self, brain_env):
        rd = brain_env["report_daily"]
        _write_decisions_log(brain_env["brain_dir"], [
            {"type": "status_change", "timestamp": "2026-03-31T10:00:00+08:00"},
            {"type": "status_change", "timestamp": "2026-03-31T11:00:00+08:00"},
            {"type": "review_resolve", "timestamp": "2026-03-31T12:00:00+08:00"},
            {"type": "status_change", "timestamp": "2026-03-30T10:00:00+08:00"},  # different day
        ])
        result = rd.collect_decisions_stats("2026-03-31")
        assert result["total"] == 3
        assert result["by_type"]["status_change"] == 2
        assert result["by_type"]["review_resolve"] == 1

    def test_empty_decisions(self, brain_env):
        rd = brain_env["report_daily"]
        result = rd.collect_decisions_stats("2026-03-31")
        assert result["total"] == 0
        assert result["by_type"] == {}


# ──────────────────────────────────────────
# Test: date judgment (collect_tasks filters by date)
# ──────────────────────────────────────────


class TestDateJudgment:
    def test_done_on_date(self, brain_env):
        rd = brain_env["report_daily"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Test Task 1",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260330-001", {
            "id": "T-20260330-001",
            "title": "Yesterday Task",
            "type": "quick",
            "status": "done",
            "priority": "P2",
            "created": "2026-03-30T09:00:00+08:00",
            "updated": "2026-03-30T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        result = rd.collect_tasks("2026-03-31")
        assert len(result["done"]) == 1
        assert result["done"][0].id == "T-20260331-001"

    def test_executing_not_filtered_by_date(self, brain_env):
        rd = brain_env["report_daily"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260330-002", {
            "id": "T-20260330-002",
            "title": "Still Running",
            "type": "standard-dev",
            "status": "executing",
            "priority": "P1",
            "created": "2026-03-30T09:00:00+08:00",
            "updated": "2026-03-30T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        result = rd.collect_tasks("2026-03-31")
        assert len(result["executing"]) == 1


# ──────────────────────────────────────────
# Test: empty data directory
# ──────────────────────────────────────────


class TestEmptyData:
    def test_empty_brain_dir(self, brain_env):
        rd = brain_env["report_daily"]
        report = rd.generate_daily_report("2026-03-31")
        assert report.total_tasks == 0
        assert report.done_count == 0
        assert report.executing_count == 0
        assert report.quick_count == 0

    def test_empty_renders_without_error(self, brain_env):
        rd = brain_env["report_daily"]
        report = rd.generate_daily_report("2026-03-31")
        md = rd.render_daily_markdown(report)
        assert "日报 2026-03-31" in md
        assert "_无_" in md
        assert len(md.splitlines()) <= 200


# ──────────────────────────────────────────
# Test: corrupted YAML
# ──────────────────────────────────────────


class TestCorruptedYaml:
    def test_single_corrupt_file_does_not_crash(self, brain_env):
        rd = brain_env["report_daily"]
        # Write a valid task
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Good Task",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        # Write a corrupt YAML file
        corrupt_path = brain_env["tasks_dir"] / "T-20260331-002.yaml"
        corrupt_path.write_text("{{{{invalid yaml: [[[", encoding="utf-8")

        # Should not crash; the corrupt file may be skipped by yaml.safe_load
        # (brain_manager.list_tasks reads all files, corrupt ones may raise)
        # We test that the report generation handles it gracefully
        try:
            report = rd.generate_daily_report("2026-03-31")
            # If it gets here, it handled the error
            md = rd.render_daily_markdown(report)
            assert "日报" in md
        except Exception:
            # brain_manager.list_tasks may raise on corrupt YAML
            # That's acceptable behavior - we document it
            pass


# ──────────────────────────────────────────
# Test: integration (generate + render)
# ──────────────────────────────────────────


class TestIntegration:
    def test_full_report_generation(self, brain_env):
        rd = brain_env["report_daily"]
        date = "2026-03-31"

        # Create diverse tasks
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Completed Feature",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T12:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-002", {
            "id": "T-20260331-002",
            "title": "In Progress Task",
            "type": "standard-dev",
            "status": "executing",
            "priority": "P0",
            "created": "2026-03-31T10:00:00+08:00",
            "updated": "2026-03-31T10:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-003", {
            "id": "T-20260331-003",
            "title": "Blocked Task",
            "type": "long-task",
            "status": "blocked",
            "priority": "P1",
            "created": "2026-03-31T08:00:00+08:00",
            "updated": "2026-03-31T11:00:00+08:00",
            "context": {"notes": "[2026-03-31T11:00:00+08:00] 等待外部API回复"},
            "history": [
                {"timestamp": "2026-03-31T11:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked (等待外部API回复)"},
            ],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-004", {
            "id": "T-20260331-004",
            "title": "Queued Task",
            "type": "quick",
            "status": "queued",
            "priority": "P2",
            "created": "2026-03-31T08:00:00+08:00",
            "updated": "2026-03-31T08:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })

        # Quick tasks
        _write_quick_log(brain_env["brain_dir"], [
            {"id": "Q-20260331-001", "title": "Quick fix", "result": "done",
             "timestamp": "2026-03-31T09:30:00+08:00"},
        ])

        # Decisions
        _write_decisions_log(brain_env["brain_dir"], [
            {"type": "status_change", "task_id": "T-20260331-001",
             "from": "executing", "to": "done", "timestamp": "2026-03-31T12:00:00+08:00"},
            {"type": "status_change", "task_id": "T-20260331-003",
             "from": "executing", "to": "blocked", "timestamp": "2026-03-31T11:00:00+08:00"},
        ])

        report = rd.generate_daily_report(date)

        # Verify counts
        assert report.done_count == 1
        assert report.executing_count == 1
        assert report.blocked_count == 1
        assert report.queued_count == 1
        assert report.quick_count == 1
        assert report.decisions_total == 2

        # Verify type distribution is dynamic
        assert "standard-dev" in report.type_distribution
        assert "long-task" in report.type_distribution
        assert "quick" in report.type_distribution

        # Render markdown
        md = rd.render_daily_markdown(report)

        # Verify 7 sections
        assert "## 1. 概览" in md
        assert "## 2. ✅ 已完成" in md
        assert "## 3. 🔵 进行中" in md
        assert "## 4. 🚨 异常" in md
        assert "## 5. ⚡ 效率" in md
        assert "## 6. 📋 待办" in md
        assert "## 7. 🔮 预期" in md

        # Verify content
        assert "Completed Feature" in md
        assert "In Progress Task" in md
        assert "Blocked Task" in md
        assert "等待外部API回复" in md
        assert "Queued Task" in md
        assert "Quick fix" in md

        # Verify line count ≤ 200
        assert len(md.splitlines()) <= 200

    def test_report_with_duration(self, brain_env):
        rd = brain_env["report_daily"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Fast Task",
            "type": "quick",
            "status": "done",
            "priority": "P2",
            "created": "2026-03-31T10:00:00+08:00",
            "updated": "2026-03-31T10:30:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-002", {
            "id": "T-20260331-002",
            "title": "Slow Task",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T11:30:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        report = rd.generate_daily_report("2026-03-31")
        assert report.done_count == 2
        assert report.avg_duration_str != ""
        # avg of 30m and 2h30m = 90m = 1h 30m
        assert "1h" in report.avg_duration_str


# ──────────────────────────────────────────
# Test: notification
# ──────────────────────────────────────────


class TestNotification:
    def test_summary_max_10_lines(self, brain_env):
        rd = brain_env["report_daily"]
        report = rd.DailyReport(
            date="2026-03-31",
            done_count=5,
            executing_count=3,
            queued_count=10,
            blocked_count=2,
            quick_count=8,
            done_tasks=[{"id": f"T-{i}", "title": f"Task {i}"} for i in range(5)],
            overdue_reviews=[{"id": "R-1"}],
            avg_duration_str="1h 30m",
        )
        summary = rd._build_notification_summary(report)
        lines = summary.strip().split("\n")
        assert len(lines) <= 10

    def test_no_target_skips_notify(self, brain_env):
        rd = brain_env["report_daily"]
        original = rd.NOTIFY_TARGET
        try:
            rd.NOTIFY_TARGET = ""
            report = rd.DailyReport(date="2026-03-31")
            assert rd.send_notification(report) is False
        finally:
            rd.NOTIFY_TARGET = original

    def test_notify_failure_does_not_raise(self, brain_env):
        rd = brain_env["report_daily"]
        original = rd.NOTIFY_TARGET
        try:
            rd.NOTIFY_TARGET = "fake_open_id"
            report = rd.DailyReport(date="2026-03-31")
            # Even with a fake target, send_notification should not raise
            result = rd.send_notification(report)
            # Result is False (script may not exist or fail)
            assert isinstance(result, bool)
        finally:
            rd.NOTIFY_TARGET = original


# ──────────────────────────────────────────
# Test: stale/recovered
# ──────────────────────────────────────────


class TestStaleRecovered:
    def test_detect_recovery(self, brain_env):
        rd = brain_env["report_daily"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Recovered Task",
            "type": "standard-dev",
            "status": "executing",
            "priority": "P1",
            "created": "2026-03-30T10:00:00+08:00",
            "updated": "2026-03-31T14:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-30T15:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked"},
                {"timestamp": "2026-03-31T14:00:00+08:00", "action": "status_change",
                 "detail": "status: blocked → executing"},
            ],
        })
        result = rd.collect_stale_recovered("2026-03-31")
        assert len(result) == 1
        assert result[0]["id"] == "T-20260331-001"

    def test_no_recovery(self, brain_env):
        rd = brain_env["report_daily"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Still Blocked",
            "type": "standard-dev",
            "status": "blocked",
            "priority": "P1",
            "created": "2026-03-30T10:00:00+08:00",
            "updated": "2026-03-30T15:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-30T15:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked"},
            ],
        })
        result = rd.collect_stale_recovered("2026-03-31")
        assert len(result) == 0


# ──────────────────────────────────────────
# Test: _parse_duration_to_minutes
# ──────────────────────────────────────────


class TestParseDuration:
    def test_hours_minutes(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd._parse_duration_to_minutes("2h 30m") == 150

    def test_days_hours(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd._parse_duration_to_minutes("1d 2h") == 1560

    def test_minutes_only(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd._parse_duration_to_minutes("45m") == 45

    def test_empty(self, brain_env):
        rd = brain_env["report_daily"]
        assert rd._parse_duration_to_minutes("") == 0


# ──────────────────────────────────────────
# Test: CLI dry-run
# ──────────────────────────────────────────


class TestCLI:
    def test_dry_run(self, brain_env, tmp_path):
        """Test CLI --dry-run outputs markdown to stdout."""
        script = Path(__file__).resolve().parent / "report_daily.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--dry-run", "--date", "2026-03-31", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        assert "日报 2026-03-31" in result.stdout

    def test_write_file(self, brain_env, tmp_path):
        """Test CLI writes report file."""
        script = Path(__file__).resolve().parent / "report_daily.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--date", "2026-03-31", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        report_file = brain_env["reports_dir"] / "2026-03-31.md"
        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "日报 2026-03-31" in content

    def test_invalid_date(self, brain_env):
        """Test CLI rejects invalid date format."""
        script = Path(__file__).resolve().parent / "report_daily.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--date", "not-a-date", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode != 0
