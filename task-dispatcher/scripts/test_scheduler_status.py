#!/usr/bin/env python3
"""
test_scheduler_status.py - Tests for get_status() function (scheduler v2).

Verifies that the function returns a dict with the correct structure:
  {"ok": True, "data": {"version": str, "max_concurrent": int, ...}}
"""

import os
import sys
import tempfile
from pathlib import Path

# Ensure scripts/ is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pytest

from scheduler import get_status, SCHEDULER_VERSION, MAX_CONCURRENT


@pytest.fixture(autouse=True)
def isolated_brain(tmp_path, monkeypatch):
    """Create an isolated brain directory for each test."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "tasks").mkdir()
    (brain_dir / "reviews").mkdir()
    (brain_dir / "review-results").mkdir()

    monkeypatch.setenv("TASK_DATA_DIR", str(brain_dir))

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

    yield brain_dir


class TestGetStatus:
    """Tests for get_status() — v2 API."""

    def test_returns_dict(self):
        result = get_status()
        assert isinstance(result, dict)

    def test_ok_true(self):
        result = get_status()
        assert result.get("ok") is True

    def test_has_data_key(self):
        result = get_status()
        assert "data" in result

    def test_data_is_dict(self):
        result = get_status()
        assert isinstance(result["data"], dict)

    def test_data_has_version(self):
        result = get_status()
        assert "version" in result["data"]

    def test_data_has_max_concurrent(self):
        result = get_status()
        assert "max_concurrent" in result["data"]

    def test_data_has_available_slots(self):
        result = get_status()
        assert "available_slots" in result["data"]

    def test_data_has_task_counts(self):
        result = get_status()
        assert "task_counts" in result["data"]

    def test_version_is_str(self):
        result = get_status()
        assert isinstance(result["data"]["version"], str)

    def test_max_concurrent_is_int(self):
        result = get_status()
        assert isinstance(result["data"]["max_concurrent"], int)

    def test_version_matches_constant(self):
        result = get_status()
        assert result["data"]["version"] == SCHEDULER_VERSION

    def test_max_concurrent_matches_constant(self):
        result = get_status()
        assert result["data"]["max_concurrent"] == MAX_CONCURRENT

    def test_available_slots_empty_queue(self):
        """With no executing tasks, available_slots == MAX_CONCURRENT."""
        result = get_status()
        assert result["data"]["available_slots"] == MAX_CONCURRENT
