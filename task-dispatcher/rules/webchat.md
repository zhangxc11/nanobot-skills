# web-chat 项目规则

<!-- detection_keywords: web-chat, webchat, CronPage, frontend, vite, react -->

## 🔴 L0 MUST（违反即失败）

### WEB-001: web-chat 必须在 dev-dir 中开发
web-chat 项目的所有代码变更必须在 dev-workdir（`~/.nanobot/workspace/dev-workdir/web-chat/`）中进行。
禁止直接修改 prod 部署目录（`~/.nanobot/workspace/web-chat/`）下的源代码。

### WEB-002: 前端构建只能在 dev 环境执行
`npm run build`（或 vite build）只能在 dev-workdir 中执行。
prod 的 dist 目录通过 rsync/cp 从 dev 同步，禁止在 prod 目录直接执行构建命令。
- 正确流程: dev-workdir 修改 → dev build → dev 实测 → rsync dist 到 prod
- 禁止流程: 直接在 prod 目录修改代码并 build

## 🟡 L1 REQUIRED（项目要求）

### WEB-003: web-chat 变更需先在 dev 环境实测通过
所有涉及 web-chat 代码的变更，必须在 dev 环境实测通过后才能同步到 prod。
- Dev 环境: http://localhost:9081
- Prod 环境: http://localhost:8081
- 在 dev 环境验证页面功能、交互逻辑正常后，再同步到 prod。
