# batch-orchestrator — State & Result Schema

> 本文件包含 state.json 和 result.json 的完整 Schema 定义。由 SKILL.md 索引引用。

---

## 文件组织结构

```
{batch_name}/
├── DESIGN.md                    # 整体设计（背景、目标、约束、质量标准）
├── TASK_PLAN.md                 # 任务计划清单（每个子任务的具体要求）
├── DISPATCHER_PROMPT.md         # 调度 session Prompt（粘贴到新 session 或 web-subsession 启动）
├── WORKER_PROMPT_TEMPLATE.md    # Worker Prompt 模板（调度引用，从 TASK_PLAN 填充）
├── state.json                   # 运行时状态（调度 session 读写）
├── results/                     # Worker 结果文件目录
│   └── {task_id}.json
└── RETROSPECTIVE.md             # 复盘（事后填写）
```

---

## state.json Schema

```json
{
  "version": 2,
  "created_at": "YYYY-MM-DD HH:MM",
  "updated_at": "YYYY-MM-DD HH:MM",

  "config": {
    "worker_concurrency": 4,
    "worker_timeout_minutes": 15,
    "watchdog_interval_seconds": 300,
    "max_follow_up_attempts": 2,
    "results_dir": "results/"
  },

  "queue": ["task_id_1", "task_id_2"],
  "in_progress": [],
  "completed": [],
  "failed": [],
  "skipped": [],

  "tasks": {
    "task_id_1": {
      "display_name": "描述",
      "task_id_spawn": null,
      "started_at": null,
      "completed_at": null,
      "follow_up_count": 0,
      "last_status": null
    }
  },

  "watchdog": {
    "task_id_spawn": null,
    "active": false
  },

  "user_watchdog": {
    "task_id_spawn": null,
    "interval_seconds": 600,
    "active": false,
    "last_check_at": null
  },

  "dispatcher": {
    "generation": 1,
    "session_key": null,
    "status": "not_started | running | exhausted | completed",
    "history": [
      {
        "generation": 1,
        "session_key": "...",
        "started_at": "...",
        "ended_at": "...",
        "reason": "exhausted | completed | error"
      }
    ]
  }
}
```

---

## result.json Schema

```json
{
  "task_id": "string — 任务 ID",
  "status": "success | needs_review | failed",
  "completed_at": "YYYY-MM-DD HH:MM",
  "output": {},
  "notes": "string — 补充说明"
}
```

---

## spawn 回报可靠性

| 结束方式 | status | 是否回报 | 回报内容 |
|---------|--------|---------|---------|
| 正常完成 | completed | ✅ | LLM 最后一轮的 text reply |
| 达到 max_iterations | max_iterations | ✅ | "Subagent reached maximum iterations..." |
| LLM 调用异常（重试耗尽） | failed | ✅ | error message |
| 进程崩溃 / OOM | — | ❌ | 无回报（极端罕见） |

---

## User Watchdog state.json 字段

```json
{
  "user_watchdog": {
    "task_id_spawn": null,
    "interval_seconds": 600,
    "active": false,
    "last_check_at": null
  }
}
```

---

## 手动检查状态命令

```bash
cat {batch_dir}/state.json | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'queue={len(d[\"queue\"])} in_progress={len(d[\"in_progress\"])} completed={len(d[\"completed\"])} failed={len(d[\"failed\"])}')
print(f'dispatcher: gen={d[\"dispatcher\"][\"generation\"]} status={d[\"dispatcher\"][\"status\"]}')
"
```
