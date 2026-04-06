#!/usr/bin/env python3
"""
test_brain_manager.py - Unit tests for brain_manager.py

Isolation strategy:
  Each test sets BRAIN_DIR env var to a fresh tempdir, then reloads the
  brain_manager module so all path constants are recalculated.  tearDown
  removes the tempdir and clears the env var.
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
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# Ensure scripts/ is on sys.path so we can import task_store as brain_manager
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


# ─────────────────────────────────────────────
# Base class: fresh tempdir per test
# ─────────────────────────────────────────────

class BrainManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bm_test_")
        self.bm = _reload_bm(self.tmpdir)

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # Convenience wrappers
    def _create(self, title="Task", type_="quick", priority="P1", desc=""):
        return _run(self.bm, self.bm.cmd_task_create,
                    title=title, type=type_, priority=priority, desc=desc)

    def _update(self, task_id, *, status=None, title=None, priority=None, note=None, force=False):
        return _run(self.bm, self.bm.cmd_task_update,
                    task_id=task_id, status=status, title=title,
                    priority=priority, note=note, force=force)

    def _list_tasks(self, status=None):
        return _run(self.bm, self.bm.cmd_task_list, status=status)

    def _add_review(self, task_id, summary="needs review", prompt="please decide"):
        return _run(self.bm, self.bm.cmd_review_add,
                    task_id=task_id, summary=summary, prompt=prompt)

    def _list_reviews(self):
        return _run(self.bm, self.bm.cmd_review_list)

    def _resolve_review(self, review_id, decision="approved", note=None):
        return _run(self.bm, self.bm.cmd_review_resolve,
                    review_id=review_id, decision=decision, note=note)


# ─────────────────────────────────────────────
# 1. Task create + YAML validation
# ─────────────────────────────────────────────

class TestTaskCreate(BrainManagerTestCase):

    def test_create_returns_ok(self):
        result = self._create(title="Hello", type_="standard-dev", priority="P0")
        self.assertTrue(result["ok"])
        self.assertIn("id", result["data"])
        self.assertIn("task", result["data"])

    def test_yaml_file_exists_with_correct_fields(self):
        result = self._create(title="YAML Test", type_="quick", priority="P2", desc="desc here")
        task_id = result["data"]["id"]

        yaml_path = self.bm.TASKS_DIR / f"{task_id}.yaml"
        self.assertTrue(yaml_path.exists(), "YAML file not created")

        task = self.bm.load_task(task_id)
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["title"], "YAML Test")
        self.assertEqual(task["type"], "quick")
        self.assertEqual(task["priority"], "P2")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["description"], "desc here")
        self.assertIn("history", task)
        self.assertEqual(len(task["history"]), 1)
        self.assertEqual(task["history"][0]["action"], "created")

    def test_id_format(self):
        result = self._create()
        task_id = result["data"]["id"]
        # Format: T-YYYYMMDD-NNN
        parts = task_id.split("-")
        self.assertEqual(parts[0], "T")
        self.assertEqual(len(parts[1]), 8)   # YYYYMMDD
        self.assertTrue(parts[2].isdigit())


# ─────────────────────────────────────────────
# 2. State transitions
# ─────────────────────────────────────────────

class TestStateTransitions(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="Trans Task")
        self.task_id = r["data"]["id"]

    def test_valid_queued_to_executing(self):
        r = self._update(self.task_id, status="executing")
        self.assertTrue(r["ok"])
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "executing")

    def test_valid_executing_to_review(self):
        self._update(self.task_id, status="executing")
        r = self._update(self.task_id, status="review")
        self.assertTrue(r["ok"])

    def test_valid_executing_to_done(self):
        self._update(self.task_id, status="executing")
        # Default task gets L2 review level, so use --force to test pure FSM
        r = _run(self.bm, self.bm.cmd_task_update,
                 task_id=self.task_id, status="done", title=None,
                 priority=None, note=None, force=True)
        self.assertTrue(r["ok"])

    def test_valid_done_is_terminal(self):
        self._update(self.task_id, status="executing")
        _run(self.bm, self.bm.cmd_task_update,
             task_id=self.task_id, status="done", title=None,
             priority=None, note=None, force=True)
        r = self._update(self.task_id, status="cancelled")
        self.assertFalse(r["ok"])
        self.assertIn("terminal state", r["error"])

    def test_invalid_queued_to_done(self):
        r = self._update(self.task_id, status="done")
        self.assertFalse(r["ok"])
        self.assertIn("Invalid transition", r["error"])

    def test_invalid_queued_to_review(self):
        r = self._update(self.task_id, status="review")
        self.assertFalse(r["ok"])

    def test_history_appended_on_status_change(self):
        self._update(self.task_id, status="executing")
        task = self.bm.load_task(self.task_id)
        self.assertEqual(len(task["history"]), 2)
        # cmd_task_update now delegates to transition_task, which records "status_change"
        self.assertEqual(task["history"][1]["action"], "status_change")
        self.assertIn("queued → executing", task["history"][1]["detail"])

    def test_cancelled_can_reactivate_to_queued(self):
        self._update(self.task_id, status="cancelled")
        r = self._update(self.task_id, status="queued")
        self.assertTrue(r["ok"])

    def test_no_changes_returns_error(self):
        r = self._update(self.task_id)
        self.assertFalse(r["ok"])
        self.assertIn("No changes", r["error"])

    def test_unknown_task_returns_error(self):
        r = self._update("T-00000000-999", status="executing")
        self.assertFalse(r["ok"])


# ─────────────────────────────────────────────
# 3. Task list with active filter
# ─────────────────────────────────────────────

class TestTaskList(BrainManagerTestCase):

    def test_list_all(self):
        self._create(title="A")
        self._create(title="B")
        r = self._list_tasks()
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["count"], 2)

    def test_active_filter_excludes_done_and_cancelled(self):
        r_queued   = self._create(title="Queued")
        r_done     = self._create(title="Done")
        r_cancelled = self._create(title="Cancelled")

        # Move to done (force=True to bypass review level gate)
        done_id = r_done["data"]["id"]
        self._update(done_id, status="executing")
        self._update(done_id, status="done", force=True)

        # Move to cancelled
        cancelled_id = r_cancelled["data"]["id"]
        self._update(cancelled_id, status="cancelled")

        r = self._list_tasks(status="active")
        self.assertTrue(r["ok"])
        ids = [t["id"] for t in r["data"]["tasks"]]
        self.assertIn(r_queued["data"]["id"], ids)
        self.assertNotIn(done_id, ids)
        self.assertNotIn(cancelled_id, ids)

    def test_status_filter_exact(self):
        r1 = self._create(title="Q1")
        r2 = self._create(title="Q2")
        self._update(r2["data"]["id"], status="executing")

        r = self._list_tasks(status="queued")
        ids = [t["id"] for t in r["data"]["tasks"]]
        self.assertIn(r1["data"]["id"], ids)
        self.assertNotIn(r2["data"]["id"], ids)

    def test_empty_brain_dir(self):
        r = self._list_tasks()
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["count"], 0)


# ─────────────────────────────────────────────
# 4. Review add + list + resolve
# ─────────────────────────────────────────────

class TestReview(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="Review Target")
        self.task_id = r["data"]["id"]

    def test_add_review_creates_file(self):
        r = self._add_review(self.task_id, summary="Needs sign-off", prompt="Details here")
        self.assertTrue(r["ok"])
        review_id = r["data"]["id"]

        review_path = self.bm.REVIEWS_DIR / f"{review_id}.yaml"
        self.assertTrue(review_path.exists())

        review = self.bm.load_review(review_id)
        self.assertEqual(review["task_id"], self.task_id)
        self.assertEqual(review["status"], "pending")
        self.assertEqual(review["summary"], "Needs sign-off")

    def test_add_review_updates_task_metadata(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]

        task = self.bm.load_task(self.task_id)
        self.assertIn(review_id, task["review"]["items"])
        self.assertEqual(task["review"]["pending_count"], 1)

    def test_add_review_appends_task_history(self):
        self._add_review(self.task_id)
        task = self.bm.load_task(self.task_id)
        actions = [h["action"] for h in task["history"]]
        self.assertIn("review_added", actions)

    def test_list_pending_reviews(self):
        self._add_review(self.task_id, summary="Rev1")
        self._add_review(self.task_id, summary="Rev2")
        r = self._list_reviews()
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["count"], 2)

    def test_resolve_by_review_id(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]

        r2 = self._resolve_review(review_id, decision="approved", note="LGTM")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["data"]["decision"], "approved")

        review = self.bm.load_review(review_id)
        self.assertEqual(review["status"], "resolved")
        self.assertEqual(review["decision"], "approved")

    def test_resolve_by_task_id(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]

        r2 = self._resolve_review(self.task_id, decision="rejected")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["data"]["id"], review_id)

    def test_resolved_review_disappears_from_list(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]
        self._resolve_review(review_id, decision="approved")

        r2 = self._list_reviews()
        self.assertEqual(r2["data"]["count"], 0)

    def test_pending_count_decrements_on_resolve(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]
        self._resolve_review(review_id, decision="approved")

        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["review"]["pending_count"], 0)

    def test_resolve_already_resolved_returns_error(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]
        self._resolve_review(review_id, decision="approved")
        r2 = self._resolve_review(review_id, decision="rejected")
        self.assertFalse(r2["ok"])

    def test_review_for_missing_task_returns_error(self):
        r = self._add_review("T-00000000-999")
        self.assertFalse(r["ok"])


# ─────────────────────────────────────────────
# 5. BRIEFING generation
# ─────────────────────────────────────────────

class TestBriefing(BrainManagerTestCase):

    def test_briefing_update_creates_file(self):
        self._create(title="P0 Item", priority="P0")
        r = _run(self.bm, self.bm.cmd_briefing_update)
        self.assertTrue(r["ok"])
        self.assertTrue(self.bm.BRIEFING_FILE.exists())

    def test_briefing_max_50_lines(self):
        # Create enough tasks to potentially exceed 50 lines
        for i in range(20):
            self._create(title=f"Task {i}", priority="P0")
        r = _run(self.bm, self.bm.cmd_briefing_update)
        self.assertLessEqual(r["data"]["lines"], 50)

    def test_briefing_contains_sections(self):
        self._create(title="Urgent One", priority="P0")
        _run(self.bm, self.bm.cmd_briefing_update)
        content = self.bm.BRIEFING_FILE.read_text(encoding="utf-8")
        self.assertIn("Daily Briefing", content)
        self.assertIn("紧急事项", content)
        self.assertIn("进行中", content)

    def test_briefing_empty_brain(self):
        r = _run(self.bm, self.bm.cmd_briefing_update)
        self.assertTrue(r["ok"])
        self.assertGreater(r["data"]["lines"], 0)


# ─────────────────────────────────────────────
# 6. REGISTRY generation
# ─────────────────────────────────────────────

class TestRegistry(BrainManagerTestCase):

    def test_registry_update_creates_file(self):
        self._create(title="Active Task")
        r = _run(self.bm, self.bm.cmd_registry_update)
        self.assertTrue(r["ok"])
        self.assertTrue(self.bm.REGISTRY_FILE.exists())

    def test_registry_contains_headers(self):
        self._create(title="Reg Task")
        _run(self.bm, self.bm.cmd_registry_update)
        content = self.bm.REGISTRY_FILE.read_text(encoding="utf-8")
        self.assertIn("Task Registry", content)
        self.assertIn("Active Tasks", content)
        self.assertIn("Recently Completed", content)

    def test_registry_lists_active_task(self):
        r = self._create(title="Should Appear")
        task_id = r["data"]["id"]
        _run(self.bm, self.bm.cmd_registry_update)
        content = self.bm.REGISTRY_FILE.read_text(encoding="utf-8")
        self.assertIn(task_id, content)

    def test_registry_excludes_cancelled(self):
        r = self._create(title="Cancelled One")
        task_id = r["data"]["id"]
        self._update(task_id, status="cancelled")
        _run(self.bm, self.bm.cmd_registry_update)
        content = self.bm.REGISTRY_FILE.read_text(encoding="utf-8")
        # cancelled tasks are not in active_statuses
        self.assertNotIn(task_id, content.split("Active Tasks")[1].split("Recently")[0])

    def test_registry_empty_brain(self):
        r = _run(self.bm, self.bm.cmd_registry_update)
        self.assertTrue(r["ok"])


# ─────────────────────────────────────────────
# 7. review notify
# ─────────────────────────────────────────────

class TestReviewNotify(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="Notify Task", type_="standard-dev", priority="P1")
        self.task_id = r["data"]["id"]
        r2 = self._add_review(self.task_id, summary="需要审核方案", prompt="请检查方案是否合理")
        self.review_id = r2["data"]["id"]

    def test_notify_returns_expected_fields(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        self.assertTrue(r["ok"])
        data = r["data"]
        for field in ("feishu_card_content", "activation_prompt", "summary", "review_id", "task_id"):
            self.assertIn(field, data)

    def test_notify_feishu_card_contains_task_title(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        self.assertIn("Notify Task", r["data"]["feishu_card_content"])

    def test_notify_feishu_card_contains_review_id(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        # New format uses short task ID (e.g. [T-001]) instead of review ID in card content
        # The review_id is still in the data fields, just not in the card text
        data = r["data"]
        self.assertIn(data["review_id"], [self.review_id])
        # Card should contain short ID and reply hints
        card = data["feishu_card_content"]
        self.assertIn("[T-", card)
        self.assertIn("Go", card)
        self.assertIn("NoGo", card)

    def test_notify_with_specific_review_id(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=self.review_id)
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["review_id"], self.review_id)

    def test_notify_no_pending_reviews_returns_error(self):
        self._resolve_review(self.review_id, decision="approved")
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        self.assertFalse(r["ok"])

    def test_notify_unknown_task_returns_error(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id="T-00000000-999", review_id=None)
        self.assertFalse(r["ok"])

    def test_notify_wrong_review_id_for_task_returns_error(self):
        r2 = self._create(title="Other Task")
        r3 = self._add_review(r2["data"]["id"], summary="other", prompt="other")
        other_review_id = r3["data"]["id"]
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=other_review_id)
        self.assertFalse(r["ok"])

    def test_notify_activation_prompt_contains_review_id(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        self.assertIn(self.review_id, r["data"]["activation_prompt"])

    def test_notify_new_fields_short_id_and_reply_hint(self):
        r = _run(self.bm, self.bm.cmd_review_notify, task_id=self.task_id, review_id=None)
        data = r["data"]
        # New fields from feishu_notify integration
        self.assertIn("short_id", data)
        self.assertTrue(data["short_id"].startswith("T-"))
        self.assertIn("reply_hint", data)
        self.assertIn("Go", data["reply_hint"])
        self.assertIn("NoGo", data["reply_hint"])


# ─────────────────────────────────────────────
# 8. review list --format
# ─────────────────────────────────────────────

class TestReviewListFormats(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="Format Test Task", type_="standard-dev", priority="P1")
        self.task_id = r["data"]["id"]
        self._add_review(self.task_id, summary="需要决策", prompt="详细描述内容")

    def _list_fmt(self, fmt):
        return _run(self.bm, self.bm.cmd_review_list, format=fmt)

    def test_default_format_returns_full_records(self):
        r = self._list_fmt("default")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["count"], 1)
        review = r["data"]["reviews"][0]
        self.assertIn("prompt", review)

    def test_brief_format_has_waiting_field(self):
        r = self._list_fmt("brief")
        self.assertTrue(r["ok"])
        review = r["data"]["reviews"][0]
        for field in ("id", "task_id", "summary", "waiting"):
            self.assertIn(field, review)

    def test_brief_format_no_prompt_field(self):
        r = self._list_fmt("brief")
        review = r["data"]["reviews"][0]
        self.assertNotIn("prompt", review)

    def test_detail_format_has_task_info_and_actions(self):
        r = self._list_fmt("detail")
        self.assertTrue(r["ok"])
        review = r["data"]["reviews"][0]
        for field in ("task", "suggested_actions", "waiting", "prompt"):
            self.assertIn(field, review)

    def test_detail_format_task_info_has_title(self):
        r = self._list_fmt("detail")
        review = r["data"]["reviews"][0]
        self.assertEqual(review["task"]["title"], "Format Test Task")

    def test_detail_format_suggested_actions_correct(self):
        r = self._list_fmt("detail")
        review = r["data"]["reviews"][0]
        self.assertIn("approve", review["suggested_actions"])
        self.assertIn("reject", review["suggested_actions"])
        self.assertIn("defer", review["suggested_actions"])

    def test_invalid_format_returns_error(self):
        r = self._list_fmt("unknown")
        self.assertFalse(r["ok"])
        self.assertIn("Unknown format", r["error"])

    def test_no_format_defaults_to_full_records(self):
        # Passing format=None triggers "default" code path
        r = _run(self.bm, self.bm.cmd_review_list, format=None)
        self.assertTrue(r["ok"])
        self.assertIn("reviews", r["data"])


# ─────────────────────────────────────────────
# 9. review resolve enhanced
# ─────────────────────────────────────────────

class TestReviewResolveEnhanced(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="Resolve Test Task", type_="standard-dev", priority="P1")
        self.task_id = r["data"]["id"]
        self._update(self.task_id, status="executing")
        self._update(self.task_id, status="review")

    def test_resolve_result_has_task_status_changed_field(self):
        r = self._add_review(self.task_id)
        result = self._resolve_review(r["data"]["id"], decision="approved")
        self.assertTrue(result["ok"])
        self.assertIn("task_status_changed", result["data"])

    def test_resolve_all_approved_auto_transitions_to_executing(self):
        r = self._add_review(self.task_id, summary="Rev 1", prompt="p1")
        result = self._resolve_review(r["data"]["id"], decision="approved")
        self.assertTrue(result["data"]["task_status_changed"])
        self.assertEqual(result["data"]["new_task_status"], "executing")
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "executing")

    def test_resolve_rejected_auto_transitions_to_revision(self):
        r = self._add_review(self.task_id)
        result = self._resolve_review(r["data"]["id"], decision="rejected")
        self.assertTrue(result["data"]["task_status_changed"])
        self.assertEqual(result["data"]["new_task_status"], "revision")
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "revision")

    def test_resolve_deferred_no_auto_transition(self):
        r = self._add_review(self.task_id)
        result = self._resolve_review(r["data"]["id"], decision="deferred")
        self.assertFalse(result["data"]["task_status_changed"])
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "review")

    def test_resolve_partial_reviews_no_auto_transition(self):
        r1 = self._add_review(self.task_id, summary="Rev 1", prompt="p1")
        self._add_review(self.task_id, summary="Rev 2", prompt="p2")
        result = self._resolve_review(r1["data"]["id"], decision="approved")
        self.assertFalse(result["data"]["task_status_changed"])
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "review")

    def test_resolve_updates_briefing_file(self):
        r = self._add_review(self.task_id)
        self._resolve_review(r["data"]["id"], decision="approved")
        self.assertTrue(self.bm.BRIEFING_FILE.exists())

    def test_resolve_history_has_auto_status_change_entry(self):
        r = self._add_review(self.task_id)
        self._resolve_review(r["data"]["id"], decision="approved")
        task = self.bm.load_task(self.task_id)
        actions = [h["action"] for h in task["history"]]
        self.assertIn("auto_status_change", actions)

    def test_resolve_history_has_review_resolved_entry(self):
        r = self._add_review(self.task_id)
        review_id = r["data"]["id"]
        self._resolve_review(review_id, decision="approved")
        task = self.bm.load_task(self.task_id)
        actions = [h["action"] for h in task["history"]]
        self.assertIn("review_resolved", actions)

    def test_new_task_status_none_when_deferred(self):
        r = self._add_review(self.task_id)
        result = self._resolve_review(r["data"]["id"], decision="deferred")
        self.assertIsNone(result["data"]["new_task_status"])


# ─────────────────────────────────────────────
# 10. review_connector integration
# ─────────────────────────────────────────────

class TestReviewConnector(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        import review_connector
        importlib.reload(review_connector)
        self.rc = review_connector

    def _capture(self, func, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            func(*args)
        return buf.getvalue()

    def test_pending_no_reviews_shows_empty_message(self):
        out = self._capture(self.rc.cmd_pending)
        self.assertIn("没有待审项", out)

    def test_pending_with_reviews_lists_summary(self):
        r = self._create(title="RC Test Task")
        task_id = r["data"]["id"]
        self._add_review(task_id, summary="需要决策", prompt="检查逻辑")
        out = self._capture(self.rc.cmd_pending)
        self.assertIn("待审项列表", out)
        self.assertIn("需要决策", out)
        self.assertIn(task_id, out)

    def test_pending_shows_next_step_guidance(self):
        r = self._create(title="Guidance Task")
        self._add_review(r["data"]["id"])
        out = self._capture(self.rc.cmd_pending)
        self.assertIn("建议下一步操作", out)

    def test_pending_shows_review_count(self):
        r = self._create(title="Count Task")
        task_id = r["data"]["id"]
        self._add_review(task_id, summary="Rev 1", prompt="p1")
        self._add_review(task_id, summary="Rev 2", prompt="p2")
        out = self._capture(self.rc.cmd_pending)
        self.assertIn("2", out)

    def test_load_nonexistent_review_shows_error(self):
        out = self._capture(self.rc.cmd_load, "R-00000000-999")
        self.assertIn("错误", out)

    def test_load_existing_review_shows_context(self):
        r = self._create(title="Load Test Task")
        r2 = self._add_review(r["data"]["id"], summary="审核接口设计", prompt="检查 API 文档")
        out = self._capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("Review 上下文", out)
        self.assertIn("审核接口设计", out)
        self.assertIn("检查 API 文档", out)

    def test_load_shows_task_info(self):
        r = self._create(title="Task With Context")
        r2 = self._add_review(r["data"]["id"], summary="审核", prompt="prompt")
        out = self._capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("关联任务信息", out)
        self.assertIn("Task With Context", out)

    def test_load_shows_operations_guide(self):
        r = self._create(title="Guide Task")
        r2 = self._add_review(r["data"]["id"])
        review_id = r2["data"]["id"]
        out = self._capture(self.rc.cmd_load, review_id)
        self.assertIn("操作指令", out)
        self.assertIn("approved", out)
        self.assertIn("rejected", out)

    def test_load_resolved_review_shows_resolved_status(self):
        r = self._create(title="Resolved Task")
        r2 = self._add_review(r["data"]["id"], summary="done", prompt="ok")
        review_id = r2["data"]["id"]
        self._resolve_review(review_id, decision="approved")
        out = self._capture(self.rc.cmd_load, review_id)
        self.assertIn("resolved", out)



# ─────────────────────────────────────────────
# 11. Blocked and Revision states
# ─────────────────────────────────────────────

class TestBlockedAndRevisionStates(BrainManagerTestCase):

    def setUp(self):
        super().setUp()
        r = self._create(title="State Test")
        self.task_id = r["data"]["id"]

    def test_queued_to_blocked(self):
        r = self._update(self.task_id, status="blocked")
        self.assertTrue(r["ok"])
        self.assertEqual(self.bm.load_task(self.task_id)["status"], "blocked")

    def test_executing_to_blocked(self):
        self._update(self.task_id, status="executing")
        r = self._update(self.task_id, status="blocked")
        self.assertTrue(r["ok"])

    def test_blocked_to_executing(self):
        self._update(self.task_id, status="blocked")
        r = self._update(self.task_id, status="executing")
        self.assertTrue(r["ok"])

    def test_blocked_to_queued(self):
        self._update(self.task_id, status="blocked")
        r = self._update(self.task_id, status="queued")
        self.assertTrue(r["ok"])

    def test_review_to_revision(self):
        self._update(self.task_id, status="executing")
        self._update(self.task_id, status="review")
        r = self._update(self.task_id, status="revision")
        self.assertTrue(r["ok"])

    def test_revision_to_executing(self):
        self._update(self.task_id, status="executing")
        self._update(self.task_id, status="review")
        self._update(self.task_id, status="revision")
        r = self._update(self.task_id, status="executing")
        self.assertTrue(r["ok"])

    def test_queued_to_dropped(self):
        r = self._update(self.task_id, status="dropped")
        self.assertTrue(r["ok"])

    def test_dropped_can_reactivate(self):
        self._update(self.task_id, status="dropped")
        r = self._update(self.task_id, status="queued")
        self.assertTrue(r["ok"])

    def test_invalid_revision_to_done(self):
        self._update(self.task_id, status="executing")
        self._update(self.task_id, status="review")
        self._update(self.task_id, status="revision")
        r = self._update(self.task_id, status="done")
        self.assertFalse(r["ok"])

    def test_review_reject_auto_transitions_to_revision(self):
        self._update(self.task_id, status="executing")
        self._update(self.task_id, status="review")
        r = self._add_review(self.task_id, summary="check", prompt="check it")
        result = self._resolve_review(r["data"]["id"], decision="rejected")
        self.assertTrue(result["data"]["task_status_changed"])
        self.assertEqual(result["data"]["new_task_status"], "revision")
        task = self.bm.load_task(self.task_id)
        self.assertEqual(task["status"], "revision")


# ─────────────────────────────────────────────
# 12. Decisions log
# ─────────────────────────────────────────────

class TestDecisionsLog(BrainManagerTestCase):

    def test_review_resolve_creates_decision_entry(self):
        r = self._create(title="Dec Test")
        task_id = r["data"]["id"]
        self._update(task_id, status="executing")
        self._update(task_id, status="review")
        r2 = self._add_review(task_id, summary="s", prompt="p")
        self._resolve_review(r2["data"]["id"], decision="approved")
        entries = self.bm.list_decisions()
        resolve_entries = [e for e in entries if e.get("type") == "review_resolve"]
        self.assertTrue(len(resolve_entries) >= 1)

    def test_status_change_creates_decision_entry(self):
        r = self._create(title="Status Dec")
        task_id = r["data"]["id"]
        self._update(task_id, status="executing")
        entries = self.bm.list_decisions()
        status_entries = [e for e in entries if e.get("type") == "status_change"]
        self.assertTrue(len(status_entries) >= 1)
        self.assertEqual(status_entries[-1]["from"], "queued")
        self.assertEqual(status_entries[-1]["to"], "executing")

    def test_decisions_list_command(self):
        r = self._create(title="List Dec")
        self._update(r["data"]["id"], status="executing")
        result = _run(self.bm, self.bm.cmd_decisions_list, limit=10)
        self.assertTrue(result["ok"])
        self.assertGreater(result["data"]["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ─────────────────────────────────────────────
# 13. Cross-Check: Review Level Determination
# ─────────────────────────────────────────────

class TestReviewLevel(BrainManagerTestCase):

    def test_quick_template_returns_L0(self):
        r = self._create(title="Check weather", type_="quick", priority="P2")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "quick"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L0")

    def test_cron_auto_template_returns_L0(self):
        r = self._create(title="Daily report", type_="cron-auto", priority="P2",
                         desc="定时日报")
        task = self.bm.load_task(r["data"]["id"])
        # Force template to cron-auto
        task["workgroup"]["template"] = "cron-auto"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L0")

    def test_batch_dev_returns_L3(self):
        r = self._create(title="Batch dev", type_="batch-dev", priority="P1",
                         desc="批量开发5条需求")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "batch-dev"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_P0_priority_returns_L3(self):
        r = self._create(title="Critical fix", type_="standard-dev", priority="P0")
        task = self.bm.load_task(r["data"]["id"])
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_architecture_change_returns_L3(self):
        r = self._create(title="Refactor core", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["involves_architecture_change"] = True
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_simple_standard_dev_returns_L1(self):
        r = self._create(title="Fix typo", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        task["files_changed"] = 1
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L1")

    def test_standard_dev_many_files_returns_L2(self):
        r = self._create(title="Big change", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        task["files_changed"] = 5
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L2")

    def test_interface_change_upgrades_to_L2(self):
        r = self._create(title="API change", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        task["files_changed"] = 1
        task["involves_interface_change"] = True
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L2")

    def test_default_files_changed_returns_L2(self):
        """When files_changed is not set, defaults to 99 → L2."""
        r = self._create(title="Normal dev", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L2")

    def test_cmd_review_level(self):
        r = self._create(title="Level test", type_="quick", priority="P2")
        task_id = r["data"]["id"]
        # Force template to quick for deterministic test
        task = self.bm.load_task(task_id)
        task["workgroup"]["template"] = "quick"
        self.bm.save_task(task)
        result = _run(self.bm, self.bm.cmd_review_level, task_id=task_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["review_level"], "L0")
        self.assertIn("recommended_roles", result["data"])

    def test_cmd_review_level_unknown_task(self):
        result = _run(self.bm, self.bm.cmd_review_level, task_id="T-00000000-999")
        self.assertFalse(result["ok"])

    def test_l3_external_publish(self):
        """involves_external_publish: true should trigger L3."""
        r = self._create(title="Publish API docs", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["involves_external_publish"] = True
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_l3_financial_logic(self):
        """involves_financial_logic: true should trigger L3."""
        r = self._create(title="Payment module", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["involves_financial_logic"] = True
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")


# ─────────────────────────────────────────────
# 14. Cross-Check: Review Roles
# ─────────────────────────────────────────────

class TestReviewRoles(BrainManagerTestCase):

    def test_L0_no_roles(self):
        self.assertEqual(self.bm.get_review_roles("L0", {}), [])

    def test_L1_no_roles(self):
        self.assertEqual(self.bm.get_review_roles("L1", {}), [])

    def test_L2_standard_dev_code_reviewer(self):
        task = {"workgroup": {"template": "standard-dev"}}
        roles = self.bm.get_review_roles("L2", task)
        self.assertEqual(roles, ["code_reviewer"])

    def test_L2_long_task_test_verifier(self):
        task = {"workgroup": {"template": "long-task"}}
        roles = self.bm.get_review_roles("L2", task)
        self.assertEqual(roles, ["test_verifier"])

    def test_L3_includes_code_and_test(self):
        roles = self.bm.get_review_roles("L3", {})
        self.assertIn("code_reviewer", roles)
        self.assertIn("test_verifier", roles)

    def test_L3_financial_includes_safety(self):
        task = {"involves_financial_logic": True}
        roles = self.bm.get_review_roles("L3", task)
        self.assertIn("safety_checker", roles)


# ─────────────────────────────────────────────
# 15. Cross-Check: Checklist Loading
# ─────────────────────────────────────────────

class TestChecklistLoading(BrainManagerTestCase):

    def test_load_code_review_checklist(self):
        cl = self.bm.load_checklist("code_reviewer")
        self.assertEqual(cl["name"], "code_review")
        self.assertGreater(len(cl["items"]), 10)

    def test_load_test_verify_checklist(self):
        cl = self.bm.load_checklist("test_verifier")
        self.assertEqual(cl["name"], "test_verify")
        self.assertGreater(len(cl["items"]), 5)

    def test_load_safety_check_checklist(self):
        cl = self.bm.load_checklist("safety_checker")
        self.assertEqual(cl["name"], "safety_check")
        self.assertGreater(len(cl["items"]), 5)

    def test_load_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            self.bm.load_checklist("unknown_role")

    def test_checklist_items_have_required_fields(self):
        cl = self.bm.load_checklist("code_reviewer")
        for item in cl["items"]:
            self.assertIn("id", item)
            self.assertIn("category", item)
            self.assertIn("weight", item)
            self.assertIn("question", item)
            self.assertIn("fail_severity", item)
            self.assertIn("na_allowed", item)

    def test_cmd_review_checklist(self):
        r = self._create(title="CL test", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result = _run(self.bm, self.bm.cmd_review_checklist,
                      task_id=task_id, role="code_reviewer")
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(data["task_id"], task_id)
        self.assertIn("items", data)
        self.assertIn("review_level", data)

    def test_cmd_review_checklist_unknown_task(self):
        result = _run(self.bm, self.bm.cmd_review_checklist,
                      task_id="T-00000000-999", role="code_reviewer")
        self.assertFalse(result["ok"])


# ─────────────────────────────────────────────
# 16. Cross-Check: Validate Review Result
# ─────────────────────────────────────────────

class TestValidateReviewResult(BrainManagerTestCase):

    def _valid_result(self):
        return {
            "review_id": "R-20260330-001",
            "reviewer_role": "code_reviewer",
            "verdict": "go",
            "checklist_results": [
                {"id": "CR-01", "result": "pass", "note": "ok"},
            ],
            "issues": [],
        }

    def test_valid_result_passes(self):
        valid, errors = self.bm.validate_review_result(self._valid_result())
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)

    def test_missing_verdict(self):
        r = self._valid_result()
        del r["verdict"]
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)
        self.assertTrue(any("verdict" in e for e in errors))

    def test_invalid_verdict(self):
        r = self._valid_result()
        r["verdict"] = "maybe"
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)

    def test_missing_checklist_results(self):
        r = self._valid_result()
        del r["checklist_results"]
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)

    def test_missing_issues(self):
        r = self._valid_result()
        del r["issues"]
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)

    def test_invalid_issue_severity(self):
        r = self._valid_result()
        r["issues"] = [{"severity": "extreme", "description": "bad"}]
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)

    def test_invalid_checklist_result_value(self):
        r = self._valid_result()
        r["checklist_results"] = [{"id": "CR-01", "result": "maybe"}]
        valid, errors = self.bm.validate_review_result(r)
        self.assertFalse(valid)


# ─────────────────────────────────────────────
# 17. Cross-Check: Auto Judge
# ─────────────────────────────────────────────

class TestAutoJudge(BrainManagerTestCase):

    def test_all_go(self):
        results = [
            {"verdict": "go", "issues": []},
            {"verdict": "go", "issues": []},
        ]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "go")

    def test_single_go(self):
        results = [{"verdict": "go", "issues": []}]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "go")

    def test_critical_issue_no_go(self):
        results = [
            {"verdict": "go", "issues": []},
            {"verdict": "conditional_go", "issues": [
                {"severity": "critical", "description": "data loss risk"}
            ]},
        ]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "no_go")
        self.assertIn("Critical", j["reason"])

    def test_two_major_issues_no_go(self):
        results = [
            {"verdict": "conditional_go", "issues": [
                {"severity": "major", "description": "missing validation"}
            ]},
            {"verdict": "conditional_go", "issues": [
                {"severity": "major", "description": "no error handling"}
            ]},
        ]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "no_go")

    def test_one_major_needs_arbitration(self):
        results = [
            {"verdict": "go", "issues": []},
            {"verdict": "conditional_go", "issues": [
                {"severity": "major", "description": "needs more tests"}
            ]},
        ]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "needs_arbitration")

    def test_all_no_go(self):
        results = [
            {"verdict": "no_go", "issues": [{"severity": "minor", "description": "x"}]},
            {"verdict": "no_go", "issues": [{"severity": "minor", "description": "y"}]},
        ]
        j = self.bm.auto_judge_review(results)
        self.assertEqual(j["verdict"], "no_go")

    def test_empty_results(self):
        j = self.bm.auto_judge_review([])
        self.assertEqual(j["verdict"], "needs_arbitration")


# ─────────────────────────────────────────────
# 18. Cross-Check: Submit Review Result CLI
# ─────────────────────────────────────────────

class TestReviewSubmit(BrainManagerTestCase):

    def _write_result_file(self, data: dict) -> str:
        path = os.path.join(self.tmpdir, "result.yaml")
        with open(path, "w") as f:
            yaml.dump(data, f, allow_unicode=True)
        return path

    def test_submit_valid_result(self):
        r = self._create(title="Submit test", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result_data = {
            "review_id": "R-test-001",
            "reviewer_role": "code_reviewer",
            "verdict": "go",
            "checklist_results": [{"id": "CR-01", "result": "pass"}],
            "issues": [],
        }
        path = self._write_result_file(result_data)
        result = _run(self.bm, self.bm.cmd_review_submit,
                      task_id=task_id, result_file=path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verdict"], "go")
        self.assertIn("auto_judgment", result["data"])

    def test_submit_invalid_schema(self):
        r = self._create(title="Invalid submit", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result_data = {"verdict": "maybe"}  # missing required fields
        path = self._write_result_file(result_data)
        result = _run(self.bm, self.bm.cmd_review_submit,
                      task_id=task_id, result_file=path)
        self.assertFalse(result["ok"])
        self.assertIn("Schema validation failed", result["error"])

    def test_submit_nonexistent_file(self):
        r = self._create(title="No file", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result = _run(self.bm, self.bm.cmd_review_submit,
                      task_id=task_id, result_file="/nonexistent/path.yaml")
        self.assertFalse(result["ok"])

    def test_submit_updates_task_history(self):
        r = self._create(title="History test", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result_data = {
            "review_id": "R-test-002",
            "reviewer_role": "code_reviewer",
            "verdict": "conditional_go",
            "checklist_results": [{"id": "CR-01", "result": "pass"}],
            "issues": [{"severity": "minor", "description": "style issue"}],
        }
        path = self._write_result_file(result_data)
        _run(self.bm, self.bm.cmd_review_submit,
             task_id=task_id, result_file=path)
        task = self.bm.load_task(task_id)
        actions = [h["action"] for h in task["history"]]
        self.assertIn("structured_review_submitted", actions)

    def test_submit_unknown_task(self):
        result_data = {
            "review_id": "R-test-003",
            "reviewer_role": "code_reviewer",
            "verdict": "go",
            "checklist_results": [],
            "issues": [],
        }
        path = self._write_result_file(result_data)
        result = _run(self.bm, self.bm.cmd_review_submit,
                      task_id="T-00000000-999", result_file=path)
        self.assertFalse(result["ok"])

    def test_submit_auto_judgment_with_critical(self):
        r = self._create(title="Critical test", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]
        result_data = {
            "review_id": "R-test-004",
            "reviewer_role": "code_reviewer",
            "verdict": "no_go",
            "checklist_results": [{"id": "CR-01", "result": "fail"}],
            "issues": [{"severity": "critical", "description": "data corruption risk"}],
        }
        path = self._write_result_file(result_data)
        result = _run(self.bm, self.bm.cmd_review_submit,
                      task_id=task_id, result_file=path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["auto_judgment"]["verdict"], "no_go")

    def test_submit_multiple_results_auto_judge(self):
        """Submit two review results for the same task; auto_judgment should
        reflect both results combined."""
        r = self._create(title="Multi-result test", type_="standard-dev", priority="P1")
        task_id = r["data"]["id"]

        # First result: go from code_reviewer
        result1 = {
            "review_id": "R-multi-001",
            "reviewer_role": "code_reviewer",
            "verdict": "go",
            "checklist_results": [{"id": "CR-01", "result": "pass"}],
            "issues": [],
        }
        path1 = self._write_result_file(result1)
        res1 = _run(self.bm, self.bm.cmd_review_submit,
                     task_id=task_id, result_file=path1)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["data"]["total_results"], 1)

        # Second result: no_go from test_verifier with a major issue
        result2 = {
            "review_id": "R-multi-002",
            "reviewer_role": "test_verifier",
            "verdict": "no_go",
            "checklist_results": [{"id": "TV-01", "result": "fail"}],
            "issues": [{"severity": "major", "description": "test failure"}],
        }
        path2 = self._write_result_file(result2)
        res2 = _run(self.bm, self.bm.cmd_review_submit,
                     task_id=task_id, result_file=path2)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["data"]["total_results"], 2)

        # Auto-judgment should incorporate both results: 1 go + 1 no_go with
        # 1 major → needs_arbitration (not all go, not all no_go, <2 major)
        judgment = res2["data"]["auto_judgment"]
        self.assertIn(judgment["verdict"], ("needs_arbitration",))
        # Verify task structured_results list has both entries
        task = self.bm.load_task(task_id)
        self.assertEqual(len(task["review"]["structured_results"]), 2)


# ─────────────────────────────────────────────
# 19. Cross-Check: Checklist Files Integrity
# ─────────────────────────────────────────────

class TestChecklistFiles(BrainManagerTestCase):

    def test_code_review_file_exists(self):
        path = self.bm.CHECKLISTS_DIR / "code_review.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")

    def test_test_verify_file_exists(self):
        path = self.bm.CHECKLISTS_DIR / "test_verify.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")

    def test_safety_check_file_exists(self):
        path = self.bm.CHECKLISTS_DIR / "safety_check.yaml"
        self.assertTrue(path.exists(), f"Missing: {path}")

    def test_code_review_has_15_items(self):
        cl = self.bm.load_checklist("code_reviewer")
        self.assertEqual(len(cl["items"]), 15)

    def test_test_verify_has_10_items(self):
        cl = self.bm.load_checklist("test_verifier")
        self.assertEqual(len(cl["items"]), 10)

    def test_safety_check_has_8_items(self):
        cl = self.bm.load_checklist("safety_checker")
        self.assertEqual(len(cl["items"]), 8)

    def test_all_checklists_have_version(self):
        for role in ("code_reviewer", "test_verifier", "safety_checker"):
            cl = self.bm.load_checklist(role)
            self.assertIn("version", cl)
            self.assertEqual(cl["version"], "1.0")


# ─────────────────────────────────────────────
# 20. list_overdue_reviews helper
# ─────────────────────────────────────────────

class TestListOverdueReviews(BrainManagerTestCase):

    def test_no_overdue_reviews(self):
        """Fresh reviews should not be flagged as overdue."""
        r = self._create(title="Fresh Task")
        self._add_review(r["data"]["id"], summary="fresh", prompt="p")
        overdue = self.bm.list_overdue_reviews(threshold_hours=48)
        self.assertEqual(len(overdue), 0)

    def test_overdue_review_detected(self):
        """A review older than 48h should be detected."""
        r = self._create(title="Old Task")
        r2 = self._add_review(r["data"]["id"], summary="old review", prompt="p")
        review_id = r2["data"]["id"]
        # Manipulate timestamp to be 72h ago
        review = self.bm.load_review(review_id)
        old_ts = (datetime.now().astimezone() - timedelta(hours=72)).replace(microsecond=0).isoformat()
        review["created"] = old_ts
        self.bm.save_review(review)
        overdue = self.bm.list_overdue_reviews(threshold_hours=48)
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0]["id"], review_id)
        self.assertIn("overdue_hours", overdue[0])

    def test_custom_threshold(self):
        """Custom threshold should work correctly."""
        r = self._create(title="Threshold Task")
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        review_id = r2["data"]["id"]
        # Set to 5h ago
        review = self.bm.load_review(review_id)
        old_ts = (datetime.now().astimezone() - timedelta(hours=5)).replace(microsecond=0).isoformat()
        review["created"] = old_ts
        self.bm.save_review(review)
        # 4h threshold → overdue
        overdue_4h = self.bm.list_overdue_reviews(threshold_hours=4)
        self.assertEqual(len(overdue_4h), 1)
        # 6h threshold → not overdue
        overdue_6h = self.bm.list_overdue_reviews(threshold_hours=6)
        self.assertEqual(len(overdue_6h), 0)

    def test_resolved_reviews_not_included(self):
        """Resolved reviews should not appear in overdue list."""
        r = self._create(title="Resolved Task")
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        review_id = r2["data"]["id"]
        # Make it old
        review = self.bm.load_review(review_id)
        old_ts = (datetime.now().astimezone() - timedelta(hours=72)).replace(microsecond=0).isoformat()
        review["created"] = old_ts
        self.bm.save_review(review)
        # Resolve it
        self._resolve_review(review_id, decision="approved")
        overdue = self.bm.list_overdue_reviews(threshold_hours=48)
        self.assertEqual(len(overdue), 0)


# ─────────────────────────────────────────────
# 21. Daily Maintenance
# ─────────────────────────────────────────────

class TestDailyMaintenance(BrainManagerTestCase):

    def _run_maintenance(self):
        return _run(self.bm, self.bm.cmd_daily_maintenance)

    def test_daily_maintenance_returns_ok(self):
        result = self._run_maintenance()
        self.assertTrue(result["ok"])
        self.assertIn("stats", result["data"])
        self.assertIn("overdue_reviews", result["data"])

    def test_daily_maintenance_archives_old_quick_entries(self):
        """Old quick entries should be archived to YYYY-MM-DD.jsonl files."""
        old_ts = (datetime.now().astimezone() - timedelta(hours=36)).replace(microsecond=0).isoformat()
        old_date = old_ts[:10]  # YYYY-MM-DD
        entry = {"id": "Q-old-001", "title": "Old task", "result": "", "timestamp": old_ts}
        self.bm.append_quick_log(entry)

        result = self._run_maintenance()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["stats"]["quick_archived"], 1)

        # Check archive file exists with daily granularity
        archive_file = self.bm.QUICK_ARCHIVE_DIR / f"{old_date}.jsonl"
        self.assertTrue(archive_file.exists(), f"Archive file {archive_file} should exist")
        content = archive_file.read_text(encoding="utf-8")
        self.assertIn("Q-old-001", content)

    def test_daily_maintenance_keeps_today_quick_entries(self):
        """Today's quick entries should remain in quick-log."""
        today_ts = datetime.now().astimezone().replace(microsecond=0).isoformat()
        entry = {"id": "Q-today-001", "title": "Today task", "result": "", "timestamp": today_ts}
        self.bm.append_quick_log(entry)

        result = self._run_maintenance()
        self.assertEqual(result["data"]["stats"]["quick_archived"], 0)
        self.assertEqual(result["data"]["stats"]["quick_tasks_today"], 1)

        # Verify entry still in quick-log
        entries = self.bm.list_quick_log()
        ids = [e["id"] for e in entries]
        self.assertIn("Q-today-001", ids)

    def test_daily_maintenance_detects_overdue_reviews(self):
        """Reviews pending > 48h should appear in overdue list."""
        r = self._create(title="Overdue Task")
        r2 = self._add_review(r["data"]["id"], summary="old review", prompt="p")
        review_id = r2["data"]["id"]
        # Make it 72h old
        review = self.bm.load_review(review_id)
        old_ts = (datetime.now().astimezone() - timedelta(hours=72)).replace(microsecond=0).isoformat()
        review["created"] = old_ts
        self.bm.save_review(review)

        result = self._run_maintenance()
        self.assertEqual(result["data"]["stats"]["reviews_overdue"], 1)
        self.assertEqual(len(result["data"]["overdue_reviews"]), 1)
        self.assertEqual(result["data"]["overdue_reviews"][0]["id"], review_id)

    def test_daily_maintenance_no_overdue_when_fresh(self):
        """Fresh reviews should not be flagged as overdue."""
        r = self._create(title="Fresh Task")
        self._add_review(r["data"]["id"], summary="fresh", prompt="p")

        result = self._run_maintenance()
        self.assertEqual(result["data"]["stats"]["reviews_overdue"], 0)
        self.assertEqual(len(result["data"]["overdue_reviews"]), 0)

    def test_daily_maintenance_updates_briefing(self):
        """BRIEFING.md should be updated after maintenance."""
        self._create(title="Briefing Task", priority="P0")
        result = self._run_maintenance()
        self.assertTrue(self.bm.BRIEFING_FILE.exists())
        self.assertGreater(result["data"]["briefing_lines"], 0)

    def test_daily_maintenance_logs_decision(self):
        """A decision entry with type=daily_maintenance should be created."""
        self._run_maintenance()
        entries = self.bm.list_decisions()
        maint_entries = [e for e in entries if e.get("type") == "daily_maintenance"]
        self.assertEqual(len(maint_entries), 1)
        self.assertIn("stats", maint_entries[0])

    def test_daily_maintenance_stats_correct(self):
        """All stat counts should be accurate."""
        # Create tasks in various states
        r1 = self._create(title="Active1")  # queued
        r2 = self._create(title="Active2")
        self._update(r2["data"]["id"], status="executing")
        r3 = self._create(title="Done1")
        self._update(r3["data"]["id"], status="executing")
        self._update(r3["data"]["id"], status="done", force=True)

        # Create a review
        self._add_review(r1["data"]["id"], summary="rev", prompt="p")

        # Create a quick task for today
        today_ts = datetime.now().astimezone().replace(microsecond=0).isoformat()
        self.bm.append_quick_log({"id": "Q-stat-001", "title": "Quick", "result": "", "timestamp": today_ts})

        result = self._run_maintenance()
        stats = result["data"]["stats"]
        self.assertEqual(stats["tasks_active"], 2)  # queued + executing
        self.assertEqual(stats["tasks_done_today"], 1)
        self.assertEqual(stats["reviews_pending"], 1)
        self.assertEqual(stats["quick_tasks_today"], 1)

    def test_daily_maintenance_empty_brain(self):
        """Should work fine with no tasks/reviews/quick entries."""
        result = self._run_maintenance()
        self.assertTrue(result["ok"])
        stats = result["data"]["stats"]
        self.assertEqual(stats["tasks_done_today"], 0)
        self.assertEqual(stats["tasks_active"], 0)
        self.assertEqual(stats["reviews_pending"], 0)
        self.assertEqual(stats["reviews_overdue"], 0)
        self.assertEqual(stats["quick_tasks_today"], 0)
        self.assertEqual(stats["quick_archived"], 0)


# ─────────────────────────────────────────────
# 22. Daily Report (read-only)
# ─────────────────────────────────────────────

class TestDailyReport(BrainManagerTestCase):

    def _run_report(self):
        return _run(self.bm, self.bm.cmd_daily_report)

    def test_daily_report_returns_ok(self):
        result = self._run_report()
        self.assertTrue(result["ok"])
        self.assertIn("stats", result["data"])

    def test_daily_report_counts_active_tasks(self):
        self._create(title="Q1")  # queued
        r2 = self._create(title="E1")
        self._update(r2["data"]["id"], status="executing")

        result = self._run_report()
        stats = result["data"]["stats"]
        self.assertEqual(stats["tasks_active"], 2)
        self.assertEqual(stats["tasks_queued"], 1)
        self.assertEqual(stats["tasks_executing"], 1)

    def test_daily_report_counts_done_today(self):
        r = self._create(title="Done Today")
        self._update(r["data"]["id"], status="executing")
        self._update(r["data"]["id"], status="done", force=True)

        result = self._run_report()
        self.assertEqual(result["data"]["stats"]["tasks_done_today"], 1)

    def test_daily_report_detects_overdue_reviews(self):
        r = self._create(title="Overdue")
        r2 = self._add_review(r["data"]["id"], summary="old", prompt="p")
        review = self.bm.load_review(r2["data"]["id"])
        old_ts = (datetime.now().astimezone() - timedelta(hours=72)).replace(microsecond=0).isoformat()
        review["created"] = old_ts
        self.bm.save_review(review)

        result = self._run_report()
        self.assertEqual(result["data"]["stats"]["reviews_overdue"], 1)
        self.assertEqual(len(result["data"]["overdue_reviews"]), 1)

    def test_daily_report_is_read_only(self):
        """Report should not modify any files (no BRIEFING update, no archiving)."""
        # Create an old quick entry
        old_ts = (datetime.now().astimezone() - timedelta(hours=36)).replace(microsecond=0).isoformat()
        self.bm.append_quick_log({"id": "Q-ro-001", "title": "Old", "result": "", "timestamp": old_ts})

        # Record state before
        quick_before = self.bm.list_quick_log()
        briefing_existed = self.bm.BRIEFING_FILE.exists()
        decisions_before = self.bm.list_decisions()

        self._run_report()

        # Verify no mutations
        quick_after = self.bm.list_quick_log()
        self.assertEqual(len(quick_before), len(quick_after), "Quick log should not be modified")
        self.assertEqual(briefing_existed, self.bm.BRIEFING_FILE.exists(),
                         "BRIEFING.md existence should not change")
        decisions_after = self.bm.list_decisions()
        self.assertEqual(len(decisions_before), len(decisions_after),
                         "Decisions log should not be modified")

    def test_daily_report_empty_brain(self):
        result = self._run_report()
        self.assertTrue(result["ok"])
        stats = result["data"]["stats"]
        for key in ("tasks_done_today", "tasks_active", "reviews_pending", "reviews_overdue"):
            self.assertEqual(stats[key], 0)


# ─────────────────────────────────────────────
# 23. Round 1 Fix: Review Level Priority (Fix 2)
# ─────────────────────────────────────────────

class TestReviewLevelPriorityFix(BrainManagerTestCase):

    def test_p0_cron_auto_returns_L3(self):
        """P0 + cron-auto should return L3, not L0 (the bug fix)."""
        r = self._create(title="Critical cron", type_="cron-auto", priority="P0")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "cron-auto"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_p2_cron_auto_still_L0(self):
        """P2 + cron-auto should still return L0."""
        r = self._create(title="Normal cron", type_="cron-auto", priority="P2")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "cron-auto"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L0")

    def test_architecture_change_cron_auto_returns_L3(self):
        """Architecture change + cron-auto should return L3."""
        r = self._create(title="Arch cron", type_="cron-auto", priority="P2")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "cron-auto"
        task["involves_architecture_change"] = True
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_p0_quick_returns_L3(self):
        """P0 + quick should return L3 (P0 overrides quick template)."""
        r = self._create(title="P0 quick", type_="quick", priority="P0")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "quick"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L3")

    def test_p2_quick_still_L0(self):
        """P2 + quick should still return L0."""
        r = self._create(title="Normal quick", type_="quick", priority="P2")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "quick"
        self.bm.save_task(task)
        self.assertEqual(self.bm.determine_review_level(task), "L0")


# ─────────────────────────────────────────────
# 24. Round 1 Fix: FSM Review Gate (Fix 1)
# ─────────────────────────────────────────────

class TestFSMReviewGate(BrainManagerTestCase):
    """Tests for the auditor gate (v10): executing→done requires auditor pass."""

    def _inject_auditor_pass(self, task_id: str):
        """Helper: inject auditor pass into orchestration history."""
        task = self.bm.load_task(task_id)
        orch = task.setdefault("orchestration", {})
        orch.setdefault("history", []).append({
            "type": "completion",
            "role": "auditor",
            "verdict": "pass",
            "summary": "test auditor pass",
            "timestamp": self.bm.now_iso(),
        })
        self.bm.save_task(task)

    def test_executing_to_done_blocked_without_auditor(self):
        """Any task should not transition executing→done without auditor pass."""
        r = self._create(title="No auditor task", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self.bm.transition_task(task["id"], "executing")
        with self.assertRaises(ValueError) as ctx:
            self.bm.transition_task(task["id"], "done")
        self.assertIn("Auditor", str(ctx.exception))

    def test_L2_executing_to_review_allowed(self):
        """L2 task can transition executing→review."""
        r = self._create(title="L2 task", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self.bm.transition_task(task["id"], "executing")
        updated = self.bm.transition_task(task["id"], "review")
        self.assertEqual(updated["status"], "review")

    def test_executing_to_done_allowed_with_auditor(self):
        """Task with auditor pass can transition executing→done."""
        r = self._create(title="Audited task", type_="quick", priority="P2")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "quick"
        self.bm.save_task(task)
        self.bm.transition_task(task["id"], "executing")
        self._inject_auditor_pass(task["id"])
        updated = self.bm.transition_task(task["id"], "done")
        self.assertEqual(updated["status"], "done")

    def test_force_executing_to_done_allowed(self):
        """Task can transition executing→done with force=True (no auditor needed)."""
        r = self._create(title="Force done", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self.bm.transition_task(task["id"], "executing")
        updated = self.bm.transition_task(task["id"], "done", force=True)
        self.assertEqual(updated["status"], "done")

    def test_P0_executing_to_done_blocked_without_auditor(self):
        """P0 task should not transition executing→done without auditor."""
        r = self._create(title="P0 task", type_="standard-dev", priority="P0")
        task = self.bm.load_task(r["data"]["id"])
        self.bm.transition_task(task["id"], "executing")
        with self.assertRaises(ValueError):
            self.bm.transition_task(task["id"], "done")

    def test_review_to_done_not_affected(self):
        """review→done should not be affected by the auditor gate."""
        r = self._create(title="Review done", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self.bm.transition_task(task["id"], "executing")
        self.bm.transition_task(task["id"], "review")
        updated = self.bm.transition_task(task["id"], "done")
        self.assertEqual(updated["status"], "done")

    def test_cmd_executing_to_done_blocked_without_auditor(self):
        """CLI: executing→done without auditor should fail."""
        r = self._create(title="CLI no auditor", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self._update(r["data"]["id"], status="executing")
        result = self._update(r["data"]["id"], status="done")
        self.assertFalse(result["ok"])
        self.assertIn("Auditor", result["error"])

    def test_cmd_force_done_allowed(self):
        """CLI: --status done --force should succeed without auditor."""
        r = self._create(title="CLI force", type_="standard-dev", priority="P1")
        task = self.bm.load_task(r["data"]["id"])
        task["workgroup"]["template"] = "standard-dev"
        self.bm.save_task(task)
        self._update(r["data"]["id"], status="executing")
        result = self._update(r["data"]["id"], status="done", force=True)
        self.assertTrue(result["ok"])


# ─────────────────────────────────────────────
# 25. Round 1 Fix: executing→queued protection + force audit
# ─────────────────────────────────────────────

class TestExecutingToQueuedProtection(BrainManagerTestCase):

    def test_executing_to_queued_blocked_without_force(self):
        """executing→queued should be blocked without --force."""
        r = self._create(title="Protect test")
        self.bm.transition_task(r["data"]["id"], "executing")
        with self.assertRaises(ValueError) as ctx:
            self.bm.transition_task(r["data"]["id"], "queued")
        self.assertIn("blocked", str(ctx.exception))

    def test_executing_to_queued_allowed_with_force(self):
        """executing→queued should work with force=True (timeout recovery)."""
        r = self._create(title="Force queue")
        self.bm.transition_task(r["data"]["id"], "executing")
        updated = self.bm.transition_task(r["data"]["id"], "queued", force=True, note="timeout")
        self.assertEqual(updated["status"], "queued")

    def test_force_override_audit_logged(self):
        """Using force=True should create a force_override decision entry."""
        r = self._create(title="Audit test")
        self.bm.transition_task(r["data"]["id"], "executing")
        self.bm.transition_task(r["data"]["id"], "queued", force=True, note="timeout recovery")
        entries = self.bm.list_decisions()
        force_entries = [e for e in entries if e.get("type") == "force_override"]
        self.assertTrue(len(force_entries) >= 1)
        self.assertEqual(force_entries[-1]["task_id"], r["data"]["id"])
        self.assertEqual(force_entries[-1]["from"], "executing")
        self.assertEqual(force_entries[-1]["to"], "queued")

    def test_cmd_executing_to_queued_blocked(self):
        """CLI: executing→queued without --force should fail."""
        r = self._create(title="CLI protect")
        self._update(r["data"]["id"], status="executing")
        result = self._update(r["data"]["id"], status="queued")
        self.assertFalse(result["ok"])

    def test_cmd_executing_to_queued_force(self):
        """CLI: executing→queued with --force should succeed."""
        r = self._create(title="CLI force q")
        self._update(r["data"]["id"], status="executing")
        result = self._update(r["data"]["id"], status="queued", force=True)
        self.assertTrue(result["ok"])
