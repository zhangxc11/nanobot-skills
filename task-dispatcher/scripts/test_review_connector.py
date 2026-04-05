#!/usr/bin/env python3
"""
test_review_connector.py - Independent unit tests for review_connector.py

Isolation: each test sets BRAIN_DIR to a fresh tempdir and reloads both
brain_manager and review_connector so path constants are recalculated.
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

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _setup_modules(brain_dir: str):
    """Reload brain_manager (with new BRAIN_DIR) and review_connector; return both."""
    os.environ["BRAIN_DIR"] = brain_dir
    import brain_manager
    importlib.reload(brain_manager)
    import review_connector
    importlib.reload(review_connector)
    return brain_manager, review_connector


def _bm_run(bm, handler, **kwargs):
    """Call a brain_manager handler with synthetic Namespace; return parsed JSON."""
    args = argparse.Namespace(**kwargs)
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(args)
    return json.loads(buf.getvalue())


def _capture(func, *args):
    """Capture stdout from a review_connector function call."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args)
    return buf.getvalue()


# ──────────────────────────────────────────
# Base class
# ──────────────────────────────────────────

class RCTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rc_test_")
        self.bm, self.rc = _setup_modules(self.tmpdir)

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_task(self, title="Task", type_="quick", priority="P1"):
        return _bm_run(self.bm, self.bm.cmd_task_create,
                       title=title, type=type_, priority=priority, desc="")

    def _add_review(self, task_id, summary="needs review", prompt="please decide"):
        return _bm_run(self.bm, self.bm.cmd_review_add,
                       task_id=task_id, summary=summary, prompt=prompt)

    def _resolve_review(self, review_id, decision="approved", note=None):
        return _bm_run(self.bm, self.bm.cmd_review_resolve,
                       review_id=review_id, decision=decision, note=note)


# ──────────────────────────────────────────
# Tests: cmd_pending
# ──────────────────────────────────────────

class TestCmdPending(RCTestCase):

    def test_empty_reviews_shows_no_pending_message(self):
        out = _capture(self.rc.cmd_pending)
        self.assertIn("没有待审项", out)

    def test_single_review_appears_in_output(self):
        r = self._create_task(title="Pending Task")
        self._add_review(r["data"]["id"], summary="审核设计稿", prompt="设计稿在 Figma 上")
        out = _capture(self.rc.cmd_pending)
        self.assertIn("待审项列表", out)
        self.assertIn("审核设计稿", out)

    def test_task_id_appears_in_output(self):
        r = self._create_task(title="Task ID Check")
        task_id = r["data"]["id"]
        self._add_review(task_id, summary="review it", prompt="check it")
        out = _capture(self.rc.cmd_pending)
        self.assertIn(task_id, out)

    def test_review_id_appears_in_output(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"])
        review_id = r2["data"]["id"]
        out = _capture(self.rc.cmd_pending)
        self.assertIn(review_id, out)

    def test_multiple_reviews_all_listed(self):
        r = self._create_task(title="Multi Review Task")
        task_id = r["data"]["id"]
        self._add_review(task_id, summary="Review A", prompt="pa")
        self._add_review(task_id, summary="Review B", prompt="pb")
        out = _capture(self.rc.cmd_pending)
        self.assertIn("Review A", out)
        self.assertIn("Review B", out)

    def test_review_count_in_output(self):
        r = self._create_task()
        task_id = r["data"]["id"]
        self._add_review(task_id, summary="R1", prompt="p1")
        self._add_review(task_id, summary="R2", prompt="p2")
        out = _capture(self.rc.cmd_pending)
        self.assertIn("2", out)

    def test_next_step_guidance_always_shown(self):
        r = self._create_task()
        self._add_review(r["data"]["id"])
        out = _capture(self.rc.cmd_pending)
        self.assertIn("建议下一步操作", out)

    def test_operations_hints_shown(self):
        r = self._create_task()
        self._add_review(r["data"]["id"])
        out = _capture(self.rc.cmd_pending)
        self.assertIn("approved", out)
        self.assertIn("rejected", out)

    def test_resolved_reviews_not_listed(self):
        r = self._create_task()
        task_id = r["data"]["id"]
        r2 = self._add_review(task_id, summary="已解决的", prompt="p")
        self._resolve_review(r2["data"]["id"], decision="approved")
        out = _capture(self.rc.cmd_pending)
        self.assertIn("没有待审项", out)

    def test_wait_time_shown(self):
        r = self._create_task()
        self._add_review(r["data"]["id"])
        out = _capture(self.rc.cmd_pending)
        # wait time will be "0m" for just-created reviews
        self.assertRegex(out, r"\d+[mhd]")


# ──────────────────────────────────────────
# Tests: cmd_load
# ──────────────────────────────────────────

class TestCmdLoad(RCTestCase):

    def test_load_nonexistent_review_shows_error(self):
        out = _capture(self.rc.cmd_load, "R-00000000-999")
        self.assertIn("错误", out)

    def test_load_shows_review_context_header(self):
        r = self._create_task(title="Context Task")
        r2 = self._add_review(r["data"]["id"], summary="审核", prompt="检查一下")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("Review 上下文", out)

    def test_load_shows_summary(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"], summary="需要确认接口格式", prompt="p")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("需要确认接口格式", out)

    def test_load_shows_prompt(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="请检查 API 文档完整性")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("请检查 API 文档完整性", out)

    def test_load_shows_status(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("pending", out)

    def test_load_shows_task_info_section(self):
        r = self._create_task(title="Featured Task")
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("关联任务信息", out)
        self.assertIn("Featured Task", out)

    def test_load_shows_task_priority(self):
        r = self._create_task(title="Prio Task", priority="P0")
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("P0", out)

    def test_load_shows_review_steps_section(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"])
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("建议的 Review 步骤", out)

    def test_load_shows_operations_guide(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"])
        review_id = r2["data"]["id"]
        out = _capture(self.rc.cmd_load, review_id)
        self.assertIn("操作指令", out)
        self.assertIn("approved", out)
        self.assertIn("rejected", out)
        self.assertIn("deferred", out)

    def test_load_operations_contain_review_id(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"])
        review_id = r2["data"]["id"]
        out = _capture(self.rc.cmd_load, review_id)
        # The approve/reject/defer commands should include the review_id
        self.assertIn(review_id, out)

    def test_load_resolved_review_shows_resolved_status(self):
        r = self._create_task()
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        review_id = r2["data"]["id"]
        self._resolve_review(review_id, decision="approved")
        out = _capture(self.rc.cmd_load, review_id)
        self.assertIn("resolved", out)

    def test_load_files_section_shown_when_task_has_files(self):
        """If the task has context.files, they should appear in the output."""
        r = self._create_task(title="Files Task")
        task_id = r["data"]["id"]
        # Manually inject files into the task YAML
        task = self.bm.load_task(task_id)
        task["context"]["files"] = ["src/main.py", "docs/spec.md"]
        self.bm.save_task(task)

        r2 = self._add_review(task_id, summary="check files", prompt="look at src/main.py")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        self.assertIn("相关文件", out)
        self.assertIn("src/main.py", out)
        self.assertIn("docs/spec.md", out)

    def test_load_no_files_section_when_task_has_no_files(self):
        r = self._create_task(title="No Files Task")
        r2 = self._add_review(r["data"]["id"], summary="s", prompt="p")
        out = _capture(self.rc.cmd_load, r2["data"]["id"])
        # The section header "### 相关文件" should not appear when there are no files
        self.assertNotIn("### 相关文件", out)


# ──────────────────────────────────────────
# Tests: main() entry point
# ──────────────────────────────────────────

class TestMain(RCTestCase):

    def test_no_args_exits_with_error(self):
        original_argv = sys.argv
        sys.argv = ["review_connector.py"]
        try:
            with self.assertRaises(SystemExit) as cm:
                self.rc.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = original_argv

    def test_unknown_command_exits_with_error(self):
        original_argv = sys.argv
        sys.argv = ["review_connector.py", "unknown_cmd"]
        try:
            with self.assertRaises(SystemExit) as cm:
                self.rc.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = original_argv

    def test_pending_command_via_main(self):
        original_argv = sys.argv
        sys.argv = ["review_connector.py", "pending"]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.rc.main()
            out = buf.getvalue()
            self.assertIn("待审项", out)
        finally:
            sys.argv = original_argv

    def test_load_command_missing_id_exits_with_error(self):
        original_argv = sys.argv
        sys.argv = ["review_connector.py", "load"]
        try:
            with self.assertRaises(SystemExit) as cm:
                self.rc.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.argv = original_argv

    def test_load_command_via_main(self):
        r = self._create_task(title="Main Test Task")
        r2 = self._add_review(r["data"]["id"], summary="via main", prompt="test")
        review_id = r2["data"]["id"]
        original_argv = sys.argv
        sys.argv = ["review_connector.py", "load", review_id]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.rc.main()
            out = buf.getvalue()
            self.assertIn("via main", out)
        finally:
            sys.argv = original_argv


if __name__ == "__main__":
    unittest.main(verbosity=2)
