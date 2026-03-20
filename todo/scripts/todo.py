#!/usr/bin/env python3
"""Todo list manager — CLI tool for nanobot todo skill.

Usage:
    python todo.py add --title "标题" [options]
    python todo.py list [filters]
    python todo.py show <id>
    python todo.py update <id> [fields]
    python todo.py note <id> --append/--write "内容"
    python todo.py done <id> [<id2> ...]
    python todo.py delete <id> [--hard]
    python todo.py summary [--by-group]
    python todo.py move <id> --to <group-id>
    python todo.py group add/list/show/update/note
"""

import argparse
import fcntl
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".nanobot" / "workspace" / "data" / "todo"
TODOS_FILE = DATA_DIR / "todos.json"
GROUPS_FILE = DATA_DIR / "groups.json"
NOTES_DIR = DATA_DIR / "notes"

VALID_STATUSES = ("todo", "doing", "done", "cancelled")
VALID_PRIORITIES = ("high", "medium", "low")
VALID_GROUP_STATUSES = ("active", "archived")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
PRIORITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}
STATUS_ICONS = {"todo": "⬜", "doing": "🔧", "done": "✅", "cancelled": "🚫"}
GROUP_STATUS_ICONS = {"active": "🟢", "archived": "📦"}


# ── Data Layer ─────────────────────────────────────────────────────────

def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def load_todos() -> list[dict]:
    ensure_dirs()
    if not TODOS_FILE.exists():
        return []
    with open(TODOS_FILE, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return data


def save_todos(todos: list[dict]):
    ensure_dirs()
    tmp_file = TODOS_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    tmp_file.rename(TODOS_FILE)


def gen_id(todos: list[dict]) -> str:
    existing = {t["id"] for t in todos}
    for _ in range(100):
        new_id = uuid.uuid4().hex[:8]
        if new_id not in existing:
            return new_id
    raise RuntimeError("Failed to generate unique ID")


def find_todo(todos: list[dict], todo_id: str) -> dict | None:
    """Find by exact match or prefix match (>= 4 chars)."""
    for t in todos:
        if t["id"] == todo_id:
            return t
    if len(todo_id) >= 4:
        matches = [t for t in todos if t["id"].startswith(todo_id)]
        if len(matches) == 1:
            return matches[0]
    return None


# ── Group Data Layer ───────────────────────────────────────────────────

def load_groups() -> list[dict]:
    ensure_dirs()
    if not GROUPS_FILE.exists():
        return []
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            data = json.load(f)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return data


def save_groups(groups: list[dict]):
    ensure_dirs()
    tmp_file = GROUPS_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(groups, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    tmp_file.rename(GROUPS_FILE)


def find_group(groups: list[dict], group_id: str) -> dict | None:
    """Find group by exact match or prefix match (>= 4 chars)."""
    for g in groups:
        if g["id"] == group_id:
            return g
    if len(group_id) >= 4:
        matches = [g for g in groups if g["id"].startswith(group_id)]
        if len(matches) == 1:
            return matches[0]
    return None


def group_note_path(group_id: str) -> Path:
    """Return the note file path for a group."""
    return NOTES_DIR / f"group-{group_id}.md"


def append_group_note(group_id: str, line: str):
    """Append a line to a group's note file."""
    ensure_dirs()
    note_file = group_note_path(group_id)
    existing = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    note_file.write_text(existing + line + "\n", encoding="utf-8")


# ── Commands ───────────────────────────────────────────────────────────

def cmd_add(args):
    todos = load_todos()
    new_id = gen_id(todos)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []

    todo = {
        "id": new_id,
        "title": args.title,
        "category": args.category or "inbox",
        "priority": args.priority or "medium",
        "status": "todo",
        "created_at": now,
        "due_date": args.due or None,
        "completed_at": None,
        "session_id": args.session_id or None,
        "tags": tags,
        "has_note": False,
    }

    # Handle note
    if args.note:
        note_content = args.note.replace("\\n", "\n")
        if note_content.startswith("@") and os.path.isfile(note_content[1:]):
            with open(note_content[1:], "r", encoding="utf-8") as f:
                note_content = f.read()
        note_path = NOTES_DIR / f"{new_id}.md"
        note_path.write_text(note_content, encoding="utf-8")
        todo["has_note"] = True

    todos.append(todo)
    save_todos(todos)
    print(f"✅ 已添加待办 [{new_id}]: {args.title}")
    if todo["has_note"]:
        print(f"   📄 说明文档: notes/{new_id}.md")


def cmd_list(args):
    todos = load_todos()
    if not todos:
        print("📋 暂无待办事项")
        return

    # Filter
    if not args.all:
        if args.status:
            statuses = set(args.status.split(","))
            todos = [t for t in todos if t["status"] in statuses]
        else:
            # Default: exclude done and cancelled
            todos = [t for t in todos if t["status"] not in ("done", "cancelled")]

    if args.category:
        todos = [t for t in todos if t["category"] == args.category]
    if args.priority:
        todos = [t for t in todos if t["priority"] == args.priority]
    if args.tag:
        todos = [t for t in todos if args.tag in t.get("tags", [])]
    if args.tag_none:
        exclude_tags = set(t.strip() for t in args.tag_none.split(","))
        todos = [t for t in todos if not (set(t.get("tags", [])) & exclude_tags)]
    if args.group is not None:
        if args.group == "":
            todos = [t for t in todos if t.get("group") is None]
        else:
            groups = load_groups()
            group = find_group(groups, args.group)
            if not group:
                print(f"❌ 未找到分组 ID: {args.group}")
                sys.exit(1)
            todos = [t for t in todos if t.get("group") == group["id"]]

    if not todos:
        print("📋 没有匹配的待办事项")
        return

    # Sort
    sort_key = args.sort or "priority"
    if sort_key == "priority":
        todos.sort(key=lambda t: PRIORITY_ORDER.get(t["priority"], 9))
    elif sort_key == "created":
        todos.sort(key=lambda t: t["created_at"], reverse=True)
    elif sort_key == "due":
        todos.sort(key=lambda t: t.get("due_date") or "9999-99-99")

    # JSON output mode
    output_format = getattr(args, "format", "table") or "table"
    if output_format == "json":
        # Build group name lookup for enrichment
        groups = load_groups()
        group_map = {g["id"]: g["name"] for g in groups}
        result = []
        for t in todos:
            item = dict(t)
            item["group_name"] = group_map.get(t.get("group"), None)
            result.append(item)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # Table output (default)
    # Build group name map for group column
    groups = load_groups()
    group_map = {g["id"]: g["name"] for g in groups}
    GROUP_COL_WIDTH = 14

    print(f"📋 待办事项 ({len(todos)} 条)")
    print("-" * 88)
    print(f"{'ID':<10} {'P':>2} {'状态':>4} {'分类':<8} {'标题':<28} {'分组':<{GROUP_COL_WIDTH}} {'截止':<12}")
    print("-" * 88)
    for t in todos:
        p_icon = PRIORITY_ICONS.get(t["priority"], "⚪")
        s_icon = STATUS_ICONS.get(t["status"], "❓")
        due = t.get("due_date") or "-"
        title = t["title"]
        if len(title) > 26:
            title = title[:25] + "…"
        note_mark = "📄" if t.get("has_note") else "  "
        # Group column
        gid = t.get("group")
        if gid:
            gname = group_map.get(gid, gid)
            # Truncate if too long (account for possible multi-byte chars)
            if len(gname) > GROUP_COL_WIDTH - 1:
                gname = gname[:GROUP_COL_WIDTH - 2] + "…"
        else:
            gname = "-"
        print(f"{t['id']:<10} {p_icon} {s_icon}  {t['category']:<8} {title:<26}{note_mark} {gname:<{GROUP_COL_WIDTH}} {due:<12}")
    print("-" * 88)


def cmd_show(args):
    todos = load_todos()
    todo = find_todo(todos, args.id)
    if not todo:
        print(f"❌ 未找到待办 ID: {args.id}")
        sys.exit(1)

    p_icon = PRIORITY_ICONS.get(todo["priority"], "⚪")
    s_icon = STATUS_ICONS.get(todo["status"], "❓")

    print(f"{'='*50}")
    print(f"📌 {todo['title']}")
    print(f"{'='*50}")
    print(f"  ID:       {todo['id']}")
    print(f"  分类:     {todo['category']}")
    print(f"  优先级:   {p_icon} {todo['priority']}")
    print(f"  状态:     {s_icon} {todo['status']}")
    print(f"  创建时间: {todo['created_at']}")
    if todo.get("due_date"):
        print(f"  截止日期: {todo['due_date']}")
    if todo.get("completed_at"):
        print(f"  完成时间: {todo['completed_at']}")
    if todo.get("session_id"):
        print(f"  关联会话: {todo['session_id']}")
    if todo.get("tags"):
        print(f"  标签:     {', '.join(todo['tags'])}")
    if todo.get("group"):
        groups = load_groups()
        group = find_group(groups, todo["group"])
        group_display = f"{todo['group']}"
        if group:
            group_display += f" ({group['name']})"
        print(f"  分组:     {group_display}")

    # Show note
    note_path = NOTES_DIR / f"{todo['id']}.md"
    if note_path.exists():
        print(f"\n📄 说明文档:")
        print("-" * 40)
        print(note_path.read_text(encoding="utf-8"))
        print("-" * 40)


def cmd_update(args):
    todos = load_todos()
    todo = find_todo(todos, args.id)
    if not todo:
        print(f"❌ 未找到待办 ID: {args.id}")
        sys.exit(1)

    changed = []
    if args.title:
        todo["title"] = args.title
        changed.append("title")
    if args.category:
        todo["category"] = args.category
        changed.append("category")
    if args.priority:
        if args.priority not in VALID_PRIORITIES:
            print(f"❌ 无效优先级: {args.priority}（可选: {', '.join(VALID_PRIORITIES)}）")
            sys.exit(1)
        todo["priority"] = args.priority
        changed.append("priority")
    if args.status:
        if args.status not in VALID_STATUSES:
            print(f"❌ 无效状态: {args.status}（可选: {', '.join(VALID_STATUSES)}）")
            sys.exit(1)
        todo["status"] = args.status
        if args.status == "done":
            todo["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            # 自动添加"已完成"tag
            if "已完成" not in todo.get("tags", []):
                todo.setdefault("tags", []).append("已完成")
        changed.append("status")
    if args.due:
        todo["due_date"] = args.due
        changed.append("due_date")
    if args.tags:
        todo["tags"] = [t.strip() for t in args.tags.split(",")]
        changed.append("tags")
    if args.session_id:
        todo["session_id"] = args.session_id
        changed.append("session_id")
    if args.group is not None:
        if args.group == "":
            todo["group"] = None
            changed.append("group (cleared)")
        else:
            groups = load_groups()
            group = find_group(groups, args.group)
            if not group:
                print(f"❌ 未找到分组 ID: {args.group}")
                sys.exit(1)
            todo["group"] = group["id"]
            changed.append(f"group → {group['id']}")

    if not changed:
        print("⚠️ 未指定任何更新字段")
        return

    save_todos(todos)
    print(f"✅ 已更新 [{todo['id']}]: {', '.join(changed)}")


def cmd_note(args):
    todos = load_todos()
    todo = find_todo(todos, args.id)
    if not todo:
        print(f"❌ 未找到待办 ID: {args.id}")
        sys.exit(1)

    note_path = NOTES_DIR / f"{todo['id']}.md"

    if args.write:
        content = args.write.replace("\\n", "\n")
        if content.startswith("@") and os.path.isfile(content[1:]):
            with open(content[1:], "r", encoding="utf-8") as f:
                content = f.read()
        note_path.write_text(content, encoding="utf-8")
        todo["has_note"] = True
        save_todos(todos)
        print(f"✅ 已写入说明文档: notes/{todo['id']}.md")
    elif args.append:
        content = args.append.replace("\\n", "\n")
        if content.startswith("@") and os.path.isfile(content[1:]):
            with open(content[1:], "r", encoding="utf-8") as f:
                content = f.read()
        existing = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
        note_path.write_text(existing + "\n" + content, encoding="utf-8")
        todo["has_note"] = True
        save_todos(todos)
        print(f"✅ 已追加说明文档: notes/{todo['id']}.md")
    else:
        # Read mode
        if note_path.exists():
            print(note_path.read_text(encoding="utf-8"))
        else:
            print("📄 暂无说明文档")


def cmd_done(args):
    todos = load_todos()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    done_count = 0

    for todo_id in args.ids:
        todo = find_todo(todos, todo_id)
        if not todo:
            print(f"⚠️ 未找到待办 ID: {todo_id}")
            continue
        todo["status"] = "done"
        todo["completed_at"] = now
        # 自动添加"已完成"tag，避免 done 状态与"已对齐"等 tag 产生歧义
        if "已完成" not in todo.get("tags", []):
            todo.setdefault("tags", []).append("已完成")
        print(f"✅ 已完成 [{todo['id']}]: {todo['title']}")
        done_count += 1

    if done_count > 0:
        save_todos(todos)


def cmd_delete(args):
    todos = load_todos()
    todo = find_todo(todos, args.id)
    if not todo:
        print(f"❌ 未找到待办 ID: {args.id}")
        sys.exit(1)

    if args.hard:
        todos.remove(todo)
        note_path = NOTES_DIR / f"{todo['id']}.md"
        if note_path.exists():
            note_path.unlink()
        save_todos(todos)
        print(f"🗑️ 已永久删除 [{todo['id']}]: {todo['title']}")
    else:
        todo["status"] = "cancelled"
        save_todos(todos)
        print(f"🚫 已取消 [{todo['id']}]: {todo['title']}")


def cmd_summary(args):
    todos = load_todos()
    active = [t for t in todos if t["status"] not in ("done", "cancelled")]

    if not active:
        print("📋 暂无活跃待办事项")
        return

    if getattr(args, "by_group", False):
        _summary_by_group(active)
        return

    # Stats
    total = len(active)
    by_status = {}
    by_category = {}
    by_priority = {}

    for t in active:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        by_category[t["category"]] = by_category.get(t["category"], [])
        by_category[t["category"]].append(t)
        by_priority[t["priority"]] = by_priority.get(t["priority"], 0) + 1

    # Overdue
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = [t for t in active if t.get("due_date") and t["due_date"] < today]

    print(f"📊 待办摘要")
    print(f"{'='*50}")
    print(f"  活跃: {total} 条 | ", end="")
    print(" | ".join(f"{STATUS_ICONS.get(s, '?')} {s}: {c}" for s, c in by_status.items()))
    print(f"  优先级: ", end="")
    print(" | ".join(f"{PRIORITY_ICONS.get(p, '?')} {p}: {c}" for p, c in sorted(by_priority.items(), key=lambda x: PRIORITY_ORDER.get(x[0], 9))))

    if overdue:
        print(f"\n  ⏰ 已过期: {len(overdue)} 条")
        for t in overdue:
            print(f"    🔴 [{t['id']}] {t['title']} (截止: {t['due_date']})")

    print(f"\n📂 按分类:")
    print("-" * 50)
    for cat, items in sorted(by_category.items()):
        print(f"\n  【{cat}】({len(items)} 条)")
        items.sort(key=lambda t: PRIORITY_ORDER.get(t["priority"], 9))
        for t in items:
            p_icon = PRIORITY_ICONS.get(t["priority"], "⚪")
            s_icon = STATUS_ICONS.get(t["status"], "❓")
            due = f" (截止: {t['due_date']})" if t.get("due_date") else ""
            print(f"    {p_icon} {s_icon} [{t['id']}] {t['title']}{due}")


def _summary_by_group(active: list[dict]):
    """Summary aggregated by group."""
    groups = load_groups()
    group_map = {g["id"]: g for g in groups}

    # Bucket todos by group
    by_group: dict[str | None, list[dict]] = {}
    for t in active:
        gid = t.get("group")
        by_group.setdefault(gid, []).append(t)

    print(f"📊 待办摘要 (按分组)")
    print(f"{'='*60}")
    print(f"  活跃: {len(active)} 条")
    print()

    # Print grouped
    for gid in list(by_group.keys()):
        if gid is None:
            continue
        items = by_group[gid]
        group = group_map.get(gid)
        gname = group["name"] if group else gid
        p_counts = {}
        for t in items:
            p_counts[t["priority"]] = p_counts.get(t["priority"], 0) + 1
        p_str = " ".join(f"{PRIORITY_ICONS.get(p, '?')} {c}" for p, c in sorted(p_counts.items(), key=lambda x: PRIORITY_ORDER.get(x[0], 9)))
        print(f"  📁 {gname} ({len(items)} 条) — {p_str}")

    # Ungrouped
    if None in by_group:
        items = by_group[None]
        p_counts = {}
        for t in items:
            p_counts[t["priority"]] = p_counts.get(t["priority"], 0) + 1
        p_str = " ".join(f"{PRIORITY_ICONS.get(p, '?')} {c}" for p, c in sorted(p_counts.items(), key=lambda x: PRIORITY_ORDER.get(x[0], 9)))
        print(f"  📁 未分组 ({len(items)} 条) — {p_str}")

    print(f"{'='*60}")


# ── Group Commands ─────────────────────────────────────────────────────

def cmd_group(args):
    """Dispatch group subcommands."""
    group_commands = {
        "add": cmd_group_add,
        "list": cmd_group_list,
        "show": cmd_group_show,
        "update": cmd_group_update,
        "note": cmd_group_note,
    }
    if not args.group_command:
        print("❌ 请指定 group 子命令: add, list, show, update, note")
        sys.exit(1)
    group_commands[args.group_command](args)


def cmd_group_add(args):
    groups = load_groups()
    # Check duplicate
    if any(g["id"] == args.id for g in groups):
        print(f"❌ 分组 ID 已存在: {args.id}")
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    group = {
        "id": args.id,
        "name": args.name,
        "description": args.desc or "",
        "principle": args.principle or "",
        "created_at": now,
        "session_id": args.session_id or None,
        "status": "active",
    }
    groups.append(group)
    save_groups(groups)
    print(f"✅ 已创建分组 [{args.id}]: {args.name}")


def cmd_group_list(args):
    groups = load_groups()
    if not groups:
        print("📁 暂无分组")
        return

    if not args.all:
        groups = [g for g in groups if g["status"] == "active"]

    if not groups:
        print("📁 没有活跃分组")
        return

    todos = load_todos()
    active_todos = [t for t in todos if t["status"] not in ("done", "cancelled")]

    print(f"📁 分组列表 ({len(groups)} 个)")
    print("-" * 60)
    print(f"{'ID':<20} {'状态':<6} {'待办':>4}  {'名称'}")
    print("-" * 60)
    for g in groups:
        s_icon = GROUP_STATUS_ICONS.get(g["status"], "❓")
        count = sum(1 for t in active_todos if t.get("group") == g["id"])
        print(f"{g['id']:<20} {s_icon:<5} {count:>4}  {g['name']}")
    print("-" * 60)


def cmd_group_show(args):
    groups = load_groups()
    group = find_group(groups, args.group_id)
    if not group:
        print(f"❌ 未找到分组 ID: {args.group_id}")
        sys.exit(1)

    todos = load_todos()
    group_todos = [t for t in todos if t.get("group") == group["id"]
                   and t["status"] not in ("done", "cancelled")]

    # Priority distribution
    p_counts = {}
    for t in group_todos:
        p_counts[t["priority"]] = p_counts.get(t["priority"], 0) + 1
    p_str = " ".join(f"{PRIORITY_ICONS.get(p, '?')} {c}" for p, c in sorted(p_counts.items(), key=lambda x: PRIORITY_ORDER.get(x[0], 9)))

    s_icon = GROUP_STATUS_ICONS.get(group["status"], "❓")

    print(f"{'='*50}")
    print(f"📁 {group['id']} — {group['name']}")
    print(f"{'='*50}")
    if group.get("description"):
        print(f"  描述:     {group['description']}")
    if group.get("principle"):
        print(f"  原则:     {group['principle']}")
    print(f"  状态:     {s_icon} {group['status']}")
    print(f"  创建时间: {group['created_at']}")
    if group.get("session_id"):
        print(f"  关联会话: {group['session_id']}")
    print(f"  待办数量: {len(group_todos)}" + (f" ({p_str})" if p_str else ""))

    if group_todos:
        group_todos.sort(key=lambda t: PRIORITY_ORDER.get(t["priority"], 9))
        print(f"\n📋 下属待办 ({len(group_todos)} 条)")
        print("-" * 72)
        print(f"{'ID':<10} {'P':>2} {'状态':>4} {'标题'}")
        print("-" * 72)
        for t in group_todos:
            p_icon = PRIORITY_ICONS.get(t["priority"], "⚪")
            s_icon_t = STATUS_ICONS.get(t["status"], "❓")
            print(f"{t['id']:<10} {p_icon} {s_icon_t}  {t['title']}")
        print("-" * 72)

    # Show group note
    note_file = group_note_path(group["id"])
    if note_file.exists():
        content = note_file.read_text(encoding="utf-8").strip()
        if content:
            print(f"\n📄 分组说明:")
            print("-" * 40)
            print(content)
            print("-" * 40)


def cmd_group_update(args):
    groups = load_groups()
    group = find_group(groups, args.group_id)
    if not group:
        print(f"❌ 未找到分组 ID: {args.group_id}")
        sys.exit(1)

    changed = []
    if args.name:
        group["name"] = args.name
        changed.append("name")
    if args.desc is not None:
        group["description"] = args.desc
        changed.append("description")
    if args.principle is not None:
        group["principle"] = args.principle
        changed.append("principle")
    if args.status:
        if args.status not in VALID_GROUP_STATUSES:
            print(f"❌ 无效状态: {args.status}（可选: {', '.join(VALID_GROUP_STATUSES)}）")
            sys.exit(1)
        group["status"] = args.status
        changed.append("status")

    if not changed:
        print("⚠️ 未指定任何更新字段")
        return

    save_groups(groups)
    print(f"✅ 已更新分组 [{group['id']}]: {', '.join(changed)}")


def cmd_group_note(args):
    groups = load_groups()
    group = find_group(groups, args.group_id)
    if not group:
        print(f"❌ 未找到分组 ID: {args.group_id}")
        sys.exit(1)

    note_file = group_note_path(group["id"])

    if args.write:
        content = args.write.replace("\\n", "\n")
        if content.startswith("@") and os.path.isfile(content[1:]):
            with open(content[1:], "r", encoding="utf-8") as f:
                content = f.read()
        ensure_dirs()
        note_file.write_text(content, encoding="utf-8")
        print(f"✅ 已写入分组说明: notes/group-{group['id']}.md")
    elif args.append:
        content = args.append.replace("\\n", "\n")
        if content.startswith("@") and os.path.isfile(content[1:]):
            with open(content[1:], "r", encoding="utf-8") as f:
                content = f.read()
        existing = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        ensure_dirs()
        note_file.write_text(existing + content + "\n", encoding="utf-8")
        print(f"✅ 已追加分组说明: notes/group-{group['id']}.md")
    else:
        # Read mode
        if note_file.exists():
            print(note_file.read_text(encoding="utf-8"))
        else:
            print("📄 暂无分组说明文档")


# ── Move Command ───────────────────────────────────────────────────────

def cmd_move(args):
    todos = load_todos()
    todo = find_todo(todos, args.id)
    if not todo:
        print(f"❌ 未找到待办 ID: {args.id}")
        sys.exit(1)

    groups = load_groups()
    target_group = find_group(groups, args.to)
    if not target_group:
        print(f"❌ 未找到目标分组 ID: {args.to}")
        sys.exit(1)

    old_group_id = todo.get("group")
    new_group_id = target_group["id"]

    if old_group_id == new_group_id:
        print(f"⚠️ 待办已在分组 [{new_group_id}] 中")
        return

    # Update todo
    todo["group"] = new_group_id
    save_todos(todos)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    todo_ref = f"[{todo['id']}] {todo['title']}"

    # Append to target group note
    if old_group_id:
        line = f"- [{now}] 从 `{old_group_id}` 移入: {todo_ref}"
    else:
        line = f"- [{now}] 移入 (未分组): {todo_ref}"
    append_group_note(new_group_id, line)

    # Append to source group note
    if old_group_id:
        old_group = find_group(groups, old_group_id)
        if old_group:
            line = f"- [{now}] 移出至 `{new_group_id}`: {todo_ref}"
            append_group_note(old_group_id, line)

    print(f"✅ 已移动 [{todo['id']}] → 分组 [{new_group_id}]")
    if old_group_id:
        print(f"   📝 已记录变更: {old_group_id} → {new_group_id}")
    else:
        print(f"   📝 已记录变更: 未分组 → {new_group_id}")


# ── CLI Parser ─────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(description="Todo list manager")
    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="添加待办")
    p_add.add_argument("--title", "-t", required=True, help="标题")
    p_add.add_argument("--category", "-c", help="分类 (默认: inbox)")
    p_add.add_argument("--priority", "-p", choices=VALID_PRIORITIES, help="优先级")
    p_add.add_argument("--due", "-d", help="截止日期 (YYYY-MM-DD)")
    p_add.add_argument("--tags", help="标签 (逗号分隔)")
    p_add.add_argument("--session-id", "-s", help="关联 session 文件名 (如 webchat_1773150605)")
    p_add.add_argument("--note", "-n", help="说明文档 (文本或 @filepath)")

    # list
    p_list = sub.add_parser("list", help="列出待办")
    p_list.add_argument("--status", help="状态过滤 (逗号分隔)")
    p_list.add_argument("--category", "-c", help="分类过滤")
    p_list.add_argument("--priority", "-p", help="优先级过滤")
    p_list.add_argument("--tag", help="标签过滤 (含指定标签)")
    p_list.add_argument("--tag-none", help="排除标签过滤 (逗号分隔，排除含任一标签的)")
    p_list.add_argument("--group", default=None, help="分组过滤 (空字符串=未分组)")
    p_list.add_argument("--sort", choices=["priority", "created", "due"], help="排序方式")
    p_list.add_argument("--all", "-a", action="store_true", help="显示所有（含已完成/取消）")
    p_list.add_argument("--format", choices=["table", "json"], default="table", help="输出格式 (默认: table)")

    # show
    p_show = sub.add_parser("show", help="查看详情")
    p_show.add_argument("id", help="待办 ID")

    # update
    p_update = sub.add_parser("update", help="更新待办")
    p_update.add_argument("id", help="待办 ID")
    p_update.add_argument("--title", "-t", help="新标题")
    p_update.add_argument("--category", "-c", help="新分类")
    p_update.add_argument("--priority", "-p", choices=VALID_PRIORITIES, help="新优先级")
    p_update.add_argument("--status", choices=VALID_STATUSES, help="新状态")
    p_update.add_argument("--due", "-d", help="新截止日期")
    p_update.add_argument("--tags", help="新标签 (逗号分隔)")
    p_update.add_argument("--session-id", "-s", help="关联 session 文件名 (如 webchat_1773150605)")
    p_update.add_argument("--group", default=None, help="设置分组 (空字符串=清除分组)")

    # note
    p_note = sub.add_parser("note", help="管理说明文档")
    p_note.add_argument("id", help="待办 ID")
    p_note.add_argument("--write", "-w", help="覆盖写入")
    p_note.add_argument("--append", "-a", help="追加内容")

    # done
    p_done = sub.add_parser("done", help="标记完成")
    p_done.add_argument("ids", nargs="+", help="待办 ID (可多个)")

    # delete
    p_delete = sub.add_parser("delete", help="删除待办")
    p_delete.add_argument("id", help="待办 ID")
    p_delete.add_argument("--hard", action="store_true", help="永久删除")

    # summary
    p_summary = sub.add_parser("summary", help="待办摘要")
    p_summary.add_argument("--by-group", action="store_true", help="按分组聚合")

    # move
    p_move = sub.add_parser("move", help="移动待办到分组")
    p_move.add_argument("id", help="待办 ID")
    p_move.add_argument("--to", required=True, help="目标分组 ID")

    # group (with subcommands)
    p_group = sub.add_parser("group", help="分组管理")
    group_sub = p_group.add_subparsers(dest="group_command")

    # group add
    pg_add = group_sub.add_parser("add", help="创建分组")
    pg_add.add_argument("--id", required=True, help="分组 ID")
    pg_add.add_argument("--name", required=True, help="分组名称")
    pg_add.add_argument("--desc", help="分组描述")
    pg_add.add_argument("--principle", help="分组原则")
    pg_add.add_argument("--session-id", help="关联会话")

    # group list
    pg_list = group_sub.add_parser("list", help="列出分组")
    pg_list.add_argument("--all", "-a", action="store_true", help="包含已归档分组")

    # group show
    pg_show = group_sub.add_parser("show", help="查看分组详情")
    pg_show.add_argument("group_id", help="分组 ID")

    # group update
    pg_update = group_sub.add_parser("update", help="更新分组")
    pg_update.add_argument("group_id", help="分组 ID")
    pg_update.add_argument("--name", help="新名称")
    pg_update.add_argument("--desc", default=None, help="新描述")
    pg_update.add_argument("--principle", default=None, help="新原则")
    pg_update.add_argument("--status", choices=VALID_GROUP_STATUSES, help="新状态")

    # group note
    pg_note = group_sub.add_parser("note", help="分组说明文档")
    pg_note.add_argument("group_id", help="分组 ID")
    pg_note.add_argument("--write", "-w", help="覆盖写入")
    pg_note.add_argument("--append", "-a", help="追加内容")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "add": cmd_add,
        "list": cmd_list,
        "show": cmd_show,
        "update": cmd_update,
        "note": cmd_note,
        "done": cmd_done,
        "delete": cmd_delete,
        "summary": cmd_summary,
        "move": cmd_move,
        "group": cmd_group,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
