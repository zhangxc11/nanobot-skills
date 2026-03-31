# nanobot 开发专用约束

以下约束仅在开发 nanobot 核心仓库时适用。

## Dev 环境

- Dev 任务用 curl 调 dev web-chat API (9081/9082)，fire-and-forget 不用 `--wait`
- spawn subagent 跑的是 prod 环境
- dev-workdir 只能 checkout 切换分支不能 merge
- Claude Code 默认在 prod 目录提交，dev-workdir 需 format-patch + apply 同步
- 启动 dev gateway 前必须先停 prod gateway
- 验证改动时不要搞坏 dev 运行环境
