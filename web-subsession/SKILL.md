---
name: web-subsession
description: 通过 web-chat HTTP API 创建子 session 并发送任务。适用于需要在独立进程中执行操作的场景（如重启服务、长时间后台任务）。当前 channel 为 web/cli 时可直接使用 API；gateway channel（飞书/Telegram）也可借此将任务委托给 web-chat worker。
---

# Skill: web-subsession

> 通过 web-chat HTTP API 创建子 session 并委托任务给独立的 worker 进程执行。

## 适用场景

| 场景 | 说明 |
|------|------|
| **重启服务** | 需要 kill 当前宿主进程时，委托给独立 worker 执行 |
| **长时间后台任务** | 不阻塞当前会话，让子 session 异步处理 |
| **跨进程隔离** | 需要在独立进程中执行可能影响当前进程的操作 |
| **批量调度** | 创建多个子 session 并行执行任务 |

## 前置条件

- **Web-chat 服务必须正在运行**（webserver :8081 + worker :8082）
- 检查方式：`curl -s http://127.0.0.1:8081/api/health`

## 使用方式

### 方法 1：使用脚本（推荐）

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --name "task-name" \
  --message "请执行以下操作：..."
```

参数说明：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--name` | 否 | Session 名称（默认 `subsession-auto`） |
| `--message` | 是 | 发送给子 session 的任务指令 |
| `--port` | 否 | Webserver 端口（默认 8081） |
| `--wait` | 否 | 等待完成的超时秒数（默认 0 = fire-and-forget） |
| `--poll-interval` | 否 | 轮询间隔秒数（默认 5，仅 --wait > 0 时有效） |

示例 — fire-and-forget（不等待结果）：

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --name "restart-gateway" \
  --message "请执行 kill 旧 gateway 并用 double-fork 方式重启"
```

示例 — 等待完成（最多 60 秒）：

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --name "code-review" \
  --message "请 review 以下文件的代码：..." \
  --wait 60
```

### 方法 2：Agent 分步 HTTP 调用

如果需要更精细的控制，可以手动调用 API：

#### Step 1: 检查 web-chat 服务

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/api/health
```

#### Step 2: 创建 session

```bash
curl -s -X POST http://127.0.0.1:8081/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "my-task"}'
```

返回 JSON 中的 `id` 字段即为 session ID。

#### Step 3: 发送消息

```bash
curl -s -X POST http://127.0.0.1:8081/api/sessions/{SESSION_ID}/messages \
  -H "Content-Type: application/json" \
  --max-time 120 \
  -d '{"message": "你的任务指令..."}'
```

> ⚠️ API 字段是 `message`（不是 `content`）。
> 使用 `--max-time` 防止 SSE 流无限等待。

#### Step 4: 查询结果（可选）

```bash
curl -s http://127.0.0.1:8081/api/sessions/{SESSION_ID}/messages
```

## 命名规范

子 session 推荐使用有意义的名称，便于在前端辨识：

| 用途 | 推荐命名 | 示例 |
|------|----------|------|
| 重启 gateway | `restart-gateway-auto` | — |
| 批量调度 | `dispatch_<batch_id>` | `dispatch_20260306` |
| 并行 worker | `worker_<task_id>` | `worker_task001` |
| 代码审查 | `review_<target>` | `review_frontend` |

> 前端会自动将非纯数字 session key 归入「🤖 自动任务」分组。

## 技术说明

- Web-chat worker 是独立进程，不受 gateway 重启影响
- 子 session 中的 agent 拥有完整的工具能力（exec、read_file、write_file 等）
- 子 session 的 agent **可以直接执行 kill、重启等操作**，因为它运行在 worker 进程中，与 gateway 进程隔离
- `--max-time` 参数很重要：发送消息的 API 是 SSE 流式响应，不设超时会一直阻塞

## 与其他 Skill 的关系

| Skill | 关系 |
|-------|------|
| **restart-gateway** | restart-gateway 内部使用本 skill 的机制（创建子 session 委托 worker 执行重启） |
| **restart-webchat** | 不需要子 session，因为 restart.sh 使用 double-fork 可以直接执行 |
