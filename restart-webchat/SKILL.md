---
name: restart-webchat
description: Restart nanobot Web Chat services (webserver + worker). Use when code is updated, config changed, or services need restarting. Provides restart.sh commands for all/webserver/worker/stop/status.
---

# Skill: restart-webchat

> 重启 nanobot Web Chat 服务（webserver + worker）

## 背景

Web Chat 由两个独立进程组成：
- **webserver.py** (:8081) — API 网关 + 静态文件服务 + SSE 转发
- **worker.py** (:8082) — nanobot agent 执行器 + session 记录 + task registry

代码更新、配置变更后需要重启服务使改动生效。

## 文件位置

| 项目 | 路径 |
|------|------|
| 服务目录 | `~/.nanobot/workspace/web-chat/` |
| 重启脚本 | `~/.nanobot/workspace/web-chat/restart.sh` |
| Webserver 日志 | `/tmp/nanobot-webserver.log` |
| Worker 日志 | `/tmp/nanobot-worker.log` |
| 前端源码 | `~/.nanobot/workspace/web-chat/frontend/` |
| 前端构建产物 | `~/.nanobot/workspace/web-chat/frontend/dist/` |

## 使用方式

### 重启全部服务（最常用）

```bash
bash ~/.nanobot/workspace/web-chat/restart.sh all
```

### 仅重启 webserver

```bash
bash ~/.nanobot/workspace/web-chat/restart.sh webserver
```

### 仅重启 worker

```bash
bash ~/.nanobot/workspace/web-chat/restart.sh worker
```

### 停止全部服务

```bash
bash ~/.nanobot/workspace/web-chat/restart.sh stop
```

### 查看服务状态

```bash
bash ~/.nanobot/workspace/web-chat/restart.sh status
```

## 前端重新构建

如果修改了前端代码，需要先构建再重启 webserver：

```bash
cd ~/.nanobot/workspace/web-chat/frontend && npm run build
bash ~/.nanobot/workspace/web-chat/restart.sh webserver
```

## ⚠️ 注意事项

1. **Web Chat worker 是当前 session 的宿主进程**：从 web-chat 发起重启 worker 会导致**当前任务中断**，但脚本使用 `--daemonize`（double-fork），重启后新 worker 会立即接管
2. **exec 工具可以直接执行 `restart.sh`**：脚本内部的 `&` 和后台操作不受 exec 工具限制
3. **重启不影响 gateway**：webserver/worker 与 gateway（飞书/Telegram）是独立进程
4. **重启后 SSE 流会断开**：前端会自动重连

## 典型场景

| 场景 | 操作 |
|------|------|
| nanobot 核心代码更新 | `restart.sh all`（worker 加载新代码） |
| 前端代码修改 | `npm run build` + `restart.sh webserver` |
| webserver.py / worker.py 修改 | `restart.sh all` |
| 配置文件变更 | `restart.sh all` |
| 排查问题 | `restart.sh status` + 查看日志 |

## 日志查看

```bash
# Webserver 日志
tail -50 /tmp/nanobot-webserver.log

# Worker 日志
tail -50 /tmp/nanobot-worker.log
```
