#!/usr/bin/env python3
"""Journal 读写模块。

管理按日 Markdown journal 文件（data/brain/mind/journal/YYYY-MM-DD.md）。
支持：追加条目、读取当日条目、获取近期摘要、统计改进建议条目。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _SCRIPT_DIR.parent

TZ_CST = timezone(timedelta(hours=8))


def _get_data_dir(data_dir: Path = None) -> Path:
    """获取 brain data 目录。"""
    if data_dir:
        return Path(data_dir).resolve()
    env = os.environ.get("BRAIN_DATA_DIR")
    if env:
        return Path(env).resolve()
    # 向上推导
    cur = _SKILL_DIR
    for _ in range(10):
        candidate = cur / "data" / "brain" / "mind"
        if candidate.is_dir():
            return candidate
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    # fallback
    default = Path.home() / ".nanobot" / "workspace" / "data" / "brain" / "mind"
    return default


def get_journal_dir(data_dir: Path = None) -> Path:
    """获取 journal 目录路径。"""
    d = _get_data_dir(data_dir)
    journal_dir = d / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    return journal_dir


def _today_str() -> str:
    """返回当日日期字符串（YYYY-MM-DD，CST 时区）。"""
    return datetime.now(TZ_CST).strftime("%Y-%m-%d")


def _now_time_str() -> str:
    """返回当前时间字符串（HH:MM，CST 时区）。"""
    return datetime.now(TZ_CST).strftime("%H:%M")


def append_entry(
    analysis: str,
    reflection: str,
    improvements: list,
    summary: str,
    heartbeat_type: str = "regular",
    heartbeat_id: str = None,
    data_dir: Path = None,
) -> str:
    """追加一条 journal 条目到当日文件。

    Args:
        analysis: 态势分析文本
        reflection: 反思文本
        improvements: 改进建议列表，每项为 dict(title, description, scope, priority)
        summary: 一句话摘要
        heartbeat_type: 心跳类型（regular/interactive/urgent/recovery）
        heartbeat_id: 心跳 ID（如 HB-20260407-1430）
        data_dir: brain data 目录

    Returns:
        写入的文件路径
    """
    journal_dir = get_journal_dir(data_dir)
    today = _today_str()
    file_path = journal_dir / f"{today}.md"
    time_str = _now_time_str()

    hb_label = f" [{heartbeat_id}]" if heartbeat_id else ""
    type_label = {
        "regular": "常规",
        "interactive": "交互",
        "urgent": "紧急",
        "recovery": "恢复",
    }.get(heartbeat_type, heartbeat_type)

    entry_lines = [
        f"## {time_str} {type_label}心跳{hb_label}",
        "",
        "### 态势分析",
        analysis.strip(),
        "",
        "### 反思",
        reflection.strip(),
        "",
        "### 改进建议",
    ]

    if improvements:
        for imp in improvements:
            if isinstance(imp, dict):
                title = imp.get("title", "未命名")
                desc = imp.get("description", "")
                scope = imp.get("scope", "")
                priority = imp.get("priority", "")
                scope_tag = f" [{scope}]" if scope else ""
                priority_tag = f" ({priority})" if priority else ""
                entry_lines.append(f"- [ ] {title}: {desc}{scope_tag}{priority_tag}")
            else:
                entry_lines.append(f"- [ ] {str(imp)}")
    else:
        entry_lines.append("_无_")

    entry_lines.extend([
        "",
        "### 摘要",
        summary.strip(),
        "",
        "---",
        "",
    ])

    entry_text = "\n".join(entry_lines)

    # 写入文件
    try:
        if file_path.exists():
            existing = file_path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            file_path.write_text(existing + entry_text, encoding="utf-8")
        else:
            file_path.write_text(entry_text, encoding="utf-8")
    except (PermissionError, OSError) as e:
        # 降级：写入 /tmp 备份
        backup_path = Path(f"/tmp/heartbeat-journal-backup-{today}.md")
        try:
            with open(backup_path, "a", encoding="utf-8") as f:
                f.write(entry_text)
            print(f"警告: journal 写入失败({e})，已备份到 {backup_path}", file=sys.stderr)
            return str(backup_path)
        except Exception as e2:
            print(f"错误: journal 写入和备份均失败: {e}, {e2}", file=sys.stderr)
            raise

    return str(file_path)


def get_today_entries(data_dir: Path = None) -> str:
    """读取当日 journal 全文。"""
    journal_dir = get_journal_dir(data_dir)
    today = _today_str()
    file_path = journal_dir / f"{today}.md"
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8")


def get_recent_summaries(days: int = 3, data_dir: Path = None) -> str:
    """读取最近 N 天 journal 摘要（每天取最后一条的 '### 摘要' 段）。

    用于注入到心跳 prompt 中提供历史上下文。
    """
    journal_dir = get_journal_dir(data_dir)

    summaries = []
    today = date.today()

    for i in range(days):
        d = today - timedelta(days=i)
        file_path = journal_dir / f"{d.isoformat()}.md"
        if not file_path.exists():
            continue

        content = file_path.read_text(encoding="utf-8")

        # 找到所有 ### 摘要 段
        summary_sections = []
        in_summary = False
        current_summary = []

        for line in content.split("\n"):
            if line.strip().startswith("### 摘要"):
                in_summary = True
                current_summary = []
            elif in_summary and (line.startswith("## ") or line.startswith("---")):
                if current_summary:
                    summary_sections.append("\n".join(current_summary).strip())
                in_summary = False
                current_summary = []
            elif in_summary:
                current_summary.append(line)

        # 处理文件末尾
        if in_summary and current_summary:
            summary_sections.append("\n".join(current_summary).strip())

        if summary_sections:
            # 取最后一条
            last_summary = summary_sections[-1]
            if last_summary:
                summaries.append(f"[{d.isoformat()}] {last_summary}")

    if not summaries:
        return "（无近期 journal 记录）"

    return "\n".join(summaries)


def count_improvement_entries(data_dir: Path = None, days: int = 2) -> dict:
    """统计最近 N 天 journal 中含改进建议的条目占比。

    Returns:
        {
            "total_entries": 10,
            "entries_with_improvements": 7,
            "improvement_ratio": 0.7,
            "days_covered": 2
        }
    """
    journal_dir = get_journal_dir(data_dir)
    today = date.today()

    total_entries = 0
    entries_with_improvements = 0
    days_found = 0

    for i in range(days):
        d = today - timedelta(days=i)
        file_path = journal_dir / f"{d.isoformat()}.md"
        if not file_path.exists():
            continue

        days_found += 1
        content = file_path.read_text(encoding="utf-8")

        # 按 ## 分割条目
        entries = re.split(r"^## ", content, flags=re.MULTILINE)
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            # 检查是否是心跳条目（包含 "心跳" 关键字）
            first_line = entry.split("\n")[0]
            if "心跳" not in first_line:
                continue

            total_entries += 1

            # 检查是否有非空的改进建议
            if "### 改进建议" in entry:
                # 取改进建议段
                imp_start = entry.index("### 改进建议")
                imp_section = entry[imp_start:]
                # 到下一个 ### 或 --- 截断
                for marker in ["### 摘要", "---"]:
                    if marker in imp_section[len("### 改进建议"):]:
                        idx = imp_section.index(marker, len("### 改进建议"))
                        imp_section = imp_section[:idx]
                        break

                # 检查是否有 checkbox 项
                if "- [ ]" in imp_section or "- [x]" in imp_section:
                    entries_with_improvements += 1

    ratio = entries_with_improvements / total_entries if total_entries > 0 else 0.0

    return {
        "total_entries": total_entries,
        "entries_with_improvements": entries_with_improvements,
        "improvement_ratio": round(ratio, 2),
        "days_covered": days_found,
    }


def main():
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Journal 读写工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # append
    p_append = subparsers.add_parser("append", help="追加 journal 条目")
    p_append.add_argument("--input-file", help="JSON 输入文件路径（含 analysis, reflection, improvements, summary）")
    p_append.add_argument("--heartbeat-type", default="regular", help="心跳类型")
    p_append.add_argument("--heartbeat-id", help="心跳 ID")
    p_append.add_argument("--data-dir", help="brain data 目录")

    # today
    p_today = subparsers.add_parser("today", help="读取当日 journal")
    p_today.add_argument("--data-dir", help="brain data 目录")

    # recent
    p_recent = subparsers.add_parser("recent", help="读取近期 journal 摘要")
    p_recent.add_argument("--days", type=int, default=3, help="天数")
    p_recent.add_argument("--data-dir", help="brain data 目录")

    # stats
    p_stats = subparsers.add_parser("stats", help="统计改进建议条目")
    p_stats.add_argument("--days", type=int, default=2, help="天数")
    p_stats.add_argument("--data-dir", help="brain data 目录")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    data_dir_arg = getattr(args, "data_dir", None)

    if args.command == "append":
        if args.input_file:
            with open(args.input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)

        path = append_entry(
            analysis=data.get("analysis", ""),
            reflection=data.get("reflection", ""),
            improvements=data.get("improvements", []),
            summary=data.get("summary", ""),
            heartbeat_type=args.heartbeat_type,
            heartbeat_id=args.heartbeat_id,
            data_dir=data_dir_arg,
        )
        print(json.dumps({"status": "ok", "path": path}, ensure_ascii=False))

    elif args.command == "today":
        print(get_today_entries(data_dir_arg))

    elif args.command == "recent":
        print(get_recent_summaries(args.days, data_dir_arg))

    elif args.command == "stats":
        result = count_improvement_entries(data_dir_arg, args.days)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
