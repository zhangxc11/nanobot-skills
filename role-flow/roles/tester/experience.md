# Tester — 经验积累

> 本文件记录该角色在实际执行中积累的经验教训，由 Retrospective 回流写入。

---

## 2026-04-07 — T-001/T-002 连续两次测试报告质量不足

- **事件**：T-001 Tester 13/13 AP 全部执行但代码审查发现的 3 个 bug 全部漏检；T-002 Tester AP 全 pass 但报告缺 test_evidence，Architect[测试审查] 被迫给 conditional_pass
- **根因**：
  1. Tester ROLE.md 对输出的约束太弱——只有格式定义，没有"缺什么就 fail"的红线规则
  2. Tester 未读取 Architect 代码审查报告，不知道有哪些已发现的问题需要验证
  3. 测试只做 happy-path，未主动补充边界/异常场景
- **教训**：
  - test_evidence **必须**存在且非空，每条必须有 step_id
  - E2E 步骤必须有实际执行命令和输出，不能只写"通过"
  - Architect 代码审查发现的 issues 是 Tester 的**必验项**——这些是已知 bug，漏验不可接受
  - 主动补充至少 1 个 AP 未覆盖的边界/异常测试
- **修复**：ROLE.md 已增加"必须执行的前置步骤"、"输出验证规则"、"红线规则"和"Verdict 前自检清单"
