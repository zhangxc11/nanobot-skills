# 状态管理 + 资源锁 + 决策记录 + 复盘流程

> batch-dev-planner 的运行时状态、并发控制、历史决策和持续改进机制。

---

## §1. 状态管理

### 1.1 文件布局

```
~/.nanobot/workspace/data/batch-dev/
├── active_batch.json         ← 当前活跃 batch ID
├── active_batch.lock         ← 资源锁
└── batches/batch-YYYYMMDD/
    ├── PLAN.md               ← 人可读计划
    ├── STATUS.md             ← 自动生成的状态总览
    ├── state.json            ← batch 级状态
    └── plans/
        ├── plan-a.json       ← plan 级状态
        └── plan-b.json
```

### 1.2 状态流转

**Batch**: `planning` → `developing` → `reviewing` → `merging` → `completed`

**Plan**: `pending` → `developing` → `dev_done` → `reviewing` → `fix_in_progress` ↔ `reviewing` → `passed` → `merged`

**动态追加**: Plan 在 `pending` 状态时 `allow_append=true`，进入 `developing` 后锁定为 `false`。

---

## §2. 资源锁机制

通过 `active_batch.lock` 确保单一 session 操作 batch 资源：

```json
{ "session_key": "webchat_xxx", "acquired_at": "...", "heartbeat": "..." }
```

- **软超时 10 分钟**：heartbeat 未更新 → 探测持锁 session 是否存活
- **硬超时 1 小时**：无条件强制释放
- 执行中的 session / SA 定期更新 heartbeat
- 兼容手动触发和未来 cron 自动触发

---

## §3. 决策记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 动态追加需求 | `pending` 可追加，`developing` 后锁定 |
| 2 | 资源锁机制 | `active_batch.lock` + 双超时（软 10min / 硬 1h）+ 心跳 |
| 3 | 工作目录设计 | 通用 `dev-workdir` 下 clone；nanobot 特化另见子文档 |
| 4 | llm-logs | 共用，append-only + 按天分文件，无写冲突 |
| 5 | 配置文件 | 复用 `config.json`，端口通过命令行参数覆盖 |
| 6 | 日志保留 | dev 环境关闭时只 kill 进程，不清理日志 |
| 7 | 合并策略 | `merge --no-ff` 保留分支历史 |
| 8 | SA 执行策略 | 控制步骤粒度，每个 Step 独立 SA |
| 9 | SA 接力兜底 | <90K 用 `follow_up`，≥90K 新建 SA + 摘要 |
| 10 | 验收回退 | 不预设策略，交互决定（原地修 / 打回 todo） |
| 11 | Gateway 验收 | 停 prod → 启 dev → 验收 → 恢复；web-chat 不受限 |
| 12 | 实现优先级 | ① 通用框架优先 ② nanobot 特化作为正常需求开发 |
| 13 | 核心代码加载 | PYTHONPATH 为主；独立 venv + `pip install -e` 后备 |
| 14 | 验收与 todo 关联 | 弱化方案：plan JSON 记录 `todo_ids`，验收后提醒标记 done，git log 仅辅助参考 |

→ 经验教训见 [`lessons-learned.md`](lessons-learned.md)

---

## §4. 复盘改进

每次批量开发完成后，**必须**执行复盘：

1. **创建复盘文档**：`cp docs/retrospectives/TEMPLATE.md docs/retrospectives/{batch-name}.md`
2. **填写复盘**：对照模板逐项检查流程合规性、记录问题与改进
3. **回写改进**：
   - 流程问题 → 修改 SKILL.md 或对应阶段详述文档
   - 经验教训 → 追加到 [`lessons-learned.md`](lessons-learned.md)
   - 决策变更 → 更新上方 §3 决策记录表
   - 工具/脚本 bug → 创建 todo 跟踪修复
4. ~~**试运行标记**：首次实施通过复盘后，移除顶部 ⚠️ 试运行标记~~ ✅ 已移除 (batch-20260313)
