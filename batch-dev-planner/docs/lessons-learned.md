# 经验教训（持续更新）

> 来自 batch-dev-planner 实践中的经验总结。

---

## 1. 任务拆分

- **跨仓库 + 跨层（协议/后端/前端）的需求容易超 100 轮迭代**，应拆成 2 个 subagent
- 单 subagent 改动控制在 **1 个仓库、200 行以内** 最稳妥
- 5 个小需求合并到 1 个 subagent 没问题，只要改动文件不重叠

## 2. Prompt 质量

- subagent prompt 中给出 **关键代码位置**（文件名 + 行号范围）能显著减少探索迭代
- 明确说 **"不要重启服务"** 避免 subagent 自作主张
- 要求 **"最终报告"** 格式化输出，方便主 session 快速判断
- 如有设计审查结论，**将边界场景处理方案写入 prompt**，而非只给需求描述
- 错误格式设计应先调研现有实现——开发前先检查 prod 现有逻辑再设计，避免反复重构 (batch-20260313 #19)

## 3. 异常恢复

- 迭代超限时，**优先 follow_up 继续**（上下文 < 90k tokens），保留完整对话历史，避免信息丢失
- follow_up 后仍超限或上下文过大时，再用接力 subagent
- 接力 subagent 启动前**必须逐文件 git diff 检查**，不能只看 commit 记录
- 接力 subagent 的 prompt 要把已完成/未完成/未提交的改动说清楚
- 已 commit 但未 push 的改动是安全的，未 commit 的改动需要在接力 prompt 中描述

## 4. 验收

- 验收清单应区分 **"代码确认即可"** 和 **"需要运行时验证"** 的项目
- 需要运行时验证的项目必须先重启服务
- 并发相关的需求（如 spawn 并发限制）验收时需要同时触发多个操作
- **subagent 自验收能拦截大部分低级问题**，统一验收可聚焦运行时行为

### 4.5 验收反馈处理

- 验收反馈先做根因分析再拆解修复任务，避免逐条修复导致重复工作 (batch-20260313 #8)
- 验收修复涉及设计改动时，应按正式 dev-workflow 走（对齐→文档→拆解→开发），不能当 hotfix (batch-20260313 #12)
- Agent 收到验收反馈应先分析对齐再动手，克制"立即 spawn subagent 修复"的冲动 (batch-20260313 #13)
- 验收反馈应逐条编号确认处理状态，避免某条被遗漏 (batch-20260313 #16)
- 涉及 channel 交互的改动必须部署 dev 环境做真实交互验收，mock 测试不够 (batch-20260313 #11)

## 5. 前端开发策略

**核心结论：前端改动必须经过人眼确认，自动化测试无法替代视觉/交互验收。**

前端验收在 **Stage 3 统一验收阶段**按 Plan（组）进行，不打断开发流程。

为什么人眼确认不可替代：
- subagent 无法验证视觉/交互正确性（build 通过 ≠ 功能正确）
- 前端 bug 往往需要 2-3 轮修复才能完全解决（标记泄露、滚动行为、样式溢出）
- 验收时按 Plan 逐组确认，问题定位范围清晰

### 典型问题及拦截策略

| 问题类型 | 实例 | 应被哪个环节拦截 |
|---------|------|----------------|
| **设计不完整** | 隐藏标记只有开始没有闭合 | 设计审查（§3.5） |
| **role 考虑不全** | 隐藏标记对所有 role 生效而非仅 user | 设计审查 + 自验收 checklist |
| **遗漏基础设施对接** | subagent 没接入 detail_logger | 自验收 checklist |
| **持久化遗漏** | budget alert 未写入 session JSONL | 自验收 checklist |
| **视觉/交互问题** | turn 结束直接滚到底部而非显示按钮 | **只能人眼确认发现**（Stage 3 验收） |
| **体验补充** | 滚动到底部按钮 | 无法提前拦截，属于正常迭代 |

### 关键实践

- 前 4 类问题可通过 **设计审查 + 自验收 checklist** 在开发阶段拦截
- 第 5 类（视觉/交互）**只能通过人眼确认发现**，这是 Stage 3 验收中前端 Plan 必须等用户确认的原因
- 第 6 类（体验补充）是正常产品迭代，不算效率问题
- **高风险前端需求必须走设计审查**，纯后端小改动可以跳过
- 前端修复通常是小改动（< 20 行），主 session 直接修比 spawn subagent 更高效
- 前端状态管理 bug 应先用 React DevTools / console.log 系统性定位，而非逐轮猜测修复 (batch-20260313 #21)

## 6. max_iterations 估算

| 预估改动量 | 建议 max_iterations |
|-----------|-------------------|
| < 100 行 + 测试 | 40-50 |
| 100-200 行 + 测试 | 60-70 |
| 200-300 行 + 测试 | 70-80 |
| 跨仓库 300+ 行 | **拆分为 2 个 subagent** |

## 7. Feature Branch 隔离开发的好处（2026-03-11 实践总结）

### 7.1 Git History 清晰

- 每个 Plan 的开发和修复 commit 都在独立分支上，主分支 history 只有 merge commit
- `git log --graph` 可清晰看到每个 Plan 的边界
- merge commit 作为"验收通过"的标记点，方便审计

### 7.2 可回滚单个 Plan

- 某个 Plan 验收不通过需要撤回时，revert 整个 merge commit 即可，干净利落
- 不会影响其他 Plan 的代码

### 7.3 验收修复不污染主分支

- 验收发现问题时在 feature branch 上修复，主分支始终保持可用状态
- 修复 commit 和原始开发 commit 在同一分支上，逻辑内聚

## 8. 分支策略经验

### 8.1 merge --no-ff 的好处

- 保留分支历史拓扑，`git log --graph` 可清晰看到每个 Plan 的边界
- merge commit 作为"验收通过"的标记点，方便审计
- 回滚时可以 revert 整个 merge commit，干净利落

### 8.2 验收修复后的同步策略

- **有依赖链时**：Plan A 验收修复后，Plan B 需要 `git merge feat/plan-a`（不用 rebase）
- **用 merge 而非 rebase**：保持已有 commit hash 稳定，避免 force push 和历史重写
- hash 稳定意味着 state.json 中记录的 commit hash 始终有效，不会因 rebase 失效

### 8.3 Hotfix 后的分支同步

- 线上紧急 hotfix 合入主分支后，dev-workdir 需要 `git fetch origin`
- 各 feature branch 需要同步最新主分支（merge 或 rebase，视情况而定）
- 有依赖链的分支按拓扑顺序依次同步
- 验收中的分支同步后需重启 dev 环境

### 8.4 有依赖链时的传播策略

```
Plan A 修复 → Plan B merge Plan A → Plan C merge Plan B（如 C 依赖 B）
                                  → Plan D 不受影响（如 D 独立）
```

- 只传播给直接依赖方，不需要全量传播
- 主 session 在切换验收 Plan 前自动执行 merge 同步

### 8.5 分支纪律

- 发现 subagent 提前 merge 后应立即回退到干净基线，不要拖延到验收后处理 (batch-20260313 #7)
- 跨仓库修复应在 commit message 和文档中明确标注仓库归属，避免合并时遗漏 (batch-20260313 #22)
- 主 session 接手 subagent 工作时也必须遵守分支流程，应急不等于可以跳过流程 (batch-20260313 #30)

## 9. Dev 环境经验

### 9.1 数据层共用的利弊

**好处**：
- sessions、skills、memory 共用，dev 环境可以直接使用现有数据，无需复制
- 配置文件共用，减少维护成本

**风险与缓解**：
- dev 环境的 worker 处理消息时会写入共用的 sessions 目录 → 验收时注意使用专门的测试 session
- config.json 共用意味着 dev 环境的行为与 prod 一致 → 端口差异通过命令行参数覆盖

### 9.2 日志隔离的必要性

- prod 日志在 `~/.nanobot/logs/`，dev 日志在 `~/.nanobot/logs-dev/`
- 隔离的核心原因：验收时需要查看 dev 环境的日志来确认行为，不能与 prod 日志混在一起
- llm-logs 例外：append-only + 按天分文件，prod/dev 同时写无冲突，可以共用

### 9.3 端口规划

| 服务 | Prod | Dev |
|------|------|-----|
| Gateway | 8080 | 9080 |
| Webserver | 8081 | 9081 |
| Worker | 8082 | 9082 |

- Web-chat 验收：prod/dev 可并行运行，用户访问 `localhost:9081`
- Gateway 验收：必须停 prod Gateway 再启 dev Gateway（飞书长连接冲突）

### 9.4 dev-workdir 与 prod 仓库隔离

- dev-workdir 的 remote push 配置应设为 DISABLED，防止误 push 到 prod 仓库（已通过 #26 实施） (batch-20260313 #23)

### 9.5 连续验收时保持 dev 环境

- 连续验收多个 Plan 时应保持 dev 环境运行，避免反复启停带来的固定开销
- 特别是 gateway 验收，重启会中断飞书长连接 session，需要重新建立
- 验收完所有需要 dev 环境的 Plan 后再统一关闭，非 gateway Plan 可在 prod 环境下验收

## 10. 串行批次的好处

严格串行批次（上一批验收完成并合并前，不开启下一批），好处：

### 10.1 代码基线清晰

- 每个 batch 开始时，主分支是上一个 batch 合并后的稳定状态
- 所有 feature branch 从同一个已知基点拉出，不会有"基于未合并代码开发"的问题
- 合并冲突最小化——同一 batch 内的 Plan 通过依赖分析已经处理了交叉

### 10.2 认知负担低

- 同一时刻只有一个 batch 在进行，状态简单明确
- 不需要处理"batch A 在验收、batch B 在开发、batch C 在规划"的复杂并发状态
- 主 session 和用户都能清楚知道当前进度

### 10.3 验收质量有保障

- 验收时只关注当前 batch 的改动，不会被其他 batch 的变更干扰
- 发现问题可以在 feature branch 上从容修复，不用担心影响其他正在进行的开发
- 合并后的回归测试结果可信——不会有其他未合并的改动引入干扰

## 11. 测试策略

- 跨调用路径（gateway vs worker）的行为一致性需要测试覆盖，开发 subagent 不能只测一条路径 (batch-20260313 #20)

## 12. 收尾流程

- batch 收尾需逐 Plan 与用户确认状态，不能批量操作，避免误标记 (batch-20260313 #25)
- prod 合并后需 build 前端（`npm run build`），合并检查清单应包含前端构建步骤 (batch-20260313 #31)

## 13. Dev/Prod 进程隔离

- `restart.sh` 的 `find_pids()` 不能用宽泛 pgrep 模式（如 `pgrep -f "python.*webserver.py"`），会匹配到所有环境的同名进程，导致 dev 重启误杀 prod 或反之 (batch-20260313 复盘后发现)
- **正确做法**: 只用精确 `SCRIPT_DIR` 路径匹配 + 端口匹配（`find_pid_on_port`），两者取并集
- dev 和 prod 的 restart.sh 必须**同时维护**，一方修复另一方也要同步
