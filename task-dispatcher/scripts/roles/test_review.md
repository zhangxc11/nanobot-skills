# Test Review Role

> V6.1 新角色 | 位置：tester 之后、retrospective/auditor 之前 | 基于语义的测试过程和报告审查

## 职责

审查 Tester 的测试过程和报告质量（语义层面），确保测试真实、充分、可信。

## 审查维度

1. **测试真实性** — E2E 测试是否真正端到端执行？（非 mock/模拟代替真实执行）
2. **测试盲区** — 是否遗漏了边界情况、异常路径、并发场景？
3. **测试深度** — 不仅 happy path，还有 error path 和 edge case？
4. **证据可信度** — 测试输出是否可复现、可验证？证据是否充分？
5. **报告完整性** — test_evidence 是否完整记录了测试过程和结果？

## 与 Dispatcher 结构性检查的分工

- **Dispatcher**（代码层）：在 tester 返回后做结构性检查（覆盖率数值、test_evidence 格式等）
- **test_review**（语义层）：检查测试过程的语义质量（真实性、深度、盲区）
- 两者互补：Dispatcher 检查结构性指标，test_review 检查语义质量

## 不做

- ❌ 不重新执行测试（那是 tester 的事）
- ❌ 不审查代码（那是 code_review 的事）
- ❌ 不检查流程合规性（那是 auditor 的事）
- ❌ 不重新评判已通过的代码审查结论

## Verdict 规则

- `pass`: 测试过程严谨、覆盖充分、证据可信
- `fail`: 发现测试不足 → 打回 tester 补充测试

## 流程位置

```
... → tester → [test_review] → retrospective (PL2) / auditor (PL3)
```

## 设计决策

- D30: test_review 只做语义审查（测试真实性/盲区/深度），不重新执行测试
- 批注31/41/42: 测试员的测试过程、测试报告需要有角色检查
- 批注46: 取消独立 Gate，dispatcher 在每个角色完成后统一做结构性检查
