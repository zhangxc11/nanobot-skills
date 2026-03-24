# Dev 环境管理指南（nanobot + web-chat）

> Dev 环境使用 dev-workdir 下的代码，通过端口隔离与 prod 并行运行，用于验收批量开发的改动。

---

## 端口规划

| 服务 | Prod 端口 | Dev 端口 |
|------|----------|----------|
| Gateway | 18790 | 18791 |
| Webserver | 8081 | 9081 |
| Worker | 8082 | 9082 |

---

## 统一管理脚本

所有 dev 环境的服务管理统一使用 `nanobot-svc.sh`：

```bash
# 路径（symlink）
~/.nanobot/bin/nanobot-svc.sh

# 实际位置
~/Documents/code/workspace/nanobot/scripts/nanobot-svc.sh
```

环境配置文件：`~/.nanobot/env/dev.env`、`~/.nanobot/env/prod.env`

---

## 代码加载方式

### 方案 A: PYTHONPATH（主方案）

`nanobot-svc.sh` 启动 dev 环境时自动设置 PYTHONPATH，优先加载 dev-workdir 中的 nanobot 代码。

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
| 配置 | `config.json` | 共用 | 端口差异通过 env 文件配置 |

---

## 启动 Dev 环境

### 启动全部（gateway + webserver + worker）

```bash
bash ~/.nanobot/bin/nanobot-svc.sh start dev all
```

### 仅启动 Web-Chat（webserver + worker）

```bash
bash ~/.nanobot/bin/nanobot-svc.sh start dev worker
bash ~/.nanobot/bin/nanobot-svc.sh start dev webserver
```

### 启动 Gateway（仅在验收 gateway 改动时需要）

⚠️ Gateway 维护飞书长连接（WebSocket），dev/prod 不能同时运行。`nanobot-svc.sh` 自动检查互斥。

```bash
# 先停 prod gateway
bash ~/.nanobot/bin/nanobot-svc.sh stop prod gateway

# 启动 dev gateway
bash ~/.nanobot/bin/nanobot-svc.sh start dev gateway

# 或者使用 switch-gw 一键切换
bash ~/.nanobot/bin/nanobot-svc.sh switch-gw prod dev
```

### 查看状态

```bash
bash ~/.nanobot/bin/nanobot-svc.sh status dev
```

---

## 停止 Dev 环境

```bash
# 停止全部
bash ~/.nanobot/bin/nanobot-svc.sh stop dev all

# 仅停止 web-chat
bash ~/.nanobot/bin/nanobot-svc.sh stop dev worker
bash ~/.nanobot/bin/nanobot-svc.sh stop dev webserver
```

日志保留在 `~/.nanobot/logs-dev/`，不清理。

---

## 重启 Dev 环境

```bash
# 重启全部
bash ~/.nanobot/bin/nanobot-svc.sh restart dev all

# 仅重启 worker
bash ~/.nanobot/bin/nanobot-svc.sh restart dev worker
```

---

## Gateway 验收特殊流程

Gateway 与飞书的 WebSocket 连接是排他的，验收流程：

1. 切换 gateway：`nanobot-svc.sh switch-gw prod dev`
2. 用户通过飞书发消息验收
3. 验收完成 → 切回：`nanobot-svc.sh switch-gw dev prod`

**注意**：
- 验收窗口期间飞书消息服务中断，尽量缩短
- 安排在低峰时段
- 不涉及 Gateway 改动时，prod Gateway 保持运行，只启动 dev webserver + worker 即可
