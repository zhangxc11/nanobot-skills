# Prompt 模板参考

> 本文件包含 batch-dev-planner 各阶段使用的 subagent prompt 模板。
> 主 session 根据实际需求裁剪后使用。

---

## 1. 设计审查 Subagent Prompt

```markdown
## DESIGN-{N}: {需求标题} 设计方案

请为以下需求输出设计方案，**不要写代码**。

### 需求描述
{从 todo note 提取}

### 要求输出

1. **改动范围**：列出所有需要改动的文件，每个文件改什么
2. **数据流**：数据从哪里产生、经过哪些层、最终到哪里消费
3. **边界场景**：
   - 缺失/异常数据时的行为（如字段不存在、API 超时）
   - 多种消息 role 的兼容（user/assistant/tool/system）
   - 流式 vs 完成态的差异
   - session 恢复/刷新后的状态一致性
   - 闭合/配对逻辑（如有标记语法）
4. **接口契约**：新增的 API、事件、props 的精确定义
5. **与现有功能的交互**：是否影响已有功能，如何兼容

### 仓库信息
- **代码位置**: `~/.nanobot/workspace/dev-workdir/{repo}`（只读浏览，不要修改任何文件）
- {关键文件位置}

### ⚠️ 注意
- 本 subagent 仅做设计分析，**不要修改任何代码**
- 在 `~/.nanobot/workspace/dev-workdir/{repo}` 中浏览代码以了解现有结构
- 线上仓库（如 ~/Documents/code/workspace/...）**不要访问**
```

设计 subagent 建议 `max_iterations=25`，只读代码 + 输出方案，不动代码。

### 审查要点

主 session 收到设计方案后，重点检查：

- [ ] 边界场景是否完整（这是验收阶段最常发现问题的地方）
- [ ] 数据流是否闭环（产生 → 传递 → 消费 → 持久化）
- [ ] 标记/协议是否有闭合机制
- [ ] 对所有 role/状态是否都考虑了
- [ ] 是否遗漏了与现有基础设施的接入（如 detail_logger、session 持久化）

审查通过后，将设计方案要点写入开发 subagent 的 prompt 中，作为实现约束。

---

## 2. 开发 Subagent Prompt

```markdown
## SA-{N}: {Plan 名称} — Step {X}: {标题}

你需要完成以下需求的开发。请严格遵循 dev-workflow。

### 工作目录与分支
- **工作目录**: `~/.nanobot/workspace/dev-workdir/{repo}`
- **分支**: `feat/batch-{YYYYMMDD}-plan-{name}`（已创建，请确认当前在此分支上）

### ⛔ 严禁操作
- **不要 merge** — 不要将 feature branch 合并到任何其他分支（main/local/dev 等）。只在 feature branch 上 commit。
- **不要 git push** — dev-workdir 的 remote 指向本地 prod 仓库，push 会直接污染生产环境。
- **不要重启任何服务**
- **不要操作线上仓库**（如 ~/Documents/code/workspace/...），所有操作限定在 dev-workdir 内

### 需求列表

#### {序号}: {需求标题}
（从 todo note 中提取的详细需求描述、验收标准）
（关键代码位置提示：哪些文件需要改、参考哪些现有代码）
（如有设计审查结论，附上关键约束和边界场景处理方案）

### 开发顺序
1. 先确认当前分支正确：`git branch --show-current` 应为 `feat/batch-{YYYYMMDD}-plan-{name}`
2. 读取相关代码，理解现有结构
3. 按需求逐个实现
4. 每个需求写测试
5. 全量回归
6. 文档更新（REQUIREMENTS / DEVLOG）
7. Git commit（在 feature branch 上）
8. **自验收**（见下方 checklist）

### 其他注意
- 前端构建命令: `cd ~/.nanobot/workspace/dev-workdir/web-chat/frontend && npm run build`
- （其他项目特定注意事项）

### 自验收 Checklist

开发完成、commit 之后，逐项检查以下内容并在最终报告中标注通过/失败：

**通用项**：
- [ ] 全量测试通过（pytest / npm run build）
- [ ] 新功能有对应测试覆盖
- [ ] 改动文件中无调试代码残留（console.log、print、TODO hack）
- [ ] 确认在正确的 feature branch 上 commit（`git log --oneline -3` 检查）
- [ ] 确认没有误操作 push（`git log --oneline origin/main..HEAD` 不应报错为"无 remote"）

**数据流完整性**（如涉及新数据字段）：
- [ ] 数据从产生到消费的完整链路都已实现
- [ ] 数据持久化到 session JSONL（如需要 session 恢复后仍可用）
- [ ] 与现有基础设施对接（detail_logger、usage_recorder 等）

**前端专项**（如涉及前端改动）：
- [ ] 构建无 warning（或 warning 与本次改动无关）
- [ ] 不同消息 role（user/assistant/tool）下的表现是否正确
- [ ] 流式传输中 vs 完成态的表现是否一致
- [ ] 页面刷新 / session 切换后状态是否正确恢复
- [ ] 标记/语法是否有闭合机制，缺失闭合时的 fallback 行为
- [ ] 空数据 / 异常数据的 defensive 处理

**协议/接口专项**（如涉及新 API 或消息格式）：
- [ ] 接口有明确的输入输出定义
- [ ] 旧版本客户端/数据的向后兼容

（主 session 可根据具体需求增删 checklist 项目）

### 最终报告
完成后请报告：
1. 实现摘要（改了哪些文件、关键改动）
2. 新增测试数量和全量回归结果
3. 前端构建结果（如涉及）
4. Git commit hash + 当前分支名
5. 遇到的问题和决策
6. **自验收结果**（逐项标注 ✅/❌，失败项说明原因和处理）
7. **【前端需求必填】用户验收指南**：
   - 需要验收的功能点列表（每个功能 1-2 句话描述预期行为）
   - 具体操作步骤（如：在 dev 环境 localhost:9081 发送一条长消息 → 等待 turn 结束 → 观察是否出现滚动按钮）
   - 已知的边界场景（如：空消息、超长内容、不同 role 下的表现）
```

---

## 3. 接力 Subagent Prompt

当原 subagent 因迭代超限且上下文过大（> 90k tokens）无法 follow_up 时使用：

```markdown
## SA-{N}b: 接力完成 {Plan 名称} — Step {X}

SA-{N} 因迭代上限未完成。已完成部分：
- {已完成的描述，commit hash}

未提交的改动：
- {git diff --stat 摘要}

### 工作目录与分支
- **工作目录**: `~/.nanobot/workspace/dev-workdir/{repo}`
- **分支**: `feat/batch-{YYYYMMDD}-plan-{name}`

### ⛔ 严禁操作
- **不要 merge** — 不要将 feature branch 合并到任何其他分支（main/local/dev 等）。只在 feature branch 上 commit。
- **不要 git push** — dev-workdir 的 remote 指向本地 prod 仓库，push 会直接污染生产环境。
- **不要重启任何服务**
- **不要操作线上仓库**（如 ~/Documents/code/workspace/...），所有操作限定在 dev-workdir 内

剩余任务：
1. {具体未完成的任务}
2. ...
```

**注意**：启动前**必须逐文件 git diff 检查**，不能只看 commit 记录。

---

## 4. 验收修复 Subagent Prompt

验收阶段发现问题后，在 feature branch 上进行修复：

```markdown
## FIX-{N}: {Plan 名称} 验收修复 — Round {R}

验收 Plan "{plan_title}" 时发现以下问题，需要在 feature branch 上修复。

### 工作目录与分支
- **工作目录**: `~/.nanobot/workspace/dev-workdir/{repo}`
- **分支**: `feat/batch-{YYYYMMDD}-plan-{name}`（已切换到此分支）

### ⛔ 严禁操作
- **不要 merge** — 不要将 feature branch 合并到任何其他分支（main/local/dev 等）。只在 feature branch 上 commit。
- **不要 git push** — dev-workdir 的 remote 指向本地 prod 仓库，push 会直接污染生产环境。
- **不要重启任何服务**（主 session 负责重启 dev 环境）
- **不要操作线上仓库**（如 ~/Documents/code/workspace/...），所有操作限定在 dev-workdir 内

### 需要修复的问题

#### 问题 1: {问题描述}
- **现象**: {用户反馈的具体现象}
- **预期行为**: {正确的行为应该是什么}
- **相关文件**: {可能需要修改的文件}

#### 问题 2: ...
（如有多个问题）

### 修复要求
1. 先确认当前分支正确：`git branch --show-current`
2. 阅读相关代码，定位问题根因
3. 修复所有列出的问题
4. 运行全量测试确保没有回归
5. 前端改动需重新 build：`cd ~/.nanobot/workspace/dev-workdir/web-chat/frontend && npm run build`
6. Git commit（commit message 格式：`fix: {plan-name} review round {R} — {修复摘要}`）

### 自验收 Checklist
- [ ] 所有列出的问题均已修复
- [ ] 全量测试通过
- [ ] 前端构建通过（如涉及前端改动）
- [ ] 确认在正确的 feature branch 上 commit
- [ ] 修复没有引入新的问题（检查改动范围是否合理）

### 最终报告
完成后请报告：
1. 每个问题的修复方案和改动文件
2. 全量测试 / 前端构建结果
3. Git commit hash
4. 是否有额外发现的问题（修复过程中注意到的其他潜在问题）
```

**使用时机**：
- 验收中发现的问题较多或较复杂时，spawn 修复 subagent
- 小问题（< 20 行改动）主 session 可直接修，不必 spawn subagent
- 修复完成后，主 session 负责重启 dev 环境让用户再次验收

---

## 5. Review State 文件模板

验收过程中，为每个 Plan 维护一个 `review-state-{plan}.md` 文件，放在 batch 目录下（如 `data/batch-dev/batches/batch-{YYYYMMDD}/review-state-{plan}.md`）。

此文件是跨轮次的**实时状态摘要**，供主 session agent 快速恢复验收上下文。与每轮的详细反馈文件 `review-plan-*-r{N}.md` 互补：
- `review-plan-*-r{N}.md`：某一轮验收的详细反馈记录（问题描述、复现步骤、修复方案），给修复 subagent 看
- `review-state-{plan}.md`：跨轮次累积的状态摘要，给主 session 看

模板：

```markdown
# Review State: Plan {name}

## 当前阶段
reviewing | fix_round_1 | fix_round_2 | passed

## 验收检查项
- [ ] 静态审查: {具体项}
- [ ] 单元测试: {测试文件/命令}
- [ ] 人工交互: {交互场景}

## 反馈记录

### R1 ({日期})
- F1: {问题描述} → {处置: 修复 / 不做 / 延后}
- F2: {问题描述} → {处置}

### R2 ({日期})
- G1: ...

## 已完成的修复
- commit {hash}: {修复内容}

## 关键对齐结论
- {重要决策，如 "F1 不做，记为后续 todo"}
- {如 "按 xxx 方案处理 G4"}
```

---

## 6. 验收检查清单（主 session 用）

主 session 在 Stage 3 验收每个 Plan 时，按此清单逐步执行。此清单确保验收流程标准化、不遗漏关键步骤。

### 6.1 验收前准备

```markdown
### Plan {name} 验收准备

1. **合并最新主分支到 feature branch**（确保基线一致）：
   ```bash
   cd ~/.nanobot/workspace/dev-workdir/{repo}
   git checkout feat/batch-{YYYYMMDD}-plan-{name}
   git merge {主分支名}  # main 或 local
   # 如有冲突，解决后 commit
   ```

2. **重启 dev 环境**（加载最新代码）：
   ```bash
   bash ~/.nanobot/workspace/dev-workdir/web-chat/restart.sh all  # 如涉及 web-chat
   # 或其他 dev 环境重启方式
   ```

3. **确认 dev 环境正常**：
   - 服务启动无报错
   - 基础功能可用（发消息、收回复）
```

### 6.2 静态审查

```markdown
### 静态审查 Checklist

- [ ] **代码 diff 审查**：`git diff {主分支}..HEAD --stat` 查看改动范围是否合理
- [ ] **无调试残留**：`git diff {主分支}..HEAD | grep -E "console\.log|print\(|TODO|HACK|FIXME"` 
- [ ] **测试覆盖**：新功能有对应测试，`pytest` / `npm run build` 通过
- [ ] **文档更新**：REQUIREMENTS.md / DEVLOG 等是否需要更新
- [ ] **commit 历史**：`git log --oneline {主分支}..HEAD` 检查 commit 是否清晰
```

### 6.3 功能验收

```markdown
### 功能验收 Checklist

- [ ] **正常路径**：核心功能按预期工作
- [ ] **边界场景**：空数据、异常输入、超长内容等
- [ ] **回归测试**：已有功能未被破坏
- [ ] **前端表现**（如涉及）：
  - [ ] 不同 role 消息显示正确
  - [ ] 流式 vs 完成态一致
  - [ ] 刷新/切换 session 后状态恢复
```

### 6.4 验收通过后处理

```markdown
### 验收通过后 Checklist

1. **整理 commit**（feature branch 上 squash 或 fixup）：
   ```bash
   cd ~/.nanobot/workspace/dev-workdir/{repo}
   git checkout feat/batch-{YYYYMMDD}-plan-{name}
   # 查看 commit 数量
   git log --oneline {主分支}..HEAD
   # 如有多个 commit，压缩为 1-2 个有意义的 commit
   git rebase -i {主分支}
   ```

2. **合并到 dev 主分支**（merge --no-ff）：
   ```bash
   git checkout {主分支}
   git merge --no-ff feat/batch-{YYYYMMDD}-plan-{name} -m "Merge plan-{name}: {简要描述}"
   ```

3. **更新 review-state**：标记为 `passed`

4. **继续下一个 Plan 验收**（如有）
   - 下一个 Plan 的 feature branch 需要先 merge 更新后的主分支
```

### 6.5 验收未通过处理

```markdown
### 验收未通过 Checklist

1. **记录问题**到 `review-plan-{name}-r{N}.md`（详细描述 + 复现步骤）
2. **更新 review-state**：标记当前轮次和问题列表
3. **Spawn 修复 subagent**（使用 §4 模板）或小问题主 session 直接修
4. **修复后重新从 6.1 开始验收**
```
