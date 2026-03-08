---
name: restart-gateway
description: "仅限飞书/Telegram 等 gateway channel 使用：因为这些 channel 的 agent 运行在 gateway 进程内，不能直接 kill 自己，需要通过 web-chat 子 session 间接重启。⚠️ 如果当前 channel 是 web 或 cli，请忽略此 skill，直接执行 kill + double-fork 重启即可。"
---

# Skill: restart-gateway

> ⚠️ **本 skill 仅适用于飞书/Telegram 等 gateway channel。**
>
> 如果当前 channel 是 **web** 或 **cli**，**不需要此 skill**，直接用 `restart_gateway_direct.sh` 即可。

## 为什么需要区分？

| 当前 channel | Agent 宿主进程 | 能否直接 kill gateway？ |
|-------------|---------------|----------------------|
| **飞书/Telegram** | gateway 进程 | ❌ 不能（kill gateway = 自杀） |
| **web / cli** | worker 或 CLI 进程 | ✅ 可以（与 gateway 进程隔离） |

## 直接重启（web / cli channel）

```bash
bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway_direct.sh restart
bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway_direct.sh stop
bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway_direct.sh status
```

功能：
- **健壮进程发现**：`pgrep -f "nanobot gateway"` + 过滤确认是 Python 进程
- **SIGTERM → SIGKILL 兜底**：2 秒后强制 kill
- **Python double-fork 后台启动**：不依赖 `&` 或 `nohup`
- **启动验证**：等待进程存活 ≥3 秒确认稳定（非立即崩溃），最长等 15 秒
- **日志**：`/tmp/nanobot-gateway.log`（append 模式）

## 间接重启（飞书/Telegram channel 专用）

```bash
bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway.sh
```

原理：创建 web-chat 子 session → agent 在 worker 进程中执行 `restart_gateway_direct.sh` → 轮询验证 PID 变更。

## ⚠️ 技术要点

1. **`nanobot gateway` 没有 `--daemonize` 选项**，不要尝试
2. **exec 工具禁止命令行直接包含 `&`**，不能用 `nohup ... &`
3. **必须用 Python double-fork 方式后台启动**
4. **exec 工具可以执行 bash 脚本文件**，脚本内部的 `&` 不受限制

## 前置条件

- **直接重启**：无特殊要求
- **间接重启**：Web-chat 服务必须正在运行（webserver :8081 + worker :8082）

## 注意事项

- 重启 gateway 会导致所有 IM channel（飞书、Telegram）**短暂中断**（约 5-10 秒）
- 重启后 IM channel 的对话上下文会丢失（agent 从 session 文件重新加载历史）
- 间接重启会在 web-chat 中创建一个临时 session（`restart-gateway-auto`），可事后清理
