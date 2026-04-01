# 全局规则

<!-- detection_keywords: -->

## 🔴 L0 MUST（违反即失败）

### G-001: 禁止自动重启 prod 环境服务
任何 prod 环境的服务重启必须人工确认。
需要重启 prod 时，在报告中标注 verdict: blocked，说明原因。
Dev 环境可自由操作。

### G-002: Worker 必须写入结构化报告
Worker 完成工作后必须写入 JSON 格式的报告文件。
报告是调度器了解工作结果的唯一渠道，不写报告 = 调度器无法继续流程。
报告必须包含 task_id、role、verdict、summary 四个必填字段。

### G-003: 验收必须基于真实执行
验收测试必须基于真实的代码执行结果，不能纯靠 mock 或假设。
单元测试可以 mock 外部依赖，但集成验收必须端到端运行。
仅靠 pytest 通过不能替代实际功能验证。

### G-004: 不修改不相关的代码和文件
只修改与当前任务直接相关的文件。
发现超出任务范围的问题，记入报告 issues 中，不当场修复。
禁止"顺手"重构不相关的代码。

### G-006: standard-dev 任务必须有设计文档
standard-dev 模板的任务在派发 developer 之前，必须有设计文档（architect 报告、design_ref、或 ARCHITECTURE.md）。
没有设计文档的任务会被自动重定向到 architect 流程。
紧急修复（emergency=true）可豁免，但必须事后补文档。

## 🟢 L2 RECOMMENDED（推荐实践）

### G-005: Session 结束校验（INBOX 需求捕获）
在 session 即将结束（用户长时间不回复 / 明确结束语 / 话题自然收尾）时，
回顾本 session 内容，逐项检查：

- □ 用户是否提出了新需求/想法？→ 已注册 Task 或写入 INBOX？
- □ 是否有执行到一半的工作？→ 已记录待跟进？
- □ 是否有讨论中达成的结论？→ 已记录？
- □ 是否有"等 XX 之后再做"的事项？→ 已写入 INBOX？

发现遗漏时，写入 INBOX：
```bash
cd ~/.nanobot/workspace
python3 scripts/inbox_helper.py append \
  --source "session/{当前session标识}" \
  --type user_intent \
  --summary "一句话描述捕获的需求/想法" \
  --priority normal
```

注意：纯聊天、已经正式注册为 Task 的需求不需要重复写入 INBOX。
只捕获"非正式但值得跟进"的需求和想法。
