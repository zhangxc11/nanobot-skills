#!/usr/bin/env python3
"""
trigger_scheduler.py - Trigger the Task Dispatcher Scheduler

Lightweight entry point that uses a **fixed dispatcher session** to run
the scheduling task. If no active dispatcher session exists, creates one.
If the current session has exceeded its iteration cap, triggers a
generation handoff (successor session).

Design constraints:
  - Uses a fixed dispatcher session (tracked in dispatcher.json)
  - Sends wake-up messages to the existing session instead of creating new ones
  - Generation handoff after MAX_ITERATIONS to prevent context bloat
  - No file locks — dispatcher.json is the single source of truth
  - Workers are spawned as subagents (not create_subsession)
  - Framework automatically sends [Subagent Result Notification] when workers complete
  - This script is called by:
    1. Cron (30min fallback)
    2. Feishu/CLI session (主动触发 via web subsession)

Dispatcher state:
  - State file: data/brain/dispatcher.json
  - Format: {"session_id": "webchat_dispatch_xxx", "session_key": "webchat:dispatch_xxx",
              "created_at": "ISO", "iteration_count": 0}
  - iteration_count incremented each time a wake-up message is sent

Usage:
    python3 skills/task-dispatcher/scripts/trigger_scheduler.py
    python3 skills/task-dispatcher/scripts/trigger_scheduler.py --parent "feishu.ST.xxx"
    python3 skills/task-dispatcher/scripts/trigger_scheduler.py --dry-run
    python3 skills/task-dispatcher/scripts/trigger_scheduler.py --cron-setup
    python3 skills/task-dispatcher/scripts/trigger_scheduler.py --status

Environment:
    WEBSERVER_PORT  — web-chat webserver port (default: 8081)
    WORKER_PORT     — web-chat worker port (default: 8082)
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────

WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
SCHEDULER_SCRIPT = SCRIPTS_DIR / "scheduler.py"

_brain_dir_env = os.environ.get("TASK_DATA_DIR") or os.environ.get("BRAIN_DIR")
TASK_DATA_DIR = Path(_brain_dir_env) if _brain_dir_env else WORKSPACE / "data" / "tasks"
BRAIN_DIR = TASK_DATA_DIR  # backward compat
DISPATCHER_FILE = TASK_DATA_DIR / "dispatcher.json"

WEBSERVER_PORT = int(os.environ.get("WEBSERVER_PORT", "8081"))
WORKER_PORT = int(os.environ.get("WORKER_PORT", "8082"))

# T-010: Irreversible operation confirmation
IRREVERSIBLE_CONFIRM_ENABLED = os.environ.get("IRREVERSIBLE_CONFIRM_ENABLED", "1") != "0"

IRREVERSIBLE_CONFIRM_RULES = """
### ⚠️ 不可逆操作二次确认规则

**不可逆操作清单**: cancel（取消）、reject（拒绝）

**规则**: 当用户通过飞书回复发出 cancel 或 reject 操作时，不得直接执行。必须走二次确认流程：

1. 用户发送: "T-001 取消" 或 "001 取消"
2. 你回复确认: "⚠️ 确认取消 [T-001] {任务标题}？此操作不可逆，任务将标记为 cancelled。回复'确认取消 001'执行"
3. 用户回复: "确认取消 001"
4. 你执行取消操作

**确认消息格式**: 必须以 "确认" 开头 + 操作动词 + task short_id
- 确认取消 001
- 确认拒绝 001

**记录要求**: 每次不可逆操作（无论是否确认）都必须记录到 decisions.jsonl。

**豁免**: 调度器自身的 mark_blocked 决策不受此规则约束（那是系统决策非用户操作）。
"""

MAX_ITERATIONS = 500  # Trigger generation handoff after this many iterations
SESSION_STALE_MINUTES = 60  # Consider session stale if no activity for this long
SESSION_BUSY_SECONDS = 300  # Skip wake-up if session was active within this window
MESSAGE_COUNT_CAP = 1500  # Rotate session if message count exceeds this (兜底)
MAX_FOLLOW_UP_ON_EXHAUSTION = 3  # Max follow_up retries when worker exhausts iterations


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse ISO datetime string, tolerant of various formats."""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        from datetime import timezone
        return datetime.min.replace(tzinfo=timezone.utc)


# ──────────────────────────────────────────
# Dispatcher state management
# ──────────────────────────────────────────

def load_dispatcher() -> dict | None:
    """Load dispatcher state from dispatcher.json.

    Returns None if file doesn't exist or is corrupted.
    """
    if not DISPATCHER_FILE.exists():
        return None
    try:
        with DISPATCHER_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Validate required fields
        if not data.get("session_id") or not data.get("session_key"):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_dispatcher(data: dict) -> None:
    """Atomically save dispatcher state to dispatcher.json."""
    DISPATCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISPATCHER_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DISPATCHER_FILE)


def increment_iteration(dispatcher: dict) -> dict:
    """Increment iteration count and save."""
    dispatcher["iteration_count"] = dispatcher.get("iteration_count", 0) + 1
    dispatcher["last_triggered_at"] = _now_iso()
    save_dispatcher(dispatcher)
    return dispatcher


# ──────────────────────────────────────────
# Session status check
# ──────────────────────────────────────────

def check_session_alive(session_id: str) -> dict:
    """Check if a web-chat session is alive via the API.

    Returns:
        {"alive": bool, "last_active": str or None, "message_count": int}
    """
    try:
        url = f"http://127.0.0.1:{WEBSERVER_PORT}/api/sessions?limit=2000"
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        sessions = data if isinstance(data, list) else data.get("sessions", [])
        for s in sessions:
            if s.get("id") == session_id:
                last_active = s.get("lastActiveAt", "")
                msg_count = s.get("messageCount", 0)

                # Check if session is stale
                if last_active:
                    try:
                        last_dt = datetime.fromisoformat(last_active)
                        now = datetime.now()
                        # Handle timezone-aware vs naive comparison
                        if last_dt.tzinfo is not None and now.tzinfo is None:
                            now = now.astimezone()
                        elif last_dt.tzinfo is None and now.tzinfo is not None:
                            now = now.replace(tzinfo=None)
                        stale = (now - last_dt) > timedelta(minutes=SESSION_STALE_MINUTES)
                    except (ValueError, TypeError):
                        stale = True
                else:
                    stale = True

                return {
                    "alive": not stale,
                    "exists": True,
                    "last_active": last_active,
                    "message_count": msg_count,
                    "stale": stale,
                }

        return {"alive": False, "exists": False, "last_active": None, "message_count": 0, "stale": True}
    except Exception as e:
        return {"alive": False, "exists": False, "last_active": None, "message_count": 0, "error": str(e)}


def should_skip_wakeup(status: dict) -> bool:
    """Skip wake-up if session was active very recently (likely still executing).

    This prevents sending redundant wake-up messages when the dispatcher
    session is already processing a previous wake-up.

    Args:
        status: Result from check_session_alive()

    Returns:
        True if wake-up should be skipped (session is busy)
    """
    last_active = status.get("last_active", "")
    if not last_active:
        return False
    try:
        last_dt = datetime.fromisoformat(last_active)
        now = datetime.now()
        # Handle timezone-aware vs naive comparison
        if last_dt.tzinfo is not None and now.tzinfo is None:
            now = now.astimezone()
        elif last_dt.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        return (now - last_dt).total_seconds() < SESSION_BUSY_SECONDS
    except (ValueError, TypeError):
        return False


def check_iteration_limit(dispatcher: dict, session_status: dict | None = None) -> bool:
    """Check if dispatcher session should rotate (generation handoff).

    Uses two signals:
    1. trigger_count: how many times we've sent wake-up messages (local counter)
    2. message_count: actual messages in session (from /api/sessions, if available)

    Rotate if EITHER exceeds its threshold.

    Args:
        dispatcher: Current dispatcher state dict
        session_status: Optional result from check_session_alive() to avoid extra API call

    Returns:
        True if session should rotate
    """
    # Signal 1: local trigger count (primary, fast, no network)
    trigger_count = dispatcher.get("iteration_count", 0)
    if trigger_count >= MAX_ITERATIONS:
        return True

    # Signal 2: actual message count from API (兜底, catches internal session bloat)
    if session_status is not None:
        message_count = session_status.get("message_count", 0)
        if message_count >= MESSAGE_COUNT_CAP:
            return True

    return False


# ──────────────────────────────────────────
# Health check
# ──────────────────────────────────────────

def check_webchat_health() -> bool:
    """Check if web-chat service is running (stdlib only, no curl dependency)."""
    try:
        req = Request(f"http://127.0.0.1:{WEBSERVER_PORT}/api/health", method="GET")
        with urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


# ──────────────────────────────────────────
# Scheduler prompt generation
# ──────────────────────────────────────────

def build_scheduler_prompt(dry_run: bool = False, parent_session_id: str = "",
                           is_wake_up: bool = False) -> str:
    """Build the prompt sent to the dispatcher session.

    For new sessions: full instructions including how to handle wake-ups and subagent results.
    For wake-ups: concise trigger message.

    Args:
        dry_run: If True, scheduler runs in dry-run mode
        parent_session_id: Parent session ID for tracking
        is_wake_up: If True, this is a wake-up message (concise)
    """
    mode = "run --dry-run" if dry_run else "run"
    parent_flag = f' --parent "{parent_session_id}"' if parent_session_id else ""

    if is_wake_up:
        return f"""⏰ 调度器唤醒 — 请执行一轮调度。

如果这是 Subagent 完成通知，按决策树处理：

**1. 先判断 Worker 是否迭代用满**（通知含 "reached the maximum number of tool call iterations" 或 "Iterations used: N/N" 两数相等）：
   - 是 → `follow_up` 让 Worker 继续工作并写报告（最多 {MAX_FOLLOW_UP_ON_EXHAUSTION} 次），不要直接 handle-completion
   - 否 → 继续下一步

**2. 调用 handle-completion**（从通知 Label 提取任务 ID，格式如 "🔨 T-xxx: ..." 或 "🧪 T-xxx: ..."）：
```bash
cd {WORKSPACE}
python3 skills/task-dispatcher/scripts/scheduler.py handle-completion --task-id T-xxx
```

**3. 分析 handle-completion 结果**：
   - `verdict: pass` → 根据 pattern 决定下一步（spawn 下一个 role 或 mark-done）
   - `verdict: fail` → 分析原因，决定是否让 Worker 重试或换策略
   - `ok: false` + `action: no_report` → **不要无脑 blocked！** 分析原因：
     - "no report found" → `follow_up` 让 Worker 补写报告后重试
   - 参考 `prior_context` 和 `history` 字段了解任务进展

**4. 📬 INBOX 巡检**：
```bash
cd {WORKSPACE}
python3 scripts/inbox_helper.py pending
```
如果有 pending 消息，根据消息类型决定处理方式：
- `task_result` → 更新相关任务状态，必要时触发 handle-completion
- `error_alert` → 评估严重度，决定是否通知用户
- `user_intent` → 保留在 pending（不自动处理），会出现在 BRIEFING 待确认区
- `info` → 标记为已处理
处理完每条消息后调用：
```bash
python3 scripts/inbox_helper.py process --id {{消息ID}} --processed-by "scheduler" --action-taken "处理摘要"
```

**5. 执行常规调度**：
```bash
cd {WORKSPACE}
python3 skills/task-dispatcher/scripts/scheduler.py {mode}{parent_flag}
```

解析 `dispatched` 列表，读取每个任务的 `pattern_path` 了解角色流程，用 spawn 派发第一个角色。

记住：你是项目经理，主动推进，能自己解决的问题不要抛给用户。
{"⚠️ 处理飞书回复时，cancel/reject 操作必须二次确认后才能执行。" if IRREVERSIBLE_CONFIRM_ENABLED else ""}
{"⚠️ dry-run 模式，不实际派发。" if dry_run else ""}
"""

    # Full prompt for new session
    prompt = f"""你是数字助理调度器（固定 session 模式）。

## 工作模式

你是一个长驻调度器 session。每次收到唤醒消息或 subagent 完成通知时，执行一轮调度。

## 当前任务：执行一轮调度

### Step 0: 📬 INBOX 巡检

检查 INBOX 待处理消息：
```bash
cd {WORKSPACE}
python3 scripts/inbox_helper.py pending
```

如果有 pending 消息，根据消息类型决定处理方式：
- `task_result` → 更新相关任务状态，必要时触发 handle-completion
- `error_alert` → 评估严重度，决定是否通知用户
- `user_intent` → 保留在 pending（不自动处理），会出现在 BRIEFING 待确认区
- `info` → 标记为已处理

处理完每条消息后调用：
```bash
python3 scripts/inbox_helper.py process --id {{消息ID}} --processed-by "scheduler" --action-taken "处理摘要"
```

### Step 1: 运行调度器获取派发决策

```bash
cd {WORKSPACE}
python3 skills/task-dispatcher/scripts/scheduler.py {mode}{parent_flag}
```

### Step 2: 解析输出

调度器输出 JSON，包含 `dispatched` 数组。每个元素包含：
- `task_id`: 任务 ID
- `title`: 显示名称
- `priority`: 优先级
- `description`: 任务描述
- `pattern`: 角色流程模式名（如 dev-pipeline）
- `pattern_path`: 模式文档路径（如 skills/role-flow/patterns/dev-pipeline.md）

如果 `dispatched` 为空，说明没有需要派发的任务，直接跳到 Step 4。

### Step 3: 读取 pattern 并用 spawn 派发 worker subagent

对每个 dispatched 任务：
1. 读取 `pattern_path` 文档，了解角色流程（如 dev-pipeline: architect → developer → tester → auditor）
2. 先调用 record-spawn 记录：
   ```bash
   python3 skills/task-dispatcher/scripts/scheduler.py record-spawn --task-id T-xxx --role <first_role>
   ```
3. 用 spawn 派发第一个角色的 Worker：
   ```
   spawn(task="<根据 pattern 和 task 信息构建的完整任务指令>", max_iterations=<按角色: developer=60, tester=30, architect=30, auditor=20>)
   ```

⚠️ 注意：
- **必须传入 max_iterations 参数**（developer=60, tester=30, architect=30, auditor=20）
- spawn 是异步的 — 不需要等待 worker 完成
- worker subagent 完成后，框架会自动向本 session 发送 `[Subagent Result Notification]` 消息
- 可以同时 spawn 多个 worker（每个 dispatched 任务一个）
- 每次 spawn 前都要先 record-spawn 记录到 orchestration history

### Step 4: 输出调度报告

汇总：派发数 / 跳过数 / 执行中数 / 待审数

如果 Step 0 处理了 INBOX 消息，在报告中追加：
```
## 📬 INBOX (N 条已处理)
- 🔄 [INB-xxx] 处理摘要
```

如果调度器输出包含 `notification` 字段（飞书通知文本），也输出它。

{"## ⚠️ 这是 dry-run 模式，不实际派发任务，只报告计划。" if dry_run else ""}

---

{IRREVERSIBLE_CONFIRM_RULES if IRREVERSIBLE_CONFIRM_ENABLED else ""}

## 🧠 核心理念：你是项目经理，主动推进

你不是被动的消息转发器。你是项目经理，你的职责是**确保任务推进**。
遇到问题时，你的第一反应应该是"我能怎么解决？"而不是"标记 blocked 等人来处理"。

**行动优先级**（从高到低）：
1. **follow_up 续命** — Worker 没做完？给它更多时间继续
2. **分析输出自行判断** — 没有报告？从 Worker 的输出摘要中提取信息
3. **重新派发** — Worker 失败了？分析原因，换个策略重试
4. **标记 blocked 通知用户** — 真正的外部阻塞（需要人工决策/权限/资源），才走这条路

---

## 收到 Subagent 完成通知时

当你收到 `[Subagent Result Notification]` 消息时，按以下决策树处理：

### 决策树（按顺序判断）

#### 情况 A: Worker 迭代用满

**识别特征**（任一即可）：
- 通知包含 "reached the maximum number of tool call iterations"
- 通知包含 "Iterations used: N/N"（两个数字相等，如 "30/30"、"60/60"）

**处理方式**：
1. **不要调用 handle-completion**（Worker 可能没来得及写报告，会误判 blocked）
2. 使用 `follow_up` 向该 subagent 发送续命消息：
   ```
   follow_up(task="你的迭代次数已用完。请继续完成你的工作。\\n\\n优先级：\\n1. 如果核心工作已完成但还没写报告 → 立即写报告\\n2. 如果核心工作未完成 → 继续执行最关键的部分，然后写报告\\n3. 报告 verdict 根据实际完成度选择 pass/fail/partial\\n\\n⚠️ 无论如何必须写报告文件，这是调度器了解你工作结果的唯一渠道。")
   ```
3. 等待 follow_up 返回后：
   - 如果正常完成 → 进入**情况 B/C/D** 继续处理
   - 如果再次迭代用满 → 再次 follow_up（最多 {MAX_FOLLOW_UP_ON_EXHAUSTION} 次）
   - 达到 follow_up 上限后仍未完成 → 调用 handle-completion（Worker 大概率已在某次 follow_up 中写了报告）

#### 情况 B: Worker 正常完成，有报告

**识别特征**：通知不包含迭代用满特征（正常完成）

**处理方式**：
1. 从通知 Label 中提取任务 ID（格式如 "🔨 T-xxx: ..." 或 "🧪 T-xxx: ..."）
2. 调用 handle-completion：
   ```bash
   cd {WORKSPACE}
   python3 skills/task-dispatcher/scripts/scheduler.py handle-completion --task-id T-xxx
   ```
   ⚠️ `--task-id` 是必填参数，必须从 Label 中提取。
3. 解析 handle-completion 输出 → 进入**结果处理**

#### 情况 C: handle-completion 返回 mark_blocked（无报告）

如果 handle-completion 返回 `action: mark_blocked`，**不要直接接受**，先分析：

1. **检查 reason**：
   - 如果 reason 是 "no worker report found"：
     - 回看 Subagent Result Notification 的文本摘要（Worker 的最终输出）
     - 从摘要中判断 Worker 实际做了什么：
       - 如果 Worker 明显完成了工作（摘要中提到 "已完成"、"测试通过" 等）→ `follow_up` 让 Worker 补写报告
       - 如果 Worker 的输出看起来是中途中断 → `follow_up` 让 Worker 继续并写报告
       - 如果完全无法判断 → `follow_up` 询问 Worker 状态并要求写报告
     - follow_up 后重新调用 handle-completion
   - 如果 reason 包含 "max iterations reached" 或 "max consecutive" → 这是编排层面的限制，确认是否合理，如果任务确实需要更多轮次，考虑重置编排计数器后重新派发
   - 如果 reason 包含真正的外部阻塞信息 → 接受 blocked，通知用户

2. **最多重试 {MAX_FOLLOW_UP_ON_EXHAUSTION} 次** follow_up 补报告，之后才接受 blocked

#### 情况 D: handle-completion 返回 mark_blocked（partial 报告）

如果 handle-completion 返回 `action: mark_blocked` 且 reason 中包含 "partial"：

1. **分析 partial 的原因**（从报告 summary 和 issues 中判断）：
   - **任务太大/时间不够**（"未完成"、"部分完成"、"还需要..."）→ `follow_up` 让 Worker 继续完成剩余工作
   - **遇到技术问题可重试**（"测试失败"、"编译错误"、"依赖问题"）→ `follow_up` 让 Worker 修复问题并继续
   - **遇到真正的外部阻塞**（"需要 API key"、"需要权限"、"需要用户确认"）→ 接受 blocked，通知用户

2. follow_up 时把 partial 报告的 summary 作为上下文传给 Worker：
   ```
   follow_up(task="你之前的工作报告显示 partial 完成。请继续完成剩余工作：\\n\\n之前的进度：<summary>\\n未完成的部分：<issues>\\n\\n请继续执行，完成后更新报告文件。")
   ```

### 结果处理（handle-completion 输出解析）

解析 handle-completion 的 JSON 输出：
- `ok: true` + `verdict: pass` → 查看 pattern 决定下一角色，record-spawn + spawn
- `ok: true` + `verdict: fail` → 分析原因，决定重试或升级
- `ok: true` + `verdict: blocked` → 分析是否真正阻塞
- `ok: false` + `action: no_report` → follow_up 让 Worker 补写报告
- 参考 `prior_context` 字段获取前序角色产出，传给下一个角色
- 参考 `history` 字段了解已完成的角色和 verdict

### 后续：执行新一轮常规调度

处理完 Worker 结果后，执行常规调度：

```bash
cd {WORKSPACE}
python3 skills/task-dispatcher/scripts/scheduler.py {mode}{parent_flag}
```

如果有新的 `dispatched`，用 spawn tool 派发（传入 `max_iterations`）。

### 通知用户（仅重要事件）

以下情况通知用户：
- 任务完成（done）或进入 review
- 任务被真正 blocked（经过你的重试仍无法解决）
- 连续多次 follow_up 后仍未成功

使用 feishu_notify.py 格式化通知：
```bash
python3 {WORKSPACE}/skills/task-dispatcher/scripts/feishu_notify.py format-done <task_id>
python3 {WORKSPACE}/skills/task-dispatcher/scripts/feishu_notify.py format-error <task_id> --reason "原因"
```

---

## 后续唤醒

之后每次收到 "⏰ 调度器唤醒" 或 "[Subagent Result Notification]" 消息时，重复上述流程。
每轮调度都要先检查 INBOX（Step 0），再执行常规调度。
始终记住：你是项目经理，主动推进，能自己解决的问题不要抛给用户。
"""
    return prompt


# ──────────────────────────────────────────
# Core: send message to existing session
# ──────────────────────────────────────────

def send_to_session(session_key: str, message: str, wait: int = 0) -> dict:
    """Send a message to an existing session via worker execute-stream API.

    This reuses the same session_key, so the worker appends to the existing
    session's conversation history.
    """
    try:
        payload = json.dumps({
            "session_key": session_key,
            "message": message,
        }).encode("utf-8")

        worker_url = f"http://127.0.0.1:{WORKER_PORT}/execute-stream"
        req = Request(worker_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")

        # Fire-and-forget: send request with short timeout, don't wait for completion
        timeout = max(wait + 30, 15) if wait > 0 else 15
        try:
            with urlopen(req, timeout=timeout) as resp:
                # Read a small amount to confirm it started
                chunk = resp.read(200)
                return {"ok": True, "started": True}
        except Exception:
            # Timeout is expected for fire-and-forget — the worker continues
            return {"ok": True, "started": True, "note": "Request sent, timed out waiting (expected)"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_to_session_async(session_key: str, message: str) -> dict:
    """Send a message to a session asynchronously using subprocess + curl.

    Fire-and-forget: curl runs in background, we don't wait.
    """
    try:
        payload = json.dumps({
            "session_key": session_key,
            "message": message,
        })
        worker_url = f"http://127.0.0.1:{WORKER_PORT}/execute-stream"

        proc = subprocess.Popen(
            ["curl", "-s", "--max-time", "600", "-X", "POST",
             worker_url,
             "-H", "Content-Type: application/json",
             "-d", payload],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Detach from parent to prevent zombie processes
        )

        return {
            "ok": True,
            "started": True,
            "pid": proc.pid,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────
# Core: create new dispatcher session
# ──────────────────────────────────────────

def create_dispatcher_session(
    parent_session_id: str = "",
    dry_run: bool = False,
    wait: int = 0,
) -> dict:
    """Create a new dispatcher session and update dispatcher.json.

    Uses send_to_session_async to create/activate the session via worker API.
    Returns the new dispatcher state and creation result.
    """
    ts = int(time.time())
    rand_suffix = random.randint(100, 999)
    parent_chat_id = parent_session_id.split("_")[-1] if parent_session_id else str(ts)
    session_key = f"webchat:dispatch_{parent_chat_id}_{ts}_{rand_suffix}"
    session_id = session_key.replace(":", "_")

    # Build the full initial prompt
    prompt = build_scheduler_prompt(
        dry_run=dry_run,
        parent_session_id=parent_session_id,
        is_wake_up=False,
    )

    # Send prompt to create/activate the dispatcher session
    send_result = send_to_session_async(session_key, prompt)

    if send_result.get("ok"):
        # Load old dispatcher to track generation lineage
        old_dispatcher = load_dispatcher()
        old_generation = old_dispatcher.get("generation", 0) if old_dispatcher else 0
        old_session_id = old_dispatcher.get("session_id", "") if old_dispatcher else ""

        # Save new dispatcher state
        dispatcher = {
            "session_id": session_id,
            "session_key": session_key,
            "created_at": _now_iso(),
            "iteration_count": 1,
            "last_triggered_at": _now_iso(),
            "parent_session_id": parent_session_id,
            "dry_run": dry_run,
            "version": 3,
            "generation": old_generation + 1,
            "previous_session_id": old_session_id,
        }
        save_dispatcher(dispatcher)

        return {
            "ok": True,
            "action": "created_new",
            "session_key": session_key,
            "session_id": session_id,
            "dispatcher": dispatcher,
            "send_pid": send_result.get("pid"),
        }
    else:
        return {
            "ok": False,
            "error": f"Failed to create dispatcher session: {send_result.get('error', 'unknown')}",
        }


# ──────────────────────────────────────────
# Trigger logic (main entry point)
# ──────────────────────────────────────────

def trigger_scheduler(
    parent_session_id: str = "",
    dry_run: bool = False,
    wait: int = 0,
) -> dict:
    """Trigger the scheduler — reuse existing dispatcher session or create new one.

    Flow:
    1. Check web-chat health
    2. Load dispatcher.json
    3. If dispatcher exists and session is alive and under iteration cap:
       → Send wake-up message to existing session
    4. Otherwise:
       → Create new dispatcher session and update dispatcher.json
    """
    # 1. Health check
    if not check_webchat_health():
        return {
            "ok": False,
            "error": f"Web-chat service not running at port {WEBSERVER_PORT}",
        }

    # 2. Load dispatcher state
    dispatcher = load_dispatcher()

    # 3. Check if existing dispatcher session is usable
    if dispatcher:
        session_id = dispatcher.get("session_id", "")
        session_key = dispatcher.get("session_key", "")
        iteration_count = dispatcher.get("iteration_count", 0)

        # 3a. Check iteration cap (generation handoff) — primary check (no API call)
        if iteration_count >= MAX_ITERATIONS:
            # Create successor session
            result = create_dispatcher_session(
                parent_session_id=parent_session_id,
                dry_run=dry_run,
                wait=wait,
            )
            if result.get("ok"):
                result["action"] = "generation_handoff"
                result["previous_session_id"] = session_id
                result["previous_iterations"] = iteration_count
                result["handoff_reason"] = "iteration_cap"
            return result

        # 3b. Check if session is still alive
        status = check_session_alive(session_id)

        if status.get("alive"):
            # 3b.1 Check message_count cap (兜底 — catches internal session bloat)
            if check_iteration_limit(dispatcher, session_status=status):
                result = create_dispatcher_session(
                    parent_session_id=parent_session_id,
                    dry_run=dry_run,
                    wait=wait,
                )
                if result.get("ok"):
                    result["action"] = "generation_handoff"
                    result["previous_session_id"] = session_id
                    result["previous_iterations"] = iteration_count
                    result["handoff_reason"] = "message_count_cap"
                    result["message_count"] = status.get("message_count", 0)
                return result

            # Check if session is busy (active within last 5 minutes)
            if should_skip_wakeup(status):
                return {
                    "ok": True,
                    "action": "skipped_busy",
                    "session_id": session_id,
                    "session_key": session_key,
                    "iteration_count": iteration_count,
                    "last_active": status.get("last_active", ""),
                    "reason": f"Session was active within last {SESSION_BUSY_SECONDS}s, likely still executing",
                }

            # Session is alive and idle — send wake-up message
            prompt = build_scheduler_prompt(
                dry_run=dry_run,
                parent_session_id=parent_session_id,
                is_wake_up=True,
            )

            send_result = send_to_session_async(session_key, prompt)

            if send_result.get("ok"):
                dispatcher = increment_iteration(dispatcher)
                return {
                    "ok": True,
                    "action": "wake_up",
                    "session_id": session_id,
                    "session_key": session_key,
                    "iteration_count": dispatcher["iteration_count"],
                    "dry_run": dry_run,
                    "send_pid": send_result.get("pid"),
                }
            else:
                # Failed to send — fall through to create new session
                pass

        # Session is dead/stale or send failed — create new one
        # (fall through)

    # 4. Create new dispatcher session
    result = create_dispatcher_session(
        parent_session_id=parent_session_id,
        dry_run=dry_run,
        wait=wait,
    )
    if result.get("ok") and dispatcher:
        result["previous_session_id"] = dispatcher.get("session_id", "")
        result["previous_reason"] = "stale_or_dead"
    return result


# ──────────────────────────────────────────
# Status / diagnostics
# ──────────────────────────────────────────

def get_dispatcher_status() -> dict:
    """Get current dispatcher status (read-only diagnostic)."""
    dispatcher = load_dispatcher()
    if not dispatcher:
        return {
            "ok": True,
            "status": "no_dispatcher",
            "dispatcher": None,
            "message": "No active dispatcher session. Next trigger will create one.",
        }

    session_id = dispatcher.get("session_id", "")
    session_status = check_session_alive(session_id)

    return {
        "ok": True,
        "status": "active" if session_status.get("alive") else "stale",
        "dispatcher": dispatcher,
        "session_status": session_status,
        "iterations_remaining": max(0, MAX_ITERATIONS - dispatcher.get("iteration_count", 0)),
    }


# ──────────────────────────────────────────
# Cron setup helper
# ──────────────────────────────────────────

def print_cron_setup():
    """Print instructions for setting up the 30min cron fallback."""
    script_path = Path(__file__).resolve()
    # Minimal cron message — LLM only needs to run one command, no complex understanding
    cron_message = (
        f"请触发数字助理调度器：\n\n"
        f"python3 {script_path}\n\n"
        f"直接运行上述命令，输出 JSON 结果即可。不需要做其他事情。"
    )
    print(f"""
## Cron 30min 兜底调度 — 设置方法

### 方法 1: 使用 nanobot cron tool (推荐)

在任意 nanobot session 中执行:

```
cron(
    action="add",
    name="task-dispatcher-scheduler",
    message="{cron_message}",
    every_seconds=1800,
)
```

### 方法 2: 系统 crontab

```bash
*/30 * * * * cd {WORKSPACE} && python3 {script_path} >> /tmp/da-scheduler.log 2>&1
```

### 说明
- Cron 是兜底机制，确保任务被定期处理
- 触发器优先复用已有调度器 session（发送唤醒消息）
- 如果调度器 session 已失效或达到迭代上限，自动创建新的
- 无需文件锁 — dispatcher.json 是唯一状态源
- Cron 消息极简化：LLM 只需运行一个命令，不需要理解复杂流程
""")


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Trigger the Task Dispatcher Scheduler via fixed dispatcher session",
    )
    parser.add_argument("--parent", default="",
                        help="Parent session ID for tracking")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scheduler won't dispatch, only report")
    parser.add_argument("--wait", type=int, default=0,
                        help="Wait N seconds for completion (0=fire-and-forget)")
    parser.add_argument("--cron-setup", action="store_true",
                        help="Print cron setup instructions")
    parser.add_argument("--status", action="store_true",
                        help="Show current dispatcher status")
    parser.add_argument("--reset", action="store_true",
                        help="Reset dispatcher state (force next trigger to create new session)")

    args = parser.parse_args()

    if args.cron_setup:
        print_cron_setup()
    elif args.status:
        result = get_dispatcher_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.reset:
        if DISPATCHER_FILE.exists():
            old = load_dispatcher()
            DISPATCHER_FILE.unlink()
            print(json.dumps({
                "ok": True,
                "action": "reset",
                "previous_dispatcher": old,
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"ok": True, "action": "reset", "note": "No dispatcher.json to reset"}))
    else:
        result = trigger_scheduler(
            parent_session_id=args.parent,
            dry_run=args.dry_run,
            wait=args.wait,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
