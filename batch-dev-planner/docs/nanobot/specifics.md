# nanobot + web-chat 项目特化配置

> batch-dev-planner 在 nanobot + web-chat 项目上的特化说明。
> dev-workdir 操作见 [dev-workdir.md](dev-workdir.md)，dev 环境启停见 [dev-env.md](dev-env.md)。

---

## 1. 仓库信息

| 仓库 | 本地路径 | 默认分支 | 远程 |
|------|----------|----------|------|
| nanobot | `~/Documents/code/workspace/nanobot` | `local` | `zhangxc11/nanobot.git` |
| web-chat | `~/.nanobot/workspace/web-chat` | `main` | `zhangxc11/nanobot-web-chat.git` |

---

## 2. 代码加载方式

### 方案 A: PYTHONPATH（主方案）

通过 `PYTHONPATH` 指向 dev-workdir 中的代码，使 dev 环境加载开发中的代码：

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

## 3. 数据层共用策略

| 维度 | Prod | Dev | 说明 |
|------|------|-----|------|
| 日志 | `~/.nanobot/logs/` | `~/.nanobot/logs-dev/` | **隔离** |
| llm-logs | `workspace/llm-logs/` | 共用 | append-only，无写冲突 |
| sessions | `workspace/sessions/` | 共用 | 验收可用真实数据 |
| skills / memory | `workspace/` | 共用 | |
| 配置 | `config.json` | 共用 | 端口差异通过命令行参数覆盖 |

**注意**：dev 环境的操作会影响真实数据（如创建 session），验收时需留意。

---

## 4. 跨仓库 Feature 分支

nanobot 和 web-chat 的 feature 分支使用相同命名，便于关联：

```
feat/batch-20260313-plan-core-cron      ← 两个仓库都有
feat/batch-20260313-plan-core-fixes     ← 两个仓库都有
feat/batch-20260313-plan-session-feishu ← 仅 nanobot
feat/batch-20260313-plan-web-frontend   ← 仅 web-chat
```

合并顺序按依赖拓扑：先合并被依赖的 Plan，再合并依赖方。

---

## 5. 全量回归测试

nanobot 核心仓库合并后运行全量测试：

```bash
cd ~/Documents/code/workspace/nanobot
python -m pytest tests/ -x --timeout=30
```

web-chat 前端编译检查：

```bash
cd ~/.nanobot/workspace/web-chat/frontend
npx tsc --noEmit
```
