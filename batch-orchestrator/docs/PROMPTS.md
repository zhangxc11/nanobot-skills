# batch-orchestrator — Prompt 模板

> 本文件包含所有 Prompt 模板和用户操作流程。由 SKILL.md 索引引用。

---

## 1. DISPATCHER_PROMPT.md 模板

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

---

## 2. WORKER_PROMPT_TEMPLATE.md 模板

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

---

## 3. DESIGN.md 模板

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

---

## 4. TASK_PLAN.md 模板

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

## 5. 用户 session 操作流程（含 User Watchdog）

用户 session 启动批量任务的完整流程：

```
1. 准备阶段（与用户讨论，产出设计文档）
   - 创建 DESIGN.md、TASK_PLAN.md、DISPATCHER_PROMPT.md、WORKER_PROMPT_TEMPLATE.md
   - 初始化 state.json

2. 启动调度 session
   - 通过 web-subsession 或新 session 启动调度
   - 记录调度 session_key 到 state.json

3. 启动 User Watchdog（推荐）
   spawn({
     task: "你是 user-level watchdog。执行 exec sleep 600，完成后返回 USER_WATCHDOG: check dispatcher status",
     label: "user-watchdog",
     max_iterations: 5
   })
   - 记录 watchdog task_id 到 state.json.user_watchdog.task_id_spawn

4. 收到 User Watchdog 回报时
   - 读取 state.json 检查任务状态
   - 全部完成 → 汇报，不再 follow_up
   - 调度仍在运行 → follow_up(watchdog_task_id, "继续监控")
   - 调度已耗尽 → 启动新调度 session 接力 + follow_up watchdog
   - 异常 → 诊断修复 + follow_up watchdog
```

---

## 6. Watchdog Prompt

```
你是 watchdog。你的唯一任务：执行 sleep 等待，然后返回检查信号。

执行：exec sleep {watchdog_interval_seconds}
完成后返回：WATCHDOG: timeout check
```

---

## 7. 父子关系注册（web-subsession 启动调度时）

通过 web-subsession 启动调度 session 时，使用 `--parent` 参数注册父子关系：

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --session-key "webchat:dispatch_${MASTER_TS}_${DISPATCH_TS}" \
  --message "$(cat DISPATCHER_PROMPT.md)" \
  --title "📋 调度 gen1" \
  --parent "${MASTER_SESSION_ID}"
```

`--parent` 值是父 session 的 ID（下划线格式如 `webchat_1773170043`），会通过 `POST /api/sessions/parents` 注册到 session_parents.json。
