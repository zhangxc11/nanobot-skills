# Auditor — 经验积累

> 本文件记录该角色在实际执行中积累的经验教训，由 Retrospective 回流写入。

---

## 2026-04-06 — Phase 2 审计遗漏关键环节

- **事件**：Phase 2 审计遗漏了 Architect[代码审查] 和 Architect[测试审查] 两个环节，未发现流程缺失
- **根因**：未读取 dev-pipeline pattern 文件，以调度方 prompt 中不完整的流程描述作为 baseline，导致 baseline 本身就缺少这两个环节
- **教训**：**必须从 pattern 文件获取标准流程**，不能依赖调度方描述。调度方的 prompt 可能本身就有遗漏，只有 pattern 文件才是权威的流程定义
- **修复**：ROLE.md 已增加"必须执行的前置步骤"，强制要求先读取 pattern 文件作为 baseline

## 2026-04-07 — T-001/T-002 审计未充分记录输出合规问题

- **事件**：T-001 Auditor 发现 Tester 测试深度不足但仅记录表象（"Tester 弱"），未具体指出 Tester 输出违反了哪些 ROLE.md 要求（如缺 test_evidence、E2E 无执行记录等）。T-002 独立审计才发现这是多环节联动问题。
- **根因**：Auditor 在记录流程合规问题时不够具体——应对照各角色 ROLE.md 的输出要求，检查输出是否合规，而非笼统描述"质量不足"
- **教训**：
  - 发现某角色输出有问题时，应对照该角色 ROLE.md 的输出格式和验证规则，具体指出违反了哪一条
  - 流程合规检查包括"各角色输出是否符合其 ROLE.md 定义的格式和必填字段"
  - 深层根因分析（如"是 Tester 的问题还是 Architect AP 的问题"）属于 Retrospective 的职责，Auditor 可在 issues 中添加 `related_upstream` 指向性标签提示 Retrospective 关注
- **修复**：Auditor 保持"只查流程不碰质量"定位，但在流程合规检查中更加具体和精确
