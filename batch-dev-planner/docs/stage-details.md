# Stage 1~5 详述 + 紧急 Hotfix 通道

> 各阶段的完整操作细节。首页 SKILL.md 仅含简述，详细流程参考本文档。

---

## §1. Stage 1: 规划

1. **筛选**：`todo list --tag 已对齐` + `todo show <id>` 读取详情
2. **依赖分析**：仓库依赖、代码依赖（协议→实现→调用）、文件重叠、跨仓库关联
3. **归集为 Plan**：
   - 独立小需求（< 50 行）合并；大需求（> 100 行 / 核心架构）独占
   - 依赖链按拓扑排列；前端与后端隔离
4. **输出**：`plans/<批次名>_DEV_PLAN.md` + `state.json` + 各 `plan-{name}.json`
5. **分支命名**：`feat/batch-YYYYMMDD-plan-{name}`

### Plan JSON 结构

plan JSON 中增加 `todo_ids` 字段，记录该 Plan 关联的 todo：

```json
{
  "name": "plan-config-refactor",
  "status": "pending",
  "todo_ids": [42, 45],
  "depends_on": [],
  "branch": "feat/batch-20260319-plan-config-refactor",
  "steps": [...]
}
```

**⚠️ 计划文档必须经用户确认后才能开始开发。**

---

## §2. Stage 2: 开发

1. **获取资源锁**（`active_batch.lock`）
2. **初始化 dev-workdir**：首次 `git clone`，后续 `git fetch + reset --hard`
   > 如果 Plan 涉及的仓库不在 dev-workdir 中，应按需 clone 到 `dev-workdir/` 下（同样设置 push DISABLED），而非跳过 dev-workdir 流程。
3. **编号占位 commit**（规划完成后、开发开始前）：
   - 在各仓库的文档（REQUIREMENTS.md / DEVLOG.md 等）中为本批次需求**预先创建编号占位符**
   - 各仓库编号体系独立（nanobot-core 用 §N，web-chat 用 Phase N，skills 用各自编号）
   - 占位 commit 提交到 dev 主分支，作为所有 feature branch 的共同起点
   - 验收阶段新增需求的编号在占位编号之后顺延
   - 记录各仓库当前最大编号到 `state.json` 或 `PLAN.md`
4. **逐 Plan 串行开发**（按依赖顺序）：

> **⚠️ 无论仓库类型（代码/文档/数据/独立 skill 仓库），所有 Plan 必须走 feature branch，禁止直接在主分支提交。**
> - **独立 skill 仓库**（如 feishu-parser、feishu-messenger 等有独立 git 的 skill）同样适用，必须拉 feature branch 开发
> - 分支命名统一：`feat/batch-YYYYMMDD-plan-{name}`，独立仓库也遵循此命名

   - 无依赖 → 从 `main` 拉分支；有依赖 → 从前序分支拉
   - 高风险 → 先 spawn 设计审查 SA（[prompt-templates.md §1](prompt-templates.md)）
   - spawn 开发 SA（[prompt-templates.md §2](prompt-templates.md)）
   - 完成 → 更新 plan 状态为 `dev_done`

### Subagent 策略

- **步骤粒度**：控制步骤粒度而非 Plan 粒度，每个 Step 独立 SA，不设硬约束
- **接力兜底**：< 90K tokens → `follow_up`；≥ 90K → 新建 SA + 摘要（[prompt-templates.md §3](prompt-templates.md)）
- **跨仓库**：SA 在各 `~/.nanobot/workspace/dev-workdir/{repo}` 分别操作、分别 commit，分支名一致

---

## §3. Stage 3: 验收

1. 读取 `state.json`，列出所有 `dev_done` 的 Plan
2. **启动 dev 环境**（独立端口 + 独立日志，详见 [nanobot/dev-env.md](nanobot/dev-env.md)）
3. **逐 Plan 验收**（按依赖顺序），每个 Plan 流程：
   1. **验收前**：feature branch 从 dev 主分支 merge 更新
   2. **在 feature branch 上验收**（静态审查 + 测试 + 人工交互）
   3. 发现问题 → 在 feature branch 上修复 → 再次验收（R2/R3...）
   4. **验收通过** → commit 压缩 → `merge --no-ff` 到 dev 主分支
   5. 下一个 Plan 重复 1-4
4. 所有 Plan 验收通过 → 关闭 dev 环境

### commit 压缩规则

- 开发阶段：每个需求各保留一个 commit
- 修复阶段：每轮修复压缩成一个 commit（特别是前端修复，把反复尝试的冗余逻辑清理压缩）

### 前端改动必须人眼确认

自动化测试（build 通过、lint 通过）不能替代视觉/交互验收。前端相关 Plan 验收时，必须 build → 重启 dev webserver → 通知用户访问 `localhost:9081` → **等用户明确确认后才标记通过**。详见 [frontend-acceptance.md](frontend-acceptance.md)。

### 验收回退

交互决定，可原地修复或打回 todo。

### Gateway 验收

飞书长连接冲突，需停 prod → 启 dev → 验收 → 恢复。web-chat 不受限。详见 [nanobot/dev-env.md](nanobot/dev-env.md)。

### 验收上下文保持

为每个 Plan 维护 `review-state-{plan}.md`（模板见 [`prompt-templates.md`](prompt-templates.md)），遵循以下规则：

0. **验收开始时创建**：为每个 Plan 创建 `review-state-{plan-name}.md`，记录以下结构化信息：
   - 验收轮次（R1/R2/R3...）
   - 每轮发现的问题清单及修复状态（pending / fixed / wontfix）
   - 新增编号分配（验收中发现的新增需求，编号在占位编号之后顺延）
1. 每次收到用户反馈后，立即将反馈内容和对齐结论写入 `review-state-{plan}.md`
2. 每次 spawn subagent 修复前，先 `read_file` review-state 确认当前状态
3. 每次修复完成后，更新 review-state 中的修复记录和检查项
4. 如果不确定之前的对齐结论，先 `read_file` review-state 再行动
5. 切换 Round 时，先更新 review-state 的「当前阶段」再继续
6. **跨 session 验收时，review-state 是上下文同步的唯一可靠来源**，新 session 必须先读取 review-state 再开始工作
7. **验收通过后，review-state 作为归档材料保留**，不删除

### 验收与 Todo 关联检查

验收通过后，执行 todo 关联检查（弱化方案，不强制 todo-commit 一一对应）：

1. 读取 plan JSON 的 `todo_ids` 字段
2. 检查关联的 todo 是否已标记 `done`
3. 如果未标记 → 提醒并执行 `todo done <id>`
4. git log 仅作为辅助参考（"建议检查"而非"必须通过"）

```bash
# 辅助参考：检查 feature branch 的 commit 是否覆盖了关联 todo
git log main..feat/batch-YYYYMMDD-plan-x --oneline
```

---

## §4. Stage 4: 发布

每个 Plan 验收通过后已合并到 dev 主分支，此阶段不再有合并动作。

```bash
# 1. 在 dev-workdir 中全量回归
cd ~/.nanobot/workspace/dev-workdir/{repo}
python -m pytest tests/ -q --tb=short
cd frontend && npm run build

# 2. 在 prod 仓库中从 dev pull merge
cd ~/Documents/code/workspace/{repo}   # 线上 prod 仓库
git pull ~/.nanobot/workspace/dev-workdir/{repo} main --no-ff

# 3. 重启 prod
bash ~/.nanobot/workspace/web-chat/restart.sh all
```

---

## §5. Stage 5: 收尾

- [ ] 更新 todo 状态（`done`）
- [ ] 更新 MEMORY.md / HISTORY.md
- [ ] **补建 todo**：对照复盘文档 + MEMORY Known Bugs + 验收过程记录，梳理所有新发现的问题，与现有 todo 对比后补建遗漏项（详见 [`state-and-decisions.md` §4.4](state-and-decisions.md#4-复盘改进)）。**agent 主动执行，不等用户发起**
- [ ] 清理工作目录（可选，建议保留复用）
- [ ] Batch 标记 `completed`，释放资源锁

---

## §6. 紧急 Hotfix 通道

不走 batch 流程，直接在线上主分支修复 → 测试 → commit → push → 重启。

**Hotfix 后 feature branch 必须同步**：
```bash
cd ~/.nanobot/workspace/dev-workdir/{repo}
git fetch origin
# 所有 feature branch 用 merge 同步（保留分支历史）
git checkout feat/batch-YYYYMMDD-plan-x
git merge origin/main
```
按拓扑顺序处理依赖链。验收中则重启 dev 环境。
