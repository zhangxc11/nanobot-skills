#!/usr/bin/env python3
"""态势感知采集模块。

从 4 个数据源（BRIEFING、INBOX、todo、CIL 日报）采集信息，
生成 ≤2K 字符的结构化摘要，写入 awareness-cache.txt。

每个数据源独立 try/except，单个数据源失败不影响其他数据源。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import yaml

# ── 路径推导 ─────────────────────────────────────────────────
# 不硬编码 skill 名称，从 __file__ 向上推导 workspace 根目录

_SCRIPT_DIR = Path(__file__).resolve().parent          # scripts/
_SKILL_DIR = _SCRIPT_DIR.parent                        # agent-brain/

# 字符预算
BUDGET_BRIEFING = 600
BUDGET_INBOX = 400
BUDGET_TODO = 500
BUDGET_CIL = 500
BUDGET_CIL_DEEP = 800


def _find_workspace(hint: str = None) -> Path:
    """定位 workspace 根目录。

    优先级：
    1. 显式传入的 hint
    2. NANOBOT_WORKSPACE 环境变量
    3. 向上查找包含 data/brain 的目录
    """
    if hint:
        p = Path(hint).resolve()
        if p.exists():
            return p

    env = os.environ.get("NANOBOT_WORKSPACE")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    # 向上查找
    cur = _SKILL_DIR
    for _ in range(10):
        if (cur / "data" / "brain").is_dir():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    # 最终 fallback
    default = Path.home() / ".nanobot" / "workspace"
    if default.exists():
        return default

    raise RuntimeError("无法定位 workspace 目录。请通过 --workspace 参数或 NANOBOT_WORKSPACE 环境变量指定。")


def _get_data_dir(data_dir: str = None) -> Path:
    """获取 brain data 目录（mind/）。"""
    env = os.environ.get("BRAIN_DATA_DIR")
    if data_dir:
        return Path(data_dir).resolve()
    if env:
        return Path(env).resolve()
    return None  # 由调用者决定 fallback


def _truncate(text: str, max_chars: int) -> str:
    """截断文本到指定字符数，保留完整行。"""
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    result = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_chars - 20:  # 留 20 字符给省略标记
            break
        result.append(line)
        total += len(line) + 1
    result.append("...（已截断）")
    return "\n".join(result)


# ── 数据源采集 ────────────────────────────────────────────────

def collect_briefing(workspace: Path) -> dict:
    """采集 BRIEFING.md 数据。"""
    briefing_path = workspace / "data" / "brain" / "BRIEFING.md"
    if not briefing_path.exists():
        return {"error": "BRIEFING.md 不存在"}

    content = briefing_path.read_text(encoding="utf-8")

    # 提取各段落
    sections = {}
    current_section = None
    current_lines = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_section:
                sections[current_section] = "\n".join(current_lines)
            current_section = line.strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_section:
        sections[current_section] = "\n".join(current_lines)

    urgent = []
    in_progress = []
    waiting = []

    for key, val in sections.items():
        if "紧急" in key or "🔴" in key:
            for l in val.strip().split("\n"):
                l = l.strip()
                if l.startswith("- "):
                    urgent.append(l[2:].strip())
        elif "进行中" in key or "🔵" in key:
            for l in val.strip().split("\n"):
                l = l.strip()
                if l.startswith("- "):
                    in_progress.append(l[2:].strip())
        elif "等待" in key or "⏳" in key:
            for l in val.strip().split("\n"):
                l = l.strip()
                if l.startswith("- "):
                    waiting.append(l[2:].strip())

    return {
        "urgent": urgent,
        "in_progress": in_progress,
        "waiting": waiting,
    }


def collect_inbox(workspace: Path) -> dict:
    """采集 INBOX.jsonl 数据（只读统计）。"""
    inbox_path = workspace / "data" / "brain" / "INBOX.jsonl"
    if not inbox_path.exists():
        return {"pending_count": 0, "recent": [], "note": "INBOX.jsonl 不存在"}

    pending = []
    total = 0

    for line in inbox_path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            total += 1
            if item.get("status") == "pending":
                pending.append({
                    "id": item.get("id", ""),
                    "summary": item.get("summary", "")[:100],
                    "priority": item.get("priority", "normal"),
                    "time": item.get("time", ""),
                })
        except json.JSONDecodeError:
            continue

    # 取最新 3 条 pending
    recent = sorted(pending, key=lambda x: x.get("time", ""), reverse=True)[:3]

    return {
        "pending_count": len(pending),
        "total_count": total,
        "recent": recent,
    }


def collect_todo(workspace: Path) -> dict:
    """采集 todo summary（执行 todo.py summary）。"""
    todo_script = workspace / "skills" / "todo" / "scripts" / "todo.py"
    if not todo_script.exists():
        return {"error": f"todo.py 不存在: {todo_script}"}

    try:
        result = subprocess.run(
            [sys.executable, str(todo_script), "summary"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(workspace),
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            return {"error": f"todo.py 返回码 {result.returncode}: {result.stderr[:200]}"}
        return {"summary_text": output}
    except subprocess.TimeoutExpired:
        return {"error": "todo.py 执行超时(10s)"}
    except Exception as e:
        return {"error": f"todo.py 执行异常: {str(e)[:200]}"}


def collect_cil_report(workspace: Path) -> dict:
    """采集 CIL 最新日报摘要。"""
    reports_dir = workspace / "data" / "brain" / "reports" / "daily"
    if not reports_dir.exists():
        return {"error": "CIL 日报目录不存在"}

    md_files = sorted(reports_dir.glob("*.md"), reverse=True)
    if not md_files:
        return {"error": "无 CIL 日报文件"}

    latest = md_files[0]
    content = latest.read_text(encoding="utf-8")
    report_date = latest.stem  # e.g., 2026-04-06

    # 提取 ## 1. 概览 段（到下一个 ## 为止）
    overview = ""
    anomalies = ""
    current_section = None
    current_lines = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_section:
                text = "\n".join(current_lines).strip()
                if "概览" in current_section or "1." in current_section:
                    overview = text
                elif "异常" in current_section or "🚨" in current_section:
                    anomalies = text
            current_section = line.strip()
            current_lines = []
        else:
            current_lines.append(line)

    # 处理最后一个 section
    if current_section:
        text = "\n".join(current_lines).strip()
        if "概览" in current_section or "1." in current_section:
            overview = text
        elif "异常" in current_section or "🚨" in current_section:
            anomalies = text

    return {
        "date": report_date,
        "overview": overview,
        "anomalies": anomalies,
    }



def collect_cil_deep(workspace: Path) -> dict:
    """深度采集 CIL 数据：insights/actions/reports 全貌。"""
    cil_dir = workspace / "data" / "cil"
    if not cil_dir.exists():
        return {"error": "CIL 数据目录不存在"}

    result = {
        "insights": [],
        "actions": [],
        "recent_reports": [],
        "status_distribution": {"open": 0, "resolved": 0, "dismissed": 0},
        "action_distribution": {"proposed": 0, "approved": 0, "implementing": 0, "tracking": 0, "closed": 0, "dismissed": 0},
        "stale_items": [],  # >7天未更新
    }

    now = datetime.now(timezone(timedelta(hours=8)))
    stale_threshold = now - timedelta(days=7)

    # ── Insights ──
    insights_dir = cil_dir / "insights"
    if insights_dir.exists():
        for f in sorted(insights_dir.glob("INS-*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if not data:
                    continue
                status = data.get("status", "unknown")
                result["status_distribution"][status] = result["status_distribution"].get(status, 0) + 1

                entry = {
                    "id": data.get("id", f.stem),
                    "title": data.get("title", "")[:80],
                    "status": status,
                    "severity": data.get("severity", "info"),
                }
                result["insights"].append(entry)

                # Check staleness for open insights
                if status == "open":
                    updated = data.get("updated_at", "")
                    if updated:
                        try:
                            updated_dt = datetime.fromisoformat(str(updated))
                            if updated_dt.tzinfo is None:
                                updated_dt = updated_dt.replace(tzinfo=timezone(timedelta(hours=8)))
                            if updated_dt < stale_threshold:
                                result["stale_items"].append({
                                    "id": entry["id"],
                                    "title": entry["title"],
                                    "type": "insight",
                                    "days_stale": (now - updated_dt).days,
                                })
                        except (ValueError, TypeError):
                            pass
            except Exception:
                continue

    # ── Actions ──
    actions_dir = cil_dir / "actions"
    if actions_dir.exists():
        for f in sorted(actions_dir.glob("ACT-*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if not data:
                    continue
                status = data.get("status", "unknown")
                result["action_distribution"][status] = result["action_distribution"].get(status, 0) + 1

                entry = {
                    "id": data.get("id", f.stem),
                    "title": data.get("title", "")[:80],
                    "status": status,
                    "effort": data.get("effort", "?"),
                    "insight_id": data.get("insight_id", ""),
                }
                result["actions"].append(entry)

                # Check staleness for active actions
                if status in ("proposed", "approved", "implementing", "tracking"):
                    updated = data.get("updated_at", "")
                    if updated:
                        try:
                            updated_dt = datetime.fromisoformat(str(updated))
                            if updated_dt.tzinfo is None:
                                updated_dt = updated_dt.replace(tzinfo=timezone(timedelta(hours=8)))
                            if updated_dt < stale_threshold:
                                result["stale_items"].append({
                                    "id": entry["id"],
                                    "title": entry["title"],
                                    "type": "action",
                                    "status": status,
                                    "days_stale": (now - updated_dt).days,
                                })
                        except (ValueError, TypeError):
                            pass
            except Exception:
                continue

    # ── Recent reports (latest 3 run dates) ──
    runs_dir = cil_dir / "reports" / "runs"
    if runs_dir.exists():
        md_files = sorted(runs_dir.glob("*.md"), reverse=True)[:3]
        for md in md_files:
            try:
                text = md.read_text(encoding="utf-8")
                # Extract warning/critical counts from report
                warnings = text.count("⚠️") + text.count("warning")
                criticals = text.count("🔴") + text.count("critical")
                # Extract concern statuses
                healthy = text.count("✅")
                unhealthy = text.count("⚠️")
                result["recent_reports"].append({
                    "date": md.stem,
                    "healthy_concerns": healthy,
                    "warning_concerns": unhealthy,
                })
            except Exception:
                continue

    # ── Weekly reports (latest) ──
    weekly_dir = cil_dir / "reports" / "weekly"
    if weekly_dir.exists():
        weekly_mds = sorted(weekly_dir.glob("*.md"), reverse=True)[:1]
        if weekly_mds:
            result["latest_weekly"] = weekly_mds[0].stem

    return result

# ── 主采集逻辑 ────────────────────────────────────────────────

def collect_snapshot(workspace: Path) -> dict:
    """采集态势快照，返回结构化数据。

    Args:
        workspace: nanobot workspace 根目录

    Returns:
        {
            "timestamp": "ISO 8601",
            "briefing": {...},
            "inbox": {...},
            "todo": {...},
            "cil": {...},
            "errors": [...]
        }
    """
    tz_cst = timezone(timedelta(hours=8))
    now = datetime.now(tz_cst)

    snapshot = {
        "timestamp": now.isoformat(),
        "briefing": {},
        "inbox": {},
        "todo": {},
        "cil": {},
        "cil_deep": {},
        "errors": [],
    }

    # 1. BRIEFING
    try:
        snapshot["briefing"] = collect_briefing(workspace)
    except Exception as e:
        snapshot["errors"].append(f"BRIEFING 采集失败: {str(e)[:200]}")

    # 2. INBOX
    try:
        snapshot["inbox"] = collect_inbox(workspace)
    except Exception as e:
        snapshot["errors"].append(f"INBOX 采集失败: {str(e)[:200]}")

    # 3. todo
    try:
        snapshot["todo"] = collect_todo(workspace)
    except Exception as e:
        snapshot["errors"].append(f"todo 采集失败: {str(e)[:200]}")

    # 4. CIL 日报
    try:
        snapshot["cil"] = collect_cil_report(workspace)
    except Exception as e:
        snapshot["errors"].append(f"CIL 日报采集失败: {str(e)[:200]}")

    # 5. CIL 深度数据
    try:
        snapshot["cil_deep"] = collect_cil_deep(workspace)
    except Exception as e:
        snapshot["errors"].append(f"CIL 深度采集失败: {str(e)[:200]}")

    return snapshot


def format_snapshot(snapshot: dict) -> str:
    """将快照格式化为文本摘要（≤2K 字符）。"""
    parts = []
    ts = snapshot.get("timestamp", "unknown")
    parts.append(f"[态势感知 | {ts}]\n")

    # BRIEFING
    briefing = snapshot.get("briefing", {})
    if "error" in briefing:
        parts.append(f"📌 BRIEFING: {briefing['error']}")
    else:
        urgent = briefing.get("urgent", [])
        in_progress = briefing.get("in_progress", [])
        waiting = briefing.get("waiting", [])

        section = "📌 BRIEFING:\n"
        if urgent:
            section += f"  🔴 紧急({len(urgent)}): " + "; ".join(u[:60] for u in urgent[:5]) + "\n"
        if in_progress:
            section += f"  🔵 进行中({len(in_progress)}): " + "; ".join(u[:60] for u in in_progress[:5]) + "\n"
        if waiting:
            section += f"  ⏳ 等待输入({len(waiting)}): {len(waiting)} 项\n"

        parts.append(_truncate(section, BUDGET_BRIEFING))

    # INBOX
    inbox = snapshot.get("inbox", {})
    if "error" in inbox:
        parts.append(f"\n📬 INBOX: {inbox['error']}")
    else:
        pending = inbox.get("pending_count", 0)
        total = inbox.get("total_count", 0)
        section = f"\n📬 INBOX: {pending} 条待处理 / {total} 条总计"
        recent = inbox.get("recent", [])
        if recent:
            section += "\n  最新 pending:"
            for r in recent:
                section += f"\n  - [{r.get('priority','?')}] {r.get('summary','')[:60]}"
        parts.append(_truncate(section, BUDGET_INBOX))

    # todo
    todo = snapshot.get("todo", {})
    if "error" in todo:
        parts.append(f"\n📋 TODO: {todo['error']}")
    else:
        summary_text = todo.get("summary_text", "")
        parts.append(f"\n📋 TODO:\n{_truncate(summary_text, BUDGET_TODO)}")

    # CIL
    cil = snapshot.get("cil", {})
    if "error" in cil:
        parts.append(f"\n📊 CIL: {cil['error']}")
    else:
        cil_date = cil.get("date", "?")
        overview = cil.get("overview", "")
        anomalies = cil.get("anomalies", "")

        section = f"\n📊 CIL 日报 ({cil_date}):"
        if overview:
            section += f"\n  概览: {_truncate(overview, 250)}"
        if anomalies:
            section += f"\n  异常: {_truncate(anomalies, 200)}"
        parts.append(_truncate(section, BUDGET_CIL))

    # CIL 深度
    cil_deep = snapshot.get("cil_deep", {})
    if "error" not in cil_deep and cil_deep:
        deep_section = "\n🔬 CIL 深度:"

        # Insight 状态分布
        ins_dist = cil_deep.get("status_distribution", {})
        open_ins = ins_dist.get("open", 0)
        resolved_ins = ins_dist.get("resolved", 0)
        deep_section += f"\n  洞察: {open_ins} open / {resolved_ins} resolved"

        # Action 状态分布
        act_dist = cil_deep.get("action_distribution", {})
        active_acts = act_dist.get("proposed", 0) + act_dist.get("implementing", 0) + act_dist.get("tracking", 0)
        closed_acts = act_dist.get("closed", 0) + act_dist.get("dismissed", 0)
        deep_section += f"\n  行动项: {active_acts} 活跃 / {closed_acts} 已完结"

        # Active actions detail
        actions = cil_deep.get("actions", [])
        active_list = [a for a in actions if a.get("status") in ("proposed", "approved", "implementing", "tracking")]
        if active_list:
            deep_section += "\n  活跃行动项:"
            for a in active_list:
                deep_section += f"\n    - [{a['status']}] {a['id']}: {a['title'][:60]}"

        # Open insights detail
        insights = cil_deep.get("insights", [])
        open_list = [i for i in insights if i.get("status") == "open"]
        if open_list:
            deep_section += "\n  待处理洞察:"
            for i in open_list:
                deep_section += f"\n    - [{i['severity']}] {i['id']}: {i['title'][:60]}"

        # Stale items
        stale = cil_deep.get("stale_items", [])
        if stale:
            deep_section += f"\n  ⚠️ 停滞项({len(stale)}):"
            for s in stale[:5]:
                deep_section += f"\n    - {s['id']}({s['type']}): {s.get('days_stale',0)}天未更新 — {s['title'][:50]}"

        # Recent reports trend
        reports = cil_deep.get("recent_reports", [])
        if reports:
            deep_section += "\n  近期日报:"
            for r in reports:
                deep_section += f"\n    - {r['date']}: ✅{r.get('healthy_concerns',0)} ⚠️{r.get('warning_concerns',0)}"

        parts.append(_truncate(deep_section, BUDGET_CIL_DEEP))

    # 错误
    errors = snapshot.get("errors", [])
    if errors:
        parts.append(f"\n⚠️ 采集异常: {'; '.join(errors)}")

    result = "\n".join(parts)

    # 最终截断保证 ≤2K
    if len(result) > 3000:
        result = result[:2980] + "\n...（总体已截断至 3K）"

    return result


def main():
    """CLI 入口：采集 + 格式化 + 写入 awareness-cache.txt + stdout 输出"""
    parser = argparse.ArgumentParser(description="态势感知采集")
    parser.add_argument("--workspace", help="nanobot workspace 根目录")
    parser.add_argument("--data-dir", help="brain data 目录（覆盖默认）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON 而非格式化文本")
    args = parser.parse_args()

    workspace = _find_workspace(args.workspace)
    data_dir = _get_data_dir(args.data_dir)
    if data_dir is None:
        data_dir = workspace / "data" / "brain" / "mind"

    snapshot = collect_snapshot(workspace)
    formatted = format_snapshot(snapshot)

    # 写入 awareness-cache.txt
    cache_path = data_dir / "awareness-cache.txt"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(formatted, encoding="utf-8")
    except Exception as e:
        print(f"警告: 无法写入 {cache_path}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(formatted)


if __name__ == "__main__":
    main()
