---
name: batch-orchestrator
description: 通用批工作调度框架。将可并行的任务拆解为 Worker subagent，通过"调度 session + spawn Worker"两层编排模式执行。支持滑动窗口并发、watchdog 兜底、follow_up 异常恢复。
---

# batch-orchestrator — 通用批工作调度框架

> 将可并行的任务拆解为 Worker subagent，通过两层编排模式高效执行。

---

## 1. 概述

### 适用场景

- 批量构造/修复/迁移等任务，可拆解为多个独立子任务
- 单 session 串行处理会导致上下文爆炸或轮次耗尽
- 子任务之间无强依赖，可并行执行

### 架构：两层编排

```
用户 session ─── 与用户讨论，产出设计文档和 Prompt
     │
     │  用户手动创建新 session（或通过 web-subsession 启动）
     ▼
调度 session ─── 任务管理：spawn Worker、接收回报、状态更新、异常恢复
     │              session_key: webchat:dispatch_<ts>
     │
     │  通过 spawn subagent 启动（每个 Worker 一个）
     ▼
Worker subagent ── 执行单个子任务，完成后自动回报结果
                    （spawn 的 task_id 记录在 state.json 中）
```

**与旧版四层架构的区别**：
- ~~主控 session~~ → 去掉。调度 session 自身就能感知 Worker 完成、做决策
- ~~Worker session (web-subsession)~~ → 改为 spawn subagent，完成后自动回报
- ~~文件轮询~~ → 被动接收 spawn 回报消息，不消耗迭代次数

### 依赖

- `web-subsession` skill（用户 session 启动调度 session 时使用）
- spawn subagent（调度 session 启动 Worker 时使用）

---

## 2. 角色分工

### 两级角色

| 角色 | 职责 | 不做什么 |
|------|------|---------|
| **调度 session** | 读取任务队列、spawn Worker、接收回报、更新 state.json、异常恢复、启动 watchdog | 不执行具体任务（只调度不干活） |
| **Worker subagent** | 执行单个子任务、写 result 文件、回报结果 | 不关心其他 Worker 的状态 |

### 异常处理分层

```
Worker 异常 → 调度 session 处理（follow_up 恢复、标记 failed）
调度 session 轮次耗尽 → 用户 session 兜底（启动新调度 session 接力）
```

---

## 3. 并发模型：滑动窗口

> **不是**"等一批全部完成再启动下一批"，而是**滑动窗口**。

- 初始启动 N 个 Worker（N = 并发度，通常 3~5）
- 每完成一个 Worker，立即从 queue 中取下一个启动
- 调度 session spawn 若干 Worker 后，进入"空闲等待"——不消耗迭代次数
- Worker 完成后通过 SessionMessenger 注入 `role="user"` 消息到调度 session，触发下一轮 LLM 调用

**关键优势**：快的 Worker 不会被慢的拖累，整体吞吐量最大化。

---

## 4. 执行流程

### Phase 0 — 准备（当前 session，与用户交互）

1. 与用户讨论任务范围、约束、质量标准
2. 设计任务计划清单（TASK_PLAN.md）
3. 设计 Worker Prompt 模板
4. 生成所有文档（见 §8 文件组织结构）
5. 生成 DISPATCHER_PROMPT.md — 用户粘贴到新 session 或通过 web-subsession 启动

### Phase 1 — 调度执行

**触发**：用户创建新 session 粘贴 DISPATCHER_PROMPT，或通过 web-subsession 启动。

> 💡 **父子关系注册**：通过 web-subsession 启动调度 session 时，使用 `--parent` 参数注册父子关系：
> ```bash
> bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
>   --session-key "webchat:dispatch_${MASTER_TS}_${DISPATCH_TS}" \
>   --message "$(cat DISPATCHER_PROMPT.md)" \
>   --title "📋 调度 gen1" \
>   --parent "${MASTER_SESSION_ID}"
> ```
> `--parent` 值是父 session 的 ID（下划线格式如 `webchat_1773170043`），会通过 `POST /api/sessions/parents` 注册到 session_parents.json。

**调度 session 执行流程**：

1. **初始化**：读取 state.json + TASK_PLAN.md → 了解任务全貌和当前进度
2. **启动初始窗口**：从 queue 中取 N 个任务，逐个 spawn Worker subagent
3. **启动 watchdog**：spawn 一个 watchdog subagent（见 §5）
4. **等待回报**：调度 session 进入空闲，等待 Worker 或 watchdog 的回报消息
5. **处理 Worker 回报**：
   - 更新 state.json（in_progress → completed/failed）
   - 如果 queue 非空，立即 spawn 下一个 Worker（滑动窗口）
6. **处理 watchdog 回报**：检查超时 Worker，必要时 follow_up 恢复（见 §5）
7. **循环**：重复 4~6，直到 queue 为空且 in_progress 为空
8. **完成**：更新 state.json，标记 dispatcher.status = "completed"

### Phase 2 — Worker 执行（spawn subagent）

Worker subagent 的生命周期极简：

1. 读取任务 Prompt → 理解具体任务
2. 执行任务（读取输入、处理、写入输出）
3. 写 result JSON 到指定路径
4. 最后一轮 text reply 作为回报内容，自动发送到调度 session

---

## 5. Watchdog 机制

> Watchdog 是整个框架的核心兜底机制。基于 spawn follow_up 复用，一个 watchdog 持续服务整个批次。

### 为什么需要 watchdog

spawn subagent 没有整体超时机制。虽然三种正常退出路径（completed / max_iterations / failed）都会回报，但极端情况下（进程崩溃/OOM）会无回报。Watchdog 定期唤醒调度 session 检查是否有"失联"Worker。

### spawn 回报可靠性

| 结束方式 | status | 是否回报 | 回报内容 |
|---------|--------|---------|---------|
| 正常完成 | completed | ✅ | LLM 最后一轮的 text reply |
| 达到 max_iterations | max_iterations | ✅ | "Subagent reached maximum iterations..." |
| LLM 调用异常（重试耗尽） | failed | ✅ | error message |
| 进程崩溃 / OOM | — | ❌ | 无回报（极端罕见） |

### Watchdog 工作流程

```
调度 session
  │
  ├── spawn Worker A (task_id=xxx)
  ├── spawn Worker B (task_id=yyy)
  ├── spawn Worker C (task_id=zzz)
  │
  └── spawn Watchdog ──→ exec("sleep 300") ──→ 回报 "WATCHDOG: timeout check"
                                                        │
                              ┌──────────────────────────┘
                              ▼
                    调度 session 被唤醒
                    ├── 检查所有 in_progress Worker 的启动时间
                    ├── 超时未回报的 → follow_up 或标记 failed
                    ├── 还有未完成 Worker？
                    │     ├── 是 → follow_up(watchdog_task_id, "继续监控")
                    │     │         watchdog 再次 sleep → 再次回报 → 循环
                    │     └── 否 → 不再 follow_up，watchdog 自然结束
```

### 关键设计

1. **Watchdog 是一个 spawn subagent**，任务就是 `exec sleep N` 然后返回固定文本
2. **通过 follow_up 复用**——调度 session 收到 watchdog 回报后，如果还有未完成的 Worker，就 `follow_up(watchdog_task_id, "继续监控，N 秒后再检查")`，watchdog 再 sleep 一轮
3. **一个 watchdog 服务整个批次**，不需要反复创建新的
4. **所有 Worker 完成后**，不再 follow_up watchdog，让它自然结束

### Watchdog Prompt

```
你是 watchdog。你的唯一任务：执行 sleep 等待，然后返回检查信号。

执行：exec sleep {watchdog_interval_seconds}
完成后返回：WATCHDOG: timeout check
```

### 调度 session 处理 watchdog 回报的逻辑

```
收到 "WATCHDOG: timeout check" 后：
1. 遍历 state.json 中 in_progress 的所有任务
2. 对每个任务，检查 started_at 距今是否超过 worker_timeout_minutes
3. 超时的任务：
   a. 检查 result 文件是否已存在（Worker 可能完成了但回报丢失）
   b. result 存在 → 修复 state（in_progress → completed/failed）
   c. result 不存在 → follow_up(task_id, "请继续完成任务") 尝试恢复
   d. 已 follow_up 过一次仍超时 → 标记 failed
4. 如果还有 in_progress 任务 → follow_up(watchdog_task_id, "继续监控")
5. 如果所有任务完成 → 不再 follow_up watchdog
```

---

## 6. 异常恢复（spawn follow_up）

spawn follow_up 是异常恢复的核心手段——向已结束的 subagent 发送新消息，让它继续执行。

### 恢复策略矩阵

| Worker 回报情况 | 判断 | 处理方式 |
|----------------|------|---------|
| status=completed, 结果正常 | result 文件存在且 status=success | 更新 state → completed，spawn 下一个 |
| status=completed, 结果异常 | result 文件存在但 status=failed/needs_review | 更新 state → failed 或 completed（按业务逻辑） |
| status=max_iterations | 回报含 "maximum iterations" | `follow_up(task_id, "请继续完成任务")` 给更多迭代 |
| status=failed, 可重试 | LLM 调用失败等临时错误 | `follow_up(task_id, "请重试")` |
| status=failed, 不可重试 | 多次 follow_up 仍失败 | 标记 failed，记录原因 |
| 无回报（watchdog 检测到超时） | started_at 超时且无回报 | 检查 result 文件 → follow_up 或标记 failed |

### follow_up 使用要点

- `follow_up(task_id, message)` 会向该 subagent 发送新消息，触发新一轮执行
- subagent 保留完整历史上下文，可以从断点继续
- follow_up 次数建议限制（如最多 2 次），避免无限重试

---

## 7. 调度接力（用户 session 兜底）

调度 session 本身也有迭代上限。当 Worker 多、每个回报消耗一轮迭代时，可能耗尽。

### 检测方式

用户 session 通过 web-subsession 定期检查 state.json：

```bash
cat {batch_dir}/state.json | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'queue={len(d[\"queue\"])} in_progress={len(d[\"in_progress\"])} completed={len(d[\"completed\"])} failed={len(d[\"failed\"])}')
print(f'dispatcher: gen={d[\"dispatcher\"][\"generation\"]} status={d[\"dispatcher\"][\"status\"]}')
"
```

### 接力流程

1. 检测到调度 session 不再活跃（state.json 长时间未更新 + queue/in_progress 非空）
2. 读取 state.json，检查 in_progress 中的任务是否实际已完成（检查 result 文件）
3. 修复不一致状态（result 存在但 state 未更新 → 修复）
4. 更新 dispatcher.generation += 1，记录上一代到 history
5. 启动新调度 session → 新调度读 state.json 自然从断点继续

### 预算感知退出

调度 session 收到框架 "⚠️ Budget alert" 提醒后：
1. **不再 spawn 新 Worker**
2. 等待当前 in_progress Worker 的回报（被动等待，不消耗迭代）
3. 更新 state.json，标记 dispatcher.status = "exhausted"
4. 退出

---

## 8. 文件组织结构

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

### state.json Schema

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

### result.json Schema

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

## 9. Prompt 模板

### 9.1 DISPATCHER_PROMPT.md 模板

```markdown
# 调度 Session Prompt

你是调度 session，负责任务执行管理。**只调度不干活**——所有具体任务都由 spawn Worker subagent 执行。

## 你的职责

1. 从 state.json 读取待处理任务队列
2. 通过 spawn 启动 Worker subagent（滑动窗口模型）
3. 启动 watchdog subagent 做超时兜底
4. 被动接收 Worker/watchdog 回报，更新 state.json
5. 异常恢复（follow_up）

## 关键文件

- 运行状态: `{batch_dir}/state.json`
- 任务计划: `{batch_dir}/TASK_PLAN.md`
- Worker Prompt 模板: `{batch_dir}/WORKER_PROMPT_TEMPLATE.md`
- 结果目录: `{batch_dir}/results/`

## 执行流程

### Step 1: 读取状态
读取 state.json，获取 queue 中的待处理任务列表和 config 配置。
读取 TASK_PLAN.md，了解每个任务的具体要求。

### Step 2: 启动初始 Worker 窗口
从 queue 中取出 N 个任务（N = config.worker_concurrency）。
对每个任务：
1. 读取 WORKER_PROMPT_TEMPLATE.md
2. 从 TASK_PLAN.md 中获取该任务的具体要求，填充模板
3. spawn Worker subagent（记录返回的 task_id）
4. 更新 state.json：queue → in_progress，记录 task_id_spawn 和 started_at

### Step 3: 启动 Watchdog
spawn 一个 watchdog subagent，指令为：
> 执行 `exec sleep {watchdog_interval_seconds}`，完成后返回 "WATCHDOG: timeout check"

记录 watchdog 的 task_id_spawn 到 state.json。

### Step 4: 等待回报
此时你进入空闲等待。不需要做任何事——Worker 完成后会自动给你发消息。

### Step 5: 处理回报消息

**收到 Worker 回报时**：
1. 识别是哪个 Worker（从回报内容中解析 task_id）
2. 检查 result 文件是否已写入
3. 更新 state.json：in_progress → completed 或 failed
4. 如果 queue 非空 → spawn 下一个 Worker（滑动窗口）
5. 如果回报含 "maximum iterations" → follow_up(task_id, "请继续完成任务")

**收到 Watchdog 回报时**：
1. 遍历 in_progress 任务，检查是否有超时（started_at 超过 worker_timeout_minutes）
2. 超时任务：检查 result 文件 → 存在则修复 state；不存在则 follow_up 或标记 failed
3. 还有未完成任务 → follow_up(watchdog_task_id, "继续监控，{N}秒后再检查")
4. 所有任务完成 → 不再 follow_up watchdog

### Step 6: 完成
queue 为空且 in_progress 为空时：
1. 更新 state.json，标记 dispatcher.status = "completed"
2. 输出汇总报告

### 预算感知退出
收到 "⚠️ Budget alert" 提醒后：
- 不再 spawn 新 Worker
- 被动等待当前 Worker 回报
- 更新 state.json，标记 dispatcher.status = "exhausted"
- 退出（用户 session 会检测到并启动新调度 session 接力）

## 注意事项

- 每次收到回报后，**立即更新 state.json**
- Worker 回报内容应包含 task_id，便于识别
- 不要自己执行任何具体任务
- follow_up 最多尝试 config.max_follow_up_attempts 次
```

### 9.2 WORKER_PROMPT_TEMPLATE.md 模板

```markdown
# Worker Prompt

你是一个 Worker subagent，负责执行一个具体子任务。

## 任务信息

- **Task ID**: {{task_id}}
- **描述**: {{task_description}}

## 具体要求

{{task_specific_requirements}}

（以上内容由调度 session 从 TASK_PLAN.md 中获取并填充）

## 完成标准

{{completion_criteria}}

## 结果文件

完成后，将结果写入以下路径：

```
{batch_dir}/results/{{task_id}}.json
```

格式：
```json
{
  "task_id": "{{task_id}}",
  "status": "success | needs_review | failed",
  "completed_at": "YYYY-MM-DD HH:MM",
  "output": {
    // 任务特定的输出信息
  },
  "notes": "补充说明（如遇到的问题、做出的决策等）"
}
```

## 回报

完成后，你的最后一条回复将自动发送给调度 session。
请在最后一条回复中包含：
- task_id: {{task_id}}
- status: success / needs_review / failed
- 简要说明完成情况

## 注意事项

{{task_specific_notes}}
```

### 9.3 DESIGN.md 模板

```markdown
# {批次名称} — 设计文档

> 创建时间: YYYY-MM-DD HH:MM
> 状态: 📋 设计中 / ✅ 已确认

---

## 1. 背景

{为什么需要这次批量操作}

## 2. 目标

{本次批量操作要达成什么}

## 3. 任务范围

- 总量: {N} 个子任务
- 分类: {按类型/难度/来源等维度}

## 4. 约束条件

### 技术约束
- Worker 并发度: {N}
- Watchdog 间隔: {N} 秒
- Worker 超时: {N} 分钟

## 5. 质量标准

- {完成标准}
- {验证方式}
```

### 9.4 TASK_PLAN.md 模板

```markdown
# {批次名称} — 任务计划清单

> 总量: {N} 个子任务

---

## 任务清单

### {task_id_1}: {简短描述}

- **输入来源**: {需要读取的文件/数据}
- **预期产出**: {应该生成什么}
- **特殊要求**: {该任务独有的约束}

### {task_id_2}: {简短描述}

...
```

---

## 10. 经验法则

> 以下经验来自多次大规模实践。

| 经验 | 说明 |
|------|------|
| **调度只调度不干活** | 调度 session 自己执行任务会消耗大量迭代，压缩调度预算。所有任务一律由 Worker 执行。 |
| **滑动窗口优于分批** | 完成一个立即启动下一个，吞吐量远高于"等一批完再启动下一批"。 |
| **并发 3~5 个 Worker** | 过多并发会增加调度复杂度，过少则利用率不足。 |
| **Watchdog 间隔 5 分钟** | 太短浪费 watchdog 的迭代，太长延迟发现问题。 |
| **Worker 回报几乎 100% 可靠** | 三种正常退出路径都有回报，watchdog 只是兜底极端情况。 |
| **follow_up 优先于重建** | follow_up 保留完整历史上下文，比重新 spawn 更高效。 |
| **state.json 是恢复的关键** | 调度 session 轮次耗尽后，新调度从 state.json 断点继续。务必及时更新。 |
| **被动等待不消耗迭代** | spawn 后调度 session 空闲等待，只有收到回报才消耗迭代——比轮询高效得多。 |
| **task_id_spawn 必须记录** | 这是 follow_up 的唯一凭证，丢失则无法恢复。 |
| **Worker 回报要包含 task_id** | 调度 session 收到回报时需要知道是哪个 Worker，Prompt 中要明确要求。 |
| **exec sleep 用于 watchdog** | watchdog 的核心就是 `exec sleep N`，简单可靠。 |
