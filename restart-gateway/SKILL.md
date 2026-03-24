---
name: restart-gateway
description: "仅限飞书/Telegram 等 gateway channel 使用：因为这些 channel 的 agent 运行在 gateway 进程内，不能直接 kill 自己，需要通过 web-chat 子 session 间接重启。⚠️ 如果当前 channel 是 web 或 cli，请忽略此 skill，直接用 nanobot-svc.sh 即可。"
---

# Skill: restart-gateway

> ⚠️ **本 skill 仅适用于飞书/Telegram 等 gateway channel。**
>
> 如果当前 channel 是 **web** 或 **cli**，**不需要此 skill**，直接用 `nanobot-svc.sh` 即可。

## 为什么需要区分？

| 当前 channel | Agent 宿主进程 | 能否直接 kill gateway？ |
|-------------|---------------|----------------------|
| **飞书/Telegram** | gateway 进程 | ❌ 不能（kill gateway = 自杀） |
| **web / cli** | worker 或 CLI 进程 | ✅ 可以（与 gateway 进程隔离） |

## 统一管理脚本

所有服务管理操作统一使用 `nanobot-svc.sh`：

```bash
# 路径（symlink）
~/.nanobot/bin/nanobot-svc.sh
# 实际位置
~/Documents/code/workspace/nanobot/scripts/nanobot-svc.sh
```

## 直接重启（web / cli channel）

```bash
# 重启 prod gateway
bash ~/.nanobot/bin/nanobot-svc.sh restart prod gateway

# 重启 dev gateway
bash ~/.nanobot/bin/nanobot-svc.sh restart dev gateway

# 停止 / 启动 / 查看状态
bash ~/.nanobot/bin/nanobot-svc.sh stop prod gateway
bash ~/.nanobot/bin/nanobot-svc.sh start prod gateway
bash ~/.nanobot/bin/nanobot-svc.sh status prod gateway
```

功能：
- **Gateway 互斥**：dev/prod gateway 不能同时运行（共享飞书 WebSocket 连接）
- **自杀保护**：不会 kill 自身所在进程
- **审计日志**：`~/.nanobot/logs/nanobot-svc.log`
- **PID 文件管理**：`~/.nanobot/run/{env}-gateway-{port}.pid`

## 间接重启（飞书/Telegram channel 专用）

原理：通过 web-subsession skill 创建子 session → agent 在 worker 进程中执行 `nanobot-svc.sh restart` → 轮询验证 PID 变更。

```bash
# 在子 session 中执行：
bash ~/.nanobot/bin/nanobot-svc.sh restart prod gateway
```

## Gateway 切换（dev ↔ prod）

```bash
# 从 prod 切换到 dev
bash ~/.nanobot/bin/nanobot-svc.sh switch-gw prod dev

# 从 dev 切换回 prod
bash ~/.nanobot/bin/nanobot-svc.sh switch-gw dev prod
```

功能：
- 后台执行（nohup + disown）
- 文件锁防止并发切换
- 自动停止源 gateway → 启动目标 gateway

## 自杀保护与应对策略

`nanobot-svc.sh` 内置自杀保护：当 `NANOBOT_PORT` 环境变量 == 目标端口时，脚本 **REFUSED** 并返回 exit code 1。

**识别方法**：输出包含 `REFUSED` 关键字，exit code 非 0。示例：
```
❌ REFUSED: Cannot stop prod gateway (port 18790) — this is our own process!
❌ Self-kill protection triggered. NANOBOT_PORT=18790 matches target port.
```

### 应对策略速查

| 你在哪 | 想操作什么 | 结果 | 应对方式 |
|--------|-----------|------|---------|
| gateway (18790) | restart prod gateway | ✅ REFUSED | 用 web-subsession 间接重启（让 worker 进程代执行） |
| gateway (18790) | restart prod worker | ❌ 安全 | 直接执行 |
| gateway (18790) | restart prod webserver | ❌ 安全 | 直接执行 |
| prod worker (8082) | restart prod gateway | ❌ 安全 | 直接执行 |
| dev worker (9082) | restart prod gateway | ❌ 安全 | 直接执行 |

> **Gateway channel（飞书/Telegram）重启 gateway 必须走间接重启**：因为 agent 运行在 gateway 进程内，直接执行一定触发自杀保护。用 web-subsession 创建子 session，让 worker 进程代为执行 `nanobot-svc.sh restart prod gateway`。

### Gateway 互斥 REFUSED

这不是自杀保护，而是**互斥保护**：dev/prod gateway 不能同时运行。示例：
```
❌ REFUSED: Cannot start dev gateway — prod gateway is running (PID: 74735)!
❌ Stop prod gateway first: nanobot-svc.sh stop prod gateway
```

**应对**：先 stop 另一环境的 gateway，再 start/restart。或直接用 `switch-gw` 命令一步完成。

### 通用原则

- 看到 `REFUSED` → **不要重试同样的命令**
- 自杀 REFUSED → 委托给其他进程执行（web-subsession 间接重启）
- 互斥 REFUSED → 先 stop 冲突的 gateway，或用 `switch-gw`

## ⚠️ 重启后 session 断连与 subagent 丢失

**关键提醒**：间接重启 gateway 成功后，**当前 gateway 进程会被 kill**，导致：
- 当前飞书/Telegram session **短暂断开**（新 gateway 进程启动后自动重连）
- 正在运行的 **subagent 状态可能丢失**（subagent 跑在被 kill 的 gateway 进程中）

**正确做法**：
1. **重启前/重启后都用 `nanobot-svc.sh status prod gateway` 确认** — 检查 PID 是否变化、Uptime 是否重置
2. **如果怀疑已经发生过重启（如 subagent 状态丢失），先 `status` 确认再决定是否重试** — 不要盲目重复操作
3. 间接重启可以用 subagent 或主 session 发起，但要预期 subagent 可能因 gateway 重启而丢失状态

## ⚠️ 技术要点

1. **Gateway 互斥**：只能有一个 gateway 运行（dev 或 prod），`nanobot-svc.sh` 自动检查
2. **`nanobot gateway` 没有 `--daemonize` 选项**，`nanobot-svc.sh` 内部使用 nohup + disown 后台启动
3. **exec 工具可以执行 bash 脚本文件**，脚本内部的 `&` 不受限制

## 前置条件

- **直接重启**：无特殊要求
- **间接重启**：Web-chat 服务必须正在运行（webserver + worker）

## 注意事项

- 重启 gateway 会导致所有 IM channel（飞书、Telegram）**短暂中断**（约 5-10 秒）
- 重启后 IM channel 的对话上下文会丢失（agent 从 session 文件重新加载历史）
- 间接重启会在 web-chat 中创建一个临时 session，可事后清理
