---
name: batch-dev-planner
description: 批量需求统筹开发。将多条已对齐的待办需求进行依赖分析、分组编排、Phase 拆解，通过 spawn subagent 并行开发，最后统一验收。适用于积累了多条已对齐需求需要集中开发的场景。
---

# batch-dev-planner — 批量需求统筹开发 v3

> 需求盘点 → 规划 → feature branch 开发 → dev 环境验收 → merge --no-ff 合并 → 收尾

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

**Stage 0: 需求盘点** — `todo list` 获取全量待办，按对齐状态分 4 层（L1 可排期 / L2 需补方案 / L3 待对齐 / L4 无标记），按 group 分组展示，用户确认可排期清单。→ [详述](docs/stage-0-inventory.md)

**Stage 1: 规划** — 筛选已对齐需求 → 依赖分析（仓库/代码/文件重叠） → 归集为 Plan（小合并、大独占、拓扑排序） → 输出 PLAN.md + state.json + plan JSON（含 `todo_ids`） → **用户确认**。→ [详述](docs/stage-details.md#1-stage-1-规划)

**Stage 2: 开发** — 获取资源锁 → 初始化 dev-workdir → 逐 Plan 拉 feature branch → spawn SA 开发（步骤粒度，接力兜底） → 更新状态 `dev_done`。所有 Plan 必须走 feature branch。→ [详述](docs/stage-details.md#2-stage-2-开发)

**Stage 3: 验收** — 启动 dev 环境 → 逐 Plan 验收（feature branch 上审查+测试+人工） → 通过则 commit 压缩 + `merge --no-ff` 到 dev 主分支 → 检查关联 todo 并标记 done → 关闭 dev 环境。前端改动必须人眼确认。→ [详述](docs/stage-details.md#3-stage-3-验收)

**Stage 4: 发布** — dev-workdir 全量回归 → prod 仓库 `git pull --no-ff` → 重启 prod。→ [详述](docs/stage-details.md#4-stage-4-发布)

**Stage 5: 收尾** — 更新 todo/MEMORY/HISTORY → 清理工作目录 → Batch 标记 `completed` → 释放锁 → **执行复盘**。→ [详述](docs/stage-details.md#5-stage-5-收尾)

---

## 3. 附属文档摘要

**Stage 0 需求盘点** (`stage-0-inventory.md`)：todo 按 tag 分为 L1~L4 四层，L1（已对齐+方案确认）直接排期，L2~L4 需补充对齐后升级。按 group 分组展示，输出用户确认的可排期清单作为 Stage 1 输入。

**Stage 1~5 详述** (`stage-details.md`)：各阶段完整操作细节，包括 Plan JSON 结构（含 `todo_ids`）、SA 策略（步骤粒度 + 接力兜底）、验收上下文保持（review-state）、commit 压缩规则、前端人眼验收、验收与 todo 关联检查（弱化方案）、紧急 Hotfix 通道及分支同步。

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
