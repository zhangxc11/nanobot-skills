# 批量开发规则

<!-- detection_keywords: batch-dev, batch, 批量开发, plan, stage -->

## 🟡 L1 REQUIRED（项目要求）

### BAT-001: 所有 Plan 必须走 feature branch
每个 Plan 在独立的 feature branch 上开发。
分支命名: `feat/batch-YYYYMMDD-plan-{name}`，合并: `merge --no-ff`。

### BAT-002: dev_done 后不要 merge
开发完成标记 dev_done 后不要立即 merge。
merge 在验收阶段通过后才执行。

### BAT-003: 主 session 只做调度，不直接改代码
主调度 session 只负责统筹调度和状态跟踪。
所有代码修改必须通过 spawn subagent 执行。
判断标准：如果需要 grep/read_file 超过 2 次，就应该 spawn subagent。
