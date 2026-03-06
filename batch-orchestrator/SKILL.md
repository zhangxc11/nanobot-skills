---
name: batch-orchestrator
description: 通用批工作调度框架。将可并行的任务拆解为 Worker，通过"准备→主控→调度→Worker"四层编排模式执行。适用于批量构造、批量修复、批量迁移等需要并行处理多个独立子任务的场景。
---

# batch-orchestrator — 通用批工作调度框架

> 将可并行的任务拆解为 Worker，通过四层编排模式高效执行。

---

## 1. 概述

### 适用场景

- 批量构造/修复/迁移等任务，可拆解为多个独立子任务
- 单 session 串行处理会导致上下文爆炸或轮次耗尽
- 子任务之间无强依赖，可并行执行

### 依赖

- `web-subsession` skill（当前 Worker 模式）
- 长期切换到 `spawn`（当前有 bug，暂不可用）

---

## 2. 角色分工

```
准备 session ─── 与用户讨论，产出设计文档和 Prompt
     │
     │  用户手动创建新 session，粘贴 MASTER_PROMPT
     ▼
主控 session ─── 业务逻辑、决策、处理调度 session 级异常
     │
     │  通过 web-subsession 启动
     ▼
调度 session ─── 任务执行管理：启动 Worker、轮询、状态更新、Worker 异常恢复
     │
     │  通过 web-subsession 启动（每个 Worker 一个）
     ▼
Worker session ── 执行单个子任务，写 result 文件
```

### 职责边界

| 角色 | 职责 | 不做什么 |
|------|------|---------|
| **准备 session** | 与用户讨论设计、产出所有文档和 Prompt | 不执行任何任务 |
| **主控 session** | 启动调度、监控调度健康、处理调度级异常、汇总最终结果 | 不直接管理 Worker |
| **调度 session** | 启动 Worker、轮询结果、Worker 异常恢复、更新 state.json | 不执行具体任务（只调度不干活） |
| **Worker session** | 执行单个子任务、写 result 文件 | 不关心其他 Worker 的状态 |

### 异常处理分层

```
Worker 异常 → 调度 session 优先处理（发送恢复消息、标记 failed）
调度 session 异常（轮次耗尽、卡住）→ 主控 session 处理（启动新调度 session）
主控 session 轮次接近上限 → 写交接信息，用户启动新主控 session 接力
```

---

## 3. Session 命名规范与父子关系

> 详见 `web-subsession` skill 的"Session Key 命名规范"章节。以下是批量调度场景的要点。

### ⚠️ 命名规则（强制要求）

通过 `web-subsession` 路径 A 创建的所有子 session，**必须**遵循命名规则。前端启发式规则会自动识别父子关系。

### 命名格式

```
webchat:<role>_<parent_ref>_<detail>
```

- `parent_ref`：主控 session 的 timestamp（如 `1772696251`）
- 所有层级（调度、Worker）都使用**同一个 parent_ref**，保持扁平化

### 批量调度命名示例

假设主控 session 是 `webchat:1772696251`：

| 角色 | session_key | 自动识别的父 session |
|------|------------|-------------------|
| 调度 gen1 | `webchat:dispatch_1772696251_gen1` | `webchat:1772696251` |
| 调度 gen2 | `webchat:dispatch_1772696251_gen2` | `webchat:1772696251` |
| Worker | `webchat:worker_1772696251_task003` | `webchat:1772696251` |
| Worker | `webchat:worker_1772696251_task017` | `webchat:1772696251` |

> 父子关系由前端启发式规则自动识别（从 session_key 中提取 10 位 timestamp），无需手动注册。

---

## 4. Worker 模式

> **当前推荐**：`web-subsession`（spawn 有 bug 暂不可用）
> **长期目标**：切换到 `spawn`（修复后）

| 维度 | web-subsession (当前) | spawn (长期) |
|------|----------------------|-------------|
| 启动方式 | `curl --max-time 5` 非阻塞启动 | `spawn(persist=true, max_iterations=N)` |
| 结果获取 | 文件轮询 result JSON | 自动返回结果 |
| 失败恢复 | curl 发送继续消息到同一 session | 无法恢复，需重新 spawn |
| 前端可见 | ✅ 独立 session，可查看过程 | ✅ persist 模式写入 session JSONL |
| 进程隔离 | ✅ 独立 worker 进程 | ❌ 同进程内 |
| 切换条件 | — | spawn bug 修复 + 支持文件写入 |

### 跨通道使用（CLI / 飞书发起 batch）

从 CLI 或飞书通道发起 batch 时，子 session 的 parent_ref 使用父 session 的 timestamp。前端启发式规则 B 自动跨通道匹配父 session（如 `cli:xxx`、`feishu.lab:xxx`）。详见 `web-subsession` skill 的"跨通道使用"章节。

---

## 5. 工作流四阶段

### Phase 0 — 准备（当前 session，与用户交互）

**目标**：讨论清楚任务全貌，产出所有执行所需的文档。

**步骤**：
1. 与用户讨论：哪些任务可以并行拆解
2. 明确任务范围、约束条件、质量标准
3. 设计任务计划清单（逐条列出每个子任务的具体要求）
4. 设计 Worker Prompt 模板
5. 设计主控 session 的 follow 思路和异常处理策略
6. 生成所有文档（见 §5 文件组织结构）
7. 生成 MASTER_PROMPT.md — 用户粘贴到新 session 的启动 Prompt

**产出物**：见 §5 标准文件组织结构中的所有文件。

### Phase 1 — 主控执行（用户手动创建新 session）

**触发**：用户创建新 session，粘贴 MASTER_PROMPT.md 内容。

**主控 session 执行流程**：
1. 读取 DESIGN.md + TASK_PLAN.md + state.json → 理解任务全貌
2. 通过 `web-subsession` 启动调度 session（传入 DISPATCHER_PROMPT.md 内容）
3. 长间隔检查调度进度（建议 5~10 分钟）
4. 异常处理（见 §7 关键机制）
5. 全部完成 → 读取所有 result 文件 → 汇总报告

**主控轮询方式**：
```bash
# 检查调度 session 是否还在运行（查看 JSONL 最后更新时间）
ls -la ~/.nanobot/sessions/{dispatcher_session_key}.jsonl

# 检查 state.json 进度
cat {batch_dir}/state.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'queue={len(d[\"queue\"])} in_progress={len(d[\"in_progress\"])} completed={len(d[\"completed\"])} failed={len(d[\"failed\"])}')"
```

### Phase 2 — 调度执行（主控通过 web-subsession 启动）

**调度 session 执行流程**：
1. 读取 state.json → 获取 queue 中的待处理任务
2. 读取 TASK_PLAN.md → 获取每个任务的具体要求
3. 根据并发度（config.worker_concurrency）从 queue 中取出一批任务
4. 为每个任务填充 WORKER_PROMPT_TEMPLATE.md → 生成具体 Worker Prompt
5. 通过 `web-subsession` 启动 Worker session
6. 更新 state.json（queue → in_progress）
7. 批量轮询 Worker result 文件（间隔从 config.poll_interval_seconds 读取）
8. 收割完成的 Worker → 更新 state.json（in_progress → completed/failed）
9. 启动下一批 Worker
10. 预算感知 → 收到框架软限制提醒后优雅退出（见 §7.4）

**分批策略**：调度 session 根据并发度和运行情况自行安排，不需要预先规划分批。

### Phase 3 — Worker 执行（调度通过 web-subsession 启动）

**Worker session 执行流程**：
1. 读取任务 Prompt → 理解具体任务
2. 执行任务（读取输入、处理、写入输出）
3. 写 result JSON 到指定路径
4. 自然结束

---

## 6. 标准文件组织结构

```
{batch_name}/
├── DESIGN.md                    # 整体设计（背景、目标、约束、质量标准）
├── TASK_PLAN.md                 # 任务计划清单（每个子任务的具体要求）
├── MASTER_PROMPT.md             # 主控 session 启动 Prompt（粘贴到新 session）
├── DISPATCHER_PROMPT.md         # 调度 session Prompt（主控引用）
├── WORKER_PROMPT_TEMPLATE.md    # Worker Prompt 模板（调度引用，从 TASK_PLAN 填充）
├── state.json                   # 运行时状态（调度 session 读写）
├── results/                     # Worker 结果文件目录
│   └── {task_id}.json
└── RETROSPECTIVE.md             # 复盘（事后填写）
```

---

## 7. 文档模板

### 6.1 DESIGN.md 模板

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

### 业务约束
- {领域特定的约束}

### 技术约束
- Worker 并发度: {N}
- 轮询间隔: {N} 秒
- Worker 超时: {N} 分钟

## 5. 质量标准

- {完成标准}
- {验证方式}

## 6. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| {风险1} | {措施1} |
```

### 6.2 TASK_PLAN.md 模板

```markdown
# {批次名称} — 任务计划清单

> 来源: DESIGN.md
> 总量: {N} 个子任务

---

## 任务清单

### {task_id_1}: {简短描述}

- **输入来源**: {需要读取的文件/数据}
- **预期产出**: {应该生成什么}
- **特殊要求**: {该任务独有的约束}
- **预估复杂度**: easy / medium / hard

### {task_id_2}: {简短描述}

- **输入来源**: ...
- **预期产出**: ...
- **特殊要求**: ...
- **预估复杂度**: ...

...
```

### 6.3 MASTER_PROMPT.md 模板

```markdown
# 主控 Session Prompt

你是主控 session，负责管理一次批量任务的执行。

## 你的职责

1. 理解任务全貌
2. 启动调度 session
3. 监控调度 session 健康状态
4. 处理调度 session 级异常
5. 最终汇总报告

## 关键文件

- 设计文档: `{batch_dir}/DESIGN.md`
- 任务计划: `{batch_dir}/TASK_PLAN.md`
- 运行状态: `{batch_dir}/state.json`
- 调度 Prompt: `{batch_dir}/DISPATCHER_PROMPT.md`
- 结果目录: `{batch_dir}/results/`

## 执行步骤

### Step 1: 了解任务
读取 DESIGN.md 和 TASK_PLAN.md，了解任务全貌。
读取 state.json，了解当前执行状态（可能是接力执行，部分任务已完成）。

### Step 2: 启动调度 session
读取 DISPATCHER_PROMPT.md 内容，通过 web-subsession 启动调度 session。
参考 `web-subsession` skill 的用法。

### Step 3: 监控调度进度
每 5~10 分钟检查一次：

```bash
# 查看 state.json 进度摘要
cat {batch_dir}/state.json | python3 -c "
import sys,json; d=json.load(sys.stdin)
print(f'queue={len(d[\"queue\"])} in_progress={len(d[\"in_progress\"])} completed={len(d[\"completed\"])} failed={len(d[\"failed\"])}')
print(f'dispatcher: gen={d[\"dispatcher\"][\"generation\"]} status={d[\"dispatcher\"][\"status\"]}')
"
```

### Step 4: 异常处理

**调度 session 轮次耗尽**（queue 非空但调度不再活跃）：
1. 读取 state.json
2. 检查 in_progress 中的任务是否实际已完成（检查 result 文件）
3. 修复不一致状态
4. 更新 dispatcher.generation += 1
5. 启动新调度 session（新调度从 state.json 断点继续）

**如何检测调度 session 是否结束**：
```bash
# 查看调度 session JSONL 最后一行的时间戳
tail -1 ~/.nanobot/sessions/{dispatcher_session_key}.jsonl

# 如果最后消息超过 10 分钟且 queue 非空，说明调度已结束
```

### Step 5: 自身接力
如果自身轮次接近上限（收到预算提醒），写交接信息到 state.json 或单独文件，
包含当前状态摘要和继续执行的指引，供用户粘贴到新 session。

### Step 6: 汇总报告
全部任务完成后，读取所有 result 文件，生成汇总报告。
```

### 6.4 DISPATCHER_PROMPT.md 模板

```markdown
# 调度 Session Prompt

你是调度 session，负责任务执行管理。**只调度不干活**——所有具体任务都由 Worker session 执行。

## 你的职责

1. 从 state.json 读取待处理任务队列
2. 根据并发度启动 Worker session
3. 轮询 Worker 结果
4. 处理 Worker 异常
5. 更新 state.json

## 关键文件

- 运行状态: `{batch_dir}/state.json`
- 任务计划: `{batch_dir}/TASK_PLAN.md`
- Worker Prompt 模板: `{batch_dir}/WORKER_PROMPT_TEMPLATE.md`
- 结果目录: `{batch_dir}/results/`

## 执行流程

### Step 1: 读取状态
读取 state.json，获取 queue 中的待处理任务列表和 config 配置。
读取 TASK_PLAN.md，了解每个任务的具体要求。

### Step 2: 启动一批 Worker
从 queue 中取出 N 个任务（N = config.worker_concurrency）。
对每个任务：
1. 读取 WORKER_PROMPT_TEMPLATE.md
2. 从 TASK_PLAN.md 中获取该任务的具体要求，填充模板
3. 通过 web-subsession 启动 Worker session（curl --max-time 5 非阻塞）
4. 更新 state.json：queue → in_progress，记录 session_key

### Step 3: 轮询结果
等待 config.poll_interval_seconds 秒后，批量检查所有 in_progress Worker 的结果：

```bash
# 一条命令检查所有 pending Worker 的 result 文件
for f in {batch_dir}/results/{task_id_1}.json {batch_dir}/results/{task_id_2}.json ...; do
  [ -f "$f" ] && echo "DONE: $f" || echo "PENDING: $f"
done
```

### Step 4: 收割完成的 Worker
对已完成的 Worker：
1. 读取 result JSON，确认 status
2. 更新 state.json：in_progress → completed 或 failed

### Step 5: Worker 异常处理
对长时间未完成的 Worker，检查健康状态：

```bash
# 查看 Worker session 最后一条消息
tail -1 ~/.nanobot/sessions/{worker_session_key}.jsonl
```

**判断标准**：
- 最后消息包含 "Error calling LLM" → Worker 已崩溃
- 最后消息时间戳超过 worker_timeout_minutes → Worker 可能卡住
- session JSONL 文件大小不再增长 → Worker 已停止

**恢复策略**：
1. 向同一 session_key 发送继续消息：
   ```bash
   curl --max-time 5 -X POST http://localhost:8082/api/execute-stream \
     -H "Content-Type: application/json" \
     -d '{"session_key":"{worker_session_key}","message":"请继续之前的工作，完成后写 result 文件"}'
   ```
2. 二次失败 → 标记 failed，更新 state.json

### Step 6: 循环
重复 Step 2~5，直到 queue 为空且 in_progress 为空。

### Step 7: 预算感知退出
收到框架软限制提醒（"⚠️ Budget alert"）后：
1. **不再启动新 Worker**
2. 等待当前 in_progress 的 Worker 完成（最多再轮询 1~2 次）
3. 更新 state.json（确保状态准确）
4. 更新 dispatcher.status = "exhausted"
5. 退出（主控 session 会检测到并启动新调度 session）

## 注意事项

- 每次启动新 Worker 或收割结果后，**立即更新 state.json**
- 轮询间隔不要太短，避免浪费迭代次数
- 不要自己执行任何具体任务
```

### 6.5 WORKER_PROMPT_TEMPLATE.md 模板

```markdown
# Worker Prompt

你是一个 Worker session，负责执行一个具体子任务。

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

## 注意事项

{{task_specific_notes}}
```

### 6.6 state.json Schema

```json
{
  "version": 1,
  "created_at": "YYYY-MM-DD HH:MM",
  "updated_at": "YYYY-MM-DD HH:MM",

  "config": {
    "worker_concurrency": 4,
    "poll_interval_seconds": 180,
    "worker_timeout_minutes": 15,
    "results_dir": "results/",
    "worker_mode": "web-subsession"
  },

  "queue": ["task_id_1", "task_id_2"],
  "in_progress": [],
  "completed": [],
  "failed": [],
  "skipped": [],

  "tasks": {
    "task_id_1": {
      "display_name": "描述",
      "session_key": null,
      "started_at": null,
      "completed_at": null
    }
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

### 6.7 result.json Schema

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

## 8. 关键机制

### 7.1 调度 session 轮次耗尽 → 主控启动新调度

**触发条件**：
- state.json 中 queue 非空或 in_progress 非空
- 调度 session 不再活跃

**主控检测方法**：
```bash
# 方法 1: 查看调度 session JSONL 最后一行
tail -1 ~/.nanobot/sessions/{dispatcher_session_key}.jsonl
# 如果最后消息时间戳距今超过 10 分钟，且 queue/in_progress 非空 → 调度已结束

# 方法 2: 查看 state.json 中 dispatcher.status
# 如果为 "exhausted" → 调度主动退出，明确需要新调度

# 方法 3: 查看 JSONL 文件修改时间
ls -la ~/.nanobot/sessions/{dispatcher_session_key}.jsonl
# 文件修改时间不再更新 → 调度已结束
```

**恢复流程**：
1. 读取 state.json，检查 in_progress 中的任务
2. 对每个 in_progress 任务，检查 result 文件是否已存在
3. 如果 result 已存在但 state 未更新 → 修复（移到 completed/failed）
4. 更新 dispatcher.generation += 1，记录上一代到 history
5. 启动新调度 session → 新调度读 state.json 自然从断点继续

### 7.2 Worker 失败检测与恢复

**由调度 session 负责**。检测手段：

```bash
# 查看 Worker session 最后一条消息
tail -1 ~/.nanobot/sessions/{worker_session_key}.jsonl

# 检查 JSONL 文件大小是否还在增长
ls -la ~/.nanobot/sessions/{worker_session_key}.jsonl
# 间隔 30s 再查一次，对比文件大小
```

**恢复策略**：
1. 向同一 session_key 发送继续消息（基于 history 机制可从断点恢复）
2. 二次失败 → 标记 failed，等待主控决策

### 7.3 主控 session 自身接力

当主控收到预算提醒时：
1. 确保 state.json 是最新的
2. 写交接信息，包含：
   - state.json 路径
   - 当前调度 session 状态
   - 待处理的异常（如有）
   - 继续执行的指引
3. 用户可将交接信息粘贴到新 session 继续

### 7.4 预算感知与优雅退出

nanobot 框架在迭代次数接近上限时会发送 "⚠️ Budget alert" 提醒。

**调度 session 收到提醒后**：
- 不启动新 Worker
- 等待当前 Worker 完成（最多 1~2 次轮询）
- 更新 state.json，标记 dispatcher.status = "exhausted"
- 退出

**主控 session 收到提醒后**：
- 写交接信息
- 退出

---

## 9. 经验法则

> 以下经验来自两次大规模实践（34 个测例批量构造 + 26 个测例批量修复）。

| 经验 | 说明 |
|------|------|
| **调度只调度不干活** | 调度 session 自己执行任务会消耗大量迭代，压缩调度预算。所有任务一律由 Worker 执行。 |
| **100 轮迭代预算** | 纯调度模式下，单个调度 session 可管理 15~20 个 Worker。 |
| **并发 3~5 个 Worker** | 过多并发会导致轮询复杂度上升，过少则利用率不足。 |
| **轮询间隔 3~5 分钟** | 太短浪费迭代次数，太长延迟发现问题。 |
| **Worker 卡住率 ~10~20%** | 主要原因是 LLM 报错。调度 session 应有自动恢复能力。 |
| **慢 Worker 是调度预算杀手** | 等待慢 Worker 完成会消耗大量轮询迭代。超时后应果断标记 failed。 |
| **state.json 是恢复的关键** | 调度 session 轮次耗尽后，新调度从 state.json 断点继续。务必及时更新。 |
| **主控长间隔轮询** | 主控的价值在异常处理，不在被动等待。5~10 分钟检查一次足够。 |
| **Worker 恢复优先于重试** | 向同一 session 发送继续消息比重新启动新 Worker 更高效（保留已完成的工作）。 |
| **exec sleep 可用于等待** | `exec` 的 timeout 参数可设置到 600s，用 `sleep` 命令实现长间隔等待。 |
