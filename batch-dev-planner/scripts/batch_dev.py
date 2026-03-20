#!/usr/bin/env python3
"""batch-dev-planner 状态管理脚本

CLI 工具，管理批量开发的 batch/plan 生命周期、验收记录、资源锁。

Usage:
    python batch_dev.py batch create --name <name> [--base-commit-nanobot <hash>] [--base-commit-webchat <hash>]
    python batch_dev.py batch list
    python batch_dev.py batch show [--batch <id>]
    python batch_dev.py batch advance [--batch <id>]
    python batch_dev.py batch complete [--batch <id>]

    python batch_dev.py plan add --title <title> --todos <id1,id2> [--depends-on <plan>] [--repos <r1,r2>]
    python batch_dev.py plan list [--batch <id>]
    python batch_dev.py plan show <plan-id>
    python batch_dev.py plan update <plan-id> [--status <s>] [--branch-nanobot <b>] [--branch-webchat <b>] ...
    python batch_dev.py plan add-todo <plan-id> --todo-id <id>

    python batch_dev.py review add <plan-id> --feedback <text>
    python batch_dev.py review fix <plan-id> --round <n> --fix-commit <hash>
    python batch_dev.py review pass <plan-id>

    python batch_dev.py merge <plan-id> --commit <hash> [--repo <repo>]

    python batch_dev.py status

    python batch_dev.py lock acquire --session <session>
    python batch_dev.py lock release
    python batch_dev.py lock status
    python batch_dev.py lock heartbeat
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────

DATA_DIR = Path.home() / ".nanobot" / "workspace" / "data" / "batch-dev"
ACTIVE_BATCH_FILE = DATA_DIR / "active_batch.json"
BATCHES_DIR = DATA_DIR / "batches"

# ── Helpers ────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as e:
        print(f"⚠️ JSON 解析失败 ({path.name}): {e}，返回空字典")
        return {}

def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def get_active_batch_id() -> str | None:
    info = load_json(ACTIVE_BATCH_FILE)
    return info.get("batch_id")

def require_active_batch(batch_id: str | None = None) -> str:
    bid = batch_id or get_active_batch_id()
    if not bid:
        print("❌ 没有活跃的 batch。请先 batch create 或指定 --batch。")
        sys.exit(1)
    batch_dir = BATCHES_DIR / bid
    if not batch_dir.exists():
        print(f"❌ Batch '{bid}' 不存在。")
        sys.exit(1)
    return bid

def load_batch_state(batch_id: str) -> dict:
    return load_json(BATCHES_DIR / batch_id / "state.json")

def save_batch_state(batch_id: str, state: dict):
    save_json(BATCHES_DIR / batch_id / "state.json", state)

def load_plan(batch_id: str, plan_id: str) -> dict:
    return load_json(BATCHES_DIR / batch_id / "plans" / f"{plan_id}.json")

def save_plan(batch_id: str, plan_id: str, plan: dict):
    save_json(BATCHES_DIR / batch_id / "plans" / f"{plan_id}.json", plan)

def status_emoji(status: str) -> str:
    return {
        "pending": "⏳",
        "developing": "🔨",
        "dev_done": "📦",
        "reviewing": "🔍",
        "fix_in_progress": "🔧",
        "passed": "✅",
        "merging": "🔀",
        "merged": "🎉",
    }.get(status, "❓")

def stage_emoji(stage: str) -> str:
    return {
        "planning": "📋",
        "developing": "🔨",
        "reviewing": "🔍",
        "merging": "🔀",
        "completed": "✅",
    }.get(stage, "❓")


# ── Batch Commands ─────────────────────────────────────────────────

def cmd_batch_create(args):
    name = args.name
    if (BATCHES_DIR / name).exists():
        print(f"❌ Batch '{name}' 已存在。")
        sys.exit(1)

    # Check no active batch
    active = get_active_batch_id()
    if active:
        active_state = load_batch_state(active)
        if active_state.get("stage") != "completed":
            print(f"❌ 已有活跃 batch '{active}'（阶段: {active_state.get('stage')}）。")
            print("   串行批次原则：上一批完成前不能开启新批次。")
            sys.exit(1)

    base_commits = {}
    if args.base_commit_nanobot:
        base_commits["nanobot"] = args.base_commit_nanobot
    if args.base_commit_webchat:
        base_commits["web-chat"] = args.base_commit_webchat

    state = {
        "batch_id": name,
        "created_at": now_iso(),
        "stage": "planning",
        "base_commits": base_commits,
        "workdir": str(Path.home() / ".nanobot" / "workspace" / "dev-workdir"),
        "plans": [],
        "plan_order": [],
        "sessions": {
            "planning": None,
            "developing": None,
            "reviewing": None,
        },
    }
    save_batch_state(name, state)

    # Set as active batch
    save_json(ACTIVE_BATCH_FILE, {"batch_id": name})

    print(f"✅ Batch '{name}' 创建成功（阶段: planning）")


def cmd_batch_list(args):
    if not BATCHES_DIR.exists():
        print("📭 暂无 batch 记录。")
        return

    active = get_active_batch_id()
    batches = sorted(BATCHES_DIR.iterdir())
    if not batches:
        print("📭 暂无 batch 记录。")
        return

    print(f"{'Batch ID':<30} {'阶段':<15} {'Plans':<8} {'创建时间':<22} {'Active'}")
    print("-" * 90)
    for bd in batches:
        if not bd.is_dir():
            continue
        st = load_json(bd / "state.json")
        bid = st.get("batch_id", bd.name)
        stage = st.get("stage", "?")
        plans = len(st.get("plans", []))
        created = st.get("created_at", "?")[:19]
        marker = " ← active" if bid == active else ""
        print(f"{bid:<30} {stage_emoji(stage)} {stage:<12} {plans:<8} {created:<22}{marker}")


def cmd_batch_show(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)

    print(f"# Batch: {bid}")
    print(f"**阶段**: {stage_emoji(state['stage'])} {state['stage']} | **创建**: {state['created_at'][:19]}")
    print()

    if state.get("base_commits"):
        print("**基点 Commits**:")
        for repo, commit in state["base_commits"].items():
            print(f"  - {repo}: `{commit}`")
        print()

    plans = state.get("plans", [])
    if not plans:
        print("📭 暂无 Plan。")
        return

    print(f"| {'Plan':<25} | {'状态':<18} | {'需求数':<6} | {'分支':<40} | {'验收轮次':<8} |")
    print(f"|{'-'*27}|{'-'*20}|{'-'*8}|{'-'*42}|{'-'*10}|")
    for pid in state.get("plan_order", plans):
        try:
            p = load_plan(bid, pid)
        except Exception as e:
            print(f"| {pid:<25} | ⚠️ 加载失败: {e}")
            continue
        if not p:
            print(f"| {pid:<25} | ⚠️ plan JSON 缺失，跳过")
            continue
        st = p.get("status", "?")
        todos = len(p.get("todo_ids", []))
        branches = p.get("branches", {})
        branch_str = ", ".join(v for v in branches.values() if v) or "-"
        if len(branch_str) > 38:
            branch_str = branch_str[:35] + "..."
        rounds = len(p.get("review", {}).get("rounds", []))
        print(f"| {pid:<25} | {status_emoji(st)} {st:<15} | {todos:<6} | {branch_str:<40} | {rounds:<8} |")


STAGE_ORDER = ["planning", "developing", "reviewing", "merging", "completed"]

def _safe_plan_status(batch_id: str, plan_id: str) -> str | None:
    """Load plan status with tolerance for missing JSON files."""
    try:
        p = load_plan(batch_id, plan_id)
        if not p:
            print(f"⚠️ Plan '{plan_id}' JSON 缺失，跳过前置条件检查。")
            return None
        return p.get("status", "?")
    except Exception as e:
        print(f"⚠️ Plan '{plan_id}' 加载失败: {e}，跳过前置条件检查。")
        return None

ADVANCE_PRECONDITIONS = {
    "planning": lambda state: len(state.get("plans", [])) > 0,
    "developing": lambda state: all(
        (s := _safe_plan_status(state["batch_id"], pid)) is None or s in ("dev_done", "reviewing", "passed", "merged")
        for pid in state.get("plans", [])
    ),
    "reviewing": lambda state: all(
        (s := _safe_plan_status(state["batch_id"], pid)) is None or s in ("passed", "merging", "merged")
        for pid in state.get("plans", [])
    ),
    "merging": lambda state: all(
        (s := _safe_plan_status(state["batch_id"], pid)) is None or s == "merged"
        for pid in state.get("plans", [])
    ),
}

def cmd_batch_advance(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)
    current = state["stage"]

    idx = STAGE_ORDER.index(current)
    if idx >= len(STAGE_ORDER) - 1:
        print(f"❌ Batch 已在最终阶段 '{current}'，无法推进。")
        sys.exit(1)

    # Check preconditions
    check = ADVANCE_PRECONDITIONS.get(current)
    if check and not check(state):
        print(f"❌ 推进条件不满足（当前阶段: {current}）。")
        if current == "planning":
            print("   需要至少添加一个 Plan。")
        elif current == "developing":
            print("   需要所有 Plan 至少达到 dev_done 状态。")
        elif current == "reviewing":
            print("   需要所有 Plan 达到 passed 状态。")
        elif current == "merging":
            print("   需要所有 Plan 达到 merged 状态。")
        sys.exit(1)

    next_stage = STAGE_ORDER[idx + 1]
    state["stage"] = next_stage
    save_batch_state(bid, state)
    print(f"✅ Batch '{bid}' 推进: {current} → {next_stage}")


def cmd_batch_complete(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)

    if state["stage"] != "merging":
        # Allow force complete from any stage
        pass

    state["stage"] = "completed"
    state["completed_at"] = now_iso()
    save_batch_state(bid, state)

    # Clear active batch
    save_json(ACTIVE_BATCH_FILE, {})
    print(f"✅ Batch '{bid}' 已完成。下一批已解锁。")


# ── Plan Commands ──────────────────────────────────────────────────

def cmd_plan_add(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)

    if state["stage"] not in ("planning", "developing"):
        print(f"❌ 当前阶段 '{state['stage']}' 不允许添加 Plan。")
        sys.exit(1)

    # Generate plan_id from title
    plan_id = args.title.lower().replace(" ", "-").replace("/", "-")
    # Remove non-ascii
    plan_id = "".join(c for c in plan_id if c.isascii() and (c.isalnum() or c == "-"))
    plan_id = plan_id.strip("-")
    if not plan_id:
        plan_id = f"plan-{len(state['plans']) + 1}"

    if plan_id in state["plans"]:
        print(f"❌ Plan '{plan_id}' 已存在。")
        sys.exit(1)

    todo_ids = [t.strip() for t in args.todos.split(",") if t.strip()] if args.todos else []
    repos = [r.strip() for r in args.repos.split(",") if r.strip()] if args.repos else []
    depends_on = args.depends_on

    # Bug 1 fix: 校验 depends_on 的 plan_id 是否存在于当前 batch 中
    if depends_on and depends_on not in state["plans"]:
        print(f"❌ 依赖的 Plan '{depends_on}' 不存在于 Batch '{bid}' 中。")
        print(f"   当前 Plans: {', '.join(state['plans']) or '（空）'}")
        sys.exit(1)

    plan = {
        "plan_id": plan_id,
        "title": args.title,
        "status": "pending",
        "todo_ids": todo_ids,
        "depends_on": depends_on,
        "repos": repos,
        "branches": {r: None for r in repos},
        "dev": {
            "session": None,
            "subagent_id": None,
            "started_at": None,
            "completed_at": None,
            "commits": [],
        },
        "review": {
            "session": None,
            "rounds": [],
            "passed_at": None,
        },
        "merge": {
            "commits": {},
            "merged_at": None,
        },
    }
    save_plan(bid, plan_id, plan)

    state["plans"].append(plan_id)
    state["plan_order"].append(plan_id)
    save_batch_state(bid, state)

    print(f"✅ Plan '{plan_id}' 添加成功（需求: {len(todo_ids)} 条，仓库: {', '.join(repos) or '-'}）")


def cmd_plan_list(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)
    plans = state.get("plans", [])

    if not plans:
        print("📭 暂无 Plan。")
        return

    print(f"| {'#':<3} | {'Plan ID':<25} | {'标题':<30} | {'状态':<15} | {'依赖':<15} |")
    print(f"|{'-'*5}|{'-'*27}|{'-'*32}|{'-'*17}|{'-'*17}|")
    for i, pid in enumerate(state.get("plan_order", plans), 1):
        try:
            p = load_plan(bid, pid)
        except Exception as e:
            print(f"| {i:<3} | {pid:<25} | ⚠️ 加载失败: {e}")
            continue
        if not p:
            print(f"| {i:<3} | {pid:<25} | ⚠️ plan JSON 缺失，跳过")
            continue
        st = p.get("status", "?")
        dep = p.get("depends_on") or "-"
        title = p.get("title", "?")
        if len(title) > 28:
            title = title[:25] + "..."
        print(f"| {i:<3} | {pid:<25} | {title:<30} | {status_emoji(st)} {st:<12} | {dep:<15} |")


def cmd_plan_show(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    print(f"# Plan: {p['plan_id']}")
    print(f"**标题**: {p['title']}")
    print(f"**状态**: {status_emoji(p['status'])} {p['status']}")
    print(f"**依赖**: {p.get('depends_on') or '无'}")
    print(f"**仓库**: {', '.join(p.get('repos', [])) or '-'}")
    print(f"**需求 IDs**: {', '.join(p.get('todo_ids', [])) or '-'}")
    print()

    branches = p.get("branches", {})
    if any(branches.values()):
        print("**分支**:")
        for repo, branch in branches.items():
            if branch:
                print(f"  - {repo}: `{branch}`")
        print()

    dev = p.get("dev", {})
    if dev.get("started_at"):
        print(f"**开发**: session={dev.get('session')}, started={dev['started_at'][:19]}")
        if dev.get("completed_at"):
            print(f"  completed={dev['completed_at'][:19]}, commits={dev.get('commits', [])}")
        print()

    review = p.get("review", {})
    rounds = review.get("rounds", [])
    if rounds:
        print(f"**验收**: {len(rounds)} 轮")
        for r in rounds:
            status = "✅" if r.get("result") == "fixed" else "🔧"
            print(f"  Round {r['round']}: {status} {r.get('feedback', '')[:60]}")
        if review.get("passed_at"):
            print(f"  ✅ 通过于 {review['passed_at'][:19]}")
        print()

    merge = p.get("merge", {})
    if merge.get("merged_at"):
        print(f"**合并**: {merge['merged_at'][:19]}, commits={merge.get('commits', {})}")


def cmd_plan_update(args):
    bid = require_active_batch(args.batch)
    state = load_batch_state(bid)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    updated = []

    if args.status:
        old = p["status"]
        p["status"] = args.status
        updated.append(f"status: {old} → {args.status}")

    if args.branch_nanobot:
        p.setdefault("branches", {})["nanobot"] = args.branch_nanobot
        updated.append(f"branch-nanobot: {args.branch_nanobot}")

    if args.branch_webchat:
        p.setdefault("branches", {})["web-chat"] = args.branch_webchat
        updated.append(f"branch-webchat: {args.branch_webchat}")

    if args.dev_session:
        p.setdefault("dev", {})["session"] = args.dev_session
        updated.append(f"dev-session: {args.dev_session}")

    if args.dev_subagent:
        p.setdefault("dev", {})["subagent_id"] = args.dev_subagent
        updated.append(f"dev-subagent: {args.dev_subagent}")

    if args.dev_commit:
        p.setdefault("dev", {}).setdefault("commits", []).append(args.dev_commit)
        updated.append(f"dev-commit: +{args.dev_commit}")

    if args.dev_started:
        p.setdefault("dev", {})["started_at"] = now_iso()
        updated.append("dev-started: now")

    if args.dev_completed:
        p.setdefault("dev", {})["completed_at"] = now_iso()
        updated.append("dev-completed: now")

    if args.review_session:
        p.setdefault("review", {})["session"] = args.review_session
        updated.append(f"review-session: {args.review_session}")

    # Bug 2 fix: 支持 --depends-on 更新依赖关系
    if args.depends_on is not None:
        if args.depends_on == "":
            # 空字符串表示清除依赖
            p["depends_on"] = None
            updated.append("depends-on: (cleared)")
        else:
            # 校验依赖 plan_id 存在性
            if args.depends_on not in state["plans"]:
                print(f"❌ 依赖的 Plan '{args.depends_on}' 不存在于 Batch '{bid}' 中。")
                sys.exit(1)
            if args.depends_on == plan_id:
                print(f"❌ Plan 不能依赖自身。")
                sys.exit(1)
            p["depends_on"] = args.depends_on
            updated.append(f"depends-on: {args.depends_on}")

    if not updated:
        print("⚠️ 没有指定任何更新字段。")
        return

    save_plan(bid, plan_id, p)
    print(f"✅ Plan '{plan_id}' 已更新:")
    for u in updated:
        print(f"   {u}")


def cmd_plan_add_todo(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    if p["status"] != "pending":
        print(f"❌ Plan '{plan_id}' 状态为 '{p['status']}'，只有 pending 状态可追加需求。")
        sys.exit(1)

    todo_id = args.todo_id
    if todo_id in p.get("todo_ids", []):
        print(f"⚠️ Todo '{todo_id}' 已在 Plan 中。")
        return

    p.setdefault("todo_ids", []).append(todo_id)
    save_plan(bid, plan_id, p)
    print(f"✅ Todo '{todo_id}' 已追加到 Plan '{plan_id}'（共 {len(p['todo_ids'])} 条需求）")


# ── Review Commands ────────────────────────────────────────────────

def cmd_review_add(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    if p["status"] not in ("reviewing", "dev_done"):
        print(f"⚠️ Plan 状态为 '{p['status']}'，自动切换到 reviewing。")

    p["status"] = "reviewing"
    review = p.setdefault("review", {"session": None, "rounds": [], "passed_at": None})
    round_num = len(review["rounds"]) + 1
    review["rounds"].append({
        "round": round_num,
        "feedback": args.feedback,
        "fix_commit": None,
        "result": "pending",
        "created_at": now_iso(),
    })

    save_plan(bid, plan_id, p)
    print(f"✅ Plan '{plan_id}' 验收反馈已记录（Round {round_num}）")


def cmd_review_fix(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    review = p.get("review", {})
    rounds = review.get("rounds", [])
    target_round = args.round

    found = False
    for r in rounds:
        if r["round"] == target_round:
            r["fix_commit"] = args.fix_commit
            r["result"] = "fixed"
            r["fixed_at"] = now_iso()
            found = True
            break

    if not found:
        print(f"❌ Round {target_round} 不存在。")
        sys.exit(1)

    p["status"] = "reviewing"  # Back to reviewing after fix
    save_plan(bid, plan_id, p)
    print(f"✅ Plan '{plan_id}' Round {target_round} 已修复（commit: {args.fix_commit}）")


def cmd_review_pass(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    p["status"] = "passed"
    p.setdefault("review", {})["passed_at"] = now_iso()
    save_plan(bid, plan_id, p)
    print(f"✅ Plan '{plan_id}' 验收通过！")


# ── Merge Command ──────────────────────────────────────────────────

def cmd_merge(args):
    bid = require_active_batch(args.batch)
    plan_id = args.plan_id
    p = load_plan(bid, plan_id)
    if not p:
        print(f"❌ Plan '{plan_id}' 不存在。")
        sys.exit(1)

    # Bug 3 fix: 允许 passed 和 merging 状态执行合并
    if p["status"] not in ("passed", "merging"):
        print(f"❌ Plan '{plan_id}' 状态为 '{p['status']}'，只有 passed 或 merging 状态可合并。")
        sys.exit(1)

    repo = args.repo or "nanobot"
    p.setdefault("merge", {"commits": {}, "merged_at": None})
    p["merge"]["commits"][repo] = args.commit

    # Bug 3 fix: 检查是否所有声明仓库都已有 merge commit
    plan_repos = p.get("repos", [])
    if plan_repos and len(plan_repos) > 1:
        # 跨仓库场景：检查所有仓库是否都已合并
        all_merged = all(
            p["merge"]["commits"].get(r) for r in plan_repos
        )
        if all_merged:
            p["merge"]["merged_at"] = now_iso()
            p["status"] = "merged"
            print(f"✅ Plan '{plan_id}' 所有仓库已合并（{repo}: {args.commit}）→ merged")
        else:
            p["status"] = "merging"
            remaining = [r for r in plan_repos if not p["merge"]["commits"].get(r)]
            print(f"✅ Plan '{plan_id}' 部分合并（{repo}: {args.commit}），剩余仓库: {', '.join(remaining)}")
    else:
        # 单仓库或未声明 repos：直接标记 merged
        p["merge"]["merged_at"] = now_iso()
        p["status"] = "merged"
        print(f"✅ Plan '{plan_id}' 已合并（{repo}: {args.commit}）")

    save_plan(bid, plan_id, p)


# ── Status Command ─────────────────────────────────────────────────

def cmd_status(args):
    bid = get_active_batch_id()
    if not bid:
        print("📭 没有活跃的 batch。")
        return

    state = load_batch_state(bid)
    plans = state.get("plans", [])

    lines = []
    lines.append(f"# Batch {bid} 状态")
    lines.append("")
    lines.append(f"**阶段**: {stage_emoji(state['stage'])} {state['stage']} | **创建**: {state['created_at'][:19]}")
    lines.append("")

    if plans:
        lines.append(f"| Plan | 标题 | 状态 | 需求数 | 验收轮次 |")
        lines.append(f"|------|------|------|--------|---------|")
        for pid in state.get("plan_order", plans):
            try:
                p = load_plan(bid, pid)
            except Exception as e:
                lines.append(f"| {pid} | ⚠️ 加载失败 | - | - | - |")
                continue
            if not p:
                lines.append(f"| {pid} | ⚠️ JSON 缺失 | - | - | - |")
                continue
            st = p.get("status", "?")
            title = p.get("title", "?")
            if len(title) > 25:
                title = title[:22] + "..."
            todos = len(p.get("todo_ids", []))
            rounds = len(p.get("review", {}).get("rounds", []))
            lines.append(f"| {pid} | {title} | {status_emoji(st)} {st} | {todos} | {rounds} |")
    else:
        lines.append("📭 暂无 Plan。")

    output = "\n".join(lines)
    print(output)

    # Also write STATUS.md
    status_path = BATCHES_DIR / bid / "STATUS.md"
    status_path.write_text(output + "\n", encoding="utf-8")


# ── Lock Commands ──────────────────────────────────────────────────

LOCK_FILE = DATA_DIR / "active_batch.lock"
DEFAULT_SOFT_TIMEOUT = 10  # minutes
DEFAULT_HARD_TIMEOUT = 60  # minutes


def cmd_lock_acquire(args):
    session = args.session
    lock = load_json(LOCK_FILE)

    if lock and lock.get("session"):
        # Check timeouts
        heartbeat = lock.get("heartbeat_at", lock.get("acquired_at", ""))
        if heartbeat:
            from datetime import datetime as dt
            try:
                hb_time = dt.fromisoformat(heartbeat)
                elapsed = (dt.now(timezone.utc).astimezone() - hb_time).total_seconds() / 60
                hard = lock.get("hard_timeout_minutes", DEFAULT_HARD_TIMEOUT)
                soft = lock.get("soft_timeout_minutes", DEFAULT_SOFT_TIMEOUT)

                if elapsed < soft:
                    print(f"❌ 锁被 '{lock['session']}' 持有（{elapsed:.0f}min 前心跳）。")
                    print(f"   软超时 {soft}min / 硬超时 {hard}min。")
                    sys.exit(1)
                elif elapsed < hard:
                    print(f"⚠️ 锁已软超时（{elapsed:.0f}min 无心跳），但未达硬超时。")
                    print(f"   强制获取中...")
                else:
                    print(f"⚠️ 锁已硬超时（{elapsed:.0f}min 无心跳），强制获取。")
            except (ValueError, TypeError):
                print("⚠️ 锁时间戳解析失败，强制获取。")

    new_lock = {
        "session": session,
        "acquired_at": now_iso(),
        "heartbeat_at": now_iso(),
        "soft_timeout_minutes": DEFAULT_SOFT_TIMEOUT,
        "hard_timeout_minutes": DEFAULT_HARD_TIMEOUT,
    }
    save_json(LOCK_FILE, new_lock)
    print(f"✅ 锁已获取（session: {session}）")


def cmd_lock_release(args):
    if not LOCK_FILE.exists():
        print("⚠️ 没有活跃的锁。")
        return
    save_json(LOCK_FILE, {})
    print("✅ 锁已释放。")


def cmd_lock_status(args):
    lock = load_json(LOCK_FILE)
    if not lock or not lock.get("session"):
        print("🔓 无活跃锁。")
        return

    print(f"🔒 锁状态:")
    print(f"   Session: {lock['session']}")
    print(f"   获取时间: {lock.get('acquired_at', '?')}")
    print(f"   最近心跳: {lock.get('heartbeat_at', '?')}")
    print(f"   超时: 软 {lock.get('soft_timeout_minutes', DEFAULT_SOFT_TIMEOUT)}min / 硬 {lock.get('hard_timeout_minutes', DEFAULT_HARD_TIMEOUT)}min")


def cmd_lock_heartbeat(args):
    lock = load_json(LOCK_FILE)
    if not lock or not lock.get("session"):
        print("⚠️ 没有活跃的锁，无法更新心跳。")
        sys.exit(1)

    lock["heartbeat_at"] = now_iso()
    save_json(LOCK_FILE, lock)
    print(f"✅ 心跳已更新（session: {lock['session']}）")


# ── CLI Parser ─────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(description="batch-dev-planner 状态管理")
    sub = parser.add_subparsers(dest="command")

    # ── batch ──
    batch_parser = sub.add_parser("batch", help="Batch 生命周期管理")
    batch_sub = batch_parser.add_subparsers(dest="batch_action")

    p = batch_sub.add_parser("create", help="创建新 batch")
    p.add_argument("--name", required=True, help="Batch 名称，如 batch-20260312")
    p.add_argument("--base-commit-nanobot", help="nanobot 仓库基点 commit")
    p.add_argument("--base-commit-webchat", help="web-chat 仓库基点 commit")

    batch_sub.add_parser("list", help="列出所有 batch")

    p = batch_sub.add_parser("show", help="显示 batch 详情")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = batch_sub.add_parser("advance", help="推进到下一阶段")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = batch_sub.add_parser("complete", help="标记完成")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    # ── plan ──
    plan_parser = sub.add_parser("plan", help="Plan 管理")
    plan_sub = plan_parser.add_subparsers(dest="plan_action")

    p = plan_sub.add_parser("add", help="添加 Plan")
    p.add_argument("--title", required=True, help="Plan 标题")
    p.add_argument("--todos", default="", help="关联 todo IDs（逗号分隔）")
    p.add_argument("--depends-on", help="依赖的 Plan ID")
    p.add_argument("--repos", default="", help="涉及仓库（逗号分隔）")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = plan_sub.add_parser("list", help="列出所有 Plan")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = plan_sub.add_parser("show", help="显示 Plan 详情")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = plan_sub.add_parser("update", help="更新 Plan")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--status", help="新状态")
    p.add_argument("--branch-nanobot", help="nanobot 分支名")
    p.add_argument("--branch-webchat", help="web-chat 分支名")
    p.add_argument("--dev-session", help="开发 session ID")
    p.add_argument("--dev-subagent", help="开发 subagent ID")
    p.add_argument("--dev-commit", help="追加开发 commit hash")
    p.add_argument("--dev-started", action="store_true", help="标记开发开始")
    p.add_argument("--dev-completed", action="store_true", help="标记开发完成")
    p.add_argument("--review-session", help="验收 session ID")
    p.add_argument("--depends-on", default=None, help="依赖的 Plan ID（空字符串清除依赖）")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    p = plan_sub.add_parser("add-todo", help="追加需求到 Plan")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--todo-id", required=True, help="Todo ID")
    p.add_argument("--batch", help="Batch ID（默认活跃 batch）")

    # ── review ──
    review_parser = sub.add_parser("review", help="验收记录")
    review_sub = review_parser.add_subparsers(dest="review_action")

    p = review_sub.add_parser("add", help="添加验收反馈")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--feedback", required=True, help="反馈内容")
    p.add_argument("--batch", help="Batch ID")

    p = review_sub.add_parser("fix", help="记录修复")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--round", type=int, required=True, help="轮次")
    p.add_argument("--fix-commit", required=True, help="修复 commit hash")
    p.add_argument("--batch", help="Batch ID")

    p = review_sub.add_parser("pass", help="标记验收通过")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--batch", help="Batch ID")

    # ── merge ──
    p = sub.add_parser("merge", help="记录合并")
    p.add_argument("plan_id", help="Plan ID")
    p.add_argument("--commit", required=True, help="合并 commit hash")
    p.add_argument("--repo", default="nanobot", help="仓库名（默认 nanobot）")
    p.add_argument("--batch", help="Batch ID")

    # ── status ──
    sub.add_parser("status", help="状态总览")

    # ── lock ──
    lock_parser = sub.add_parser("lock", help="资源锁管理")
    lock_sub = lock_parser.add_subparsers(dest="lock_action")

    p = lock_sub.add_parser("acquire", help="获取锁")
    p.add_argument("--session", required=True, help="Session ID")

    lock_sub.add_parser("release", help="释放锁")
    lock_sub.add_parser("status", help="查看锁状态")
    lock_sub.add_parser("heartbeat", help="更新心跳")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Dispatch
    if args.command == "batch":
        if not args.batch_action:
            print("请指定 batch 子命令: create, list, show, advance, complete")
            sys.exit(1)
        {
            "create": cmd_batch_create,
            "list": cmd_batch_list,
            "show": cmd_batch_show,
            "advance": cmd_batch_advance,
            "complete": cmd_batch_complete,
        }[args.batch_action](args)

    elif args.command == "plan":
        if not args.plan_action:
            print("请指定 plan 子命令: add, list, show, update, add-todo")
            sys.exit(1)
        {
            "add": cmd_plan_add,
            "list": cmd_plan_list,
            "show": cmd_plan_show,
            "update": cmd_plan_update,
            "add-todo": cmd_plan_add_todo,
        }[args.plan_action](args)

    elif args.command == "review":
        if not args.review_action:
            print("请指定 review 子命令: add, fix, pass")
            sys.exit(1)
        {
            "add": cmd_review_add,
            "fix": cmd_review_fix,
            "pass": cmd_review_pass,
        }[args.review_action](args)

    elif args.command == "merge":
        cmd_merge(args)

    elif args.command == "status":
        cmd_status(args)

    elif args.command == "lock":
        if not args.lock_action:
            print("请指定 lock 子命令: acquire, release, status, heartbeat")
            sys.exit(1)
        {
            "acquire": cmd_lock_acquire,
            "release": cmd_lock_release,
            "status": cmd_lock_status,
            "heartbeat": cmd_lock_heartbeat,
        }[args.lock_action](args)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
