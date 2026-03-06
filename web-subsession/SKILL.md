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

### ⚠️ 父子命名规则（强制要求）

通过路径 A 创建的子 session，**必须**在 session_key 中包含父 session 引用，以建立父子关系。

**命名格式**：

```
webchat:<role>_<parent_ref>_<detail>
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `role` | 角色标识 | `dispatch`, `worker`, `fix`, `restart` |
| `parent_ref` | 父 session 的 timestamp（从父 session_key 中提取） | `1772696251` |
| `detail` | 具体任务标识 | `gen1`, `task003`, `gateway` |

**示例**：假设主 session 是 `webchat:1772696251`

| 角色 | session_key | 说明 |
|------|------------|------|
| 主 session | `webchat:1772696251` | 用户手动创建 |
| 调度 | `webchat:dispatch_1772696251_gen1` | 第 1 代调度 |
| Worker | `webchat:worker_1772696251_task003` | 执行 task-003 |
| 修复 | `webchat:fix_1772696251_task010` | 修复 task-010 |

**多层嵌套**：如果调度 session 启动的 Worker 需要进一步创建子 session，parent_ref 仍然使用**根 session 的 timestamp**，保持扁平化管理。

### 父子关系识别

前端通过启发式规则**自动识别**父子关系，无需手动注册：

- `webchat:<role>_<10位timestamp>_<detail>` → 提取 timestamp → 在所有已加载 session 中搜索以 `:<timestamp>` 结尾的 session 作为父节点
- **支持跨通道**：父 session 可以是 `webchat:xxx`、`cli:xxx`、`feishu.lab:xxx` 等任意通道
- 例：`webchat:dispatch_1772603563_gen1` 自动识别父 session 为 `cli:1772603563`（如果该 session 存在）

**前提**：命名必须严格遵循上述格式，timestamp 必须是 10 位数字且对应真实存在的父 session。

> 如果启发式规则无法覆盖（如命名不规范的历史 session），可通过 `PUT /api/sessions/parents` 手动注册作为兜底。

### 前端辨识规则

Web-chat 前端通过 session_key 格式自动分组：

| session_key 格式 | 前端归类 | 说明 |
|-----------------|---------|------|
| `webchat:<纯数字>` | 手动对话 | 如 `webchat:1772696251`（用户手动创建） |
| `webchat:<含非数字>` | 🤖 自动任务 | 如 `webchat:dispatch_1772696251_gen1`（API 创建） |
| `subagent:<parent>_<8hex>` | 🤖 子任务 | spawn persist 模式自动生成，启发式规则自动识别父子 |

### 父子关系数据源（优先级递减）

1. **映射文件** `session_parents.json`：通过 `PUT /api/sessions/parents` 手动注册（兜底）
2. **启发式规则 A**：`subagent:` 前缀自动识别（spawn persist 模式）
3. **启发式规则 B**：`webchat:<role>_<10位timestamp>_<detail>` 自动识别，跨通道搜索父 session

> 只要命名符合规范，父子关系**自动生效**，支持跨通道（webchat/cli/feishu 均可作为父 session）。

### 文件名映射

session_key 中的 `:` 自动替换为 `_` 作为文件名：

```
sessions/
├── webchat_1772696251.jsonl                        # 主 session
├── webchat_dispatch_1772696251_gen1.jsonl           # 调度
├── webchat_worker_1772696251_task003.jsonl          # Worker
```

## 使用方式

### 路径 A：直接调用 Worker API（推荐，强制命名规则）

直接向 worker (端口 8082) 发送 `execute-stream` 请求，**自定义 session_key**：

```bash
# 1. 启动子 session（fire-and-forget）
curl -s --max-time 5 -X POST http://localhost:8082/execute-stream \
  -H "Content-Type: application/json" \
  -d '{"session_key": "webchat:worker_1772696251_task003", "message": "请执行..."}' \
  > /dev/null 2>&1 || true

# 2. 设置显示名称（等 session 文件创建后）
sleep 2
SESSION_ID="webchat_worker_1772696251_task003"
curl -s -X PATCH "http://localhost:8081/api/sessions/${SESSION_ID}" \
  -H "Content-Type: application/json" \
  -d '{"summary": "🔨 构造 task-003"}'

# 父子关系由前端启发式规则自动识别，无需手动注册
```

**特点**：
- 可完全控制 session_key（必须符合命名规范）
- `--max-time 5` 让 curl 超时退出，但 worker 已接收任务会继续执行
- 必须注册父子关系

### 路径 B：通过 Webserver API（仅限特殊场景）

先通过 webserver (端口 8081) 创建 session，再发送消息。session_key 自动生成为 `webchat:{timestamp}`（纯数字）。

```bash
# Step 1: 创建 session
SESSION_RESPONSE=$(curl -s -X POST http://127.0.0.1:8081/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "my-task"}')
SESSION_ID=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Step 2: 发送消息
curl -s -X POST "http://127.0.0.1:8081/api/sessions/${SESSION_ID}/messages" \
  -H "Content-Type: application/json" \
  -d '{"message": "你的任务指令..."}' \
  --max-time 120 > /dev/null 2>&1 &
```

> ⚠️ 路径 B 无法自定义 session_key，生成的 `webchat:{timestamp}` 会被前端归类为手动对话。仅在不需要父子关系的一次性任务（如 restart-gateway 脚本）中使用。

### 脚本工具

```bash
# 路径 A：自定义 session_key（命名符合规范即自动建立父子关系）
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --session-key "webchat:worker_1772696251_task003" \
  --message "请执行..." \
  --title "🔨 构造 task-003"

# 路径 B：自动生成 session_key（特殊场景，无父子关系）
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --message "请执行..." \
  --wait 60
```

参数说明：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--session-key` | 路径 A 必填 | 自定义 session_key（必须符合命名规范） |
| `--message` | 是 | 发送给子 session 的任务指令 |
| `--title` | 否 | 显示名称（路径 A 有效） |
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
| **batch-orchestrator** | 批量调度框架，调度 session 和 Worker session 通过本 skill 的路径 A 创建，必须遵循命名规范 |
| **restart-gateway** | 飞书/Telegram channel 下，restart-gateway 脚本使用路径 B 创建子 session（一次性任务，无需父子关系） |
| **restart-webchat** | 不需要子 session，restart.sh 使用 double-fork 直接执行 |

## 跨通道使用（CLI / 飞书 → webchat 子 session）

从 CLI 或飞书通道发起 batch 任务时，子 session 的 parent_ref 使用**父 session 的 timestamp**（即 session_key 中的 10 位数字部分）。

前端启发式规则 B 会在所有已加载 session 中搜索以 `:<timestamp>` 结尾的 session，**自动跨通道匹配**。

**示例**：从 `cli:1772603563` 发起 batch

| 角色 | session_key | 自动识别的父 session |
|------|------------|-------------------|
| 调度 | `webchat:dispatch_1772603563_gen1` | `cli:1772603563` |
| Worker | `webchat:worker_1772603563_task003` | `cli:1772603563` |

> **注意**：前端需要同时加载了父 session（如 `cli:1772603563`）才能匹配。如果父 session 不在当前 session 列表中（如已归档），则回退为根节点显示。
