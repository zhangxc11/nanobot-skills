#!/usr/bin/env python3
"""
test_feishu_notify.py - Unit tests for feishu_notify.py

Tests cover:
  - Short ID extraction
  - Reply parsing (approve, reject, conditional, control, comment, no-match)
  - Action extraction
  - Notification formatting (all 5 scenes)
  - Short ID resolution (with mocked brain_manager)
  - CLI entry points
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
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

# Ensure scripts/ is on sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import feishu_notify as fn


# ─────────────────────────────────────────────
# Helper: reload brain_manager with temp dir
# ─────────────────────────────────────────────

def _reload_bm(brain_dir: str):
    """Set BRAIN_DIR env var and reload brain_manager; return the module."""
    os.environ["BRAIN_DIR"] = brain_dir
    import brain_manager
    importlib.reload(brain_manager)
    return brain_manager


def _create_task(bm, task_id: str, title: str = "Test Task", status: str = "queued"):
    """Directly create a task YAML file for testing."""
    task = {
        "id": task_id,
        "title": title,
        "type": "standard-dev",
        "priority": "P1",
        "status": status,
        "description": "Test task for feishu_notify tests",
        "created": datetime.now().astimezone().isoformat(),
        "updated": datetime.now().astimezone().isoformat(),
        "history": [],
        "context": {},
    }
    task_path = bm.TASKS_DIR / f"{task_id}.yaml"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    with task_path.open("w", encoding="utf-8") as f:
        yaml.dump(task, f, allow_unicode=True)
    return task


def _create_review(bm, review_id: str, task_id: str, summary: str = "needs review",
                   prompt: str = "please review", status: str = "pending"):
    """Directly create a review YAML file for testing."""
    review = {
        "id": review_id,
        "task_id": task_id,
        "summary": summary,
        "prompt": prompt,
        "status": status,
        "created": datetime.now().astimezone().isoformat(),
    }
    review_path = bm.REVIEWS_DIR / f"{review_id}.yaml"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w", encoding="utf-8") as f:
        yaml.dump(review, f, allow_unicode=True)
    return review


# ═════════════════════════════════════════════
# Test: extract_short_id
# ═════════════════════════════════════════════

class TestExtractShortId(unittest.TestCase):

    def test_task_id(self):
        self.assertEqual(fn.extract_short_id("T-20260330-001"), "T-001")

    def test_review_id(self):
        self.assertEqual(fn.extract_short_id("R-20260330-012"), "R-012")

    def test_already_short(self):
        self.assertEqual(fn.extract_short_id("T-001"), "T-001")

    def test_different_date(self):
        self.assertEqual(fn.extract_short_id("T-20250101-099"), "T-099")

    def test_three_digit_seq(self):
        self.assertEqual(fn.extract_short_id("T-20260330-100"), "T-100")


# ═════════════════════════════════════════════
# Test: extract_action
# ═════════════════════════════════════════════

class TestExtractAction(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(fn.extract_action(""), ("comment", ""))

    def test_go(self):
        self.assertEqual(fn.extract_action("Go"), ("approve", ""))

    def test_go_lowercase(self):
        self.assertEqual(fn.extract_action("go"), ("approve", ""))

    def test_nogo(self):
        action, comment = fn.extract_action("NoGo 测试不够")
        self.assertEqual(action, "reject")
        self.assertEqual(comment, "测试不够")

    def test_chinese_approve(self):
        self.assertEqual(fn.extract_action("通过")[0], "approve")

    def test_chinese_reject(self):
        action, comment = fn.extract_action("不行，需要改缓存逻辑")
        self.assertEqual(action, "reject")
        self.assertIn("缓存逻辑", comment)

    def test_conditional_approve_but(self):
        action, comment = fn.extract_action("Go 但需要补文档")
        self.assertEqual(action, "conditional_approve")
        self.assertIn("补文档", comment)

    def test_conditional_approve_however(self):
        action, comment = fn.extract_action("Go 不过要加日志")
        self.assertEqual(action, "conditional_approve")
        self.assertIn("加日志", comment)

    def test_pause(self):
        self.assertEqual(fn.extract_action("暂停"), ("pause", ""))

    def test_cancel(self):
        self.assertEqual(fn.extract_action("取消"), ("cancel", ""))

    def test_resume(self):
        self.assertEqual(fn.extract_action("继续"), ("resume", ""))

    def test_defer(self):
        self.assertEqual(fn.extract_action("推迟"), ("defer", ""))

    def test_lgtm(self):
        self.assertEqual(fn.extract_action("LGTM"), ("approve", ""))

    def test_ok(self):
        self.assertEqual(fn.extract_action("ok"), ("approve", ""))

    def test_approve_english(self):
        self.assertEqual(fn.extract_action("approve"), ("approve", ""))

    def test_reject_english(self):
        action, comment = fn.extract_action("reject need more tests")
        self.assertEqual(action, "reject")
        self.assertIn("more tests", comment)

    def test_unknown_keyword(self):
        action, comment = fn.extract_action("有问题 缓存过期时间不对")
        self.assertEqual(action, "comment")
        self.assertIn("缓存过期", comment)

    def test_no_go_hyphenated(self):
        action, _ = fn.extract_action("no-go")
        self.assertEqual(action, "reject")

    def test_打回(self):
        action, _ = fn.extract_action("打回")
        self.assertEqual(action, "reject")

    def test_不通过(self):
        action, _ = fn.extract_action("不通过")
        self.assertEqual(action, "reject")

    def test_没问题(self):
        action, _ = fn.extract_action("没问题")
        self.assertEqual(action, "approve")

    def test_可以(self):
        action, _ = fn.extract_action("可以")
        self.assertEqual(action, "approve")

    def test_go_with_comma_condition(self):
        """Go但xxx (no space before 但) should be conditional_approve."""
        action, comment = fn.extract_action("Go但要加单测")
        self.assertEqual(action, "conditional_approve")
        self.assertIn("加单测", comment)


# ═════════════════════════════════════════════
# Test: parse_task_reply (standalone mode, no brain_manager)
# ═════════════════════════════════════════════

class TestParseTaskReply(unittest.TestCase):
    """Tests using resolve_id=False (standalone, no brain_manager needed)."""

    def _parse(self, msg: str) -> fn.TaskReply | None:
        return fn.parse_task_reply(msg, resolve_id=False)

    # ── Approve variants ──

    def test_approve_go(self):
        r = self._parse("T-001 Go")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")
        self.assertEqual(r.raw_short_id, "T-001")
        self.assertEqual(r.ref_type, "task")
        self.assertTrue(r.task_id.endswith("-001"))

    def test_approve_chinese(self):
        r = self._parse("T-001 通过")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    def test_approve_lgtm(self):
        r = self._parse("T-002 LGTM")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    # ── Reject variants ──

    def test_reject_nogo(self):
        r = self._parse("T-001 NoGo 测试不够")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "reject")
        self.assertEqual(r.comment, "测试不够")

    def test_reject_chinese(self):
        r = self._parse("T-001 不行，需要改缓存逻辑")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "reject")
        self.assertIn("缓存逻辑", r.comment)

    def test_reject_不通过(self):
        r = self._parse("T-003 不通过 质量太差")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "reject")
        self.assertIn("质量太差", r.comment)

    # ── Conditional approve ──

    def test_conditional_approve(self):
        r = self._parse("T-001 Go 但下版本补测试")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "conditional_approve")
        self.assertIn("补测试", r.comment)

    def test_conditional_approve_however(self):
        r = self._parse("T-001 Go 不过要加日志")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "conditional_approve")
        self.assertIn("加日志", r.comment)

    # ── Control actions ──

    def test_pause(self):
        r = self._parse("T-001 暂停")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "pause")

    def test_cancel(self):
        r = self._parse("T-001 取消")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "cancel")

    def test_resume(self):
        r = self._parse("T-001 继续")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "resume")

    def test_defer(self):
        r = self._parse("T-001 推迟")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "defer")

    # ── Full ID ──

    def test_full_id(self):
        r = self._parse("T-20260330-001 Go")
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, "T-20260330-001")
        self.assertEqual(r.confidence, 1.0)
        self.assertEqual(r.action, "approve")

    # ── Review ID ──

    def test_review_id(self):
        r = self._parse("R-005 approve")
        self.assertIsNotNone(r)
        self.assertEqual(r.ref_type, "review")
        self.assertEqual(r.action, "approve")
        self.assertEqual(r.raw_short_id, "R-005")

    def test_review_full_id(self):
        r = self._parse("R-20260330-005 approve")
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, "R-20260330-005")
        self.assertEqual(r.ref_type, "review")
        self.assertEqual(r.confidence, 1.0)

    # ── No task reference (should return None) ──

    def test_no_ref_weather(self):
        self.assertIsNone(self._parse("今天天气怎么样"))

    def test_no_ref_script(self):
        self.assertIsNone(self._parse("帮我写个脚本"))

    def test_no_ref_empty(self):
        self.assertIsNone(self._parse(""))

    def test_no_ref_none(self):
        self.assertIsNone(fn.parse_task_reply(None, resolve_id=False))

    # ── Pure comment (T-xxx + unrecognized action) ──

    def test_pure_comment(self):
        r = self._parse("T-001 有问题 缓存过期时间不对")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "comment")
        self.assertIn("缓存过期", r.comment)

    # ── Leading whitespace / Chinese quotes ──

    def test_leading_whitespace(self):
        r = self._parse("  T-001 Go")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    def test_chinese_quote(self):
        r = self._parse("「T-001 Go")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    # ── Colon separator ──

    def test_colon_separator(self):
        r = self._parse("T-001: Go")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    def test_chinese_colon_separator(self):
        r = self._parse("T-001：通过")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")

    # ── Case insensitivity ──

    def test_lowercase_prefix(self):
        r = self._parse("t-001 go")
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "approve")
        self.assertEqual(r.ref_type, "task")

    # ── Middle reference should NOT match (anchored to start) ──

    def test_middle_ref_no_match(self):
        """T-xxx in the middle of a sentence should not be parsed."""
        r = self._parse("帮我查一下 T-001 的进度")
        self.assertIsNone(r)


# ═════════════════════════════════════════════
# Test: parse_task_reply with brain_manager (resolve_id=True)
# ═════════════════════════════════════════════

class TestParseWithResolve(unittest.TestCase):
    """Tests with actual brain_manager resolution against temp BRAIN_DIR."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fn_test_")
        self.bm = _reload_bm(self.tmpdir)
        # Reload feishu_notify so it picks up the reloaded brain_manager
        importlib.reload(fn)

        today = datetime.now().strftime("%Y%m%d")
        self.today = today
        self.task_id = f"T-{today}-001"
        self.task = _create_task(self.bm, self.task_id, title="Cache Optimization", status="review")
        self.review_id = f"R-{today}-001"
        self.review = _create_review(self.bm, self.review_id, self.task_id)

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resolve_short_to_today(self):
        r = fn.parse_task_reply("T-001 Go", resolve_id=True)
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, self.task_id)
        self.assertEqual(r.action, "approve")

    def test_resolve_review_short(self):
        r = fn.parse_task_reply("R-001 approve", resolve_id=True)
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, self.review_id)
        self.assertEqual(r.ref_type, "review")

    def test_resolve_full_id(self):
        r = fn.parse_task_reply(f"{self.task_id} Go", resolve_id=True)
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, self.task_id)
        self.assertEqual(r.confidence, 1.0)

    def test_resolve_nonexistent_returns_none(self):
        r = fn.parse_task_reply("T-999 Go", resolve_id=True)
        self.assertIsNone(r)

    def test_resolve_active_search(self):
        """If short ID doesn't match today, search active entities."""
        # Create a task with a different date
        old_task_id = "T-20260329-005"
        _create_task(self.bm, old_task_id, title="Old Task", status="executing")

        r = fn.parse_task_reply("T-005 Go", resolve_id=True)
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, old_task_id)

    def test_resolve_done_task_today_still_found(self):
        """Done tasks with today's date are still found (entity_exists checks file, not status).
        This is by design: short code resolves to the file on disk."""
        done_task_id = f"T-{self.today}-002"
        _create_task(self.bm, done_task_id, title="Done Task", status="done")

        r = fn.parse_task_reply("T-002 Go", resolve_id=True)
        # Today's task file exists, so it resolves even if done
        self.assertIsNotNone(r)
        self.assertEqual(r.task_id, done_task_id)

    def test_resolve_old_done_task_not_found(self):
        """Done tasks from other days are NOT found via active search."""
        old_done_id = "T-20260101-007"
        _create_task(self.bm, old_done_id, title="Old Done Task", status="done")

        r = fn.parse_task_reply("T-007 Go", resolve_id=True)
        # Not today, and done is not in active filter → None
        self.assertIsNone(r)


# ═════════════════════════════════════════════
# Test: resolve_short_id directly
# ═════════════════════════════════════════════

class TestResolveShortId(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fn_resolve_")
        self.bm = _reload_bm(self.tmpdir)
        importlib.reload(fn)

        self.today = datetime.now().strftime("%Y%m%d")
        self.task_id = f"T-{self.today}-003"
        _create_task(self.bm, self.task_id, status="executing")

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_id_passthrough(self):
        result = fn.resolve_short_id("T", f"{self.today}-003")
        self.assertEqual(result, self.task_id)

    def test_short_seq_today(self):
        result = fn.resolve_short_id("T", "003")
        self.assertEqual(result, self.task_id)

    def test_short_seq_not_found(self):
        result = fn.resolve_short_id("T", "999")
        self.assertIsNone(result)

    def test_review_resolve(self):
        review_id = f"R-{self.today}-001"
        _create_review(self.bm, review_id, self.task_id)
        result = fn.resolve_short_id("R", "001")
        self.assertEqual(result, review_id)


# ═════════════════════════════════════════════
# Test: format_review_notify
# ═════════════════════════════════════════════

class TestFormatReviewNotify(unittest.TestCase):

    def test_basic_format(self):
        task = {"id": "T-20260330-001", "title": "缓存命中率优化"}
        review = {"id": "R-20260330-001", "summary": "核心逻辑正确", "prompt": "请审查缓存模块"}

        text = fn.format_review_notify(task, review)
        self.assertIn("[T-001]", text)
        self.assertIn("缓存命中率优化", text)
        self.assertIn("Review 完成", text)
        self.assertIn("T-001 Go", text)
        self.assertIn("T-001 NoGo", text)
        self.assertIn("请审查缓存模块", text)
        self.assertIn("核心逻辑正确", text)
        self.assertIn(fn.SEPARATOR, text)

    def test_no_prompt(self):
        task = {"id": "T-20260330-002", "title": "API 限流"}
        review = {"id": "R-20260330-002", "summary": "看起来没问题", "prompt": ""}

        text = fn.format_review_notify(task, review)
        self.assertIn("[T-002]", text)
        self.assertIn("T-002 Go", text)

    def test_missing_fields(self):
        task = {"id": "T-20260330-003"}
        review = {"id": "R-20260330-003"}

        text = fn.format_review_notify(task, review)
        self.assertIn("[T-003]", text)
        self.assertIn("Unknown Task", text)


# ═════════════════════════════════════════════
# Test: format_status_change
# ═════════════════════════════════════════════

class TestFormatStatusChange(unittest.TestCase):

    def test_queued_to_executing(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_status_change(task, "queued", "executing")
        self.assertIn("[T-001]", text)
        self.assertIn("状态变更", text)
        self.assertIn("queued → executing", text)
        self.assertIn("调度器已派发", text)
        self.assertIn("T-001 暂停", text)

    def test_executing_to_review(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_status_change(task, "executing", "review")
        self.assertIn("开发完成", text)

    def test_generic_transition(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_status_change(task, "blocked", "queued")
        self.assertIn("blocked", text)
        self.assertIn("queued", text)


# ═════════════════════════════════════════════
# Test: format_done_notify
# ═════════════════════════════════════════════

class TestFormatDoneNotify(unittest.TestCase):

    def test_basic(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_done_notify(task)
        self.assertIn("[T-001]", text)
        self.assertIn("已完成", text)
        self.assertIn("T-001 有问题", text)

    def test_with_duration_and_artifacts(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_done_notify(task, duration="2h 15m", artifacts=["cache.py", "test_cache.py"])
        self.assertIn("2h 15m", text)
        self.assertIn("cache.py", text)
        self.assertIn("test_cache.py", text)
        self.assertIn("产出:", text)


# ═════════════════════════════════════════════
# Test: format_error_notify
# ═════════════════════════════════════════════

class TestFormatErrorNotify(unittest.TestCase):

    def test_basic(self):
        task = {"id": "T-20260330-001", "title": "缓存优化"}
        text = fn.format_error_notify(task, "依赖 T-002 未完成")
        self.assertIn("[T-001]", text)
        self.assertIn("执行阻塞", text)
        self.assertIn("依赖 T-002 未完成", text)
        self.assertIn("T-001 继续", text)
        self.assertIn("T-001 取消", text)


# ═════════════════════════════════════════════
# Test: format_batch_summary
# ═════════════════════════════════════════════

class TestFormatBatchSummary(unittest.TestCase):

    def test_full_summary(self):
        dispatched = [
            {"task_id": "T-20260330-001", "title": "缓存优化", "priority": "P1"},
            {"task_id": "T-20260330-003", "title": "API 限流", "priority": "P1"},
        ]
        review_pending = [
            {"task_id": "T-20260330-005", "summary": "探针采样过滤"},
        ]
        errors = [
            {"task_id": "T-20260330-002", "error": "blocked (依赖外部 API)"},
        ]

        text = fn.format_batch_summary(dispatched, review_pending, errors)
        self.assertIn("调度摘要", text)
        self.assertIn("新派发", text)
        self.assertIn("[T-001]", text)
        self.assertIn("[T-003]", text)
        self.assertIn("等待 Review", text)
        self.assertIn("[T-005]", text)
        self.assertIn("Go/NoGo", text)
        self.assertIn("需关注", text)
        self.assertIn("[T-002]", text)

    def test_empty_summary(self):
        text = fn.format_batch_summary()
        self.assertIn("调度摘要", text)
        self.assertIn("当前无需处理的事项", text)

    def test_dispatched_only(self):
        dispatched = [{"task_id": "T-20260330-001", "title": "Test", "priority": "P2"}]
        text = fn.format_batch_summary(dispatched=dispatched)
        self.assertIn("新派发", text)
        self.assertNotIn("等待 Review", text)
        self.assertNotIn("需关注", text)


# ═════════════════════════════════════════════
# Test: CLI parse command
# ═════════════════════════════════════════════

class TestCLIParse(unittest.TestCase):

    def _run_parse(self, message: str) -> dict:
        args = argparse.Namespace(message=message, no_resolve=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_parse(args)
        return json.loads(buf.getvalue())

    def test_parse_go(self):
        result = self._run_parse("T-001 Go")
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["data"])
        self.assertEqual(result["data"]["action"], "approve")
        self.assertEqual(result["data"]["raw_short_id"], "T-001")

    def test_parse_no_ref(self):
        result = self._run_parse("今天天气怎么样")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["data"])

    def test_parse_reject(self):
        result = self._run_parse("T-001 NoGo 测试不够")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["action"], "reject")
        self.assertEqual(result["data"]["comment"], "测试不够")


# ═════════════════════════════════════════════
# Test: CLI format commands (with brain_manager)
# ═════════════════════════════════════════════

class TestCLIFormat(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fn_cli_")
        self.bm = _reload_bm(self.tmpdir)
        importlib.reload(fn)

        today = datetime.now().strftime("%Y%m%d")
        self.task_id = f"T-{today}-001"
        self.task = _create_task(self.bm, self.task_id, title="CLI Test Task", status="review")
        self.review_id = f"R-{today}-001"
        self.review = _create_review(self.bm, self.review_id, self.task_id, summary="CLI review test")

    def tearDown(self):
        os.environ.pop("BRAIN_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_format_review_cli(self):
        args = argparse.Namespace(task_id=self.task_id, review_id=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_format_review(args)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("T-001", result["data"]["short_id"])
        self.assertIn("[T-001]", result["data"]["text"])

    def test_format_status_cli(self):
        args = argparse.Namespace(task_id=self.task_id, old_status="queued", new_status="executing")
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_format_status(args)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("状态变更", result["data"]["text"])

    def test_format_done_cli(self):
        args = argparse.Namespace(task_id=self.task_id, duration="1h", artifacts=["file.py"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_format_done(args)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("已完成", result["data"]["text"])

    def test_format_error_cli(self):
        args = argparse.Namespace(task_id=self.task_id, reason="dependency missing")
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_format_error(args)
        result = json.loads(buf.getvalue())
        self.assertTrue(result["ok"])
        self.assertIn("执行阻塞", result["data"]["text"])

    def test_format_review_not_found(self):
        args = argparse.Namespace(task_id="T-99999999-999", review_id=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn.cli_format_review(args)
        result = json.loads(buf.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


# ═════════════════════════════════════════════
# Test: Edge cases from design doc section 6
# ═════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_no_id_just_go(self):
        """User replies just 'Go' without T-xxx → returns None."""
        r = fn.parse_task_reply("Go", resolve_id=False)
        self.assertIsNone(r)

    def test_single_digit_seq(self):
        """T-1 should work (padded to 001)."""
        r = fn.parse_task_reply("T-1 Go", resolve_id=False)
        self.assertIsNotNone(r)
        self.assertTrue(r.task_id.endswith("-001"))

    def test_two_digit_seq(self):
        """T-12 should work (padded to 012)."""
        r = fn.parse_task_reply("T-12 Go", resolve_id=False)
        self.assertIsNotNone(r)
        self.assertTrue(r.task_id.endswith("-012"))

    def test_multiline_comment(self):
        """Multi-line reply should capture full comment."""
        msg = "T-001 不行\n需要改以下几点:\n1. 缓存过期\n2. 并发控制"
        r = fn.parse_task_reply(msg, resolve_id=False)
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "reject")
        self.assertIn("缓存过期", r.comment)
        self.assertIn("并发控制", r.comment)


if __name__ == "__main__":
    unittest.main()
