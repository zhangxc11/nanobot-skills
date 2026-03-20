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

## 获取当前 Session ID

Runtime Context 中直接注入了 `Session ID`，可直接使用：

```
Session ID: webchat_1773250094
```

> `Session ID` 就是 `--parent` 参数的值。无需手动拼接。

补充说明：`Session ID` = JSONL 文件名（不含 `.jsonl`），是全链路唯一标识（参见 nanobot core §51、web-chat §三十九）。

## Session Key 命名规范

### 格式

```
webchat:<role>_<parent_chat_id>_<detail>
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `role` | 角色标识 | `dispatch`, `worker`, `fix`, `restart` |
| `parent_chat_id` | 直接父 session 的 Chat ID | `1773250094` |
| `detail` | 具体任务标识 | `gen1`, `task003`, `gateway` |

### 父子关系

通过 `--parent` 参数显式注册，传父 session 的 ID（如 `web_1773250094`）。

### 三级树（batch 调度场景）

```
web_1773250094 (主控，--parent 不需要)
├── webchat_dispatch_1773250094_1773260000 (调度，--parent web_1773250094)
│   ├── webchat_worker_1773260000_task003 (Worker，--parent webchat_dispatch_1773250094_1773260000)
│   └── webchat_worker_1773260000_task005
└── webchat_dispatch_1773250094_1773270000 (调度 gen2)
    └── webchat_worker_1773270000_task017
```

调度 session key 中的第二个 timestamp 在创建时生成：

```bash
DISPATCH_TS=$(date +%s)
SESSION_KEY="webchat:dispatch_${PARENT_CHAT_ID}_${DISPATCH_TS}"
```

### 文件名映射

session_key 中的 `:` 自动替换为 `_` 作为文件名（即 Session ID）：

```
webchat:worker_1773260000_task003  →  webchat_worker_1773260000_task003.jsonl
```

## 使用方式

### 路径 A：直接调用 Worker API（推荐）

直接向 worker (端口 8082) 发送 `execute-stream` 请求，**自定义 session_key**：

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --session-key "webchat:worker_1773250094_task003" \
  --message "请执行..." \
  --title "🔨 构造 task-003" \
  --parent "web_1773250094"
```

### 路径 B：通过 Webserver API（仅限特殊场景）

不指定 session_key，自动生成 `webchat:{timestamp}`。无法自定义命名，仅用于一次性任务（如 restart-gateway）。

```bash
bash ~/.nanobot/workspace/skills/web-subsession/scripts/create_subsession.sh \
  --message "请执行..." \
  --wait 60
```

### 脚本参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--session-key` | 路径 A 必填 | 自定义 session_key |
| `--message` | 是 | 发送给子 session 的任务指令 |
| `--title` | 否 | 显示名称（路径 A 有效） |
| `--parent` | 否 | 父 session ID（如 `web_1773250094`），注册父子关系 |
| `--port` | 否 | Webserver 端口（默认 8081） |
| `--worker-port` | 否 | Worker 端口（默认 8082，仅路径 A） |
| `--wait` | 否 | 等待完成的超时秒数（默认 0 = fire-and-forget） |
| `--poll-interval` | 否 | 轮询间隔秒数（默认 5，仅 --wait > 0 时有效） |

## 技术说明

- Web-chat worker 是独立进程，不受 gateway 重启影响
- 子 session 中的 agent 拥有完整的工具能力（exec、read_file、write_file 等）
- `execute-stream` 是 SSE 流式接口，会等到 agent 完成才返回；`--max-time` 让 curl 提前断开但不影响 worker 继续执行

## 与其他 Skill 的关系

| Skill | 关系 |
|-------|------|
| **batch-orchestrator** | 批量调度框架，调度 session 通过本 skill 创建 |
| **restart-gateway** | 飞书/Telegram channel 下使用路径 B 创建一次性任务 |
| **restart-webchat** | 不需要子 session，restart.sh 直接执行 |
