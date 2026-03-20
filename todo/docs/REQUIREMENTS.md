# Todo Skill — 需求文档

> 状态：**活跃** | 最后更新：2026-03-11

## 一、项目概述

个人待办事项管理 skill。用户在任意对话中提出待办，agent 自动识别并归类记录。
支持上下文关联（session + 说明文档），后续跟进时可追溯原始场景。

- **技术栈**: Python 3.11 脚本 + JSON 存储
- **Skill 位置**: `~/.nanobot/workspace/skills/todo/`
- **数据位置**: `~/.nanobot/workspace/data/todo/`（与 skill 分离）

## 二、功能需求

### Phase 1: 基础待办管理

#### F1.1 添加待办
- 支持字段：title（必填）、category、priority、due_date、tags
- 可选关联当前 session_key，记录产生待办的会话
- 可选附带 context_note（Markdown 说明文档），保存为 `data/todo/notes/{id}.md`
- 自动生成短 ID（8 位 hex）、created_at 时间戳
- 默认值：category=inbox, priority=medium, status=todo

#### F1.2 列出待办
- 默认列出所有非 done/cancelled 的待办
- 支持过滤：--status, --category, --priority, --tag
- 支持排序：--sort（priority / created_at / due_date）
- 输出格式：人类可读的表格/列表

#### F1.3 更新待办
- 通过 ID 更新任意字段（title, category, priority, status, due_date, tags）
- 支持追加 context_note 内容

#### F1.4 标记完成
- `done <id>` 快捷操作，等价于 update --status done
- 记录 completed_at 时间戳

#### F1.5 删除待办
- 软删除：标记 status=cancelled
- 硬删除：--hard 参数，从 JSON 中移除 + 删除关联 notes 文件

#### F1.6 查看详情
- `show <id>` 查看单条待办的完整信息
- 包含关联的 session_key 和 context_note 内容

### Phase 2: 分组 (Group) 机制

#### 背景与痛点

用户在批量对齐待办事项时，会先对 todo 做功能分类（如 🅰 Spawn体系、🅱 Web前端、🅲 核心引擎稳定性…），然后按类逐条澄清需求。但这些分类信息只存在于对话上下文中，上下文压缩后就丢了，新 session 也不知道。讨论中还会修正分类（某条从 A 类移到 B 类），这些决策也无法追溯。

#### F2.1 分组数据结构 — `data/todo/groups.json`

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

- group status 可选值: `active`, `archived`
- group id 由用户指定（`--id` 参数），不自动生成

#### F2.2 todo 数据结构新增 `group` 字段

- 每条 todo 新增可选字段 `"group": "spawn-enhance"`（默认 null）
- 向后兼容：旧数据无此字段视为 null

#### F2.3 分组管理命令

```bash
python todo.py group add --id "spawn-enhance" --name "🅰 Spawn体系增强" [--desc "..."] [--principle "..."] [--session-id "..."]
python todo.py group list [--all]              # 列出分组（默认只显示 active）
python todo.py group show <group-id>           # 分组详情 + 下属 todo 列表
python todo.py group update <group-id> [--name "..."] [--desc "..."] [--principle "..."] [--status active|archived]
python todo.py group note <group-id> [--write "..."] [--append "..."]  # 分组说明文档
```

#### F2.4 todo 关联分组

```bash
python todo.py update <todo-id> --group "spawn-enhance"   # 设置分组
python todo.py update <todo-id> --group ""                 # 清除分组
python todo.py move <todo-id> --to "dev-workflow"          # 移动分组 + 自动记录变更
```

#### F2.5 按分组查看

```bash
python todo.py list --group "spawn-enhance"    # 按分组过滤
python todo.py list --group ""                 # 列出未分组的
python todo.py summary --by-group              # 按分组聚合的摘要
```

#### F2.6 move 命令自动记录

`move` 命令除了更新 todo 的 group 字段外，还应自动在目标分组的 note 中追加移入记录，在来源分组的 note 中追加移出记录。

#### F2.7 group show 输出

显示分组详情（描述、原则、状态、创建时间、关联会话、待办数量及优先级分布），列出下属 todo 表格，以及分组说明文档内容。

#### F2.8 summary --by-group 输出

按分组聚合，每组显示名称、待办数、优先级分布。未分组的单独列一个"未分组"类别。

### Phase 3: 规划与提醒（待定）

- 今日/本周待办摘要
- 与 cron 联动做截止日期提醒
- 与 calendar-reader 联动查看日程空隙安排待办

## 三、触发识别

Agent 应在以下场景自动识别用户意图为"添加待办"：
- 明确关键词：待办、todo、记一下、帮我记、要做的事、加个任务
- 会话中的指令：把这个记下来、这个后面要做、加到待办里
- 搭配时间词：明天要、下周、回头要

## 四、数据结构

```json
{
  "id": "a1b2c3d4",
  "title": "给小王发邮件确认方案",
  "category": "工作",
  "priority": "medium",
  "status": "todo",
  "created_at": "2026-03-11T00:26:00",
  "due_date": "2026-03-12",
  "completed_at": null,
  "session_key": "web:1773150605",
  "tags": ["邮件", "方案"],
  "has_note": true
}
```

关联说明文档：`data/todo/notes/a1b2c3d4.md`

## 五、约束

- 数据文件路径固定：`~/.nanobot/workspace/data/todo/todos.json`
- Notes 目录：`~/.nanobot/workspace/data/todo/notes/`
- 脚本无外部依赖，仅用 Python 标准库
- JSON 文件读写需加文件锁（fcntl）防并发冲突
