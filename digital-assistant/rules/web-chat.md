# web-chat 项目规则

<!-- detection_keywords: web-chat, webchat, frontend, 前端, typescript, react, vite, chat-ui, web_chat -->

## 🟡 L1 REQUIRED（项目要求）

### WEB-001: 前端改动必须浏览器实测+截图
涉及 UI 的改动必须在浏览器中实际加载并验证。
使用 browser skill 截图作为验收证据。
纯样式/布局改动也需要截图确认。

### WEB-002: TypeScript 类型检查
前端代码必须通过 TypeScript 类型检查（`npx tsc --noEmit`）。
不允许使用 `@ts-ignore` 或 `any` 绕过类型检查（除非有充分理由并在报告中说明）。
