# batch_dev.py 需求文档

## 概述

`batch_dev.py` 是 batch-dev-planner skill 的核心 CLI 脚本，用于管理批量开发的 batch/plan 生命周期、验收记录、合并追踪和资源锁。

## 子命令一览

| 子命令 | 功能 | 关键参数 |
|--------|------|----------|
| `batch create` | 创建新 batch | `--name`, `--base-commit-nanobot`, `--base-commit-webchat` |
| `batch list` | 列出所有 batch | 无 |
| `batch show` | 显示 batch 详情 | `--batch`（可选，默认活跃 batch） |
| `batch advance` | 推进到下一阶段 | `--batch` |
| `batch complete` | 标记 batch 完成 | `--batch` |
| `plan add` | 添加 Plan | `--title`, `--todos`, `--depends-on`, `--repos`, `--batch` |
| `plan list` | 列出所有 Plan | `--batch` |
| `plan show` | 显示 Plan 详情 | `<plan-id>`, `--batch` |
| `plan update` | 更新 Plan 属性 | `<plan-id>`, `--status`, `--branch-*`, `--dev-*`, `--review-session`, `--depends-on`, `--batch` |
| `plan add-todo` | 追加需求到 Plan | `<plan-id>`, `--todo-id`, `--batch` |
| `review add` | 添加验收反馈 | `<plan-id>`, `--feedback`, `--batch` |
| `review fix` | 记录修复 | `<plan-id>`, `--round`, `--fix-commit`, `--batch` |
| `review pass` | 标记验收通过 | `<plan-id>`, `--batch` |
| `merge` | 记录合并 | `<plan-id>`, `--commit`, `--repo`, `--batch` |
| `status` | 状态总览 | 无 |
| `lock acquire` | 获取资源锁 | `--session` |
| `lock release` | 释放资源锁 | 无 |
| `lock status` | 查看锁状态 | 无 |
| `lock heartbeat` | 更新心跳 | 无 |

---

## 子命令详细说明

### batch create

创建一个新的 batch，进入 `planning` 阶段。

- **串行批次原则**：如果已有活跃 batch 且未完成，则拒绝创建新 batch。
- `--name`（必填）：Batch 名称，如 `batch-20260312`。
- `--base-commit-nanobot`（可选）：nanobot 仓库的基点 commit hash。
- `--base-commit-webchat`（可选）：web-chat 仓库的基点 commit hash。
- 创建后自动设为活跃 batch。

### batch list

列出所有 batch 记录，显示 Batch ID、阶段、Plan 数量、创建时间、是否活跃。

### batch show

显示指定（或活跃）batch 的详细信息，包括阶段、基点 commits、所有 Plan 的状态表格。

### batch advance

将 batch 推进到下一阶段。每个阶段有前置条件：

| 当前阶段 | 推进条件 | 目标阶段 |
|----------|----------|----------|
| planning | 至少有 1 个 Plan | developing |
| developing | 所有 Plan ≥ dev_done | reviewing |
| reviewing | 所有 Plan ≥ passed | merging |
| merging | 所有 Plan = merged | completed |

### batch complete

强制标记 batch 为 `completed` 并清除活跃 batch，解锁新批次创建。

### plan add

在当前 batch 中添加一个 Plan。

- **阶段限制**：仅 `planning` 或 `developing` 阶段允许添加。
- `--title`（必填）：Plan 标题，自动转为 kebab-case 作为 plan_id。
- `--todos`（可选）：关联的 todo ID，逗号分隔。
- `--depends-on`（可选）：依赖的 Plan ID，**必须是当前 batch 中已存在的 plan_id**。
- `--repos`（可选）：涉及的仓库名，逗号分隔（如 `nanobot,web-chat`）。

### plan list

列出当前 batch 的所有 Plan，按 plan_order 排序，显示序号、ID、标题、状态、依赖。

### plan show

显示指定 Plan 的完整详情：标题、状态、依赖、仓库、需求 IDs、分支、开发信息、验收记录、合并记录。

### plan update

更新 Plan 的属性。可更新字段：

| 参数 | 说明 |
|------|------|
| `--status` | 新状态值 |
| `--branch-nanobot` | nanobot 分支名 |
| `--branch-webchat` | web-chat 分支名 |
| `--dev-session` | 开发 session ID |
| `--dev-subagent` | 开发 subagent ID |
| `--dev-commit` | 追加开发 commit hash |
| `--dev-started` | 标记开发开始（记录时间戳） |
| `--dev-completed` | 标记开发完成（记录时间戳） |
| `--review-session` | 验收 session ID |
| `--depends-on` | 修改依赖的 Plan ID（校验合法性） |

### plan add-todo

向 pending 状态的 Plan 追加需求。非 pending 状态拒绝操作。

### review add

为 Plan 添加验收反馈，自动创建新一轮（round），状态切换为 `reviewing`。

### review fix

记录某一轮验收的修复 commit，标记该轮 result 为 `fixed`。

### review pass

标记 Plan 验收通过，状态变为 `passed`。

### merge

记录 Plan 的合并 commit。

- `--repo`（默认 `nanobot`）：指定仓库。
- **跨仓库支持**：如果 Plan 涉及多个仓库（`repos` 字段），每次 merge 只记录对应仓库的 commit。只有当所有仓库都有 merge commit 后，状态才变为 `merged`。
- **状态限制**：Plan 必须处于 `passed` 或 `merging`（部分仓库已合并）状态。

---

## 资源锁机制

### 目的

防止多个 session 同时操作同一 batch，确保并发安全。

### 操作

| 命令 | 行为 |
|------|------|
| `lock acquire --session <id>` | 获取锁，如果已被持有则检查超时 |
| `lock release` | 释放锁 |
| `lock heartbeat` | 更新心跳时间戳 |
| `lock status` | 查看当前锁状态 |

### 超时机制

- **软超时**：10 分钟无心跳。超过软超时后，新 session 可以强制获取锁（带警告）。
- **硬超时**：60 分钟无心跳。超过硬超时后，新 session 直接强制获取锁。

### 锁数据结构

```json
{
  "session": "session-id",
  "acquired_at": "ISO-8601",
  "heartbeat_at": "ISO-8601",
  "soft_timeout_minutes": 10,
  "hard_timeout_minutes": 60
}
```

---

## 状态机

### Batch 阶段流转

```
planning → developing → reviewing → merging → completed
```

- 每次 `batch advance` 推进一步，需满足前置条件。
- `batch complete` 可从任何阶段强制标记完成。

### Plan 状态流转

```
pending → developing → dev_done → reviewing → passed → merged
                                    ↓    ↑
                              fix_in_progress
```

- `pending`：已创建，等待开发
- `developing`：开发中
- `dev_done`：开发完成，等待验收
- `reviewing`：验收中（收到反馈或修复后）
- `fix_in_progress`：修复中（可选中间状态）
- `passed`：验收通过
- `merging`：部分仓库已合并（跨仓库场景的中间状态）
- `merged`：所有仓库合并完成
