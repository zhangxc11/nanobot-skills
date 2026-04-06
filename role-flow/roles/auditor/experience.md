# Auditor — 经验积累

> 本文件记录该角色在实际执行中积累的经验教训，由 Retrospective 回流写入。

---

## 2026-04-06 — Phase 2 审计遗漏关键环节

- **事件**：Phase 2 审计遗漏了 Architect[代码审查] 和 Architect[测试审查] 两个环节，未发现流程缺失
- **根因**：未读取 dev-pipeline pattern 文件，以调度方 prompt 中不完整的流程描述作为 baseline，导致 baseline 本身就缺少这两个环节
- **教训**：**必须从 pattern 文件获取标准流程**，不能依赖调度方描述。调度方的 prompt 可能本身就有遗漏，只有 pattern 文件才是权威的流程定义
- **修复**：ROLE.md 已增加"必须执行的前置步骤"，强制要求先读取 pattern 文件作为 baseline
