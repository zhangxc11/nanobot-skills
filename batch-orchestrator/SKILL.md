---
name: batch-orchestrator
description: 通用批工作调度框架。将可并行的任务拆解为 Worker subagent，通过"调度 session + spawn Worker"两层编排模式执行。支持滑动窗口并发、watchdog 兜底、follow_up 异常恢复。
---

# batch-orchestrator — 通用批工作调度框架

> 将可并行的任务拆解为 Worker subagent，通过两层编排模式高效执行。

---

## 1. 概述

**适用场景**：批量构造/修复/迁移等可拆解为多个独立子任务的场景，子任务间无强依赖。

### 架构：两层编排

```
用户 session ─── 与用户讨论，产出设计文档和 Prompt
     │
     ▼ (web-subsession 或手动新 session)
调度 session ─── spawn Worker、接收回报、状态更新、异常恢复
     │
     ▼ (spawn subagent，每个 Worker 一个)
Worker subagent ── 执行单个子任务，完成后自动回报
```

**依赖**：`web-subsession` skill（启动调度 session）、spawn subagent（启动 Worker）

---

## 2. 角色分工

| 角色 | 职责 | 不做什么 |
|------|------|---------|
| **调度 session** | 读取任务队列、spawn Worker、接收回报、更新 state.json、异常恢复、启动 watchdog | 不执行具体任务 |
| **Worker subagent** | 执行单个子任务、写 result 文件、回报结果 | 不关心其他 Worker |

异常处理分层：Worker 异常 → 调度 session follow_up；调度 session 耗尽 → 用户 session 接力。

---

## 3. 并发模型：滑动窗口

> **不是**"等一批全部完成再启动下一批"，而是**滑动窗口**。

- 初始启动 N 个 Worker（N = 并发度，通常 3~5）
- 每完成一个 Worker，立即从 queue 中取下一个启动
- 调度 session spawn 后进入空闲等待——**不消耗迭代次数**
- Worker 完成后自动注入消息触发调度 session 下一轮

---

## 4. 执行流程

### Phase 0 — 准备（用户 session）

与用户讨论 → 产出 DESIGN.md、TASK_PLAN.md、WORKER_PROMPT_TEMPLATE.md → 初始化 state.json → 生成 DISPATCHER_PROMPT.md → 启动调度 session。

> 📄 Prompt 模板见 [docs/PROMPTS.md](docs/PROMPTS.md)
> 📄 文件结构与 Schema 见 [docs/STATE_SCHEMA.md](docs/STATE_SCHEMA.md)

### Phase 1 — 调度执行

1. **初始化**：读取 state.json + TASK_PLAN.md
2. **启动初始窗口**：从 queue 取 N 个任务，逐个 spawn Worker
3. **启动 watchdog**：spawn watchdog subagent（`exec sleep N` → 回报检查信号）
4. **等待回报**：空闲等待 Worker/watchdog 消息
5. **处理 Worker 回报**：更新 state.json，queue 非空则 spawn 下一个（滑动窗口）
6. **处理 watchdog 回报**：检查超时 Worker，follow_up 或标记 failed
7. **完成**：queue 空且 in_progress 空 → 标记 dispatcher.status = "completed"

### Phase 2 — Worker 执行

读取任务 Prompt → 执行任务 → 写 result JSON → 最后一轮 text reply 自动回报到调度 session。

---

## 5. Watchdog 机制

> Watchdog 是兜底机制。基于 spawn follow_up 复用，一个 watchdog 持续服务整个批次。

**为什么需要**：spawn 三种正常退出路径都有回报，但极端情况（进程崩溃/OOM）会无回报。Watchdog 定期唤醒调度 session 检查"失联"Worker。

> 📄 spawn 回报可靠性详细表格见 [docs/STATE_SCHEMA.md](docs/STATE_SCHEMA.md)

### 工作流程

```
调度 session
  ├── spawn Workers (A, B, C...)
  └── spawn Watchdog ──→ exec("sleep 300") ──→ 回报 "WATCHDOG: timeout check"
                                                      │
                            调度 session 被唤醒 ◄──────┘
                            ├── 检查 in_progress 超时任务
                            ├── 超时 → follow_up 或标记 failed
                            ├── 还有未完成？→ follow_up(watchdog, "继续监控") → 循环
                            └── 全部完成？→ 不再 follow_up，watchdog 自然结束
```

**关键设计**：一个 watchdog 通过 follow_up 复用服务整个批次；所有 Worker 完成后不再 follow_up 让其自然结束。

---

## 6. 异常恢复

spawn follow_up 是异常恢复的核心——向已结束的 subagent 发送新消息继续执行。

| Worker 回报情况 | 处理方式 |
|----------------|---------|
| completed + result 正常 | 更新 state → completed，spawn 下一个 |
| completed + result 异常 | 更新 state → failed 或按业务逻辑处理 |
| max_iterations | `follow_up(task_id, "请继续完成任务")` |
| failed, 可重试 | `follow_up(task_id, "请重试")` |
| failed, 多次 follow_up 仍失败 | 标记 failed，记录原因 |
| 无回报（watchdog 检测超时） | 检查 result 文件 → follow_up 或标记 failed |

**要点**：follow_up 保留完整历史上下文；次数限制建议最多 2 次。

---

## 7. 调度接力与 User Watchdog

调度 session 有迭代上限，Worker 多时可能耗尽。User Watchdog 自动检测并接力。

### User Watchdog 设计

用户 session spawn 一个 watchdog，sleep 到期后触发用户 session 检查状态。未完成则 follow_up 继续监控。**sleep 期间不消耗用户 session 迭代**（10 分钟间隔，1 小时仅 6 轮）。

```
用户 session
  ├── 调度 session（含 Workers + 调度级 watchdog）
  └── User Watchdog ← sleep N 分钟 → 回报 → 用户 session 检查
        └── (follow_up 循环：未完成 → 再 sleep → 再回报 → 再检查)
```

**收到 watchdog 回报后**：读取 state.json → 全部完成则汇报结束；调度仍运行则 follow_up 继续监控；调度已耗尽则启动新调度 session 接力。

### 接力流程

1. 检测到调度 session 不活跃（state.json 长时间未更新 + queue/in_progress 非空）
2. 检查 in_progress 中的 result 文件，修复不一致状态
3. dispatcher.generation += 1，启动新调度 session → 从 state.json 断点继续

### 预算感知退出

调度 session 收到 "⚠️ Budget alert" 后：不再 spawn 新 Worker → 等待当前 Worker 回报 → 标记 dispatcher.status = "exhausted" → 退出。

> 📄 完整操作流程见 [docs/PROMPTS.md](docs/PROMPTS.md) §5

---

## 8. ⚠️ 重要约束

### Worker subagent 工具限制

Worker 只有 7 个工具：`read_file` / `write_file` / `edit_file` / `list_dir` / `exec` / `web_search` / `web_fetch`

- ❌ Worker 不能 spawn 子 subagent，不能创建 web-subsession
- ✅ 需要子任务时由**调度 session** 拆分为多个独立 Worker

### 调度串行，Worker 并行

- ✅ 一个调度 session 串行管理，通过滑动窗口 spawn 多个 Worker 并行执行
- ❌ 不要 spawn 多个调度 subagent 并行（会导致 state.json 写冲突）

---

## 9. 经验法则

| 经验 | 说明 |
|------|------|
| **调度只调度不干活** | 所有任务一律由 Worker 执行，调度 session 不执行具体任务 |
| **滑动窗口优于分批** | 完成一个立即启动下一个，吞吐量最大化 |
| **并发 3~5 个 Worker** | 过多增加复杂度，过少利用率不足 |
| **Watchdog 间隔 5 分钟** | 太短浪费迭代，太长延迟发现问题 |
| **follow_up 优先于重建** | 保留完整历史上下文，比重新 spawn 更高效 |
| **state.json 是恢复关键** | 及时更新，新调度从断点继续 |
| **task_id_spawn 必须记录** | follow_up 的唯一凭证，丢失则无法恢复 |
| **Worker 回报要包含 task_id** | 调度 session 需要知道是哪个 Worker |
| **被动等待不消耗迭代** | spawn 后空闲等待，只有收到回报才消耗迭代 |
| **User Watchdog 避免超轮数** | sleep 期间不消耗迭代，一个 watchdog 服务整个批次生命周期 |
