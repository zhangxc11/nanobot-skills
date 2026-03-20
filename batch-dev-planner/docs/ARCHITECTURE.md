# batch_dev.py 架构文档

## 整体架构

`batch_dev.py` 是一个纯 CLI 工具，基于 argparse 构建，所有状态持久化为 JSON 文件。无数据库依赖，无网络通信，适合 agent 子进程调用。

```
CLI (argparse)
  ├── batch 子命令   → 管理 batch 生命周期
  ├── plan 子命令    → 管理 plan CRUD 和状态
  ├── review 子命令  → 验收反馈/修复/通过
  ├── merge 子命令   → 合并记录
  ├── status 子命令  → 状态总览 + STATUS.md 输出
  └── lock 子命令    → 资源锁管理
```

---

## 数据存储结构

所有数据存储在 `~/.nanobot/workspace/data/batch-dev/` 下：

```
data/batch-dev/
├── active_batch.json          # 活跃 batch 指针
├── active_batch.lock          # 资源锁
└── batches/
    └── <batch-id>/
        ├── state.json         # batch 状态
        ├── STATUS.md          # 自动生成的状态报告
        └── plans/
            ├── <plan-id>.json # plan 状态
            └── ...
```

### active_batch.json

```json
{
  "batch_id": "batch-20260312"
}
```

空对象 `{}` 表示无活跃 batch。

### state.json（Batch 状态）

```json
{
  "batch_id": "batch-20260312",
  "created_at": "2026-03-12T10:00:00+08:00",
  "stage": "developing",
  "base_commits": {
    "nanobot": "abc123",
    "web-chat": "def456"
  },
  "workdir": "/path/to/dev-workdir",
  "plans": ["plan-a", "plan-b"],
  "plan_order": ["plan-a", "plan-b"],
  "sessions": {
    "planning": null,
    "developing": null,
    "reviewing": null
  }
}
```

- `plans`：plan ID 列表（用于存在性检查）
- `plan_order`：plan 展示顺序（可能与 plans 不同）

### plan-id.json（Plan 状态）

```json
{
  "plan_id": "core-fix",
  "title": "Core Fix",
  "status": "passed",
  "todo_ids": ["t1", "t2"],
  "depends_on": "infra-setup",
  "repos": ["nanobot", "web-chat"],
  "branches": {
    "nanobot": "feat/core-fix",
    "web-chat": "feat/core-fix"
  },
  "dev": {
    "session": "session-123",
    "subagent_id": "sub-456",
    "started_at": "ISO-8601",
    "completed_at": "ISO-8601",
    "commits": ["abc", "def"]
  },
  "review": {
    "session": "session-789",
    "rounds": [
      {
        "round": 1,
        "feedback": "Bug in line 42",
        "fix_commit": "fix1",
        "result": "fixed",
        "created_at": "ISO-8601",
        "fixed_at": "ISO-8601"
      }
    ],
    "passed_at": "ISO-8601"
  },
  "merge": {
    "commits": {
      "nanobot": "merge-abc",
      "web-chat": "merge-def"
    },
    "merged_at": "ISO-8601"
  }
}
```

---

## 锁机制设计

### 文件锁

使用 `active_batch.lock` JSON 文件实现协作锁（非系统级文件锁）。

### 心跳机制

持锁 session 需定期调用 `lock heartbeat` 更新 `heartbeat_at` 时间戳。

### 超时抢占

```
0 ──────── 10min ──────── 60min ────→
  [正常持有]  [软超时:可强制获取]  [硬超时:直接获取]
```

- **< 软超时 (10min)**：拒绝新 session 获取锁。
- **软超时 ~ 硬超时 (10~60min)**：允许强制获取，打印警告。
- **> 硬超时 (60min)**：直接强制获取。

### 锁的局限性

- 非原子操作：JSON 读写之间存在竞态窗口（对 agent 场景可接受）。
- 无自动续期：依赖调用方主动 heartbeat。

---

## Plan 依赖管理

### 依赖声明

每个 Plan 可通过 `depends_on` 字段声明对另一个 Plan 的依赖：

```bash
plan add --title "Feature B" --depends-on "feature-a"
```

### 依赖校验

- `plan add` 时校验 `depends_on` 的 plan_id 必须已存在于当前 batch 中。
- `plan update --depends-on` 同样校验合法性。

### 依赖用途

- 供调度 agent 决定 Plan 的开发顺序。
- `batch advance` 不检查依赖顺序（只检查状态）。

---

## 多仓库支持

### 设计

- Batch 级别：`base_commits` 记录各仓库基点。
- Plan 级别：`repos` 声明涉及哪些仓库，`branches` 记录各仓库分支。

### 跨仓库合并

Plan 可能涉及多个仓库（如 nanobot + web-chat）。merge 命令支持：

1. 每次 `merge --repo <repo> --commit <hash>` 记录一个仓库的合并。
2. Plan 状态在所有声明仓库都有 merge commit 后才变为 `merged`。
3. 部分合并期间，Plan 状态为 `merging`。
4. 如果 Plan 未声明 `repos`（或只有一个仓库），单次 merge 即可完成。

---

## 命令调度

`main()` 函数通过 argparse 的 `dest` 字段做两级分发：

```
command (batch/plan/review/merge/status/lock)
  └── action (create/list/show/add/update/...)
```

每个 handler 函数签名为 `cmd_xxx(args)`，接收 argparse 的 Namespace 对象。

---

## 设计原则

1. **文件即数据库**：所有状态存储为 JSON 文件，便于调试和手动修复。
2. **幂等输出**：`status` 命令同时输出到 stdout 和 STATUS.md。
3. **串行批次**：同一时间只允许一个活跃 batch，避免资源冲突。
4. **宽松验证**：`plan update --status` 不强制状态机流转，允许灵活操作。
5. **Agent 友好**：所有操作通过 CLI 完成，输出可解析的文本格式。
