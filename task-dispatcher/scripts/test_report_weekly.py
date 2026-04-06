#!/usr/bin/env python3
"""
test_report_weekly.py - Unit tests for report_weekly.py

Uses tempdir + BRAIN_DIR env var isolation.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

# ── Setup: set BRAIN_DIR before importing ──


@pytest.fixture
def brain_env(tmp_path):
    """Create an isolated brain directory and set env var."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "tasks").mkdir()
    (brain_dir / "reviews").mkdir()
    (brain_dir / "reports" / "weekly").mkdir(parents=True)
    (brain_dir / "reports" / "cil-weekly").mkdir(parents=True)

    with mock.patch.dict(os.environ, {"BRAIN_DIR": str(brain_dir)}):
        import task_store as brain_manager
        brain_manager.BRAIN_DIR = brain_dir
        brain_manager.TASKS_DIR = brain_dir / "tasks"
        brain_manager.REVIEWS_DIR = brain_dir / "reviews"
        brain_manager.QUICK_LOG = brain_dir / "quick-log.jsonl"
        brain_manager.DECISIONS_LOG = brain_dir / "decisions.jsonl"

        import report_weekly
        report_weekly.REPORTS_DIR = brain_dir / "reports" / "weekly"
        report_weekly.CIL_WEEKLY_DIR = brain_dir / "reports" / "cil-weekly"

        yield {
            "brain_dir": brain_dir,
            "tasks_dir": brain_dir / "tasks",
            "reviews_dir": brain_dir / "reviews",
            "reports_dir": brain_dir / "reports" / "weekly",
            "cil_weekly_dir": brain_dir / "reports" / "cil-weekly",
            "brain_manager": brain_manager,
            "report_weekly": report_weekly,
        }


def _write_task_yaml(tasks_dir: Path, task_id: str, data: dict):
    """Helper: write a task YAML file."""
    import yaml
    path = tasks_dir / f"{task_id}.yaml"
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
# Test: iso_week_range
# ──────────────────────────────────────────


class TestIsoWeekRange:
    def test_week_14_2026(self, brain_env):
        rw = brain_env["report_weekly"]
        wr = rw.iso_week_range(2026, 14)
        assert wr.start_date == "2026-03-30"
        assert wr.end_date == "2026-04-05"
        assert wr.label == "2026-W14"

    def test_week_1_2026(self, brain_env):
        rw = brain_env["report_weekly"]
        wr = rw.iso_week_range(2026, 1)
        # 2026-01-01 is Thursday, so W01 Monday = 2025-12-29
        assert wr.start_date == "2025-12-29"
        assert wr.end_date == "2026-01-04"

    def test_week_53(self, brain_env):
        """Some years have 53 weeks (e.g., 2020)."""
        rw = brain_env["report_weekly"]
        wr = rw.iso_week_range(2020, 53)
        assert wr.start_date == "2020-12-28"
        assert wr.end_date == "2021-01-03"


class TestParseWeekLabel:
    def test_valid_label(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.parse_week_label("2026-W14") == (2026, 14)
        assert rw.parse_week_label("2026-W01") == (2026, 1)
        assert rw.parse_week_label("2026-W1") == (2026, 1)

    def test_invalid_label(self, brain_env):
        rw = brain_env["report_weekly"]
        with pytest.raises(ValueError):
            rw.parse_week_label("2026-14")
        with pytest.raises(ValueError):
            rw.parse_week_label("not-a-week")
        with pytest.raises(ValueError):
            rw.parse_week_label("2026-W0")
        with pytest.raises(ValueError):
            rw.parse_week_label("2026-W54")


class TestPreviousIsoWeek:
    def test_normal(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.previous_iso_week(2026, 14) == (2026, 13)

    def test_cross_year(self, brain_env):
        rw = brain_env["report_weekly"]
        y, w = rw.previous_iso_week(2026, 1)
        assert y == 2025
        assert w >= 52


# ──────────────────────────────────────────
# Test: date_in_range
# ──────────────────────────────────────────


class TestDateInRange:
    def test_in_range(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.date_in_range("2026-03-31T10:00:00+08:00", "2026-03-30", "2026-04-05") is True

    def test_on_boundary(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.date_in_range("2026-03-30T00:00:00+08:00", "2026-03-30", "2026-04-05") is True
        assert rw.date_in_range("2026-04-05T23:59:59+08:00", "2026-03-30", "2026-04-05") is True

    def test_out_of_range(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.date_in_range("2026-03-29T23:59:59+08:00", "2026-03-30", "2026-04-05") is False
        assert rw.date_in_range("2026-04-06T00:00:00+08:00", "2026-03-30", "2026-04-05") is False

    def test_empty_string(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.date_in_range("", "2026-03-30", "2026-04-05") is False


# ──────────────────────────────────────────
# Test: calc_duration
# ──────────────────────────────────────────


class TestCalcDuration:
    def test_hours_minutes(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.calc_duration("2026-03-31T10:00:00+08:00", "2026-03-31T12:30:00+08:00") == "2h 30m"

    def test_days_hours(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.calc_duration("2026-03-30T10:00:00+08:00", "2026-03-31T11:00:00+08:00") == "1d 1h"

    def test_minutes_only(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.calc_duration("2026-03-31T10:00:00+08:00", "2026-03-31T10:45:00+08:00") == "45m"

    def test_empty(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw.calc_duration("", "") == ""


# ──────────────────────────────────────────
# Test: collect_week_overview
# ──────────────────────────────────────────


class TestCollectWeekOverview:
    def test_basic_overview(self, brain_env):
        rw = brain_env["report_weekly"]
        # Create tasks in W14 (2026-03-30 to 2026-04-05)
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Task A",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260401-001", {
            "id": "T-20260401-001",
            "title": "Task B",
            "type": "quick",
            "status": "executing",
            "priority": "P2",
            "created": "2026-04-01T09:00:00+08:00",
            "updated": "2026-04-01T10:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        overview = rw.collect_week_overview("2026-03-30", "2026-04-05")
        assert overview.total_created == 2
        assert overview.total_done == 1

    def test_empty_overview(self, brain_env):
        rw = brain_env["report_weekly"]
        overview = rw.collect_week_overview("2026-03-30", "2026-04-05")
        assert overview.total_created == 0
        assert overview.total_done == 0
        assert overview.total_blocked == 0
        assert overview.total_quick == 0
        assert overview.total_decisions == 0

    def test_blocked_counted(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Blocked Task",
            "type": "standard-dev",
            "status": "blocked",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T11:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-31T11:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked"},
            ],
        })
        overview = rw.collect_week_overview("2026-03-30", "2026-04-05")
        assert overview.total_blocked == 1

    def test_quick_tasks_counted(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_quick_log(brain_env["brain_dir"], [
            {"id": "Q-001", "title": "Quick 1", "result": "done",
             "timestamp": "2026-03-31T09:00:00+08:00"},
            {"id": "Q-002", "title": "Quick 2", "result": "done",
             "timestamp": "2026-04-06T09:00:00+08:00"},  # out of range
        ])
        overview = rw.collect_week_overview("2026-03-30", "2026-04-05")
        assert overview.total_quick == 1

    def test_decisions_counted(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_decisions_log(brain_env["brain_dir"], [
            {"type": "status_change", "timestamp": "2026-03-31T10:00:00+08:00"},
            {"type": "status_change", "timestamp": "2026-04-01T10:00:00+08:00"},
            {"type": "status_change", "timestamp": "2026-04-06T10:00:00+08:00"},  # out of range
        ])
        overview = rw.collect_week_overview("2026-03-30", "2026-04-05")
        assert overview.total_decisions == 2


# ──────────────────────────────────────────
# Test: collect_done_tasks
# ──────────────────────────────────────────


class TestCollectDoneTasks:
    def test_done_in_range(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Done Task",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260329-001", {
            "id": "T-20260329-001",
            "title": "Old Done Task",
            "type": "quick",
            "status": "done",
            "priority": "P2",
            "created": "2026-03-29T09:00:00+08:00",
            "updated": "2026-03-29T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        done = rw.collect_done_tasks("2026-03-30", "2026-04-05")
        assert len(done) == 1
        assert done[0].id == "T-20260331-001"
        assert done[0].duration_str == "8h 0m"

    def test_no_done(self, brain_env):
        rw = brain_env["report_weekly"]
        done = rw.collect_done_tasks("2026-03-30", "2026-04-05")
        assert len(done) == 0


# ──────────────────────────────────────────
# Test: type distribution (dynamic scan)
# ──────────────────────────────────────────


class TestTypeDistribution:
    def test_dynamic_types(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Task A",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260401-001", {
            "id": "T-20260401-001",
            "title": "Task B",
            "type": "dev-fix",
            "status": "done",
            "priority": "P2",
            "created": "2026-04-01T09:00:00+08:00",
            "updated": "2026-04-01T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260401-002", {
            "id": "T-20260401-002",
            "title": "Task C",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-04-01T10:00:00+08:00",
            "updated": "2026-04-01T18:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        dist = rw.collect_type_distribution("2026-03-30", "2026-04-05")
        assert dist["standard-dev"] == 2
        assert dist["dev-fix"] == 1
        # No hardcoded types - only what exists in data
        assert set(dist.keys()) == {"standard-dev", "dev-fix"}

    def test_empty_distribution(self, brain_env):
        rw = brain_env["report_weekly"]
        dist = rw.collect_type_distribution("2026-03-30", "2026-04-05")
        assert dist == {}


# ──────────────────────────────────────────
# Test: daily efficiency
# ──────────────────────────────────────────


class TestDailyEfficiency:
    def test_7_days(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Task A",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        eff = rw.collect_daily_efficiency("2026-03-30", "2026-04-05")
        assert len(eff) == 7
        # Monday 03-30: 0 done, 0 created
        assert eff[0].date == "2026-03-30"
        assert eff[0].done_count == 0
        # Tuesday 03-31: 1 done, 1 created
        assert eff[1].date == "2026-03-31"
        assert eff[1].done_count == 1
        assert eff[1].created_count == 1


# ──────────────────────────────────────────
# Test: blocked tasks
# ──────────────────────────────────────────


class TestBlockedTasks:
    def test_currently_blocked(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Blocked Task",
            "type": "standard-dev",
            "status": "blocked",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T11:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-31T11:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked (waiting for review)"},
            ],
        })
        blocked = rw.collect_blocked_tasks("2026-03-30", "2026-04-05")
        assert len(blocked) == 1
        assert blocked[0]["current"] is True
        assert "waiting for review" in blocked[0]["reason"]

    def test_recovered_blocked(self, brain_env):
        rw = brain_env["report_weekly"]
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Recovered Task",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-03-31T11:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked"},
                {"timestamp": "2026-03-31T14:00:00+08:00", "action": "status_change",
                 "detail": "status: blocked → executing"},
            ],
        })
        blocked = rw.collect_blocked_tasks("2026-03-30", "2026-04-05")
        assert len(blocked) == 1
        assert blocked[0]["current"] is False


# ──────────────────────────────────────────
# Test: CIL weekly reference
# ──────────────────────────────────────────


class TestCILWeeklyReference:
    def test_cil_exists(self, brain_env):
        rw = brain_env["report_weekly"]
        cil_file = brain_env["cil_weekly_dir"] / "2026-W14.md"
        cil_file.write_text("# CIL Weekly Report W14\n", encoding="utf-8")
        path = rw.find_cil_weekly("2026-W14")
        assert path != ""
        assert "2026-W14" in path

    def test_cil_not_exists(self, brain_env):
        rw = brain_env["report_weekly"]
        path = rw.find_cil_weekly("2026-W14")
        assert path == ""

    def test_cil_prefix_match(self, brain_env):
        rw = brain_env["report_weekly"]
        cil_file = brain_env["cil_weekly_dir"] / "2026-W14-summary.md"
        cil_file.write_text("# CIL Weekly\n", encoding="utf-8")
        path = rw.find_cil_weekly("2026-W14")
        assert path != ""
        assert "2026-W14" in path


# ──────────────────────────────────────────
# Test: comparison table (this week vs last week)
# ──────────────────────────────────────────


class TestComparisonTable:
    def test_change_indicator(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw._change_indicator(5, 3) == "↑2"
        assert rw._change_indicator(3, 5) == "↓2"
        assert rw._change_indicator(5, 5) == "→"
        assert rw._change_indicator(0, 0) == "→"

    def test_comparison_in_markdown(self, brain_env):
        rw = brain_env["report_weekly"]
        # Create tasks in two weeks
        # W13 (2026-03-23 to 2026-03-29)
        _write_task_yaml(brain_env["tasks_dir"], "T-20260325-001", {
            "id": "T-20260325-001",
            "title": "Last Week Task",
            "type": "quick",
            "status": "done",
            "priority": "P2",
            "created": "2026-03-25T09:00:00+08:00",
            "updated": "2026-03-25T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        # W14 (2026-03-30 to 2026-04-05)
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "This Week Task A",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260401-001", {
            "id": "T-20260401-001",
            "title": "This Week Task B",
            "type": "dev-fix",
            "status": "done",
            "priority": "P2",
            "created": "2026-04-01T09:00:00+08:00",
            "updated": "2026-04-01T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })

        report = rw.generate_weekly_report(2026, 14)
        assert report.this_week.total_done == 2
        assert report.last_week.total_done == 1

        md = rw.render_weekly_markdown(report)
        assert "本周" in md
        assert "上周" in md
        assert "变化" in md
        assert "↑1" in md  # done went from 1 to 2


# ──────────────────────────────────────────
# Test: empty data
# ──────────────────────────────────────────


class TestEmptyData:
    def test_empty_brain_dir(self, brain_env):
        rw = brain_env["report_weekly"]
        report = rw.generate_weekly_report(2026, 14)
        assert report.this_week.total_done == 0
        assert report.this_week.total_created == 0
        assert report.last_week.total_done == 0
        assert len(report.done_tasks) == 0
        assert report.type_distribution == {}

    def test_empty_renders_without_error(self, brain_env):
        rw = brain_env["report_weekly"]
        report = rw.generate_weekly_report(2026, 14)
        md = rw.render_weekly_markdown(report)
        assert "周报 2026-W14" in md
        assert "_本周无完成任务_" in md
        assert "_无数据_" in md


# ──────────────────────────────────────────
# Test: full integration
# ──────────────────────────────────────────


class TestIntegration:
    def test_full_weekly_report(self, brain_env):
        rw = brain_env["report_weekly"]

        # Create diverse tasks for W14 (2026-03-30 to 2026-04-05)
        _write_task_yaml(brain_env["tasks_dir"], "T-20260331-001", {
            "id": "T-20260331-001",
            "title": "Feature A",
            "type": "standard-dev",
            "status": "done",
            "priority": "P1",
            "created": "2026-03-31T09:00:00+08:00",
            "updated": "2026-03-31T17:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260401-001", {
            "id": "T-20260401-001",
            "title": "Bug Fix B",
            "type": "dev-fix",
            "status": "done",
            "priority": "P0",
            "created": "2026-04-01T08:00:00+08:00",
            "updated": "2026-04-01T10:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260402-001", {
            "id": "T-20260402-001",
            "title": "Blocked Task",
            "type": "standard-dev",
            "status": "blocked",
            "priority": "P1",
            "created": "2026-04-02T09:00:00+08:00",
            "updated": "2026-04-02T11:00:00+08:00",
            "context": {"notes": ""},
            "history": [
                {"timestamp": "2026-04-02T11:00:00+08:00", "action": "status_change",
                 "detail": "status: executing → blocked (waiting for API)"},
            ],
        })
        _write_task_yaml(brain_env["tasks_dir"], "T-20260403-001", {
            "id": "T-20260403-001",
            "title": "Queued Task",
            "type": "design-iteration",
            "status": "queued",
            "priority": "P2",
            "created": "2026-04-03T09:00:00+08:00",
            "updated": "2026-04-03T09:00:00+08:00",
            "context": {"notes": ""},
            "history": [],
        })

        # Quick tasks
        _write_quick_log(brain_env["brain_dir"], [
            {"id": "Q-001", "title": "Quick fix", "result": "done",
             "timestamp": "2026-04-01T09:30:00+08:00"},
        ])

        # Decisions
        _write_decisions_log(brain_env["brain_dir"], [
            {"type": "status_change", "timestamp": "2026-03-31T12:00:00+08:00"},
            {"type": "status_change", "timestamp": "2026-04-01T10:00:00+08:00"},
            {"type": "review_resolve", "timestamp": "2026-04-02T15:00:00+08:00"},
        ])

        # CIL weekly
        cil_file = brain_env["cil_weekly_dir"] / "2026-W14.md"
        cil_file.write_text("# CIL Weekly Report\n", encoding="utf-8")

        report = rw.generate_weekly_report(2026, 14)

        # Verify overview
        assert report.this_week.total_done == 2
        assert report.this_week.total_created == 4
        assert report.this_week.total_blocked == 1
        assert report.this_week.total_quick == 1
        assert report.this_week.total_decisions == 3

        # Verify done tasks
        assert len(report.done_tasks) == 2

        # Verify type distribution is dynamic
        assert "standard-dev" in report.type_distribution
        assert "dev-fix" in report.type_distribution
        assert "design-iteration" in report.type_distribution

        # Verify daily efficiency has 7 days
        assert len(report.daily_efficiency) == 7

        # Verify blocked tasks
        assert len(report.blocked_tasks) >= 1

        # Verify CIL reference
        assert report.cil_weekly_path != ""
        assert "2026-W14" in report.cil_weekly_path

        # Render markdown
        md = rw.render_weekly_markdown(report)

        # Verify sections
        assert "## 1. 📋 总览对比" in md
        assert "## 2. ✅ 完成任务" in md
        assert "## 3. 📊 任务类型分布" in md
        assert "## 4. ⚡ 效率趋势" in md
        assert "## 5. 🚨 问题汇总" in md
        assert "## 6. 🔗 CIL 周报" in md

        # Verify content
        assert "Feature A" in md
        assert "Bug Fix B" in md
        assert "Blocked Task" in md
        assert "waiting for API" in md
        assert "standard-dev" in md
        assert "dev-fix" in md

    def test_no_cil_section_when_missing(self, brain_env):
        rw = brain_env["report_weekly"]
        report = rw.generate_weekly_report(2026, 14)
        md = rw.render_weekly_markdown(report)
        # CIL section should NOT appear when no CIL report exists
        assert "CIL 周报" not in md


# ──────────────────────────────────────────
# Test: weekday name
# ──────────────────────────────────────────


class TestWeekdayName:
    def test_monday(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw._weekday_name("2026-03-30") == "周一"

    def test_sunday(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw._weekday_name("2026-04-05") == "周日"

    def test_invalid(self, brain_env):
        rw = brain_env["report_weekly"]
        assert rw._weekday_name("invalid") == ""


# ──────────────────────────────────────────
# Test: notification
# ──────────────────────────────────────────


class TestNotification:
    def test_summary_max_10_lines(self, brain_env):
        rw = brain_env["report_weekly"]
        report = rw.WeeklyReport(
            week_label="2026-W14",
            start_date="2026-03-30",
            end_date="2026-04-05",
            this_week=rw.WeeklyOverview(
                total_done=10, total_created=15, total_blocked=2,
                total_quick=5, total_decisions=50,
            ),
            last_week=rw.WeeklyOverview(
                total_done=8, total_created=12, total_blocked=1,
                total_quick=3, total_decisions=40,
            ),
            done_tasks=[
                rw.WeeklyTaskSummary(id=f"T-{i}", title=f"Task {i}",
                                     status="done", priority="P1",
                                     task_type="standard-dev",
                                     created="", updated="")
                for i in range(10)
            ],
            type_distribution={"standard-dev": 8, "dev-fix": 2},
        )
        summary = rw._build_notification_summary(report)
        lines = summary.strip().split("\n")
        assert len(lines) <= 10

    def test_no_target_skips_notify(self, brain_env):
        rw = brain_env["report_weekly"]
        original = rw.NOTIFY_TARGET
        try:
            rw.NOTIFY_TARGET = ""
            report = rw.WeeklyReport(
                week_label="2026-W14",
                start_date="2026-03-30",
                end_date="2026-04-05",
            )
            assert rw.send_notification(report) is False
        finally:
            rw.NOTIFY_TARGET = original


# ──────────────────────────────────────────
# Test: CLI dry-run
# ──────────────────────────────────────────


class TestCLI:
    def test_dry_run(self, brain_env):
        """Test CLI --dry-run outputs markdown to stdout."""
        script = Path(__file__).resolve().parent / "report_weekly.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--dry-run", "--week", "2026-W14", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        assert "周报 2026-W14" in result.stdout

    def test_write_file(self, brain_env):
        """Test CLI writes report file."""
        script = Path(__file__).resolve().parent / "report_weekly.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--week", "2026-W14", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        report_file = brain_env["reports_dir"] / "2026-W14.md"
        assert report_file.exists()
        content = report_file.read_text(encoding="utf-8")
        assert "周报 2026-W14" in content

    def test_invalid_week(self, brain_env):
        """Test CLI rejects invalid week format."""
        script = Path(__file__).resolve().parent / "report_weekly.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--week", "not-a-week", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode != 0

    def test_default_week_is_previous(self, brain_env):
        """Test that default (no --week) generates previous week's report."""
        script = Path(__file__).resolve().parent / "report_weekly.py"
        env = os.environ.copy()
        env["BRAIN_DIR"] = str(brain_env["brain_dir"])

        result = subprocess.run(
            [sys.executable, str(script), "--dry-run", "--no-notify"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0
        # Should contain a week label in the output
        assert "周报" in result.stdout
