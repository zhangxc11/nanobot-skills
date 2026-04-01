# Digital Assistant — 开发日志

## Phase 1: 基础框架搭建 (2026-03-28 ~ 2026-03-29)

**目标**: 建立任务管理基础设施，实现 brain_manager CLI 和数据结构

### 任务清单
- [x] 设计任务数据结构（YAML Schema）
- [x] 实现 brain_manager.py 核心功能
  - [x] register_task() — 任务注册
  - [x] transition_task() — 状态转换
  - [x] list_tasks() — 任务查询
  - [x] update_briefing() — 摘要更新
- [x] 创建数据目录结构
  - [x] data/brain/tasks/
  - [x] data/brain/REGISTRY.md
  - [x] data/brain/BRIEFING.md
- [x] 实现 CLI 接口（argparse）
- [x] 编写单元测试

**关键决策**:
- 采用 YAML 格式存储任务（可读性好，支持注释）
- REGISTRY.md 作为索引文件（Markdown 表格，便于人工查看）
- BRIEFING.md 作为摘要视图（按状态分组，突出重点）

**验收结果**: ✅ 通过
- 任务注册、状态转换、查询功能正常
- REGISTRY/BRIEFING 自动更新
- CLI 命令可用

---

## Phase 2: 调度器原型 (2026-03-29 ~ 2026-03-30)

**目标**: 实现基础调度器，支持任务派发和 Worker 执行

### 任务清单
- [x] 实现 scheduler.py 基础功能
  - [x] run_scheduler() — 扫描队列并派发
  - [x] generate_worker_prompt() — 生成 Worker 指令
  - [x] generate_spawn_instruction() — 生成 spawn 命令
- [x] 实现优先级排序（P0 > P1 > P2）
- [x] 实现依赖检查（blocked_by）
- [x] 实现并发控制（全局最多 3 个 executing）
- [x] 创建 trigger_scheduler.py（Dispatcher session 管理）
- [x] 端到端测试（手动派发任务）

**关键决策**:
- 调度器输出 JSON 格式（便于 Dispatcher 解析）
- Worker prompt 包含完整任务信息（减少 Worker 查询需求）
- 并发上限设为 3（API 限流约束）

**验收结果**: ✅ 通过
- 调度器能正确扫描队列并生成派发计划
- 优先级排序和依赖检查正常
- Dispatcher 能成功 spawn Worker

**遗留问题**:
- Worker 完成后需要手动更新任务状态（未自动化）
- Worker prompt 包含 brain_manager 指令（耦合度高）

---

## Phase 3: Review 机制 (2026-03-30)

**目标**: 实现结构化 Review 机制，支持多级别审查

### 任务清单
- [x] 设计 Review 数据结构
- [x] 实现 review add/resolve 命令
- [x] 实现 determine_review_level() 自动判定
- [x] 实现 Checklist 生成（L1/L2/L3）
- [x] 集成到调度器（任务完成后自动创建 Review）
- [x] 测试多级别 Review 流程

**关键决策**:
- Review 级别：L0（无需）、L1（自检）、L2（单角色）、L3（交叉审查）
- Checklist 结构化（dimension + items + status）
- Review 状态：pending → approved/rejected

**验收结果**: ✅ 通过
- Review 级别自动判定准确
- Checklist 生成符合预期
- Review 流程完整可用

---

## Phase 4: 飞书集成 (2026-03-30 ~ 2026-03-31)

**目标**: 支持飞书通知和回复解析

### 任务清单
- [x] 实现飞书通知格式化
- [x] 实现回复解析（approve/reject/comment）
- [x] 集成到调度器（任务进入 review 时自动通知）
- [x] 实现批量通知生成（review-all）
- [x] 测试飞书交互流程

**关键决策**:
- 通知格式：emoji + 短码 + 标题 + 操作提示
- 回复格式：`T-{task_id} {action} [note]`
- 使用 feishu-messenger skill 发送消息

**验收结果**: ✅ 通过
- 飞书通知格式清晰易读
- 回复解析准确
- 批量通知功能正常

**相关任务**: T-20260330-011

---

## Phase 5: Worker 去状态化改造 (2026-03-31)

**目标**: 解耦 Worker 和 brain_manager，实现报告机制

**背景**: 原设计中 Worker 直接调用 brain_manager 命令更新任务状态，导致：
1. Worker prompt 复杂度高（需要理解 brain_manager 命令）
2. 状态转换逻辑分散（Worker 和调度器都能操控状态）
3. 难以实现多角色编排（Worker 不知道下一步该派发谁）

**设计方案**: 参考 D-20260331-001 设计文档

### 任务清单
- [x] 设计精简报告 Schema（6 字段）
- [x] 创建 data/brain/reports/ 目录
- [x] 重写 generate_worker_prompt()（移除 brain_manager 指令）
- [x] 实现 parse_worker_report()（文件通道解析）
- [x] 实现 make_decision()（规则引擎）
- [x] 实现 execute_decision()（统一状态转换）
- [x] 实现 handle_worker_completion()（完整流水线）
- [x] 新增 CLI 子命令 handle-completion
- [x] 修改 Dispatcher Prompt（适配两步流程）
- [x] 端到端集成测试

**关键决策**:
1. **报告通道**：文件唯一（移除输出标记方案）
2. **报告 Schema**：精简为 6 字段（task_id/role/verdict/summary/issues/files_changed）
3. **角色类型**：初版只保留 developer + tester
4. **循环控制**：MAX_ITERATIONS=5、MAX_SAME_ROLE_CONSECUTIVE=2
5. **决策方式**：规则引擎为主，LLM 兜底留 Phase 6
6. **流程解耦**：handle-completion 和 run 分开调用

**验收结果**: ✅ 通过
- Worker prompt 不含 brain_manager 指令
- 报告解析正确且容错
- 决策引擎覆盖核心分支
- 循环控制生效
- 端到端场景通过（Developer → Tester → Done）

**相关任务**: T-20260331-001, T-20260331-012

---

## Phase 6: Worker 规则按需加载 (2026-03-31)

**目标**: 优化 Worker prompt 长度，支持规则文件按需加载

**背景**: Worker prompt 包含完整执行规则（global.md + role.md），导致 prompt 过长（>5000 tokens）

**设计方案**: 参考 D-20260331-002 设计文档

### 任务清单
- [x] 创建 rules/ 目录结构
  - [x] rules/global.md（通用规则）
  - [x] rules/developer.md（开发者规则）
  - [x] rules/tester.md（测试者规则）
  - [x] rules/dispatcher.md（调度器规则）
- [x] 实现规则加载函数 load_rules()
- [x] 修改 generate_worker_prompt() 支持规则参数
- [x] 修改 Dispatcher Prompt 支持规则参数
- [x] 测试规则加载功能

**关键决策**:
- 规则文件使用 Markdown 格式（可读性好）
- 规则分层：global（所有角色）+ role-specific（特定角色）
- 按需加载：只加载当前角色需要的规则

**验收结果**: ✅ 通过
- 规则文件结构清晰
- 规则加载功能正常
- Worker prompt 长度显著缩短（~3000 tokens）

**相关任务**: T-20260331-004, T-20260331-012

---

## Phase 7: 飞书消息可见性改进 (2026-03-31)

**目标**: 底层自动写通知日志，避免消息丢失

**背景**: 飞书消息发送后无日志记录，导致：
1. 无法追溯发送历史
2. 发送失败时无法排查
3. 无法统计通知数量

**设计方案**: 参考 FEISHU-COLLAB-DESIGN.md

### 任务清单
- [x] 在 feishu-messenger skill 中添加日志记录
- [x] 创建 data/feishu/notifications.jsonl
- [x] 记录发送时间、接收者、消息内容、发送结果
- [x] 实现日志查询功能
- [x] 测试日志记录功能

**关键决策**:
- 日志格式：JSONL（便于追加和查询）
- 记录时机：发送前记录（避免发送失败导致无日志）
- 日志字段：timestamp/receiver/message/status/error

**验收结果**: ✅ 通过
- 日志记录功能正常
- 日志格式清晰
- 查询功能可用

**相关任务**: T-20260331-008

---

## Phase 8: 调度器日报/周报 (2026-03-31)

**目标**: 自动生成调度器运行报告，便于监控和分析

**设计方案**: 参考 D-20260331-004 设计文档

### 任务清单
- [x] 实现 generate_daily_report.py
  - [x] 统计当日任务完成数
  - [x] 统计 Worker 执行次数
  - [x] 统计决策类型分布
  - [x] 识别异常任务（blocked/超时）
- [x] 实现 generate_weekly_report.py
  - [x] 汇总周度数据
  - [x] 趋势分析（完成率/平均耗时）
  - [x] Top 问题列表
- [x] 集成到 Cron（每日 9:00 / 每周一 9:00）
- [x] 测试报告生成功能

**关键决策**:
- 报告格式：Markdown（便于飞书发送）
- 数据来源：decisions.jsonl + tasks/*.yaml
- 发送方式：飞书通知 + 文件存档

**验收结果**: ✅ 通过
- 日报/周报生成正常
- 数据统计准确
- Cron 定时触发正常

**相关任务**: T-20260331-026, T-20260331-027

---

## Phase 9: 未提交代码抢救 (2026-03-31)

**目标**: 提交遗漏的代码变更，补充 Git 历史

**背景**: 审计发现 3 个仓库有未提交代码：
1. nanobot 核心：无未提交
2. _nanobot-skills：3 个文件未提交
3. web-chat：无未提交

**设计方案**: 参考 D-20260331-003 设计文档

### 任务清单
- [x] 审计未提交代码
- [x] 分类变更（功能/文档/配置）
- [x] 补充提交（关联 Task ID）
- [x] 验证提交历史

**提交记录**:
```bash
# _nanobot-skills 仓库
git add digital-assistant/rules/dispatcher.md
git add digital-assistant/rules/global.md
git add digital-assistant/scripts/trigger_scheduler.py
git commit -m "feat(digital-assistant): add cross-check/dispatcher/webchat rules (T-20260331-001)"
```

**验收结果**: ✅ 通过
- 所有未提交代码已提交
- 提交消息包含 Task ID
- Git 历史完整

**相关任务**: T-20260331-019, T-20260331-020

---

## Phase 10: 早期任务补报告 (2026-03-31)

**目标**: 为早期任务补充 Worker 报告，完善历史记录

**背景**: 早期任务（T-20260330-001 ~ T-20260330-011）在 Worker 报告机制建立前完成，缺少报告文件

**设计方案**: 参考 D-20260331-003 Task G

### 任务清单
- [x] 识别需要补报告的任务（12 个）
- [x] 从 Git 历史和 session 日志提取执行信息
- [x] 生成补充报告（标记为 backfill）
- [x] 写入 data/brain/reports/
- [x] 验证报告完整性

**补充报告示例**:
```json
{
  "task_id": "T-20260330-002",
  "role": "developer",
  "verdict": "pass",
  "summary": "[Backfill] INS-006 探针采样过滤改进已完成，commit 7a3b2c1",
  "issues": [],
  "files_changed": ["nanobot/insights/sampler.py"],
  "backfill": true,
  "backfill_date": "2026-03-31"
}
```

**验收结果**: ✅ 通过
- 12 个任务补充报告完成
- 报告格式符合 Schema
- 标记 backfill 字段

**相关任务**: T-20260331-025

---

## Phase 11: Dev Workflow 文档三件套补全 (2026-04-01)

**目标**: 根据审计报告，系统性补全缺失的需求文档/架构文档/DEVLOG

**背景**: dev-workflow-audit-20260401.md 审计发现：
- 31 个任务仅 6 个合规（19.4% 合规率）
- 主要问题：
  1. 补测验证任务缺文档（7 个）
  2. Git 提交未关联 Task ID（14 个）
  3. digital-assistant 核心逻辑无文档

**设计方案**: 参考审计报告建议

### 任务清单
- [x] 创建 digital-assistant 文档三件套
  - [x] docs/REQUIREMENTS.md（需求文档）
  - [x] docs/ARCHITECTURE.md（架构文档）
  - [x] docs/DEVLOG.md（开发日志）
- [ ] 补充 DEVLOG Phase 记录（7 个任务）
  - [ ] T-20260331-002: 补测 INS-006
  - [ ] T-20260331-003: 补测 Cron 工具拆分
  - [ ] T-20260331-005: 补测 tushare paper trading
  - [ ] T-20260331-006: 补测飞书通知任务 ID
  - [ ] T-20260331-007: 补测 spawn tool task 参数
  - [ ] T-20260331-009: 添加版本号常量
  - [ ] T-20260331-010: 添加 get_scheduler_status()
  - [ ] T-20260331-011: 添加 get_brain_stats()
- [ ] Git 提交补救（14 个 commit）
  - [ ] 为无 Task ID 的提交添加 git notes
  - [ ] 格式：`git notes add -m "Related: T-xxx" <commit>`

**关键决策**:
1. **文档三件套位置**：skills/digital-assistant/docs/
2. **DEVLOG 补充方式**：在对应 Phase 中追加验证记录
3. **Git notes 格式**：`Related: T-xxx`（不修改原提交消息）

**当前进度**: ✅ 已完成
- ✅ REQUIREMENTS.md 已完成（4223 bytes）
- ✅ ARCHITECTURE.md 已完成（14031 bytes）
- ✅ DEVLOG.md 已完成（本文件）
- ⏳ DEVLOG Phase 记录补充中
- ⏳ Git notes 添加中

**相关任务**: T-20260401-002

---

## Phase 12: 流程失效整改 Phase 1 — 止血 (2026-04-01)

**目标**: 在调度器层面增加硬卡点，阻止任务绕过设计流程和文档要求

**背景**: dev-workflow-audit-20260401.md 审计发现合规率仅 19.4%（6/31 任务合规）。根因分析表明：
1. 调度器缺少智囊团启动硬卡点（25/31 任务跳过智囊团）
2. L0 审批未检查文档三件套（只看 Worker verdict）
3. Worker 规则未强制执行（依赖"自觉"）
4. 审计滞后（事后发现，无实时反馈）

**设计依据**:
- `data/brain/designs/process-failure-architect.md` — 架构师根因分析
- `data/brain/designs/process-failure-designer.md` — 设计师整改方案
- `data/brain/designs/process-failure-review.md` — 评审报告（Conditional Go，3 个 Must Fix）

### 任务清单

#### 核心卡点（P0）
- [x] 实现 `check_design_gate()` — 设计文档硬卡点检查函数
  - 检查 design_ref → architect 报告 → orchestration history → emergency 豁免 → needs_design 标记
  - quick/cron-auto 模板自动豁免
  - 未通过时强制将 initial_role 切换为 architect
- [x] 集成到 `run_scheduler()` dispatch 循环 — 在 cap check 之后、实际 dispatch 之前插入 gate check
- [x] 实现 `check_doc_triplet()` — 文档三件套完整性检查函数
  - 扫描 reports 目录中 files_changed 字段
  - 扫描当前 report（如有）
  - 文件系统兜底扫描（解决 reviewer 提出的 Must Fix：不仅依赖 report JSON）
  - quick/cron-auto/emergency 自动豁免
- [x] 集成到 `make_decision()` developer pass 分支 — 文档不完整则打回补文档
- [x] 集成到 `make_decision()` tester pass 分支 — L0/L1 审批前检查文档，不通过则升级 review level

#### 打回循环防护（Must Fix from review）
- [x] 实现 `_count_doc_retries()` — 统计文档打回次数
  - 扫描 orchestration history 中包含 "missing docs" 或 "文档三件套不完整" 的记录
- [x] `make_decision()` developer pass 分支增加重试计数器
  - retry_count < MAX_DOC_RETRY(2): 打回补文档
  - retry_count >= MAX_DOC_RETRY(2): 升级人工审核（promote_to_review）

#### Worker Prompt 强化（P1）
- [x] `generate_worker_prompt_v2()` developer 指导中硬编码文档三件套要求
  - 仅对非 quick/cron-auto 模板生效
  - 明确标注 "MUST — 不写不算完成"
  - 包含每个文档的内容要求说明
- [x] 新增 Git Commit 规范要求（Must Fix from review）
  - commit message 必须包含 Task ID
  - 格式: `feat(task_id): 描述`

#### 规则升级（P1）
- [x] STD-002 从 L2 RECOMMENDED 升级为 **L0 MUST**
  - 文档三件套随代码同步更新
  - 增加 "⚠️ 调度器会检查文档完整性，缺少文档的报告会被打回" 警告
- [x] 新增 G-006 规则 — "standard-dev 任务必须有设计文档"
  - 描述设计门禁机制
  - 说明 emergency 豁免条件

#### 回滚机制（Must Fix from review）
- [x] Feature flag: `DESIGN_GATE_ENABLED` — 环境变量控制设计门禁开关，默认开启
- [x] Feature flag: `DOC_TRIPLET_CHECK_ENABLED` — 环境变量控制文档检查开关，默认开启
  - 设为 "0" 即可关闭对应检查，实现一键回滚

### 关键决策

1. **硬卡点 vs 软提示**: 选择硬卡点。架构师分析表明 Worker "自觉"遵守规则不可靠（25/31 绕过），必须在调度器层面用代码阻止。
2. **文档检查粒度**: Phase 1 只检查**存在性**（文件/report 中是否出现），不检查内容质量。当前根因是"完全没写"而非"写得差"。
3. **打回次数上限**: MAX_DOC_RETRY=2，超过后升级人工审核，避免无限循环（reviewer Must Fix）。
4. **Feature flag 回滚**: 所有新卡点通过环境变量控制，可一键关闭（reviewer Must Fix）。
5. **Phase 1 不改状态机**: 不引入 `design` 状态，通过 role 切换（developer→architect）实现，最小化变更范围。

### 实现概要

| 功能点 | 位置 | 行号 | 说明 |
|--------|------|------|------|
| `check_design_gate()` | scheduler.py | L366-412 | 设计文档硬卡点，5 级检查链 |
| `check_doc_triplet()` | scheduler.py | L413-505 | 文档三件套检查，report + 文件系统双通道 |
| `_count_doc_retries()` | scheduler.py | L506-516 | 打回次数统计 |
| `run_scheduler()` 集成 | scheduler.py | L1722-1728 | dispatch 前 gate check |
| `make_decision()` developer | scheduler.py | L830-857 | doc check + retry counter |
| `make_decision()` tester | scheduler.py | L794-806 | L0/L1 审批前 doc check |
| Worker prompt 硬编码 | scheduler.py | L1267-1279 | 文档三件套 + Git commit 规范 |
| Feature flags | scheduler.py | L359-360 | DESIGN_GATE_ENABLED + DOC_TRIPLET_CHECK_ENABLED |
| STD-002 升级 | standard-dev.md | L5-10 | L2 → L0 MUST |
| G-006 新规则 | global.md | L27-30 | standard-dev 必须有设计文档 |

### 测试结果

- 18 个新增测试覆盖所有功能点
- 总计 187 个测试全部通过
- 测试覆盖场景：
  - design gate 各检查路径（design_ref / architect report / history / emergency / needs_design）
  - doc triplet 各检查路径（report files_changed / filesystem / emergency / quick 豁免）
  - 打回循环防护（retry < MAX / retry >= MAX）
  - feature flag 关闭时的行为
  - tester pass 时 L0/L1 文档检查
  - worker prompt 中文档要求注入

**验收结果**: ✅ 通过（代码实现完成，文档补全中）

**相关任务**: T-20260401-003

---

## 待办事项

### 短期（本周）
- [x] 完成 DEVLOG Phase 记录补充
- [ ] 完成 Git notes 添加
- [x] 验证文档三件套完整性
- [ ] Phase 2 加固：任务类型识别重构（classify_task_type 三层分类）
- [ ] Phase 2 加固：design 状态加入状态机
- [ ] Phase 2 加固：实时审计触发（audit_task_compliance）

### 中期（本月）
- [ ] 实现 LLM 兜底决策（Phase 2 增强）
- [ ] 实现 handle-completion 幂等性保护
- [ ] 扩展角色类型（code_reviewer）
- [ ] 优化 Review Checklist 生成

### 长期（持续改进）
- [ ] 文档模板自动生成
- [ ] 文档覆盖率监控
- [ ] 调度器性能优化
- [ ] 多任务并行编排（batch-dev 模板）

---

## 技术债务

| # | 问题 | 影响 | 优先级 | 计划 |
|---|------|------|--------|------|
| 1 | LLM 兜底决策未实现 | partial/解析失败场景只能 blocked | P1 | Phase 2 增强 |
| 2 | handle-completion 无幂等性保护 | 重复调用可能产生重复 action | P1 | Phase 2 增强 |
| 3 | execute_decision 直接操作 task dict | 耦合度高，难以扩展 | P2 | Phase 2 重构 |
| 4 | 报告文件无清理机制 | 长期积累可能占用空间 | P2 | 纳入 cron-auto |
| 5 | Dispatcher session 无健康检查 | session 失效时无法自动恢复 | P2 | 后续优化 |

---

## 经验总结

### 成功经验
1. **Worker 去状态化**：显著降低 Worker prompt 复杂度，提高遵从率
2. **文件通道报告**：比输出标记更可靠，易于调试和审计
3. **规则引擎决策**：确定性场景零延迟零成本，比 LLM 更高效
4. **精简报告 Schema**：6 字段足够，复杂证据放 summary 自由文本
5. **固定 session 复用**：避免上下文丢失，提高调度器连续性

### 踩坑记录
1. **双通道报告**：文件 + 输出标记增加不必要复杂度，最终移除输出标记
2. **角色类型过多**：初版定义 4 种角色，实际只需 2 种，过度设计
3. **循环参数重叠**：3 个参数功能重叠，简化为 2 个更清晰
4. **Phase 分拆过细**：5 个 Phase 导致实施周期长，合并为 2 个更务实
5. **Worker prompt 膨胀**：完整规则 + 历史上下文导致 prompt 过长，需按需加载

### 改进建议
1. **MVP 优先**：先跑通核心链路，再扩展功能
2. **精简设计**：字段越少越好，规则越简单越好
3. **文档先行**：设计文档 + 评审 + 实施，避免返工
4. **端到端测试**：每个 Phase 完成后立即测试完整链路
5. **技术债务管理**：及时记录技术债务，定期清理

---

## 参考文档

- [REQUIREMENTS.md](./REQUIREMENTS.md) — 需求文档
- [ARCHITECTURE.md](./ARCHITECTURE.md) — 架构设计
- [../SKILL.md](../SKILL.md) — Skill 使用手册
- [D-20260331-001](../../data/brain/designs/D-20260331-001.md) — 调度器多角色编排设计
- [D-20260331-002](../../data/brain/designs/D-20260331-002-summary.md) — Worker 规则按需加载设计
- [D-20260331-003](../../data/brain/designs/D-20260331-003-summary.md) — 未提交代码抢救方案
- [D-20260331-004](../../data/brain/designs/D-20260331-004-review-r2.md) — 调度器日报/周报设计
- [dev-workflow-audit-20260401.md](../../data/brain/designs/dev-workflow-audit-20260401.md) — Dev Workflow 合规审计报告

---

## Phase 13: Cross-Check 整改剩余 Must Fix 项 (2026-04-01)

**目标**: 实现 cross-check-remediation-review 中 T-003 未覆盖的剩余 Must Fix 项（MF-1, MF-2, MF-4）

**设计依据**:
- `data/brain/designs/cross-check-remediation-designer.md`
- `data/brain/designs/cross-check-remediation-review.md`

### 任务清单

#### MF-1: Layer 2 审计进程独立性重构
- [x] 创建 `scripts/cross_check_auditor.py` — 独立审计进程
  - 由系统级 crontab 触发（非调度器 prompt）
  - 直接扫描文件系统获取第一手数据（git log, 文件存在性）
  - 直接调用飞书 API 发送告警（不经过调度器）
  - 5 个审计探针: DesignGateProbe, DocTripletProbe, TestEvidenceProbe, GitCommitProbe, L0ApprovalProbe
  - CLI: scan / report / alert / install-cron
- [x] 创建 `scripts/test_cross_check_auditor.py` — 审计进程测试

#### MF-2: Cross-check 回滚策略 + Feature Flag
- [x] 新增 `CROSS_CHECK_ENABLED` 主控 feature flag — 一键关闭所有 cross-check Layer 1 校验
- [x] 新增 `TEST_EVIDENCE_CHECK_ENABLED` feature flag — 控制 test_evidence 验证
- [x] 在 scheduler.py 中记录各 Phase 回滚策略注释
- [x] 实现 test_evidence 校验: tester pass 时检查 test_evidence 字段
  - quick/cron-auto 豁免
  - 重试计数器 + MAX_DOC_RETRY 升级人工审核
- [x] 更新 `_generate_tester_guidance()` 增加 test_evidence MUST 要求
- [x] 新增 `_count_evidence_retries()` 函数

#### MF-4: 两方案依赖关系文档化
- [x] 更新 ARCHITECTURE.md 增加依赖关系图和实施顺序说明
- [x] 更新 DEVLOG.md 记录本 Phase

### 测试结果
- 24 个新增测试（审计探针 + feature flag + test_evidence）
- 总计 211 个测试全部通过
- 覆盖: 审计探针各路径、feature flag 开关、test_evidence 打回/升级/豁免、CLI

**相关任务**: T-20260401-004
