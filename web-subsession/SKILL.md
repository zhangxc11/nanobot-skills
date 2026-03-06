---
name: web-subsession
description: 通过 web-chat HTTP API 创建子 session 并发送任务。适用于需要在独立进程中执行操作的场景（如重启服务、长时间后台任务、批量调度）。当前 channel 为 web/cli 时可直接使用 API；gateway channel（飞书/Telegram）也可借此将任务委托给 web-chat worker。
---

# Skill: web-subsession

> 通过 web-chat HTTP API 创建子 session 并委托任务给独立的 worker 进程执行。

## 适用场景

| 场景 | 说明 |
|------|------|
| **重启服务** | 需要 kill 当前宿主进程时，委托给独立 worker 执行 |
| **长时间后台任务** | 不阻塞当前会话，让子 session 异步处理 |
| **跨进程隔离** | 需要在独立进程中执行可能影响当前进程的操作 |
| **批量调度** | 创建多个子 session 并行执行任务（调度 + worker 模式） |

## 前置条件

- **Web-chat 服务必须正在运行**（webserver :8081 + worker :8082）
- 检查方式：`curl -s http://127.0.0.1:8081/api/health`

## Session Key 命名规范

### 前端辨识规则

Web-chat 前端通过 session_key 格式自动分组：

| session_key 格式 | 前端归类 | 说明 |
|-----------------|---------|------|
| `webchat:<纯数字>` | 手动对话 | 如 `webchat:1772696251`（用户手动创建） |
| `webchat:<含非数字>` | 🤖 自动任务 | 如 `webchat:dispatch_xxx`（API 创建） |
| `subagent:<parent>_<8hex>` | 🤖 子任务 | spawn persist 模式自动生成 |

**规则**：`webchat:` 后面是纯数字 → 手动对话；包含非数字字符 → 自动任务子分组（默认折叠）。

### 推荐命名格式

```
webchat:<role>_<parent_ref>_<detail>
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `role` | 角色标识 | `dispatch`, `worker`, `review`, `fix`, `restart` |
| `parent_ref` | 父 session 引用（通常是主 session 的 timestamp） | `1772696251` |
| `detail` | 具体任务标识 | `gen2`, `B8`, `task001` |

### 实际用例参照

**批量测例构造**（batch_build）：

| 角色 | session_key | 显示名称 |
|------|------------|---------|
| 主 session | `webchat:1772696251` | （用户自然创建） |
| 调度 | `webchat:dispatch_1772696251_gen2` | `🔄 调度 gen2 ← 批量测例构造` |
| Worker | `webchat:worker_1772696251_B8` | `🔨 构造 B8: Analytics DB session_key 修复` |
| Review | `webchat:review_1772696251_A6` | `📋 确认 A6 ← 批量测例构造` |

**QA R2 修复**（qa_r2_fix）：

| 角色 | session_key | 显示名称 |
|------|------------|---------|
| 调度 | `webchat:qa_r2_dispatch` | `🔄 QA-R2 调度` |
| Worker | `webchat:qa_r2_fix_task001` | `🔧 修复 task-001: xxx` |

**单次任务**（重启、review 等）：

| 用途 | session_key | 说明 |
|------|------------|------|
| 重启 gateway | `webchat:restart_gateway_auto` | 一次性任务 |
| 代码审查 | `webchat:review_frontend` | — |

### 文件名映射

session_key 中的 `:` 自动替换为 `_` 作为文件名：

```
sessions/
├── webchat_1772696251.jsonl                      # 主 session（手动）
├── webchat_dispatch_1772696251_gen2.jsonl         # 调度
├── webchat_worker_1772696251_B8.jsonl             # worker
```

### 父子关系

前端 Phase 42 支持树形父子展示，数据源优先级：
1. **手动标注**：`session_parents.json`（`{ "子key": "父key" }` 映射）
2. **启发式规则**：`subagent:{parent_sanitized}_{8hex}` 自动提取 parent

> `webchat:` 前缀的子 session 目前需要通过 `session_parents.json` 手动标注父子关系。

## 使用方式

有两种 API 路径，适用于不同场景：

### 路径 A：直接调用 Worker API（推荐用于批量调度）

直接向 worker (端口 8082) 发送 `execute-stream` 请求，可**自定义 session_key**：

```bash
curl -s --max-time 5 -X POST http://localhost:8082/execute-stream \
  -H "Content-Type: application/json" \
  -d '{"session_key": "webchat:worker_1772696251_B8", "message": "请执行..."}' \
  > /dev/null 2>&1 || true
```

**特点**：
- 可完全控制 session_key（符合命名规范）
- `--max-time 5` 让 curl 超时退出，但 worker 已接收任务会继续执行（fire-and-forget）
- 启动后可通过 webserver rename API 设置显示名称：

```bash
sleep 2  # 等 session 文件创建
SESSION_ID=$(echo "webchat:worker_1772696251_B8" | tr ':' '_')
curl -s -X PATCH "http://localhost:8081/api/sessions/${SESSION_ID}" \
  -H "Content-Type: application/json" \
  -d '{"summary": "🔨 构造 B8: Analytics DB session_key 修复"}'
```

### 路径 B：通过 Webserver API（适用于单次任务）

先通过 webserver (端口 8081) 创建 session，再发送消息。session_key 自动生成为 `webchat:{timestamp}`：

```bash
# Step 1: 创建 session
SESSION_RESPONSE=$(curl -s -X POST http://127.0.0.1:8081/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "my-task"}')
SESSION_ID=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Step 2: 发送消息（--max-time 防止 SSE 流无限等待）
curl -s -X POST "http://127.0.0.1:8081/api/sessions/${SESSION_ID}/messages" \
  -H "Content-Type: application/json" \
  -d '{"message": "你的任务指令..."}' \
  --max-time 120 > /dev/null 2>&1 &
```

> ⚠️ 路径 B 生成的 session_key 是 `webchat:{timestamp}`（纯数字），前端会归类为手动对话而非自动任务。如需归入自动任务分组，请使用路径 A。

### 脚本工具

提供了封装脚本，支持两种路径：

```bash
# 路径 A：自定义 session_key（直接调用 worker）
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --session-key "webchat:worker_1772696251_B8" \
  --message "请执行..." \
  --title "🔨 构造 B8: xxx"

# 路径 B：自动生成 session_key（通过 webserver）
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --message "请执行..." \
  --wait 60
```

参数说明：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--session-key` | 否 | 自定义 session_key（使用路径 A）。不指定则使用路径 B |
| `--message` | 是 | 发送给子 session 的任务指令 |
| `--title` | 否 | 显示名称（仅路径 A 有效，设置后通过 rename API 更新） |
| `--port` | 否 | Webserver 端口（默认 8081） |
| `--worker-port` | 否 | Worker 端口（默认 8082，仅路径 A） |
| `--wait` | 否 | 等待完成的超时秒数（默认 0 = fire-and-forget） |
| `--poll-interval` | 否 | 轮询间隔秒数（默认 5，仅 --wait > 0 时有效） |

## 技术说明

- Web-chat worker 是独立进程，不受 gateway 重启影响
- 子 session 中的 agent 拥有完整的工具能力（exec、read_file、write_file 等）
- 子 session 的 agent **可以直接执行 kill、重启等操作**，因为它运行在 worker 进程中，与 gateway 进程隔离
- `execute-stream` 是 SSE 流式接口，会等到 agent 完成才返回；`--max-time` 让 curl 提前断开但不影响 worker 继续执行
- 路径 A 直接调 worker 时，session JSONL 文件由 worker 自动创建（首次执行时）

## 与其他 Skill 的关系

| Skill | 关系 |
|-------|------|
| **restart-gateway** | 飞书/Telegram channel 下，restart-gateway 脚本内部使用路径 B 创建子 session 委托 worker 执行重启 |
| **restart-webchat** | 不需要子 session，restart.sh 使用 double-fork 可以直接执行 |
