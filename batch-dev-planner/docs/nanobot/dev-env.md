# Dev 环境管理指南（nanobot + web-chat）

> Dev 环境使用 dev-workdir 下的代码，通过端口隔离与 prod 并行运行，用于验收批量开发的改动。

---

## 端口规划

| 服务 | Prod 端口 | Dev 端口 |
|------|----------|----------|
| Gateway | 8080 | 9080 |
| Webserver | 8081 | 9081 |
| Worker | 8082 | 9082 |

---

## 代码加载方式

### 方案 A: PYTHONPATH（主方案）

通过 `PYTHONPATH` 让 dev 环境优先加载 dev-workdir 中的 nanobot 代码：

```bash
export PYTHONPATH=~/.nanobot/workspace/dev-workdir/nanobot:$PYTHONPATH
```

适用于绝大多数开发场景（纯代码改动、不涉及新依赖）。

### 方案 B: 独立 venv + pip install -e（后备）

当改动涉及依赖变更（如 `pyproject.toml` 新增依赖）时：

```bash
cd ~/.nanobot/workspace/dev-workdir/nanobot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

---

## 日志与数据隔离

| 维度 | Prod | Dev | 说明 |
|------|------|-----|------|
| 日志 | `~/.nanobot/logs/` | `~/.nanobot/logs-dev/` | **隔离** |
| llm-logs | `workspace/llm-logs/` | 共用 | append-only，无写冲突 |
| sessions | `workspace/sessions/` | 共用 | 验收可用真实数据 |
| skills / memory | `workspace/` | 共用 | |
| 配置 | `config.json` | 共用 | 端口差异通过命令行参数覆盖 |

---

## 启动 Dev 环境

### 前置：确保日志目录存在

```bash
mkdir -p ~/.nanobot/logs-dev
```

### 启动 Web-Chat（webserver + worker）

**推荐方式：使用 dev-workdir 的 restart.sh**（进程隔离已修复，不会误杀 prod）：

```bash
bash ~/.nanobot/workspace/dev-workdir/web-chat/restart.sh all
```

查看状态：
```bash
bash ~/.nanobot/workspace/dev-workdir/web-chat/restart.sh status
```

> ⚠️ restart.sh 的 `find_pids()` 通过精确 `SCRIPT_DIR` 路径匹配进程，确保 dev（9081/9082）和 prod（8081/8082）互不干扰。**不要**在 find_pids 中使用宽泛 pgrep 模式。

**备选方式：手动 nohup**（当 restart.sh 不可用时）：

```bash
cd ~/.nanobot/workspace/dev-workdir/web-chat

# 启动 webserver
PYTHONPATH=~/.nanobot/workspace/dev-workdir/nanobot:$PYTHONPATH \
  nohup python3 webserver.py --port 9081 --worker-url http://127.0.0.1:9082 \
  > ~/.nanobot/logs-dev/webserver.log 2> ~/.nanobot/logs-dev/webserver-stderr.log &

# 启动 worker
PYTHONPATH=~/.nanobot/workspace/dev-workdir/nanobot:$PYTHONPATH \
  nohup python3 worker.py --port 9082 --webserver-port 9081 \
  > ~/.nanobot/logs-dev/worker.log 2> ~/.nanobot/logs-dev/worker-stderr.log &
```

验证：访问 `http://127.0.0.1:9081`

### 启动 Gateway（仅在验收 gateway 改动时需要）

⚠️ Gateway 维护飞书长连接（WebSocket），**两个实例同时连会冲突**。如需验收 gateway 改动：

```bash
# 1. 先停 prod gateway（只杀监听进程）
kill $(lsof -ti:8080 -sTCP:LISTEN) 2>/dev/null

# 2. 启动 dev gateway
PYTHONPATH=~/.nanobot/workspace/dev-workdir/nanobot:$PYTHONPATH \
  nohup nanobot gateway --port 9080 \
  > ~/.nanobot/logs-dev/gateway.log 2>&1 &

# 3. 验收完成后，停 dev gateway，恢复 prod gateway
```

---

## 停止 Dev 环境

**推荐方式：使用 dev-workdir 的 restart.sh**：

```bash
bash ~/.nanobot/workspace/dev-workdir/web-chat/restart.sh stop
```

**备选方式：手动 kill**（只杀监听进程）：

⚠️ **必须使用 `-sTCP:LISTEN`**，否则会误杀连接到 dev 端口的浏览器网络进程（如 Edge/Chrome 的 Network Service），导致该浏览器到 prod 服务的所有连接也一并断开。

```bash
# 停止 web-chat（只杀监听进程，不影响浏览器）
kill $(lsof -ti:9081 -sTCP:LISTEN) $(lsof -ti:9082 -sTCP:LISTEN) 2>/dev/null

# 停止 gateway（如果启动了）
kill $(lsof -ti:9080 -sTCP:LISTEN) 2>/dev/null
```

日志保留在 `~/.nanobot/logs-dev/`，不清理。

---

## 查看状态

```bash
# 检查端口占用
echo "9080 (dev gateway):   $(lsof -ti:9080 2>/dev/null && echo 'IN USE' || echo 'free')"
echo "9081 (dev webserver): $(lsof -ti:9081 2>/dev/null && echo 'IN USE' || echo 'free')"
echo "9082 (dev worker):    $(lsof -ti:9082 2>/dev/null && echo 'IN USE' || echo 'free')"
```

---

## Gateway 验收特殊流程

Gateway 与飞书的 WebSocket 连接是排他的，验收流程：

1. 停止 prod Gateway（飞书消息暂时不可用）
2. 启动 dev Gateway（连接飞书）
3. 用户通过飞书发消息验收
4. 验收完成 → 停止 dev Gateway → 恢复 prod Gateway

**注意**：
- 验收窗口期间飞书消息服务中断，尽量缩短
- 安排在低峰时段
- 不涉及 Gateway 改动时，prod Gateway 保持运行，只启动 dev webserver + worker 即可
