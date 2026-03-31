---
name: dev-workflow
description: 软件开发工作流规范。所有代码项目（新建或维护）必须遵循此流程：文档先行（需求/架构/DEVLOG）、任务拆解、逐步开发、测试验证、Git 版本管理、分支策略。当用户要求开发新功能、修复 Bug、改进代码、创建新项目时使用。
---

# 开发工作流规范

所有代码项目统一遵循 **设计 → 开发 → 验收** 三阶段流程。每阶段有明确 Checkpoint，通过后进入下一阶段。

## 加载指引

加载本 skill 后，根据当前所处阶段，将对应的「写入 summary 的准则」复制到 session summary 的 `## 当前工作准则` 段。阶段切换时同步更新。

---

## 一、设计阶段

### 正式开发
1. **REQUIREMENTS.md** — 主文件留索引 + summary（一段话概括目标和边界），全量内容（含 Glossary）放独立文件
2. **方案文档** — 写入 ARCHITECTURE.md，必须包含验收 Checklist（可测试的通过条件），每项标注「可自验 ✅」或「需人工 👤」
3. **关键决策复述** — Agent 复述核心设计决策和取舍，用户确认后才进入开发
4. **DEVLOG.md** — 新增 Phase，写入 checkbox 任务清单

### 紧急修复
- 跳过独立方案文档，直接在 DEVLOG 记录：问题现象、根因分析、修复思路（3-5 行）
- **文档三件套不省**：REQUIREMENTS 补 Issue 记录，ARCHITECTURE 按需更新，DEVLOG 记任务

### Checkpoint
- [ ] Glossary / 关键术语定义了？
- [ ] 验收 Checklist 写了？（可测试的通过条件）
- [ ] Checklist 每项都标注了「可自验 ✅」或「需人工 👤」？
- [ ] 用户确认了关键决策？

### 写入 summary 的准则
```
- 文档三件套（REQ/ARCH/DEVLOG）必须先于代码
- REQ 主文件只放索引+summary，全量内容放独立文件
- 方案必须包含验收 Checklist，每项标注「可自验 ✅」或「需人工 👤」，用户确认后才开发
```

---

## 二、开发阶段

### 操作步骤
1. **按 DEVLOG checkbox 逐项推进** — 每完成一项勾选，保持进度可见
2. **先读后改** — 修改文件前必须 `read_file` 确认当前内容
3. **按设计要点 commit** — 每个逻辑单元独立提交，message 格式: `feat/fix/docs/refactor: 描述`
4. **新问题记 TODO 不展开** — 发现超出当前任务范围的问题，记入 DEVLOG TODO 区，不当场修
5. **开发完成后自验收** — 对照 Checklist 中所有「可自验 ✅」项逐项验证：
   - **自验 cases 来源**：必须从关联的 TODO note 和 req 文档中提取验收场景和测试数据，不能自己编造。如果 TODO/req 中有具体的验收场景或测试数据，必须覆盖
   - **L1 代码完整性** — 所有 checkbox 已勾选，无遗漏文件，import/依赖完整
   - **L2 功能验证** — 真实场景端到端测试（仅可自验项），不是只跑单元测试
   - 不通过 → 自修，**最多 2 轮**
   - **止损**：发现根本性设计偏差 → 停止自修，回到设计阶段重新对齐
   - 所有「可自验」项通过后才标记开发完成

### Checkpoint
- [ ] 是否偏离了需求？有没有在做计划外的事？
- [ ] DEVLOG checkbox 与实际进度一致？
- [ ] 所有「可自验 ✅」项通过了？

### 写入 summary 的准则
```
- 严格按 DEVLOG checkbox 顺序推进，不跳不插
- 每个提交对应一个设计要点，改完即 git commit
- 文档三件套随代码同步更新，发现新问题记 TODO 不展开
- 开发完成后对照 Checklist 跑所有「可自验」项，自修最多 2 轮，通过才交付
- 自验 cases 从 TODO note 和 req 文档提取，不自行编造
```

---

## 三、验收阶段

> 「可自验 ✅」项在开发阶段已由 Agent 跑过并通过；验收阶段负责**所有项的用户最终确认**。

### 输出验收报告
输出结构化验收报告交用户，清楚区分两类项：
```
## 验收报告

### 自验已通过（待人工 confirm）
- [x] {Checklist 项} — 自验方式：xxx，结果：通过

### 需人工操作验证
- [ ] {Checklist 项} — 验证方式：xxx（需用户亲自操作）

### 其他
- 遗留问题：TODO 列表（如有）
- 文档状态：三件套是否已更新
```

### 人工验收确认
- **「可自验 ✅」项**：Agent 已跑过，用户 review 验收报告后 confirm 即可
- **「需人工 👤」项**：用户亲自操作验证（如前端视觉/交互、真实环境端到端等）
- **所有项都需要用户最终确认**，不能因为自验通过就跳过
- 用户确认全部通过后才算完成

### Checkpoint
- [ ] 验收报告已输出给用户？（区分自验已通过 vs 需人工操作）
- [ ] 用户确认了所有项？
- [ ] 关联 todo 已标记 done？（如有关联的 todo，验收通过后检查并更新状态）

### 写入 summary 的准则
```
- 输出验收报告，区分「自验已通过待 confirm」和「需人工操作验证」
- 所有项都需要用户最终确认，自验通过不等于验收通过
- 「需人工」项必须用户亲自操作验证
```

---

## 项目文档结构

### 简版（单文件模式）
适用于新项目或文档体量较小（每个文件 < 500 行）：
```
project/docs/
├── REQUIREMENTS.md   # 需求文档
├── ARCHITECTURE.md   # 架构设计
└── DEVLOG.md         # 开发日志
```

### 复杂版（主文件 + 子目录模式）
适用于文档体量大（任一文件 > 800 行或 > 30KB）：
```
project/docs/
├── REQUIREMENTS.md        # 索引 + Summary + Backlog
├── requirements/          # 按编号分组的需求全量内容
├── ARCHITECTURE.md        # 架构总览 + 模块索引
├── architecture/          # 按模块分组的架构设计
├── DEVLOG.md              # 状态总览 + 最近3个Phase
└── devlog/                # 按Phase归档
```

详见 [references/doc-templates.md](references/doc-templates.md)（简版模板）和 [references/doc-split-guide.md](references/doc-split-guide.md)（升级指南）。

## 分支策略

| 场景 | 策略 |
|------|------|
| 简单改动（1-2 文件） | 直接在 main/local 分支 |
| 独立功能模块 | `feat/功能名` 分支，完成后合并 |
| Bug 修复 | 视复杂度选择，通常直接 main |

## Session 恢复

新 session 开始时：
1. 读 `docs/DEVLOG.md` — 找 🔜 标记的待办，确定当前阶段
2. 读 `docs/REQUIREMENTS.md` — 理解需求（复杂版只读主文件，按需读子文件）
3. 按需读 `docs/ARCHITECTURE.md` — 理解技术设计
4. 将当前阶段的 summary 准则写入 session summary
5. 继续执行

## 持续复盘

- Session 结束时 DEVLOG 记录效率数据（耗时、卡点、改进点）
- 发现流程问题 → 更新对应阶段准则

## 约束与准则

### 任务委派
- subagent 执行具体开发时应调用 claude-code skill，而非自己直接用 read_file/edit_file 改代码
- 主 session 不直接调用 claude code

### 文档规范
- 需求编号复用已有体系（nanobot §N / feishu-docs Phase/FN.M / feishu-parser FR-N）
- 排期单位是 TODO（可跨仓库）
- req 文档索引放 REQUIREMENTS.md，细节写独立文件
- 分析文档按 `data/analysis/_INDEX_目录结构与分类规则.md` 分类

### 合并与审计
- LLM 自验收不可跳过
- 合并前审计（git 记录 + 过程文档）
- 补文档从原始设计提取，不从代码反推
- 紧急修复可跳过方案设计，但仓库文档三件套不省

### 测试
- 测试数据用相对日期，不硬编码

### nanobot 开发专用
> 详见 [references/nanobot-dev-constraints.md](references/nanobot-dev-constraints.md)

### 写入 summary 的准则
> 使用 dev-workflow 时，将以下内容一字不改追加到 session summary 的 `## 当前工作准则` 段。

```
- subagent 开发用 claude-code skill，不直接 read_file/edit_file 改代码
- 合并前审计 git 记录 + 过程文档，自验收不可跳过
```
