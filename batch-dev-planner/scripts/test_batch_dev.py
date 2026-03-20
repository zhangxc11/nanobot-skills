"""batch_dev.py 单元测试"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module
sys.path.insert(0, str(Path(__file__).parent))
import batch_dev


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    """Redirect all data to tmp_path."""
    monkeypatch.setattr(batch_dev, "DATA_DIR", tmp_path)
    monkeypatch.setattr(batch_dev, "ACTIVE_BATCH_FILE", tmp_path / "active_batch.json")
    monkeypatch.setattr(batch_dev, "BATCHES_DIR", tmp_path / "batches")
    monkeypatch.setattr(batch_dev, "LOCK_FILE", tmp_path / "active_batch.lock")
    return tmp_path


def run_cli(*args):
    """Run CLI and capture output."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            parser = batch_dev.build_parser()
            parsed = parser.parse_args(args)
            # Re-dispatch manually
            batch_dev.main.__wrapped__ = None  # not needed
        except SystemExit:
            pass
    return buf.getvalue()


def parse_and_run(args_list):
    """Parse args and dispatch."""
    parser = batch_dev.build_parser()
    args = parser.parse_args(args_list)

    cmd = args.command
    if cmd == "batch":
        return {
            "create": batch_dev.cmd_batch_create,
            "list": batch_dev.cmd_batch_list,
            "show": batch_dev.cmd_batch_show,
            "advance": batch_dev.cmd_batch_advance,
            "complete": batch_dev.cmd_batch_complete,
        }[args.batch_action](args)
    elif cmd == "plan":
        return {
            "add": batch_dev.cmd_plan_add,
            "list": batch_dev.cmd_plan_list,
            "show": batch_dev.cmd_plan_show,
            "update": batch_dev.cmd_plan_update,
            "add-todo": batch_dev.cmd_plan_add_todo,
        }[args.plan_action](args)
    elif cmd == "review":
        return {
            "add": batch_dev.cmd_review_add,
            "fix": batch_dev.cmd_review_fix,
            "pass": batch_dev.cmd_review_pass,
        }[args.review_action](args)
    elif cmd == "merge":
        return batch_dev.cmd_merge(args)
    elif cmd == "status":
        return batch_dev.cmd_status(args)
    elif cmd == "lock":
        return {
            "acquire": batch_dev.cmd_lock_acquire,
            "release": batch_dev.cmd_lock_release,
            "status": batch_dev.cmd_lock_status,
            "heartbeat": batch_dev.cmd_lock_heartbeat,
        }[args.lock_action](args)


# ── Batch Tests ────────────────────────────────────────────────────

class TestBatchLifecycle:

    def test_create_batch(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        out = capsys.readouterr().out
        assert "创建成功" in out
        assert batch_dev.get_active_batch_id() == "b1"

    def test_create_duplicate_fails(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        with pytest.raises(SystemExit):
            parse_and_run(["batch", "create", "--name", "b1"])

    def test_serial_batch_protection(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        with pytest.raises(SystemExit):
            parse_and_run(["batch", "create", "--name", "b2"])

    def test_create_after_complete(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "Test Plan", "--todos", "t1"])
        parse_and_run(["batch", "advance"])  # planning -> developing
        parse_and_run(["plan", "update", "test-plan", "--status", "dev_done"])
        parse_and_run(["batch", "advance"])  # developing -> reviewing
        parse_and_run(["review", "pass", "test-plan"])
        parse_and_run(["batch", "advance"])  # reviewing -> merging
        parse_and_run(["merge", "test-plan", "--commit", "abc"])
        parse_and_run(["batch", "advance"])  # merging -> completed
        parse_and_run(["batch", "complete", "--batch", "b1"])
        # Now can create new batch
        parse_and_run(["batch", "create", "--name", "b2"])
        assert batch_dev.get_active_batch_id() == "b2"

    def test_batch_list(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["batch", "list"])
        out = capsys.readouterr().out
        assert "b1" in out

    def test_batch_show(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["batch", "show"])
        out = capsys.readouterr().out
        assert "b1" in out

    def test_advance_requires_plans(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        with pytest.raises(SystemExit):
            parse_and_run(["batch", "advance"])  # No plans

    def test_advance_flow(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["batch", "advance"])  # planning -> developing
        state = batch_dev.load_batch_state("b1")
        assert state["stage"] == "developing"


# ── Plan Tests ─────────────────────────────────────────────────────

class TestPlanManagement:

    def _setup_batch(self):
        parse_and_run(["batch", "create", "--name", "b1"])

    def test_plan_add(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "Core Fix", "--todos", "t1,t2", "--repos", "nanobot"])
        out = capsys.readouterr().out
        assert "添加成功" in out

    def test_plan_list(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "Core Fix", "--todos", "t1"])
        parse_and_run(["plan", "list"])
        out = capsys.readouterr().out
        assert "core-fix" in out

    def test_plan_show(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "Core Fix", "--todos", "t1"])
        parse_and_run(["plan", "show", "core-fix"])
        out = capsys.readouterr().out
        assert "Core Fix" in out

    def test_plan_update(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "update", "p1", "--status", "developing", "--branch-nanobot", "feat/p1"])
        p = batch_dev.load_plan("b1", "p1")
        assert p["status"] == "developing"
        assert p["branches"]["nanobot"] == "feat/p1"

    def test_add_todo_pending_ok(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "add-todo", "p1", "--todo-id", "t2"])
        p = batch_dev.load_plan("b1", "p1")
        assert "t2" in p["todo_ids"]

    def test_add_todo_developing_fails(self, tmp_data_dir, capsys):
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "update", "p1", "--status", "developing"])
        with pytest.raises(SystemExit):
            parse_and_run(["plan", "add-todo", "p1", "--todo-id", "t2"])

    def test_plan_add_invalid_depends_on_fails(self, tmp_data_dir, capsys):
        """Bug 1: plan add --depends-on 传不存在的 plan_id 应该失败"""
        self._setup_batch()
        with pytest.raises(SystemExit):
            parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1",
                           "--depends-on", "nonexistent-plan"])

    def test_plan_add_valid_depends_on(self, tmp_data_dir, capsys):
        """Bug 1 complement: plan add --depends-on 传存在的 plan_id 应该成功"""
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "Base Plan", "--todos", "t1"])
        parse_and_run(["plan", "add", "--title", "Dependent Plan", "--todos", "t2",
                       "--depends-on", "base-plan"])
        p = batch_dev.load_plan("b1", "dependent-plan")
        assert p["depends_on"] == "base-plan"

    def test_plan_update_depends_on(self, tmp_data_dir, capsys):
        """Bug 2: plan update --depends-on 能正确修改依赖"""
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "add", "--title", "P2", "--todos", "t2"])
        # Set dependency
        parse_and_run(["plan", "update", "p2", "--depends-on", "p1"])
        p = batch_dev.load_plan("b1", "p2")
        assert p["depends_on"] == "p1"
        # Clear dependency
        parse_and_run(["plan", "update", "p2", "--depends-on", ""])
        p = batch_dev.load_plan("b1", "p2")
        assert p["depends_on"] is None

    def test_plan_update_depends_on_invalid_fails(self, tmp_data_dir, capsys):
        """Bug 2 complement: plan update --depends-on 传不存在的 ID 应失败"""
        self._setup_batch()
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        with pytest.raises(SystemExit):
            parse_and_run(["plan", "update", "p1", "--depends-on", "nonexistent"])


# ── Review Tests ───────────────────────────────────────────────────

class TestReview:

    def _setup(self):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "update", "p1", "--status", "dev_done"])

    def test_review_add(self, tmp_data_dir, capsys):
        self._setup()
        parse_and_run(["review", "add", "p1", "--feedback", "措辞不好"])
        p = batch_dev.load_plan("b1", "p1")
        assert p["status"] == "reviewing"
        assert len(p["review"]["rounds"]) == 1
        assert p["review"]["rounds"][0]["feedback"] == "措辞不好"

    def test_review_fix(self, tmp_data_dir, capsys):
        self._setup()
        parse_and_run(["review", "add", "p1", "--feedback", "bug"])
        parse_and_run(["review", "fix", "p1", "--round", "1", "--fix-commit", "abc"])
        p = batch_dev.load_plan("b1", "p1")
        assert p["review"]["rounds"][0]["result"] == "fixed"
        assert p["review"]["rounds"][0]["fix_commit"] == "abc"

    def test_review_pass(self, tmp_data_dir, capsys):
        self._setup()
        parse_and_run(["review", "pass", "p1"])
        p = batch_dev.load_plan("b1", "p1")
        assert p["status"] == "passed"
        assert p["review"]["passed_at"] is not None

    def test_review_multiple_rounds(self, tmp_data_dir, capsys):
        self._setup()
        parse_and_run(["review", "add", "p1", "--feedback", "问题1"])
        parse_and_run(["review", "fix", "p1", "--round", "1", "--fix-commit", "fix1"])
        parse_and_run(["review", "add", "p1", "--feedback", "问题2"])
        parse_and_run(["review", "fix", "p1", "--round", "2", "--fix-commit", "fix2"])
        parse_and_run(["review", "pass", "p1"])
        p = batch_dev.load_plan("b1", "p1")
        assert len(p["review"]["rounds"]) == 2
        assert p["status"] == "passed"


# ── Merge Tests ────────────────────────────────────────────────────

class TestMerge:

    def _setup_passed(self):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["plan", "update", "p1", "--status", "dev_done"])
        parse_and_run(["review", "pass", "p1"])

    def test_merge_passed(self, tmp_data_dir, capsys):
        self._setup_passed()
        parse_and_run(["merge", "p1", "--commit", "merge123"])
        p = batch_dev.load_plan("b1", "p1")
        assert p["status"] == "merged"
        assert p["merge"]["commits"]["nanobot"] == "merge123"

    def test_merge_not_passed_fails(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        with pytest.raises(SystemExit):
            parse_and_run(["merge", "p1", "--commit", "abc"])

    def test_merge_multi_repo_partial_then_complete(self, tmp_data_dir, capsys):
        """Bug 3: 跨仓库 Plan 分两次 merge 不同仓库都能成功"""
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "Cross Repo",
                       "--todos", "t1", "--repos", "nanobot,web-chat"])
        parse_and_run(["plan", "update", "cross-repo", "--status", "dev_done"])
        parse_and_run(["review", "pass", "cross-repo"])

        # First merge: nanobot only
        parse_and_run(["merge", "cross-repo", "--commit", "abc123", "--repo", "nanobot"])
        p = batch_dev.load_plan("b1", "cross-repo")
        assert p["status"] == "merging"
        assert p["merge"]["commits"]["nanobot"] == "abc123"
        assert p["merge"]["merged_at"] is None

        # Second merge: web-chat — should succeed (not rejected)
        parse_and_run(["merge", "cross-repo", "--commit", "def456", "--repo", "web-chat"])
        p = batch_dev.load_plan("b1", "cross-repo")
        assert p["status"] == "merged"
        assert p["merge"]["commits"]["web-chat"] == "def456"
        assert p["merge"]["merged_at"] is not None

    def test_merge_single_repo_immediate(self, tmp_data_dir, capsys):
        """Single repo plan merges immediately to 'merged' status"""
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "Single Repo",
                       "--todos", "t1", "--repos", "nanobot"])
        parse_and_run(["plan", "update", "single-repo", "--status", "dev_done"])
        parse_and_run(["review", "pass", "single-repo"])
        parse_and_run(["merge", "single-repo", "--commit", "abc123", "--repo", "nanobot"])
        p = batch_dev.load_plan("b1", "single-repo")
        assert p["status"] == "merged"


# ── Lock Tests ─────────────────────────────────────────────────────

class TestLock:

    def test_acquire_and_release(self, tmp_data_dir, capsys):
        parse_and_run(["lock", "acquire", "--session", "s1"])
        out = capsys.readouterr().out
        assert "锁已获取" in out
        parse_and_run(["lock", "release"])
        out = capsys.readouterr().out
        assert "锁已释放" in out

    def test_acquire_blocked(self, tmp_data_dir, capsys):
        parse_and_run(["lock", "acquire", "--session", "s1"])
        capsys.readouterr()
        with pytest.raises(SystemExit):
            parse_and_run(["lock", "acquire", "--session", "s2"])

    def test_heartbeat(self, tmp_data_dir, capsys):
        parse_and_run(["lock", "acquire", "--session", "s1"])
        parse_and_run(["lock", "heartbeat"])
        out = capsys.readouterr().out
        assert "心跳已更新" in out

    def test_heartbeat_no_lock(self, tmp_data_dir, capsys):
        with pytest.raises(SystemExit):
            parse_and_run(["lock", "heartbeat"])

    def test_lock_status(self, tmp_data_dir, capsys):
        parse_and_run(["lock", "acquire", "--session", "s1"])
        capsys.readouterr()
        parse_and_run(["lock", "status"])
        out = capsys.readouterr().out
        assert "s1" in out

    def test_force_acquire_after_hard_timeout(self, tmp_data_dir, capsys):
        """Simulate hard timeout by writing old timestamp."""
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=120)).isoformat()
        lock_data = {
            "session": "old_session",
            "acquired_at": old_time,
            "heartbeat_at": old_time,
            "soft_timeout_minutes": 10,
            "hard_timeout_minutes": 60,
        }
        batch_dev.save_json(batch_dev.LOCK_FILE, lock_data)
        parse_and_run(["lock", "acquire", "--session", "new_session"])
        out = capsys.readouterr().out
        assert "强制获取" in out


# ── Status Tests ───────────────────────────────────────────────────

class TestStatus:

    def test_status_no_batch(self, tmp_data_dir, capsys):
        parse_and_run(["status"])
        out = capsys.readouterr().out
        assert "没有活跃的 batch" in out

    def test_status_with_plans(self, tmp_data_dir, capsys):
        parse_and_run(["batch", "create", "--name", "b1"])
        parse_and_run(["plan", "add", "--title", "P1", "--todos", "t1"])
        parse_and_run(["status"])
        out = capsys.readouterr().out
        assert "b1" in out
        assert "P1" in out
        # Check STATUS.md was written
        status_md = batch_dev.BATCHES_DIR / "b1" / "STATUS.md"
        assert status_md.exists()
