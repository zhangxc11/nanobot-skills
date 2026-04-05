# 标准开发规则

<!-- detection_keywords: standard-dev -->

## 🔴 L0 MUST（违反即失败）

### STD-002: 文档三件套随代码同步更新
REQUIREMENTS.md、ARCHITECTURE.md、DEVLOG.md 随代码变更同步更新。
文档从原始设计提取，不从代码反推。
⚠️ 调度器会检查文档完整性，缺少文档的报告会被打回。

### STD-005: 已知项目必须在 dev-dir 中开发
对于已配置 dev-workdir 的项目，所有代码变更必须在 dev-dir（开发目录）中进行，禁止直接修改 prod 部署目录。
已知有 dev-workdir 的项目：
- **nanobot**: `~/.nanobot/workspace/dev-workdir/nanobot/`（详见 nanobot.md NANO-002）
- **web-chat**: `~/.nanobot/workspace/dev-workdir/web-chat/`（详见 webchat.md WEB-001）

## 🟢 L2 RECOMMENDED（最佳实践）

### STD-001: 设计→开发→验收三阶段流程
遵循设计（文档先行）→ 开发（按 checklist 推进）→ 验收（对照 checklist 验证）的标准流程。

### STD-003: 自验 cases 从需求文档提取
验收场景和测试数据必须从关联的需求文档中提取，不能自行编造。
如果需求文档中有具体的验收场景或测试数据，必须覆盖。

### STD-004: 使用 claude-code skill 执行开发
subagent 执行具体开发时应调用 claude-code skill，而非自己直接用 read_file/edit_file 改代码。

### DEF-001: 修改前先读取文件确认当前内容
修改任何文件前必须先 read_file 确认当前内容，避免基于过时假设修改。

### DEF-002: 每个逻辑单元独立 commit
每个逻辑变更独立 commit，message 格式: `type(T-XXXXXXXX-NNN): 描述`。
type 取值: `feat | fix | docs | refactor | test | chore | ci`。
Task ID 必须包含在 commit message 中（由 commit-msg hook 强制校验）。
不要把多个不相关的改动混在一个 commit 中。
豁免: Merge/Revert/Initial commit 自动跳过；紧急情况可用 `git commit --no-verify` 绕过。

### VER-001: Review 实现而非重新实现
Tester 验收时应 review 现有实现，而非重新实现一遍。
验收基于代码审查 + 功能测试，不需要重写代码。
