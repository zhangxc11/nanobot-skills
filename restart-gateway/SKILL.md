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
