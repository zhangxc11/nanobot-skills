# batch_dev.py 开发日志

---

## Phase 1: 初始实现 ✅

**状态**: 已完成

### 实现内容

- [x] Batch 生命周期管理（create/list/show/advance/complete）
- [x] 串行批次保护（同时只允许一个活跃 batch）
- [x] Plan CRUD（add/list/show/update/add-todo）
- [x] Plan 依赖声明（`--depends-on`）
- [x] 多仓库支持（repos、branches）
- [x] 验收流程（review add/fix/pass，多轮支持）
- [x] 合并记录（merge，支持指定 repo）
- [x] 状态总览（status，同时输出 STATUS.md）
- [x] 资源锁机制（acquire/release/heartbeat/status，软硬超时）
- [x] Batch 阶段推进前置条件检查
- [x] 完整单元测试覆盖

### 数据结构

- JSON 文件存储，目录结构：`data/batch-dev/batches/<id>/state.json` + `plans/<id>.json`
- 锁文件：`data/batch-dev/active_batch.lock`

---

## Phase 2: E2E 验收 Bug 修复 ✅

**状态**: 已完成
**日期**: 2026-03-15

### Bug 1: `plan add --depends-on` 不校验依赖 ID 合法性（中）

**现象**: `--depends-on plan-001` 传了不存在的 ID 也能成功添加。

**修复**: 在 `cmd_plan_add()` 中添加校验——检查 `depends_on` 的 plan_id 是否存在于当前 batch 的 `plans` 列表中，不存在则报错退出。

**变更**: `batch_dev.py` — `cmd_plan_add()` 函数

### Bug 2: `plan update` 缺少 `--depends-on` 参数（低）

**现象**: 无法通过 CLI 修改已有 Plan 的依赖关系。

**修复**:
1. 在 argparse 的 `plan update` 子命令中添加 `--depends-on` 可选参数。
2. 在 `cmd_plan_update()` 中处理该参数，包含合法性校验（空字符串清除依赖、非空值校验存在性）。

**变更**: `batch_dev.py` — `build_parser()` + `cmd_plan_update()` 函数

### Bug 3: 跨仓库 merge 只能记录一次（高）

**现象**: Plan 涉及 nanobot + web-chat 两个仓库，第一次 `merge --repo nanobot` 成功后状态变为 `merged`，第二次 `merge --repo web-chat` 被拒绝（"只有 passed 状态可合并"）。

**修复**:
1. merge 命令允许 `passed` 和 `merging` 状态的 Plan 执行合并。
2. 每次 merge 只记录对应仓库的 commit。
3. 判断 Plan 的 `repos` 字段——如果所有声明仓库都有 merge commit，则状态变为 `merged`；否则状态变为 `merging`（新增中间状态）。
4. 如果 Plan 未声明 repos 或只有单仓库，行为不变（单次 merge 即完成）。

**变更**: `batch_dev.py` — `cmd_merge()` 函数

### 新增测试

- `test_plan_add_invalid_depends_on_fails`: 验证不存在的 depends-on 被拒绝
- `test_plan_update_depends_on`: 验证 plan update --depends-on 能正确修改依赖
- `test_merge_multi_repo_partial_then_complete`: 验证跨仓库分两次 merge 都能成功
