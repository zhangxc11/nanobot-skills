---
name: restart-webchat
description: "Restart nanobot Web Chat services (webserver + worker). 支持 prod 和 dev 两套环境。使用 nanobot-svc.sh 统一管理。"
---

# Skill: restart-webchat

> 重启 nanobot Web Chat 服务（webserver + worker），支持 prod / dev 两套环境。

## 背景

Web Chat 由两个独立进程组成：
- **webserver** — API 网关 + 静态文件服务 + SSE 转发
- **worker** — nanobot agent 执行器 + session 记录 + task registry

| 环境 | Webserver 端口 | Worker 端口 |
|------|---------------|-------------|
| prod | 8081 | 8082 |
| dev  | 9081 | 9082 |

代码更新、配置变更后需要重启服务使改动生效。

## 统一管理脚本

```bash
# 路径（symlink）
~/.nanobot/bin/nanobot-svc.sh
# 实际位置
~/Documents/code/workspace/nanobot/scripts/nanobot-svc.sh
```

## 使用方式

### 重启全部服务（最常用）

```bash
# Prod 环境
bash ~/.nanobot/bin/nanobot-svc.sh restart prod all

# Dev 环境
bash ~/.nanobot/bin/nanobot-svc.sh restart dev all
```

> ⚠️ `restart all` 会重启 gateway + webserver + worker。如果只想重启 web-chat，分别重启 worker 和 webserver。

### 仅重启 webserver

```bash
bash ~/.nanobot/bin/nanobot-svc.sh restart prod webserver
bash ~/.nanobot/bin/nanobot-svc.sh restart dev webserver
```

### 仅重启 worker

```bash
bash ~/.nanobot/bin/nanobot-svc.sh restart prod worker
bash ~/.nanobot/bin/nanobot-svc.sh restart dev worker
```

### 停止服务

```bash
bash ~/.nanobot/bin/nanobot-svc.sh stop prod worker
bash ~/.nanobot/bin/nanobot-svc.sh stop prod webserver
```

### 查看服务状态

```bash
bash ~/.nanobot/bin/nanobot-svc.sh status prod
bash ~/.nanobot/bin/nanobot-svc.sh status dev
```

## 健康检查

`nanobot-svc.sh` 启动 worker/webserver 后自动执行：
- **HTTP 健康端点验证**：`/health`（worker）、`/api/health`（webserver），最多等 15 秒
- **进程年龄验证**：确认响应来自新启动的进程，而非旧进程占着端口

## 前端重新构建

如果修改了前端代码，需要先构建再重启 webserver：

```bash
cd ~/.nanobot/workspace/web-chat/frontend && npm run build
bash ~/.nanobot/bin/nanobot-svc.sh restart prod webserver
```

## 自杀保护与应对策略

`nanobot-svc.sh` 内置自杀保护：当 `NANOBOT_PORT` 环境变量 == 目标端口时，脚本 **REFUSED** 并返回 exit code 1。

**识别方法**：输出包含 `REFUSED` 关键字，exit code 非 0。示例：
```
❌ REFUSED: Cannot stop prod worker (port 8082) — this is our own process!
❌ Self-kill protection triggered. NANOBOT_PORT=8082 matches target port.
```

### 应对策略速查

| 你在哪 | 想操作什么 | 结果 | 应对方式 |
|--------|-----------|------|---------|
| prod worker (8082) | restart prod worker | ✅ REFUSED | 通过 gateway 或 dev worker 来重启 |
| prod worker (8082) | restart dev worker | ❌ 安全 | 直接执行 |
| prod worker (8082) | restart prod webserver | ❌ 安全 | 直接执行 |
| dev worker (9082) | restart dev worker | ✅ REFUSED | 通过 gateway 或 prod worker 来重启 |
| dev worker (9082) | restart prod worker | ❌ 安全 | 直接执行 |
| gateway (18790) | restart prod worker | ❌ 安全 | 直接执行 |
| gateway (18790) | restart prod webserver | ❌ 安全 | 直接执行 |

> **Worker 重启自己会 REFUSED**：需要从其他进程发起。例如 prod worker 想重启自己，可以通过 gateway channel 或 dev worker 执行 `nanobot-svc.sh restart prod worker`。

### 通用原则

- 看到 `REFUSED` → **不要重试同样的命令**
- 自杀 REFUSED → 委托给其他环境的进程执行（spawn subagent 到另一个 worker，或通过 gateway channel）
- 重启 webserver 不受自杀保护影响（worker 和 webserver 是不同进程/端口）

## ⚠️ 注意事项

1. **Web Chat worker 是当前 session 的宿主进程**：从 web-chat 发起重启 worker 会导致**当前任务中断**，但脚本使用 daemonize，重启后新 worker 会立即接管
2. **自杀保护**：`nanobot-svc.sh` 检测到目标端口是自身进程时会拒绝操作
3. **重启不影响 gateway**：webserver/worker 与 gateway 是独立进程（除非用 `restart all`）
4. **重启后 SSE 流会断开**：前端会自动重连

## 典型场景

| 场景 | 操作 |
|------|------|
| nanobot 核心代码更新 | `nanobot-svc.sh restart prod worker`（worker 加载新代码） |
| 前端代码修改 | `npm run build` + `nanobot-svc.sh restart prod webserver` |
| webserver.py / worker.py 修改 | `nanobot-svc.sh restart prod worker` + `restart prod webserver` |
| 配置文件变更 | `nanobot-svc.sh restart prod all` |
| 排查问题 | `nanobot-svc.sh status prod` + 查看日志 |

## 日志查看

```bash
# Prod 日志
tail -50 ~/.nanobot/logs/webserver.log
tail -50 ~/.nanobot/logs/worker.log

# Dev 日志
tail -50 ~/.nanobot/logs-dev/webserver.log
tail -50 ~/.nanobot/logs-dev/worker.log

# 服务管理审计日志
tail -50 ~/.nanobot/logs/nanobot-svc.log
```
