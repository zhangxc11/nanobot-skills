#!/usr/bin/env python3
"""
test_brain_stats.py - Unit tests for get_brain_stats()

Verifies that:
  1. All four expected keys exist in the returned dict
  2. All values are non-negative integers
  3. Counts are correct for an empty brain (no tasks)
  4. Counts are correct after creating tasks in various statuses
"""

import argparse
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

# Ensure scripts/ is on sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _reload_bm(brain_dir: str):
    """Set BRAIN_DIR env var and reload brain_manager; return the module."""
    os.environ["TASK_DATA_DIR"] = brain_dir
    import task_store as brain_manager
    importlib.reload(brain_manager)
    return brain_manager


def _run(bm, handler, **kwargs):
    """Call a command handler with a synthetic Namespace; return parsed JSON."""
    args = argparse.Namespace(**kwargs)
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(args)
    return json.loads(buf.getvalue())


EXPECTED_KEYS = {"total_tasks", "queued_count", "executing_count", "done_count"}


class TestGetBrainStats(unittest.TestCase):
    """Tests for brain_manager.get_brain_stats()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bm_stats_test_")
        self.bm = _reload_bm(self.tmpdir)

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── Helpers ──

    def _create_task(self, title="Task", type_="quick", priority="P1", desc=""):
        return _run(self.bm, self.bm.cmd_task_create,
                    title=title, type=type_, priority=priority, desc=desc)

    def _update_task(self, task_id, *, status=None, note=None, force=False):
        return _run(self.bm, self.bm.cmd_task_update,
                    task_id=task_id, status=status, title=None,
                    priority=None, note=note, force=force)

    def _assert_valid_stats(self, stats: dict):
        """Assert all four keys exist, are ints, and are non-negative."""
        self.assertIsInstance(stats, dict)
        for key in EXPECTED_KEYS:
            self.assertIn(key, stats, f"Missing key: {key}")
            self.assertIsInstance(stats[key], int, f"{key} should be int, got {type(stats[key])}")
            self.assertGreaterEqual(stats[key], 0, f"{key} should be >= 0, got {stats[key]}")

    # ── Test cases ──

    def test_empty_brain(self):
        """get_brain_stats() on a fresh (empty) brain returns all zeros."""
        stats = self.bm.get_brain_stats()
        self._assert_valid_stats(stats)
        self.assertEqual(stats["total_tasks"], 0)
        self.assertEqual(stats["queued_count"], 0)
        self.assertEqual(stats["executing_count"], 0)
        self.assertEqual(stats["done_count"], 0)

    def test_keys_and_types(self):
        """Returned dict has exactly the expected keys with int values >= 0."""
        # Create one task so we have a non-trivial result
        self._create_task(title="Alpha")
        stats = self.bm.get_brain_stats()
        self._assert_valid_stats(stats)
        # Should have exactly these keys (may have more in the future, but at least these)
        self.assertTrue(EXPECTED_KEYS.issubset(stats.keys()))

    def test_single_queued_task(self):
        """A newly created task is in 'queued' status."""
        self._create_task(title="Queued task")
        stats = self.bm.get_brain_stats()
        self._assert_valid_stats(stats)
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["queued_count"], 1)
        self.assertEqual(stats["executing_count"], 0)
        self.assertEqual(stats["done_count"], 0)

    def test_mixed_statuses(self):
        """Create multiple tasks and transition some; verify counts."""
        # Create 3 tasks (all start as queued)
        r1 = self._create_task(title="Task A")
        r2 = self._create_task(title="Task B")
        r3 = self._create_task(title="Task C")

        tid1 = r1["data"]["id"]
        tid2 = r2["data"]["id"]
        tid3 = r3["data"]["id"]

        # Move task A to executing
        self._update_task(tid1, status="executing")
        # Move task B to executing then done (force=True to bypass review gate)
        self._update_task(tid2, status="executing")
        self._update_task(tid2, status="done", force=True)

        stats = self.bm.get_brain_stats()
        self._assert_valid_stats(stats)
        self.assertEqual(stats["total_tasks"], 3)
        self.assertEqual(stats["queued_count"], 1)      # task C
        self.assertEqual(stats["executing_count"], 1)    # task A
        self.assertEqual(stats["done_count"], 1)         # task B

    def test_total_equals_sum_or_more(self):
        """total_tasks >= queued + executing + done (other statuses may exist)."""
        self._create_task(title="T1")
        r2 = self._create_task(title="T2")
        tid2 = r2["data"]["id"]
        # Move to blocked (not counted in queued/executing/done)
        self._update_task(tid2, status="blocked")

        stats = self.bm.get_brain_stats()
        self._assert_valid_stats(stats)
        self.assertEqual(stats["total_tasks"], 2)
        counted = stats["queued_count"] + stats["executing_count"] + stats["done_count"]
        self.assertGreaterEqual(stats["total_tasks"], counted)


if __name__ == "__main__":
    unittest.main()
