#!/usr/bin/env python3
"""心跳入口（主控制器）。

协调态势采集 → prompt 准备 → journal 记录的完整流程。
提供 prepare / record / record-error 三个子命令。

使用状态文件锁（heartbeat.lock）防止并发重入。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _SCRIPT_DIR.parent

TZ_CST = timezone(timedelta(hours=8))

# 僵尸锁超时（分钟）
LOCK_TIMEOUT_MINUTES = 15

# ── 路径推导 ─────────────────────────────────────────────────

def _find_workspace() -> Path:
    """定位 workspace 根目录。"""
    env = os.environ.get("NANOBOT_WORKSPACE")
    if env:
        p = Path(env).resolve()
        if p.exists():
            return p

    cur = _SKILL_DIR
    for _ in range(10):
        if (cur / "data" / "brain").is_dir():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    default = Path.home() / ".nanobot" / "workspace"
    if default.exists():
        return default

    raise RuntimeError("无法定位 workspace 目录")


def _get_data_dir(args_data_dir: str = None) -> Path:
    """获取 brain data 目录。"""
    if args_data_dir:
        return Path(args_data_dir).resolve()
    env = os.environ.get("BRAIN_DATA_DIR")
    if env:
        return Path(env).resolve()
    workspace = _find_workspace()
    return workspace / "data" / "brain" / "mind"


def _now() -> datetime:
    return datetime.now(TZ_CST)


def _now_iso() -> str:
    return _now().isoformat()


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


# ── 状态文件锁 ────────────────────────────────────────────────

def _lock_path(data_dir: Path) -> Path:
    return data_dir / "heartbeat.lock"


def _acquire_lock(data_dir: Path, heartbeat_id: str) -> dict:
    """尝试获取状态文件锁。

    Returns:
        {"acquired": True} 或
        {"acquired": False, "reason": "...", "lock_info": {...}}
    """
    lock_file = _lock_path(data_dir)

    if lock_file.exists():
        try:
            lock_info = json.loads(lock_file.read_text(encoding="utf-8"))
            acquired_at = lock_info.get("acquired_at", "")
            if acquired_at:
                lock_time = datetime.fromisoformat(acquired_at)
                elapsed = (_now() - lock_time).total_seconds() / 60
                if elapsed < LOCK_TIMEOUT_MINUTES:
                    return {
                        "acquired": False,
                        "reason": "lock_held",
                        "lock_info": lock_info,
                        "elapsed_minutes": round(elapsed, 1),
                    }
                else:
                    # 僵尸锁，强制释放
                    pass  # 继续获取
        except (json.JSONDecodeError, ValueError, KeyError):
            # 锁文件损坏，强制释放
            pass

    # 写入新锁
    lock_data = {
        "pid": os.getpid(),
        "heartbeat_id": heartbeat_id,
        "acquired_at": _now_iso(),
    }
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps(lock_data, ensure_ascii=False), encoding="utf-8")
    return {"acquired": True}


def _release_lock(data_dir: Path):
    """释放状态文件锁。"""
    lock_file = _lock_path(data_dir)
    if lock_file.exists():
        try:
            lock_file.unlink()
        except OSError:
            pass


# ── 配置与状态 ────────────────────────────────────────────────

def _load_config(data_dir: Path) -> dict:
    """加载 heartbeat-config.yaml。"""
    config_path = data_dir / "heartbeat-config.yaml"
    if not config_path.exists():
        return {
            "mode": "trial",
            "profiles": {"trial": {"cron_expr": "", "description": "default"}},
            "limits": {"max_regular": 10, "max_total": 20},
            "timezone": "Asia/Shanghai",
        }

    # 简单 YAML 解析（避免依赖 pyyaml）
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        # 手动解析基本 YAML
        return _parse_simple_yaml(config_path)


def _parse_simple_yaml(path: Path) -> dict:
    """极简 YAML 解析器，仅处理 heartbeat-config.yaml 的已知结构。"""
    content = path.read_text(encoding="utf-8")
    result = {}

    # 提取顶级标量
    for match in re.finditer(r'^(\w+):\s+"?([^"\n]+)"?\s*$', content, re.MULTILINE):
        key, val = match.group(1), match.group(2).strip('"')
        if val.isdigit():
            val = int(val)
        result[key] = val

    # 提取 limits
    limits = {}
    in_limits = False
    for line in content.split("\n"):
        if line.startswith("limits:"):
            in_limits = True
            continue
        if in_limits:
            if line and not line[0].isspace():
                in_limits = False
                continue
            m = re.match(r'\s+(\w+):\s+(\d+)', line)
            if m:
                limits[m.group(1)] = int(m.group(2))

    if limits:
        result["limits"] = limits

    # 确保必要字段
    result.setdefault("mode", "trial")
    result.setdefault("limits", {"max_regular": 10, "max_total": 20})
    result.setdefault("timezone", "Asia/Shanghai")

    return result


DEFAULT_STATE = {
    "last_heartbeat": None,
    "last_heartbeat_type": None,
    "today_count": {
        "regular": 0,
        "interactive": 0,
        "urgent": 0,
        "recovery": 0,
    },
    "today_date": None,
    "consecutive_errors": 0,
    "avg_interactive_trigger_latency_today": None,
}


def _load_state(data_dir: Path) -> dict:
    """加载 heartbeat-state.json，损坏时重建默认状态。"""
    state_path = data_dir / "heartbeat-state.json"
    if not state_path.exists():
        state = dict(DEFAULT_STATE)
        state["today_date"] = _today_str()
        return state

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        # 验证基本结构
        if not isinstance(state, dict) or "today_count" not in state:
            raise ValueError("state 结构不完整")
        return state
    except (json.JSONDecodeError, ValueError) as e:
        print(f"警告: heartbeat-state.json 损坏({e})，重建默认状态", file=sys.stderr)
        state = dict(DEFAULT_STATE)
        state["today_date"] = _today_str()
        _save_state(data_dir, state)
        return state


def _save_state(data_dir: Path, state: dict):
    """保存 heartbeat-state.json。"""
    state_path = data_dir / "heartbeat-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _reset_if_new_day(state: dict) -> dict:
    """如果跨天则重置 today_count。"""
    today = _today_str()
    if state.get("today_date") != today:
        state["today_date"] = today
        state["today_count"] = {
            "regular": 0,
            "interactive": 0,
            "urgent": 0,
            "recovery": 0,
        }
        state["avg_interactive_trigger_latency_today"] = None
    return state


# ── Prompt 模板 ───────────────────────────────────────────────

def _load_prompt_template() -> str:
    """加载心跳 prompt 模板。"""
    prompt_path = _SKILL_DIR / "prompts" / "heartbeat_prompt.md"
    if not prompt_path.exists():
        return "(prompt 模板文件不存在: {})".format(prompt_path)
    return prompt_path.read_text(encoding="utf-8")


def _fill_prompt(
    template: str,
    awareness: str,
    journal_summary: str,
    state: dict,
    heartbeat_id: str,
) -> str:
    """填充 prompt 模板中的占位符。"""
    today_count = state.get("today_count", {})
    regular_count = today_count.get("regular", 0)
    total_count = sum(today_count.values())

    replacements = {
        "{heartbeat_id}": heartbeat_id,
        "{awareness_snapshot}": awareness,
        "{recent_journal_summary}": journal_summary,
        "{today_regular_count}": str(regular_count + 1),  # 包含本次
        "{today_total_count}": str(total_count + 1),
        "{last_heartbeat_time}": state.get("last_heartbeat", "无") or "无",
        "{last_heartbeat_type}": state.get("last_heartbeat_type", "无") or "无",
        "{consecutive_errors}": str(state.get("consecutive_errors", 0)),
    }

    result = template
    for key, val in replacements.items():
        result = result.replace(key, val)

    return result


# ── 生成心跳 ID ──────────────────────────────────────────────

def _generate_heartbeat_id() -> str:
    """生成心跳 ID：HB-YYYYMMDD-HHMM"""
    now = _now()
    return f"HB-{now.strftime('%Y%m%d-%H%M')}"


# ── 子命令实现 ────────────────────────────────────────────────

def cmd_prepare(args):
    """prepare 子命令：采集态势、准备 prompt、检查限制。"""
    data_dir = _get_data_dir(args.data_dir)
    heartbeat_id = _generate_heartbeat_id()

    # 1. 获取锁
    lock_result = _acquire_lock(data_dir, heartbeat_id)
    if not lock_result["acquired"]:
        output = {
            "status": "skip",
            "reason": lock_result["reason"],
            "lock_pid": lock_result.get("lock_info", {}).get("pid"),
            "lock_elapsed_minutes": lock_result.get("elapsed_minutes"),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 2. 加载配置和状态
    config = _load_config(data_dir)
    state = _load_state(data_dir)
    state = _reset_if_new_day(state)

    # 3. 检查 consecutive_errors
    if state.get("consecutive_errors", 0) >= 3:
        _release_lock(data_dir)
        output = {
            "status": "skip",
            "reason": "consecutive_errors_exceeded",
            "consecutive_errors": state["consecutive_errors"],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 4. 检查每日限额
    limits = config.get("limits", {})
    max_regular = limits.get("max_regular", 10)
    today_regular = state.get("today_count", {}).get("regular", 0)

    if today_regular >= max_regular:
        _release_lock(data_dir)
        output = {
            "status": "skip",
            "reason": "daily_limit_reached",
            "today_regular": today_regular,
            "max_regular": max_regular,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 5. 态势采集
    degraded = False
    try:
        # 导入同目录的 awareness_snapshot 模块
        sys.path.insert(0, str(_SCRIPT_DIR))
        from awareness_snapshot import collect_snapshot, format_snapshot, _find_workspace

        workspace = _find_workspace(args.workspace if hasattr(args, 'workspace') else None)
        snapshot = collect_snapshot(workspace)
        awareness_text = format_snapshot(snapshot)

        # 写入 cache
        cache_path = data_dir / "awareness-cache.txt"
        cache_path.write_text(awareness_text, encoding="utf-8")

        if snapshot.get("errors"):
            degraded = True
    except Exception as e:
        # 降级：使用缓存
        degraded = True
        cache_path = data_dir / "awareness-cache.txt"
        if cache_path.exists():
            awareness_text = cache_path.read_text(encoding="utf-8")
            awareness_text = f"[降级模式 - 使用缓存数据，采集异常: {str(e)[:100]}]\n{awareness_text}"
        else:
            awareness_text = f"[态势采集完全失败: {str(e)[:200]}]"

    # 6. 获取近期 journal 摘要
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        from journal_helper import get_recent_summaries
        journal_summary = get_recent_summaries(days=3, data_dir=data_dir)
    except Exception as e:
        journal_summary = f"(journal 读取失败: {str(e)[:100]})"

    # 7. 加载并填充 prompt 模板
    template = _load_prompt_template()
    filled_prompt = _fill_prompt(template, awareness_text, journal_summary, state, heartbeat_id)

    # 8. 输出
    output = {
        "status": "ready",
        "awareness": awareness_text,
        "prompt_template": filled_prompt,
        "heartbeat_id": heartbeat_id,
        "state": {
            "today_count": state.get("today_count", {}),
            "last_heartbeat": state.get("last_heartbeat"),
            "last_heartbeat_type": state.get("last_heartbeat_type"),
            "consecutive_errors": state.get("consecutive_errors", 0),
        },
    }
    if degraded:
        output["degraded"] = True

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_record(args):
    """record 子命令：记录 LLM 思考结果到 journal，更新状态。"""
    data_dir = _get_data_dir(args.data_dir)

    # 读取 LLM 输出
    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as f:
            raw_text = f.read()
    else:
        raw_text = sys.stdin.read()

    # 解析 JSON（宽松模式）
    llm_result = _parse_llm_output(raw_text)

    # 写入 journal
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        from journal_helper import append_entry

        path = append_entry(
            analysis=llm_result.get("analysis", ""),
            reflection=llm_result.get("reflection", ""),
            improvements=llm_result.get("improvements", []),
            summary=llm_result.get("summary", ""),
            heartbeat_type=args.heartbeat_type or "regular",
            heartbeat_id=args.heartbeat_id,
            data_dir=data_dir,
        )
    except Exception as e:
        print(json.dumps({
            "status": "error",
            "error": f"journal 写入失败: {str(e)[:200]}",
        }, ensure_ascii=False), file=sys.stderr)
        # 仍然继续更新状态
        path = "error"

    # 更新状态
    state = _load_state(data_dir)
    state = _reset_if_new_day(state)
    state["last_heartbeat"] = _now_iso()
    state["last_heartbeat_type"] = args.heartbeat_type or "regular"
    state["today_count"]["regular"] = state["today_count"].get("regular", 0) + 1
    state["consecutive_errors"] = 0
    _save_state(data_dir, state)

    # 释放锁
    _release_lock(data_dir)

    output = {
        "status": "ok",
        "journal_path": path,
        "heartbeat_id": args.heartbeat_id or "",
        "state": state,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_record_error(args):
    """record-error 子命令：记录错误并更新状态。"""
    data_dir = _get_data_dir(args.data_dir)

    # 更新状态
    state = _load_state(data_dir)
    state = _reset_if_new_day(state)
    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
    state["last_error"] = {
        "time": _now_iso(),
        "message": args.error or "unknown error",
    }
    _save_state(data_dir, state)

    # 释放锁
    _release_lock(data_dir)

    output = {
        "status": "ok",
        "consecutive_errors": state["consecutive_errors"],
        "error_recorded": args.error or "unknown error",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _parse_llm_output(raw_text: str) -> dict:
    """宽松解析 LLM 输出为 JSON。

    尝试顺序：
    1. 直接 json.loads
    2. 从 markdown code block 提取 JSON
    3. 正则提取 JSON 块
    4. 降级为纯文本
    """
    raw_text = raw_text.strip()

    # 1. 直接解析
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # 2. 从 ```json ... ``` 提取
    m = re.search(r'```(?:json)?\s*\n(.*?)\n```', raw_text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. 正则提取最大的 {} 块
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw_text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # 4. 降级：整段作为 analysis
    return {
        "analysis": raw_text[:1000],
        "reflection": "[格式异常：LLM 输出非有效 JSON，已降级为纯文本记录]",
        "improvements": [],
        "summary": "心跳完成（输出格式异常，已降级记录）",
    }


# ── CLI 入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="心跳入口（主控制器）")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # prepare
    p_prepare = subparsers.add_parser("prepare", help="态势采集与 prompt 准备")
    p_prepare.add_argument("--data-dir", help="brain data 目录")
    p_prepare.add_argument("--workspace", help="nanobot workspace 根目录")

    # record
    p_record = subparsers.add_parser("record", help="记录 LLM 思考结果")
    p_record.add_argument("--input-file", help="LLM 输出 JSON 文件路径")
    p_record.add_argument("--data-dir", help="brain data 目录")
    p_record.add_argument("--heartbeat-type", default="regular", help="心跳类型")
    p_record.add_argument("--heartbeat-id", help="心跳 ID")

    # record-error
    p_error = subparsers.add_parser("record-error", help="记录心跳错误")
    p_error.add_argument("--error", required=True, help="错误描述")
    p_error.add_argument("--data-dir", help="brain data 目录")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "prepare":
        cmd_prepare(args)
    elif args.command == "record":
        cmd_record(args)
    elif args.command == "record-error":
        cmd_record_error(args)


if __name__ == "__main__":
    main()
