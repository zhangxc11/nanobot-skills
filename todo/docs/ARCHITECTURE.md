# Todo Skill — 架构设计

> 最后更新：2026-03-11

## 一、整体架构

```
skills/todo/
├── SKILL.md              # Agent 使用指南（含触发提示）
├── docs/
│   ├── REQUIREMENTS.md   # 需求文档
│   ├── ARCHITECTURE.md   # 本文件
│   └── DEVLOG.md         # 开发日志
└── scripts/
    └── todo.py           # CLI 工具（所有操作入口）

data/todo/                # 数据存储（与 skill 分离）
├── todos.json            # 待办列表（JSON 数组）
└── notes/                # 每条待办的说明文档
    ├── a1b2c3d4.md
    └── ...
```

## 二、脚本设计 — todo.py

### 命令行接口

```bash
# 添加
python todo.py add --title "标题" [--category 工作] [--priority high] \
    [--due 2026-03-12] [--tags "标签1,标签2"] [--session "web:123"] \
    [--note "说明文本或 @filepath 读取文件"]

# 列出
python todo.py list [--status todo,doing] [--category 工作] \
    [--priority high] [--tag 标签] [--sort priority|created|due] [--all]

# 查看详情
python todo.py show <id>

# 更新
python todo.py update <id> [--title "新标题"] [--category 个人] \
    [--priority low] [--status doing] [--due 2026-03-15] [--tags "新标签"]

# 追加说明
python todo.py note <id> --append "追加内容"
python todo.py note <id> --write "覆盖内容"

# 标记完成
python todo.py done <id> [<id2> ...]

# 删除
python todo.py delete <id> [--hard]

# 摘要
python todo.py summary
```

### 输出格式

- `list` 输出简洁表格：`ID | P | 状态 | 分类 | 标题 | 截止`
- `show` 输出完整详情 + note 内容
- `summary` 输出按分类分组的摘要 + 统计数字
- 所有输出为纯文本，方便 agent 解析

### 存储操作

- 读：加载 `todos.json`，不存在则初始化空数组 `[]`
- 写：原子写入（先写 `.tmp` 再 rename），fcntl 文件锁
- ID 生成：`uuid.uuid4().hex[:8]`，碰撞检查

## 三、SKILL.md 设计

SKILL.md 的 description 字段包含触发提示词，帮助 agent 在用户提出待办相关意图时自动激活：

```yaml
description: >
  待办事项管理：添加、查看、更新、完成待办。
  当用户说"待办/todo/记一下/帮我记/要做的事/加个任务/这个后面要做"时使用。
  支持分类、优先级、session 上下文关联。
```

SKILL.md 正文提供：
1. 快速命令参考（agent 直接复制使用）
2. 分类和优先级的约定值
3. 上下文关联的使用指南（何时传 session_key，何时写 note）

## 四、设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 存储格式 | JSON 数组 | 精确操作、标准库支持 |
| 数据与 skill 分离 | data/todo/ | 数据独立于 skill 版本 |
| 说明文档 | 独立 .md 文件 | 避免 JSON 中嵌入大段文本 |
| 并发保护 | fcntl 文件锁 | 多 session 可能同时操作 |
| 软删除 | 默认 cancelled | 防误删，--hard 才真删 |
| 无外部依赖 | 纯标准库 | 零安装成本 |

## 五、Phase 2 — 分组 (Group) 机制

### 5.1 数据结构

#### groups.json

```
data/todo/
├── todos.json
├── groups.json           # 分组列表（JSON 数组）
└── notes/
    ├── {todo-id}.md      # todo 说明文档
    └── group-{group-id}.md  # 分组说明文档
```

```json
[
  {
    "id": "spawn-enhance",
    "name": "🅰 Spawn / Subagent 体系增强",
    "description": "围绕 spawn 子任务机制的功能完善",
    "principle": "优先做对 subagent 稳定性和可观测性有帮助的",
    "created_at": "2026-03-11T15:00:00",
    "session_id": "webchat_1773213500",
    "status": "active"
  }
]
```

#### todo 新增字段

每条 todo 新增可选字段 `"group": "spawn-enhance"`（默认 null）。向后兼容。

### 5.2 CLI 接口扩展

```bash
# 分组管理（group 子命令下的二级子命令）
python todo.py group add --id <id> --name <name> [--desc ...] [--principle ...] [--session-id ...]
python todo.py group list [--all]
python todo.py group show <group-id>
python todo.py group update <group-id> [--name ...] [--desc ...] [--principle ...] [--status active|archived]
python todo.py group note <group-id> [--write ...] [--append ...]

# todo 关联分组
python todo.py update <todo-id> --group <group-id>   # 设置
python todo.py update <todo-id> --group ""            # 清除
python todo.py move <todo-id> --to <group-id>         # 移动 + 自动记录

# 按分组查看
python todo.py list --group <group-id>
python todo.py list --group ""                        # 未分组
python todo.py summary --by-group
```

### 5.3 存储设计

- `groups.json` 与 `todos.json` 同级，同样使用 fcntl 文件锁 + 原子写入
- 分组 note 文件命名：`notes/group-{id}.md`，与 todo note 共用 NOTES_DIR
- group id 由用户指定，不自动生成
- `find_group` 函数支持前缀匹配（与 `find_todo` 一致）

### 5.4 move 命令自动记录

move 命令在更新 todo 的 group 字段后，自动在目标分组 note 追加移入记录，在来源分组 note 追加移出记录：

```
- [2026-03-11 15:30] 从 `spawn-enhance` 移入: [b4ea3323] spawn status 异常诊断
- [2026-03-11 15:30] 移出至 `dev-workflow`: [b4ea3323] spawn status 异常诊断
```
