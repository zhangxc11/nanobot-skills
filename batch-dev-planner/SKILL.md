---
name: batch-dev-planner
description: 批量需求统筹开发。将多条已对齐的待办需求进行依赖分析、分组编排、Phase 拆解，通过 spawn subagent 并行开发，最后统一验收。适用于积累了多条已对齐需求需要集中开发的场景。
---

# batch-dev-planner — 批量需求统筹开发 v4

> 需求盘点 → 规划 → feature branch 开发 → dev 环境验收 → merge --no-ff 合并 → 收尾

## 加载指引

加载本 skill 后，根据当前所处 Stage，将对应的准则写入 session summary 的 `## 当前工作准则` 段。Stage 切换时同步更新。

**Stage 0-1 盘点规划阶段**：
```
- 需求必须有 Glossary + 验收 Checklist 才算可排期
- Plan 文档包含聚合的验收 Checklist
- 用户确认排期后才进入开发
```

**Stage 2 开发阶段**：
```
- 每个 Plan 一个 SA，不同 topic 不混在同一个 SA
- SA 内部遵循 dev-workflow，按 checkbox 逐项推进
- 主 session 只做调度跟踪，不直接改代码
```

**Stage 3 验收阶段**：
```
- SA 先对照 Checklist 自验收，输出验收报告
- 自修最多 2 轮，根本性偏差回设计阶段
- 汇总报告交用户做人工验收确认
```

## 参考文档

| 文档 | 内容 |
|------|------|
| [`docs/stage-0-inventory.md`](docs/stage-0-inventory.md) | **Stage 0 需求盘点**：todo 分层、分组展示、可排期清单 |
| [`docs/stage-details.md`](docs/stage-details.md) | **Stage 1~5 详述** + 紧急 Hotfix 通道 |
| [`docs/state-and-decisions.md`](docs/state-and-decisions.md) | **状态管理** + 资源锁 + 决策记录 + 复盘流程 |
| [`docs/prompt-templates.md`](docs/prompt-templates.md) | 设计审查 / 开发 / 接力 subagent 的完整 prompt 模板 |
| [`docs/frontend-acceptance.md`](docs/frontend-acceptance.md) | 前端人眼验收策略（验收环境、流程、编排建议） |
| [`docs/lessons-learned.md`](docs/lessons-learned.md) | 经验教训：任务拆分、prompt 质量、异常恢复、验收策略 |
| [`docs/nanobot/`](docs/nanobot/) | nanobot + web-chat 专属：dev-workdir 管理、dev 环境启停 |
| [`docs/retrospectives/`](docs/retrospectives/) | 每次批量开发的复盘记录 |

---

## 1. 适用场景 & 设计原则

**适用**：5+ 条已对齐需求集中开发 / 多仓库依赖关系 / 主 session 只做统筹调度

| 相关 Skill | 区别 |
|------------|------|
| **dev-workflow** | 单需求流程；本 skill 的每个 SA 内部遵循 dev-workflow |
| **batch-orchestrator** | 同质任务并行；本 skill 处理异构需求，需依赖分析和分组 |

**原则**：① 开发与主分支解耦（feature branch） ② 严格串行批次 + 动态追加 ③ 紧急 hotfix 独立通道 ④ 跨 session 状态持久化（state.json 驱动）

---

## 2. 核心流程

```
Stage 0 → Stage 1 → Stage 2 → Stage 3 → Stage 4 → Stage 5
盘点      规划      开发      验收      发布      收尾
```

**Stage 0: 需求盘点** — `todo list` 获取全量待办，按对齐质量分 4 层（L1 可排期：已对齐+方案已确认+Glossary+验收 Checklist / L2 需补方案或 Checklist / L3 待对齐 / L4 无标记），按 group 分组展示并标注对齐质量，用户确认可排期清单。→ [详述](docs/stage-0-inventory.md)

**Stage 1: 规划** — 筛选已对齐需求 → 依赖分析（仓库/代码/文件重叠） → 归集为 Plan（小合并、大独占、拓扑排序） → 每个 Plan 聚合验收 Checklist 并包含关键术语定义 → 输出 PLAN.md + state.json + plan JSON（含 `todo_ids`） → **用户确认**。→ [详述](docs/stage-details.md#1-stage-1-规划)

**Stage 2: 开发** — 获取资源锁 → 初始化 dev-workdir → 逐 Plan 拉 feature branch → spawn SA 开发（步骤粒度，接力兜底） → 更新状态 `dev_done`。所有 Plan 必须走 feature branch。→ [详述](docs/stage-details.md#2-stage-2-开发)

**Stage 3: 验收** — 分两层：① **Agent 自主验收**：逐 Plan 对照验收 Checklist 自检（L1 代码完整性 + L2 功能验证），不通过自修最多 2 轮，根本性偏差回设计阶段；② **人工验收确认**：自验收通过后输出结构化验收报告交用户确认 → 通过则 commit 压缩 + `merge --no-ff` 到 dev 主分支 → 检查关联 todo 并标记 done → 关闭 dev 环境。前端改动必须人眼确认。→ [详述](docs/stage-details.md#3-stage-3-验收)

**Stage 4: 发布** — dev-workdir 全量回归 → prod 仓库 `git pull --no-ff` → 重启 prod。→ [详述](docs/stage-details.md#4-stage-4-发布)

**Stage 5: 收尾** — 更新 todo/MEMORY/HISTORY → 清理工作目录 → Batch 标记 `completed` → 释放锁 → **执行复盘**。→ [详述](docs/stage-details.md#5-stage-5-收尾)

---

## 3. 附属文档摘要

**Stage 0 需求盘点** (`stage-0-inventory.md`)：todo 按对齐质量分为 L1~L4 四层，L1（已对齐+方案已确认+Glossary+验收 Checklist）直接排期，缺 Glossary/Checklist 降为 L2。按 group 分组展示并标注对齐质量，输出用户确认的可排期清单作为 Stage 1 输入。

**Stage 1~5 详述** (`stage-details.md`)：各阶段完整操作细节，包括 Plan JSON 结构（含 `todo_ids`、`checklist`、`glossary`）、Plan 级验收 Checklist 聚合、SA 策略（步骤粒度 + 接力兜底）、两层验收（Agent 自验收 + 人工确认）、验收上下文保持（review-state）、止损机制（自修 ≤ 2 轮 / 根本性偏差回设计）、commit 压缩规则、前端人眼验收、验收与 todo 关联检查（弱化方案）、紧急 Hotfix 通道及分支同步。

**状态管理 + 决策** (`state-and-decisions.md`)：文件布局（active_batch.json/lock + batches/）、Batch 和 Plan 状态流转图、动态追加规则、资源锁双超时机制（软 10min / 硬 1h + 心跳）、14 条历史决策记录、复盘改进流程。

---

## 4. 快速参考

| 项目 | 规则 |
|------|------|
| 分支命名 | `feat/batch-YYYYMMDD-plan-{name}` |
| 合并策略 | `merge --no-ff`（保留分支历史） |
| commit 压缩 | 开发阶段每需求一个 commit；修复阶段每轮一个 |
| SA 策略 | 步骤粒度，每 Step 独立 SA；< 90K follow_up，≥ 90K 新建+摘要 |
| 资源锁 | `active_batch.lock`，软超时 10min / 硬超时 1h |
| 动态追加 | `pending` 可追加，`developing` 后锁定 |
| 前端验收 | build → dev webserver → 用户访问确认 → 才标记通过 |
| Hotfix | 不走 batch，主分支直修；完成后 feature branch `merge origin/main` 同步 |
