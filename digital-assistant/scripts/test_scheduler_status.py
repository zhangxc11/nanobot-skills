#!/usr/bin/env python3
"""
test_scheduler_status.py - Tests for get_scheduler_status() function.

Verifies that the function returns a dict with the correct keys and types:
  - version (str)
  - max_concurrent (int)
  - max_iterations_developer (int)
"""

import sys
from pathlib import Path

# Ensure scripts/ is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pytest

from scheduler import get_scheduler_status, SCHEDULER_VERSION, MAX_CONCURRENT_EXECUTING


class TestGetSchedulerStatus:
    """Tests for get_scheduler_status()."""

    def test_returns_dict(self):
        result = get_scheduler_status()
        assert isinstance(result, dict)

    def test_has_all_required_keys(self):
        result = get_scheduler_status()
        assert "version" in result
        assert "max_concurrent" in result
        assert "max_iterations_developer" in result

    def test_version_is_str(self):
        result = get_scheduler_status()
        assert isinstance(result["version"], str)

    def test_max_concurrent_is_int(self):
        result = get_scheduler_status()
        assert isinstance(result["max_concurrent"], int)

    def test_max_iterations_developer_is_int(self):
        result = get_scheduler_status()
        assert isinstance(result["max_iterations_developer"], int)

    def test_version_matches_constant(self):
        result = get_scheduler_status()
        assert result["version"] == SCHEDULER_VERSION

    def test_max_concurrent_matches_constant(self):
        result = get_scheduler_status()
        assert result["max_concurrent"] == MAX_CONCURRENT_EXECUTING

    def test_max_iterations_developer_value(self):
        """Developer role should have max_iterations = 60."""
        result = get_scheduler_status()
        assert result["max_iterations_developer"] == 60

    def test_no_extra_keys(self):
        """Ensure the dict contains exactly the three expected keys."""
        result = get_scheduler_status()
        assert set(result.keys()) == {"version", "max_concurrent", "max_iterations_developer"}
