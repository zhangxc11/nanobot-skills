#!/usr/bin/env python3
"""
test_templates.py — Tests for Phase 1.2 workgroup template system.

Tests template loading, matching, quick channel, and task create with template.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ──────────────────────────────────────────
# Setup
# ──────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
BRAIN_MANAGER = SCRIPT_DIR / "brain_manager.py"
TEMPLATES_DIR = SCRIPT_DIR.parent / "templates"

# Use temp brain dir for tests to avoid polluting real data
TEST_BRAIN_DIR = None


def setup_test_brain():
    """Create a temporary brain directory for testing."""
    global TEST_BRAIN_DIR
    TEST_BRAIN_DIR = Path(tempfile.mkdtemp(prefix="test_brain_"))
    (TEST_BRAIN_DIR / "tasks").mkdir(parents=True)
    (TEST_BRAIN_DIR / "reviews").mkdir(parents=True)
    return TEST_BRAIN_DIR


def teardown_test_brain():
    """Remove the temporary brain directory."""
    global TEST_BRAIN_DIR
    if TEST_BRAIN_DIR and TEST_BRAIN_DIR.exists():
        shutil.rmtree(TEST_BRAIN_DIR)
    TEST_BRAIN_DIR = None


def run_cmd(*args) -> dict:
    """Run brain_manager.py with BRAIN_DIR env override, return parsed JSON."""
    env = os.environ.copy()
    if TEST_BRAIN_DIR:
        env["BRAIN_DIR"] = str(TEST_BRAIN_DIR)
    
    cmd = [sys.executable, str(BRAIN_MANAGER)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"No output from command: {' '.join(args)}\nstderr: {result.stderr}")
    
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Invalid JSON output: {stdout}\nstderr: {result.stderr}")


# ──────────────────────────────────────────
# Test cases
# ──────────────────────────────────────────

passed = 0
failed = 0
errors = []


def _check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        msg = f"  ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(msg)


def test_template_files_exist():
    """All 5 template YAML files exist."""
    print("\n📁 Template Files")
    expected = ["quick.yaml", "standard-dev.yaml", "batch-dev.yaml", "long-task.yaml", "cron-auto.yaml"]
    for fname in expected:
        path = TEMPLATES_DIR / fname
        _check(f"File exists: {fname}", path.exists(), f"Expected at {path}")


def test_template_list():
    """template list returns all 5 templates."""
    print("\n📋 Template List")
    result = run_cmd("template", "list")
    _check("template list ok", result.get("ok") is True, json.dumps(result))
    
    data = result.get("data", {})
    _check("count is 5", data.get("count") == 5, f"got {data.get('count')}")
    
    names = {t["name"] for t in data.get("templates", [])}
    expected = {"quick", "standard-dev", "batch-dev", "long-task", "cron-auto"}
    _check("all template names present", names == expected, f"got {names}")


def test_template_show():
    """template show returns correct details."""
    print("\n🔍 Template Show")
    for name in ["quick", "standard-dev", "batch-dev", "long-task", "cron-auto"]:
        result = run_cmd("template", "show", name)
        _check(f"show {name} ok", result.get("ok") is True)
        data = result.get("data", {})
        _check(f"show {name} has name", data.get("name") == name, f"got {data.get('name')}")
        _check(f"show {name} has matching", "matching" in data)
        _check(f"show {name} has execution", "execution" in data)
    
    # Non-existent template
    result = run_cmd("template", "show", "nonexistent")
    _check("show nonexistent fails", result.get("ok") is False)


def test_template_matching():
    """template match returns expected templates for various inputs."""
    print("\n🎯 Template Matching")
    
    test_cases = [
        ("查明天日程", "", "quick"),
        ("查天气", "", "quick"),
        ("帮我看一下这个文件", "", "quick"),
        ("开发 Cache 优化功能", "", "standard-dev"),
        ("修复登录页面的 bug", "", "standard-dev"),
        ("实现用户认证模块", "", "standard-dev"),
        ("批量开发 5 条积压需求", "", "batch-dev"),
        ("排查 tushare 数据异常的根因", "", "long-task"),
        ("调查为什么内存泄漏", "", "long-task"),
        ("设置 CIL 日报定时任务", "", "cron-auto"),
        ("配置每天自动执行的 cron 任务", "", "cron-auto"),
    ]
    
    for title, desc, expected_template in test_cases:
        args = ["template", "match", "--title", title]
        if desc:
            args += ["--desc", desc]
        result = run_cmd(*args)
        _check(f"match ok: '{title}'", result.get("ok") is True)
        matched = result.get("data", {}).get("template", "")
        _check(
            f"'{title}' → {expected_template}",
            matched == expected_template,
            f"got '{matched}'"
        )


def test_quick_log():
    """quick log creates entries correctly."""
    print("\n⚡ Quick Log")
    setup_test_brain()
    try:
        # Log first entry
        result = run_cmd("quick", "log", "--title", "查天气", "--result", "北京晴 25°C")
        _check("quick log ok", result.get("ok") is True)
        entry = result.get("data", {}).get("entry", {})
        _check("entry has id", entry.get("id", "").startswith("Q-"))
        _check("entry has title", entry.get("title") == "查天气")
        _check("entry has result", entry.get("result") == "北京晴 25°C")
        _check("entry has timestamp", "timestamp" in entry)
        
        # Log second entry
        result2 = run_cmd("quick", "log", "--title", "查日程", "--result", "明天3个会议")
        entry2 = result2.get("data", {}).get("entry", {})
        _check("second entry has different id", entry2.get("id") != entry.get("id"))
        
        # List
        result3 = run_cmd("quick", "list")
        _check("quick list ok", result3.get("ok") is True)
        _check("quick list count is 2", result3.get("data", {}).get("count") == 2, 
             f"got {result3.get('data', {}).get('count')}")
    finally:
        teardown_test_brain()


def test_quick_archive():
    """quick archive moves old entries to monthly files."""
    print("\n📦 Quick Archive")
    setup_test_brain()
    try:
        # Create some entries
        run_cmd("quick", "log", "--title", "task1", "--result", "done1")
        run_cmd("quick", "log", "--title", "task2", "--result", "done2")
        
        # Verify entries exist
        result = run_cmd("quick", "list")
        _check("entries exist before archive", result.get("data", {}).get("count") == 2)
        
        # Archive (today's entries should remain since they're from today)
        result = run_cmd("quick", "archive")
        _check("archive ok", result.get("ok") is True)
        data = result.get("data", {})
        _check("nothing archived (all from today)", data.get("archived") == 0, f"got {data.get('archived')}")
        # Today's entries should still be listable
        result_list = run_cmd("quick", "list")
        _check("today's entries remain after archive", result_list.get("data", {}).get("count") == 2,
             f"got {result_list.get('data', {}).get('count')}")
    finally:
        teardown_test_brain()


def test_task_create_with_template():
    """task create with --template stores template info."""
    print("\n🏗️ Task Create with Template")
    setup_test_brain()
    try:
        # Manual template
        result = run_cmd("task", "create", "--title", "Build auth module",
                        "--type", "standard-dev", "--priority", "P1",
                        "--template", "standard-dev")
        _check("create with manual template ok", result.get("ok") is True)
        task = result.get("data", {}).get("task", {})
        wg = task.get("workgroup", {})
        _check("workgroup.template is standard-dev", wg.get("template") == "standard-dev")
        mi = wg.get("match_info", {})
        _check("match_info.confidence is 1.0 (manual)", mi.get("confidence") == 1.0)
        
        # Auto-match template
        result2 = run_cmd("task", "create", "--title", "排查内存泄漏根因",
                         "--type", "long-task", "--priority", "P0")
        _check("create with auto-match ok", result2.get("ok") is True)
        task2 = result2.get("data", {}).get("task", {})
        wg2 = task2.get("workgroup", {})
        _check("auto-matched template is long-task", wg2.get("template") == "long-task",
             f"got {wg2.get('template')}")
        mi2 = wg2.get("match_info", {})
        _check("auto-match has confidence < 1.0", mi2.get("confidence", 1.0) < 1.0)
    finally:
        teardown_test_brain()


def test_existing_commands_still_work():
    """Existing commands (task list, briefing update, review, registry) still work."""
    print("\n🔄 Backward Compatibility")
    setup_test_brain()
    try:
        # task create (old style, no template)
        result = run_cmd("task", "create", "--title", "Test task", "--type", "quick", "--priority", "P2")
        _check("task create ok", result.get("ok") is True)
        tid = result.get("data", {}).get("id")
        
        # task list
        result = run_cmd("task", "list")
        _check("task list ok", result.get("ok") is True)
        
        # task show
        result = run_cmd("task", "show", tid)
        _check("task show ok", result.get("ok") is True)
        
        # task update
        result = run_cmd("task", "update", tid, "--status", "executing")
        _check("task update ok", result.get("ok") is True)
        
        # briefing update
        result = run_cmd("briefing", "update")
        _check("briefing update ok", result.get("ok") is True)
        
        # registry update
        result = run_cmd("registry", "update")
        _check("registry update ok", result.get("ok") is True)
        
        # review add
        result = run_cmd("review", "add", tid, "--summary", "Check design", "--prompt", "Review the design doc")
        _check("review add ok", result.get("ok") is True)
        
        # review list
        result = run_cmd("review", "list")
        _check("review list ok", result.get("ok") is True)
    finally:
        teardown_test_brain()


def test_briefing_includes_quick():
    """BRIEFING includes quick task summary."""
    print("\n📰 Briefing with Quick Tasks")
    setup_test_brain()
    try:
        # Add some quick tasks
        run_cmd("quick", "log", "--title", "查天气", "--result", "晴")
        run_cmd("quick", "log", "--title", "查日程", "--result", "3会议")
        
        # Update briefing
        result = run_cmd("briefing", "update")
        _check("briefing update ok", result.get("ok") is True)
        
        # Read briefing content
        briefing_path = TEST_BRAIN_DIR / "BRIEFING.md"
        content = briefing_path.read_text(encoding="utf-8")
        _check("briefing mentions quick tasks", "快速" in content or "Quick" in content or "quick" in content,
             f"content: {content[:200]}")
    finally:
        teardown_test_brain()


# ──────────────────────────────────────────
# Main
# ──────────────────────────────────────────

def main():
    global passed, failed
    
    print("=" * 60)
    print("Phase 1.2 Workgroup Template Tests")
    print("=" * 60)
    
    test_template_files_exist()
    test_template_list()
    test_template_show()
    test_template_matching()
    test_quick_log()
    test_quick_archive()
    test_task_create_with_template()
    test_existing_commands_still_work()
    test_briefing_includes_quick()
    
    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if errors:
        print("\nFailures:")
        for e in errors:
            print(e)
    
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
