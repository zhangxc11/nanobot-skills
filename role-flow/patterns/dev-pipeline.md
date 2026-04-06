# Pattern: Dev Pipeline

> 标准多角色开发流水线，适用于需要设计、实现、测试、审计的完整开发任务。

## 适用场景

- 中等到复杂的开发任务
- 需要架构设计和验收方案的功能开发
- 需要多角色交叉检查保证质量的场景
- Dispatcher 自动调度或主 session 手动执行

## 角色组合

1. **Architect** — 规则裁决 + 方案设计 + 验收方案 + 代码审查 + 测试审查
2. **Architect Review** — 评审架构完整性和可行性
3. **Developer** — 代码实现 + 单元测试 + 文档
4. **Tester** — 执行验收测试
5. **Auditor** — 流程完整性审计（只查流程，不碰质量）
6. **Retrospective** — 经验沉淀 + 改进建议（面向未来，不阻塞交付）

## 流转规则

```
Architect    --[pass]--> Architect Review
Architect    --[fail]--> blocked（任务阻塞）

Arch Review  --[pass]--> Developer
Arch Review  --[fail]--> Architect（修改方案）

Developer    --[pass]--> Architect[代码审查]
Developer    --[fail]--> Developer（重试）

Architect[代码审查] --[pass]--> Tester
Architect[代码审查] --[fail]--> Developer（修复代码）

Tester       --[pass]--> Architect[测试审查]
Tester       --[fail]--> Developer（修复问题）

Architect[测试审查] --[pass]--> Auditor → Retrospective
Architect[测试审查] --[fail]--> Tester（补充测试）
```

## Cross-Check 覆盖矩阵

| 产出角色 | 产出内容 | Cross-Check 角色 | 检查内容 |
|---------|---------|-----------------|---------|
| Architect | 设计方案 + 验收方案 | Architect Review | 架构完整性、技术可行性 |
| Developer | 代码 + 单元测试 | Architect[代码审查] | 架构一致性、测试覆盖 |
| Tester | 测试报告 + 证据 | Architect[测试审查] | 测试真实性、深度、盲区 |
| 整体流程 | 调度路径 + 环节完整性 | Auditor | 流程完整性、环节是否缺失 |
| 整体流程 | 经验教训 | Retrospective | 经验教训、改进建议 |

> ✅ 所有角色产出均有独立角色 cross-check，满足约束 1。Architect 对任务全貌最清楚，承担代码和测试的审查职责。

## 验收标准

- 所有角色阶段 verdict=pass（Architect 设计/代码审查/测试审查、Architect Review、Developer、Tester）
- Auditor 确认流程完整、环节无缺失
- Retrospective 沉淀经验教训（可选）
- 文档三件套完整、测试证据充分

## ⚠️ ATTENTION: Auditor 要求

此流程完成后**必须执行 Auditor → Retrospective**进行独立审计。跳过 Auditor 的风险由调用方承担。
