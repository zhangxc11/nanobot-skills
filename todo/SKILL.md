---
name: todo
description: >
  待办事项管理：添加、查看、更新、完成待办，支持分组管理。
  当用户说"待办/todo/记一下/帮我记/要做的事/加个任务/这个后面要做/回头处理"时使用。
  支持分类、优先级、截止日期、session 上下文关联、说明文档和分组。
---

# Todo — 待办事项管理

## 触发识别

当用户表达以下意图时，应使用此 skill：

**明确关键词**: 待办、todo、记一下、帮我记、要做的事、加个任务、加到待办
**会话中指令**: 把这个记下来、这个后面要做、回头处理一下、加到待办里
**分组相关**: 分个组、归类、按类别整理、移到哪个组
**搭配时间词**: 明天要…、下周需要…、回头要…

> ⚠️ 如果用户在对话过程中说"这个记一下"、"把这个加到待办"，应自动关联当前 session_id 并将上下文摘要写入 note。

## 脚本路径

```
~/.nanobot/workspace/skills/todo/scripts/todo.py
```

## 数据路径（与 skill 分离）

```
~/.nanobot/workspace/data/todo/
├── todos.json        # 待办列表
├── groups.json       # 分组列表
└── notes/            # 说明文档
    ├── {id}.md              # todo 说明
    └── group-{group-id}.md  # 分组说明
```

## 命令速查

### 添加待办

```bash
python todo.py add --title "标题" \
    [--category 工作] \
    [--priority high|medium|low] \
    [--due 2026-03-12] \
    [--tags "标签1,标签2"] \
    [--session-id "webchat_1773150605"] \
    [--note "说明文本 或 @filepath"]
```

**分类约定**（可自由扩展）：工作、个人、学习、项目、inbox（默认）

**session_id 说明**：
- session_id = session 文件名（不含 `.jsonl` 后缀）
- 格式示例：`webchat_1773150605`, `cli.1772603563`, `feishu.lab.1772848691`
- 对应文件：`~/.nanobot/workspace/sessions/{session_id}.jsonl`
- 从对话中产生的待办，**务必传 `--session-id`** 关联当前会话

**使用建议**：
- 需要记录上下文的，用 `--note` 写入背景说明
- `--note` 支持 `@filepath` 从文件读取内容

### 列出待办

```bash
python todo.py list                          # 活跃待办（排除 done/cancelled）
python todo.py list --all                    # 全部
python todo.py list --category 工作          # 按分类
python todo.py list --priority high          # 按优先级
python todo.py list --status todo,doing      # 按状态
python todo.py list --tag 标签名             # 按标签（含指定标签）
python todo.py list --tag-none "已对齐,待对齐"  # 排除标签（不含任一指定标签）
python todo.py list --tag "已对齐" --tag-none "待对齐"  # --tag 和 --tag-none 可组合
python todo.py list --group "spawn-enhance"  # 按分组过滤
python todo.py list --group ""               # 列出未分组的
python todo.py list --sort priority|created|due  # 排序
python todo.py list --format json            # JSON 格式输出（含所有字段 + group_name）
python todo.py list --format table           # 表格格式输出（默认，含 group 列）
```

**`--format json` 说明**：输出 JSON array，每条 todo 包含所有字段（id, title, status, priority, tags, group, group_name, created_at, due_date, has_note 等），适合程序化处理。

**`--tag-none` 说明**：支持逗号分隔多个标签，排除含有其中**任一**标签的 todo。与 `--tag` 可组合使用（先包含后排除）。

### 查看详情

```bash
python todo.py show <id>                     # 显示完整信息 + note + 分组信息
```

### 更新待办

```bash
python todo.py update <id> --priority high   # 改优先级
python todo.py update <id> --status doing    # 改状态
python todo.py update <id> --category 工作   # 改分类
python todo.py update <id> --session-id "webchat_xxx"  # 关联会话
python todo.py update <id> --group "spawn-enhance"     # 设置分组
python todo.py update <id> --group ""                  # 清除分组
```

### 移动分组

```bash
python todo.py move <id> --to "dev-workflow"  # 移动分组（自动记录变更到分组 note）
```

### 管理说明文档

```bash
python todo.py note <id>                     # 查看 note
python todo.py note <id> --write "内容"      # 覆盖写入
python todo.py note <id> --append "追加"     # 追加内容
```

### 标记完成 / 删除

```bash
python todo.py done <id1> [<id2> ...]        # 批量标记完成
python todo.py delete <id>                   # 软删除（标记 cancelled）
python todo.py delete <id> --hard            # 永久删除
```

### 摘要

```bash
python todo.py summary                       # 按分类分组的活跃待办摘要
python todo.py summary --by-group            # 按分组聚合的摘要
```

### 分组管理

```bash
# 创建分组
python todo.py group add --id "spawn-enhance" --name "🅰 Spawn体系增强" \
    [--desc "描述"] [--principle "原则"] [--session-id "webchat_xxx"]

# 列出分组
python todo.py group list                    # 仅 active
python todo.py group list --all              # 含 archived

# 查看分组详情（含下属 todo 列表 + 优先级分布 + 分组 note）
python todo.py group show <group-id>

# 更新分组
python todo.py group update <group-id> --name "新名称"
python todo.py group update <group-id> --desc "新描述"
python todo.py group update <group-id> --principle "新原则"
python todo.py group update <group-id> --status archived  # 归档

# 分组说明文档
python todo.py group note <group-id>                      # 查看
python todo.py group note <group-id> --write "内容"       # 覆盖
python todo.py group note <group-id> --append "追加"      # 追加
```

## 分组工作流

当用户在对话中批量对齐待办事项，按功能分类讨论时：

### 1. 创建分组

根据讨论中的分类，创建对应分组：

```bash
python todo.py group add --id "spawn-enhance" --name "🅰 Spawn体系增强" \
    --desc "围绕 spawn 子任务机制的功能完善" \
    --principle "优先做对 subagent 稳定性和可观测性有帮助的" \
    --session-id "当前session"
```

### 2. 关联待办到分组

```bash
python todo.py update <todo-id> --group "spawn-enhance"
```

### 3. 讨论中修正分类

当用户决定将某条待办从一个分组移到另一个时，使用 `move`（自动记录变更历史）：

```bash
python todo.py move <todo-id> --to "dev-workflow"
```

move 会自动在两个分组的 note 中记录变更，便于后续追溯。

### 4. 查看分组状态

```bash
python todo.py group show spawn-enhance      # 看某个分组的详情和待办
python todo.py summary --by-group            # 看所有分组的概览
```

### 5. 分组讨论结束后

在分组 note 中记录讨论结论：

```bash
python todo.py group note spawn-enhance --append "## 2026-03-11 讨论结论\n- 优先处理 xxx\n- yyy 暂缓"
```

> **关键价值**：分组信息持久化在 `groups.json` 和分组 note 中，不会随上下文压缩丢失。新 session 可以通过 `group show` 和 `group note` 完整恢复分类决策。

## 上下文关联指南

当用户在对话中说"把这个记下来"或"加个待办"时：

1. **提取标题**：从对话上下文中提炼一句话标题
2. **关联 session**：传入当前 session_id（session 文件名，如 `webchat_1773150605`）
3. **写 note**：将相关上下文摘要写入 `--note`，包括：
   - 当时在讨论什么
   - 关键结论或决定
   - 后续需要做什么
4. **智能分类**：根据上下文自动判断分类和优先级

后续用户查看待办时，`show <id>` 会显示 session_id、分组和 note。
通过 session_id 可直接定位到 `sessions/{session_id}.jsonl` 查看原始对话上下文。

## ID 前缀匹配

所有接受 `<id>` 的命令（包括 todo ID 和 group ID）都支持前缀匹配（≥4 字符），无需输入完整 ID。

## 完成感知（任何 session 通用）

agent 在**任何 session** 中完成了一项有意义的工作后，应主动检查待办列表是否有匹配项并更新状态。

### 触发时机

当 session 中出现以下情况时，应触发待办匹配检查：
- 用户明确说"这个做完了"、"搞定了"
- agent 完成了一项独立任务（如跑完 scanner、修复了 bug、完成了文档整理）
- subagent 返回了任务完成的结果

### 匹配方式

```bash
# 快速扫描活跃待办，按关键词匹配
python todo.py list
python todo.py list --tag "相关标签"
python todo.py list --group "相关分组"
```

根据当前 session 完成的工作内容，与待办标题/标签做语义匹配。

### 更新动作

匹配到后：
1. **非开发类任务**（不涉及代码开发，如 scanner、文档整理、评测）→ 直接 `done`
2. **开发类任务** → 根据完成程度更新 status（`doing` / `done`），追加 note 记录完成情况

```bash
python todo.py done <id>
python todo.py note <id> --append "在 session webchat_xxx 中完成：<简要说明>"
```

### 待办分类约定

| 类型 | 是否需要需求对齐 | 完成后动作 |
|---|---|---|
| 开发类（涉及代码） | ✅ 需要「已对齐」才可排期 | 更新 status + note |
| 非开发类（执行类任务） | ❌ 不需要对齐 | 做完直接 `done` |
