# Digital Assistant — 需求文档

## 概述

数字助理（Digital Assistant）是 nanobot 的任务管理与质量控制中枢，负责管理任务全生命周期（创建→执行→Review→完成）、工作组模板匹配、结构化 Cross-Check Review、自动调度与派发。

**核心目标**：将用户需求转化为可执行任务，通过调度器自动派发给 Worker 执行，并通过结构化 Review 机制保证质量。

## 核心需求

### 1. 任务生命周期管理

**需求描述**：管理任务从创建到完成的全流程，支持状态转换、优先级调整、依赖管理。

**关键场景**：
- 用户提出新需求 → Agent 对齐目标 → 注册任务到 REGISTRY
- 任务按优先级排队 → 调度器自动派发 → Worker 执行 → 提交 Review → 用户审批 → 完成
- 任务被阻塞 → 标记 blocked → 依赖解除后恢复
- 任务需要修订 → 状态回退到 revision → 重新执行

**验收标准**：
- ✅ 支持任务状态机：queued → executing → review → done / blocked / revision / cancelled
- ✅ 支持优先级管理：P0（紧急）、P1（重要）、P2（常规）
- ✅ 支持依赖管理：blocked_by 字段记录前置任务
- ✅ 状态转换符合有限状态机约束，不允许非法转换

### 2. 工作组模板匹配

**需求描述**：根据任务特征自动匹配最佳工作组模板，定义执行规则、验收标准、Review 级别。

**关键场景**：
- 用户说"帮我优化缓存命中率" → 匹配 standard-dev 模板 → 分配 developer 角色
- 用户说"每天早上 9 点提醒我开会" → 匹配 cron-auto 模板 → 跳过 Review
- 用户说"批量处理 10 个需求" → 匹配 batch-dev 模板 → 启用并行编排

**验收标准**：
- ✅ 支持 5 种模板：quick、standard-dev、long-task、cron-auto、batch-dev
- ✅ 自动匹配算法准确率 > 90%（基于标题/描述关键词）
- ✅ 支持手动指定模板覆盖自动匹配

### 3. 结构化 Cross-Check Review

**需求描述**：根据任务复杂度和风险级别，自动判定 Review 级别（L0-L3），生成结构化 Checklist，支持多角色审查。

**关键场景**：
- Quick 任务 → L0 级别 → 无需 Review，自动通过
- 简单改动（≤2 文件，无接口变更）→ L1 级别 → 自检即可
- 标准开发任务 → L2 级别 → 需要 code_reviewer 审查
- 架构变更/外部发布/金融逻辑 → L3 级别 → 需要 code_reviewer + test_verifier 交叉审查

**验收标准**：
- ✅ 自动判定 Review 级别，准确率 > 95%
- ✅ 生成结构化 Checklist（YAML 格式），包含审查维度、检查项、通过标准
- ✅ 支持多角色审查结果提交与自动仲裁
- 👤 人工验证：L3 级别任务的 Checklist 覆盖所有关键风险点

### 4. 自动调度与派发

**需求描述**：调度器定期扫描任务队列，按优先级、依赖关系、并发限制自动派发任务给 Worker 执行。

**关键场景**：
- 用户注册 3 个 P1 任务 → 调度器按优先级排序 → 依次派发（并发上限 3 个）
- 任务 A 依赖任务 B → 调度器检测到 blocked_by → 等待 B 完成后自动派发 A
- Worker 完成任务 → 调度器收到通知 → 更新状态 → 派发下一个任务
- Dispatcher session 超过 500 轮 → 自动换代，创建新 session 继任

**验收标准**：
- ✅ 调度器每 30 分钟自动触发（Cron 兜底）
- ✅ 单次派发上限 3 个任务（API 限流约束）
- ✅ 全局最多 3 个 executing 任务（并发控制）
- ✅ 支持固定 session 复用，500 轮自动换代
- ✅ 支持 dry-run 模式，只看不做

### 5. Worker 结构化报告机制

**需求描述**：Worker 执行完成后返回结构化报告（JSON 格式），调度器解析报告并决策下一步动作。

**关键场景**：
- Developer 完成开发 → 返回报告（verdict: pass）→ 调度器提交 Review
- Tester 发现问题 → 返回报告（verdict: fail + issues）→ 调度器派发 Developer 修复
- Worker 遇到阻塞 → 返回报告（verdict: blocked）→ 调度器标记任务 blocked，通知用户

**验收标准**：
- ✅ Worker 报告包含必填字段：task_id、role、verdict、summary、issues、files_changed
- ✅ 调度器支持规则引擎（确定性判断）+ LLM 兜底（模糊场景）
- ✅ 报告写入文件（data/brain/reports/）+ 输出标记（双通道）
- ✅ 支持多角色编排（8 角色互检），最多 18 轮（standard-dev）

### 5a. Flow Type 调度框架

**需求描述**：调度器通过 `flow_type` 字段驱动角色序列，不做语义猜测。

**Flow Types**：
- `cron-auto` — 单步执行（developer only），无 auditor/retrospective
- `standard-dev` — 完整流程（architect → architect_review → developer → code_review → tester → test_review → auditor → retrospective）

**`resolve_flow_type` 优先级**：
1. `task.flow_type` 字段（显式指定）
2. `task.process_level` 向后兼容映射（PL0→cron-auto, PL1/PL2/PL3→standard-dev）
3. 默认 `standard-dev`

**验收标准**：
- ✅ `resolve_flow_type` 无语义猜测，只读字段
- ✅ 向后兼容 `process_level` 字段（PL0-PL3）
- ✅ `FLOW_TEMPLATES` 定义每种 flow 的角色序列和 auditor/retrospective 标志

### 6. 飞书集成与通知

**需求描述**：支持飞书消息通知、回复解析、任务关联，用户可通过飞书回复直接操作任务（approve/reject/defer）。

**关键场景**：
- 任务进入 review 状态 → 自动发送飞书通知（含任务短码 T-001）
- 用户回复"T-001 approve" → 解析回复 → 执行 review resolve --decision approved
- 用户回复"T-001 reject 需要补充单元测试" → 解析回复 → 执行 review resolve --decision rejected --note "..."

**验收标准**：
- ✅ 支持飞书通知格式化（emoji + 短码 + 标题 + 操作提示）
- ✅ 支持回复解析（approve/reject/defer/pause/cancel/resume/comment）
- ✅ 支持批量通知生成（review-all）
- 👤 人工验证：飞书通知格式清晰易读，操作提示准确

## 非功能需求

### 性能要求
- 调度器单次运行时间 < 10 秒（不含 Worker 执行时间）
- 任务状态更新延迟 < 1 秒
- BRIEFING/REGISTRY 更新延迟 < 2 秒

### 可靠性要求
- 调度器失败自动重试（最多 3 次）
- Worker 报告解析失败时 fallback 到 LLM 分析
- Dispatcher session 失效时自动创建新 session

### 可维护性要求
- 所有决策记录到 decisions.jsonl（可追溯）
- 所有 Worker 报告持久化到文件（可审计）
- 支持 dry-run 模式（测试调度逻辑不实际派发）

## 术语表（Glossary）

| 术语 | 定义 |
|------|------|
| **Task** | 任务，用户需求的结构化表示，包含标题、描述、优先级、状态等字段 |
| **REGISTRY** | 任务注册表，所有任务的索引文件（data/brain/REGISTRY.md） |
| **BRIEFING** | 任务简报，当前工作状态的摘要视图（data/brain/BRIEFING.md） |
| **Worker** | 工作者，执行具体任务的 subagent（如 developer、tester、code_reviewer） |
| **Dispatcher** | 调度器，负责任务派发和 Worker 编排的固定 session |
| **Review** | 审查项，任务完成后需要人工或自动审查的检查点 |
| **Cross-Check** | 交叉审查，多角色结构化审查机制（L2/L3 级别） |
| **Template** | 工作组模板，定义任务类型、执行规则、验收标准的配置 |
| **Verdict** | 判决，Worker 报告中的执行结果（pass/fail/blocked/partial） |
| **Orchestration** | 编排，任务内部的角色轮转机制（Developer → Tester → Developer ...） |
| **Generation** | 代次，Dispatcher session 的换代计数（防止上下文膨胀） |

## 相关文档

- [ARCHITECTURE.md](./ARCHITECTURE.md) — 架构设计与实现方案
- [DEVLOG.md](./DEVLOG.md) — 开发日志与任务清单
- [../SKILL.md](../SKILL.md) — Skill 使用手册
- [D-20260331-001](../../data/brain/designs/D-20260331-001.md) — 调度器多角色编排设计
- [D-20260331-002](../../data/brain/designs/D-20260331-002-summary.md) — Worker 规则按需加载设计
- [D-20260331-003](../../data/brain/designs/D-20260331-003-summary.md) — 未提交代码抢救方案
- [D-20260331-004](../../data/brain/designs/D-20260331-004-review-r2.md) — 调度器日报/周报设计
