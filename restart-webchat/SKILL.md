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
