# Dev-workdir 管理指南（nanobot + web-chat）

> dev-workdir 是批量开发的隔离工作区，通过 `git clone` 本地仓库到独立目录，避免开发过程污染线上代码。

---

## 路径约定

```
~/.nanobot/workspace/dev-workdir/
├── nanobot/      ← clone from ~/Documents/code/workspace/nanobot
└── web-chat/     ← clone from ~/.nanobot/workspace/web-chat
```

| 变量 | 路径 |
|------|------|
| `DEV_WORKDIR` | `~/.nanobot/workspace/dev-workdir` |
| nanobot 源仓库 | `~/Documents/code/workspace/nanobot`（分支 `local`） |
| web-chat 源仓库 | `~/.nanobot/workspace/web-chat`（分支 `main`） |

---

## 初始化（首次 or 新批次）

```bash
mkdir -p ~/.nanobot/workspace/dev-workdir

# Clone nanobot（如已存在则跳过）
git clone ~/Documents/code/workspace/nanobot ~/.nanobot/workspace/dev-workdir/nanobot
# 禁止误 push 到 prod 仓库
cd ~/.nanobot/workspace/dev-workdir/nanobot && git remote set-url --push origin DISABLED

# Clone web-chat（如已存在则跳过）
git clone ~/.nanobot/workspace/web-chat ~/.nanobot/workspace/dev-workdir/web-chat
# 禁止误 push 到 prod 仓库
cd ~/.nanobot/workspace/dev-workdir/web-chat && git remote set-url --push origin DISABLED

# 安装前端依赖
cd ~/.nanobot/workspace/dev-workdir/web-chat/frontend && npm install
```

### 按需 clone 其他仓库

如果 Plan 涉及的仓库不在 dev-workdir 中（如 eval-bench-data 等），应按需 clone 到 `dev-workdir/` 下，保持所有 Plan 的流程一致性：

```bash
# 示例：clone eval-bench-data
git clone <源仓库路径> ~/.nanobot/workspace/dev-workdir/<仓库名>
# 同样禁止误 push
cd ~/.nanobot/workspace/dev-workdir/<仓库名> && git remote set-url --push origin DISABLED
```

> **原则**：所有 Plan 的开发都应在 dev-workdir 中进行，不要因为某个仓库不在 dev-workdir 中就跳过隔离流程。

---

## 创建 Feature 分支

> **⚠️ 无论仓库类型（代码/文档/数据），所有 Plan 必须走 feature branch，禁止直接在主分支提交。** 即使是纯文档仓库也应开 branch，验收通过才合并，保留验收否决权。

每个 Plan 在两个仓库（按需）各拉一个 feature 分支：

```bash
# nanobot
cd ~/.nanobot/workspace/dev-workdir/nanobot
git checkout -b feat/batch-YYYYMMDD-plan-xxx

# web-chat
cd ~/.nanobot/workspace/dev-workdir/web-chat
git checkout -b feat/batch-YYYYMMDD-plan-xxx
```

命名规范：`feat/batch-{日期}-plan-{名称}`

> **⚠️ 分支共存 vs checkout 互斥**：多个 feature branch 可以同时存在于同一仓库中（`git branch` 可见），但同一时刻只能 checkout 一个分支。checkout 切换会改变工作区文件，因此同仓库的 Plan 必须串行开发——前一个 Plan dev_done 后才能 checkout 下一个 Plan 的 feature branch。

---

## 查看状态

```bash
# nanobot
cd ~/.nanobot/workspace/dev-workdir/nanobot
git branch -v          # 当前分支 + feature 分支列表
git status --short     # 未提交变更
git log --oneline -5   # 最近提交

# web-chat（同上）
cd ~/.nanobot/workspace/dev-workdir/web-chat
git branch -v
git status --short
git log --oneline -5
```

---

## 重置（新批次复用 dev-workdir）

上一批次完成后，重置到源仓库最新状态：

```bash
# nanobot
cd ~/.nanobot/workspace/dev-workdir/nanobot
git fetch origin
git checkout local          # nanobot 默认分支是 local
git reset --hard origin/local
git clean -fd
# 清理 feature 分支
git branch --list 'feat/*' | xargs -r git branch -D

# web-chat
cd ~/.nanobot/workspace/dev-workdir/web-chat
git fetch origin
git checkout main
git reset --hard origin/main
git clean -fd
git branch --list 'feat/*' | xargs -r git branch -D

# 重新安装前端依赖（package.json 可能变了）
cd ~/.nanobot/workspace/dev-workdir/web-chat/frontend && npm install
```

---

## 合并到主仓库（验收通过后）

验收通过的 feature 分支，用 `merge --no-ff` 合并到主仓库：

```bash
# nanobot: 在主仓库操作
cd ~/Documents/code/workspace/nanobot
git merge --no-ff ~/.nanobot/workspace/dev-workdir/nanobot/feat/batch-xxx-plan-yyy
# 或者用 remote 方式
git remote add dev-workdir ~/.nanobot/workspace/dev-workdir/nanobot  # 首次
git fetch dev-workdir
git merge --no-ff dev-workdir/feat/batch-xxx-plan-yyy

# web-chat: 同理
cd ~/.nanobot/workspace/web-chat
git remote add dev-workdir ~/.nanobot/workspace/dev-workdir/web-chat  # 首次
git fetch dev-workdir
git merge --no-ff dev-workdir/feat/batch-xxx-plan-yyy
```

---

## 注意事项

- dev-workdir 里的代码**仅用于开发和验收**，不直接部署
- 验收通过前**不要合并到主仓库**（dev_done ≠ 可以 merge，merge 在 Stage 3 验收通过后执行）
- 如果 dev-workdir 已存在且状态干净，新批次可以直接复用，不需要重新 clone
- 多个 Plan 的 feature 分支可以并行存在，但同一时刻只能 checkout 一个（互不干扰的前提是串行开发）
