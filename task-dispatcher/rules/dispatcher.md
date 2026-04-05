# 调度器规则

<!-- detection_keywords: dispatcher, scheduler, 调度, 派发, worker, spawn -->

## 🔴 L0 MUST（违反即失败）

### DISP-001: 调度器是唯一流程控制者
Worker 只是执行者+报告者，不操控流程状态。
所有状态转换、角色切换、流程跳转都由调度器统一控制。
这也是 Worker 工作的 cross-check 机制。

### DISP-002: Worker 验证不通过应打回而非进 review
Worker 发现验证未完全通过时，不应提交 review，应继续修复再验证。
调度器需支持这种闭环（打回→重新执行→再验证）。

### DISP-003: Worker 必须用 spawn subagent 派发
调度器必须用 spawn subagent（非 create_subsession）派发 worker，利用框架完成通知实现回收闭环。
完整链路：Cron/飞书→激活固定调度session(web)→spawn subagent→worker完成→自动回调调度session→更新REGISTRY+派新任务+飞书通知。

### DISP-004: 流程验证优先于任务推进
调度器流程未端到端跑通前，所有基于流程推进的任务结果都存疑。
pytest 通过不等于流程在真实环境能正确运转。

## 🟡 L1 REQUIRED（项目要求）

### DISP-005: Dispatcher 应有更大自主性
基于规则的死板轮次限制不够好，应让 dispatcher 有更大自主性判断。
例如最新 verdict=pass 就应正常流转，不因连续次数限制 blocked。

### DISP-006: Dispatcher 期望的完成标准
用户期望 dispatcher 自主推进任务到"只差重启 prod 环境"这一步，用户只需确认是否重启。

### DISP-007: 调度器架构 — 固定 session + cron 激活
废弃 scheduler.lock 文件锁机制，改用"固定 web subsession + cron 发消息激活"模式：
- dispatcher.json 记录当前调度 session_id
- cron 触发 agent 读 dispatcher.json 后 curl 发消息
- 调度 session 收到消息时若有 subagent 在跑则忽略，空闲时检查轮次超 500 则换代

### DISP-008: Worker 规则按需加载
Worker 执行不同类型任务时应按需加载对应规则（开发→dev-workflow，文档→feishu-docs等），不是一股脑全塞。
不同仓库有不同开发策略（nanobot 有特色要求，其他仓库可参考但不必 follow），规则粒度应细化到仓库级别。

### DISP-009: 调度器 session 轮次效率
需关注换代阈值（当前500轮）是否合理，每轮次应做足够多的事。

### DISP-010: 后台持续推进 + 合理方案直接执行
能自动推的就推，需要确认的先跳过。
设计方案合理就直接推进开发，不必等用户确认。

### DISP-011: 智囊团与调度器分工
具体执行任务（开发/修订/审计等）交调度器跑。
智囊团（方案设计/cross-check）由主 session 自己起 subagent 编排。

### DISP-012: INBOX 巡检
每轮调度开始时，先检查 INBOX 待处理消息（`python3 scripts/inbox_helper.py pending`）。
LLM 全权决策如何处理每条消息（不硬编码路由）。处理完标记 processed。
INBOX 异常不阻塞调度主流程（降级安全）。

### DISP-013: follow_up_worker 处理规则

当调度器（scheduler.py）的 `execute_decision` 返回 `action: "follow_up_worker"` 时，Dispatcher 必须按以下流程处理：

1. **使用 `follow_up` 工具（而非 `spawn`）** 向已有 subagent 发送消息
2. **使用返回结果中的 `session_id`** 定位目标 subagent（该 session_id 来自 `task.orchestration.active_workers[role].session_id`）
3. **将 `follow_up_message`** 作为消息内容发送给 worker
4. **Fallback 降级**: 如果 follow_up 失败（session 已过期、session_id 不存在或无效），降级为 `dispatch_role`（new spawn），确保调度不卡住

与 `dispatch_role`（new spawn）的区别：
- `dispatch_role`: 创建全新 subagent（spawn），worker 无之前执行的上下文
- `follow_up_worker`: 复用已有 subagent（follow_up），worker 保留之前执行的上下文和状态，效率更高

### DISP-014: handle-completion 时更新 Worker 状态

Dispatcher 在处理 worker 完成回调（handle-completion）时，应更新 worker 的 iteration 消耗：

1. 从 subagent 状态获取实际消耗的 iteration 数
2. 更新 `task.orchestration.active_workers[role].iterations_used`
3. 调用 `scheduler.register_worker_session` 或直接更新 task YAML

这确保 `_can_follow_up` 的耗尽判断（`iterations_used < max_iterations`）基于真实数据，避免无限 follow_up。
