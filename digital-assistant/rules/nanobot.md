# nanobot 项目规则

<!-- detection_keywords: nanobot, gateway, worker, webserver, scheduler, dispatcher, brain_manager, digital-assistant, skill -->

## 🔴 L0 MUST（违反即失败）

### NANO-001: prod 变更必须先 dev 环境实测通过
所有涉及 nanobot 核心代码的变更，必须在 dev 环境实测通过后才能提交。
- Dev webserver: http://localhost:9081
- Dev worker: 端口 9082
- 启动: `bash ~/.nanobot/bin/nanobot-svc.sh start dev all`
- 停止: `bash ~/.nanobot/bin/nanobot-svc.sh stop dev all`
仅靠 pytest 不能替代 dev 环境实测。

### NANO-002: nanobot 必须在 dev-dir 中开发
nanobot 项目的所有代码变更必须在 dev-workdir（`~/.nanobot/workspace/dev-workdir/nanobot/`）中进行。
禁止直接修改 prod 部署目录下的代码。
禁止误 push: `git remote set-url --push origin DISABLED`

## 🟡 L1 REQUIRED（项目要求）

### NANO-003: feature branch 策略
分支命名: `feat/<描述>`，合并: `merge --no-ff`。
验收通过才 merge，dev_done ≠ 可以 merge。

### NANO-004: 启动 dev gateway 前必须先停 prod gateway
Gateway WebSocket 连接排他，dev/prod 不能同时运行。
切换: `nanobot-svc.sh switch-gw prod dev`
