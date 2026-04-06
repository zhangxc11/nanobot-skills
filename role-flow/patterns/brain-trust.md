# Pattern: Brain Trust

> 多视角智囊团评审，适用于需要多角度分析和交叉验证的方案评审场景。

## 适用场景

- 重要方案/设计的多视角评审
- 需要不同专业背景的意见交叉验证
- 主 session 中用户需要高质量决策支持
- 不涉及代码开发，纯方案/策略评审

## 角色组合

1. **Architect（方案提出者）** — 产出初始方案
2. **Reviewer R1（第一视角）** — 从技术可行性角度评审
3. **Reviewer R2（第二视角）** — 从风险/边界/遗漏角度评审
4. **Synthesizer（汇总者）** — 综合所有意见，产出最终结论
5. **Auditor（审计者）** — 检查评审过程完整性

> R1、R2 可以是 Architect Review 或其他角色，根据评审主题选择合适的专业视角。

## 流转规则

```
Architect      --[pass]--> R1 + R2（可并行）
Architect      --[fail]--> blocked（任务阻塞）

R1             --[pass]--> Synthesizer（等 R2 也完成）
R1             --[fail]--> Architect（修改方案）

R2             --[pass]--> Synthesizer（等 R1 也完成）
R2             --[fail]--> Architect（修改方案）

Synthesizer    --[pass]--> Auditor
Synthesizer    --[fail]--> 升级人工决策（意见冲突无法调和）

Auditor        --[pass]--> 完成
Auditor        --[fail]--> 指明缺失环节，补充后重审
```

## Cross-Check 覆盖矩阵

| 产出角色 | 产出内容 | Cross-Check 角色 | 检查内容 |
|---------|---------|-----------------|---------|
| Architect | 初始方案 | R1 + R2 | 技术可行性 + 风险边界 |
| R1 | 技术评审意见 | R2 + Synthesizer | 交叉验证 |
| R2 | 风险评审意见 | R1 + Synthesizer | 交叉验证 |
| Synthesizer | 汇总结论 | Auditor | 评审过程完整性 |

> ✅ 所有角色产出均有独立角色 cross-check。Synthesizer 产出由 Auditor 检查。

## 验收标准

- 至少 2 个独立视角完成评审
- 汇总结论明确标注采纳/不采纳每条意见及理由
- Auditor 确认评审过程完整

## ⚠️ ATTENTION: Auditor 要求

此流程完成后**必须执行 Auditor**进行独立审计。跳过 Auditor 的风险由调用方承担。
