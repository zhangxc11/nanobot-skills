# Todo Skill — 开发日志

## Phase 1: 基础待办管理

### 任务拆解

- [x] T1: todo.py 脚本 — 数据层（load/save/lock/id_gen/find）
- [x] T2: todo.py 脚本 — add 命令（含 session 关联 + note）
- [x] T3: todo.py 脚本 — list / show 命令（含过滤/排序）
- [x] T4: todo.py 脚本 — update / done / delete 命令
- [x] T5: todo.py 脚本 — note 命令（说明文档管理）
- [x] T6: todo.py 脚本 — summary 命令（按分类摘要）
- [x] T7: SKILL.md 编写（含触发提示 + 上下文关联指南）
- [x] T8: 集成测试
- [ ] T9: MEMORY.md 更新

### 开发记录

**2026-03-11 00:30 — Phase 1 实现**

完成全部脚本和 SKILL.md：

1. **todo.py** (scripts/todo.py)
   - 8 个子命令：add, list, show, update, note, done, delete, summary
   - JSON 存储 + fcntl 文件锁 + 原子写入
   - ID 前缀匹配（≥4 字符）
   - note 支持 `\n` 换行和 `@filepath` 文件读取
   - 分类/优先级/状态图标化输出

2. **SKILL.md** — 含触发关键词、命令速查、上下文关联指南

3. **集成测试通过**：
   - add（含 session + note）✅
   - list（默认/--all）✅
   - show（含 note 显示）✅
   - update（status/priority）✅
   - done（批量）✅
   - delete（软删除）✅
   - note --write/--append/读取 ✅
   - summary ✅
   - `\n` 换行修复 ✅

**Bug 修复**：
- note 的 `\n` 转义未处理 → add/note 的 write/append 统一加 `.replace("\\n", "\n")`

## Phase 2: 分组 (Group) 机制

### 任务拆解

- [x] T1: groups.json 数据层（load_groups/save_groups/find_group）
- [x] T2: group add 命令
- [x] T3: group list 命令
- [x] T4: group show 命令（含下属 todo 列表 + 优先级分布）
- [x] T5: group update 命令
- [x] T6: group note 命令（分组说明文档）
- [x] T7: update 命令新增 --group 参数
- [x] T8: move 命令（移动分组 + 自动记录变更）
- [x] T9: list 命令新增 --group 过滤
- [x] T10: summary --by-group 模式
- [x] T11: SKILL.md 更新（分组命令速查 + 分组工作流）
- [x] T12: 集成测试

### 开发记录

**2026-03-11 18:07 — Phase 2 实现**

完成全部分组功能：

1. **数据层**
   - `groups.json` load/save（fcntl 文件锁 + 原子写入）
   - `find_group` 支持前缀匹配（≥4 字符）
   - `group_note_path` / `append_group_note` 辅助函数

2. **group 子命令** (5 个)
   - `group add` — 创建分组（用户指定 id）
   - `group list` — 列出分组（默认 active，--all 含 archived）
   - `group show` — 分组详情 + 下属 todo 列表 + 优先级分布 + 分组 note
   - `group update` — 更新分组字段（name/desc/principle/status）
   - `group note` — 分组说明文档（read/write/append）

3. **move 命令**
   - 移动 todo 到目标分组
   - 自动在目标分组 note 追加移入记录
   - 自动在来源分组 note 追加移出记录

4. **扩展现有命令**
   - `update --group` 设置/清除分组
   - `list --group` 按分组过滤（空字符串=未分组）
   - `summary --by-group` 按分组聚合摘要
   - `show` 显示分组信息

5. **SKILL.md** — 新增分组命令速查 + 分组工作流章节

6. **向后兼容** — 旧 todos 无 group 字段视为 null，groups.json 不存在时初始化为 []

7. **集成测试通过**：
   - group add/list/show/update/note ✅
   - update --group (设置/清除) ✅
   - list --group (过滤/未分组) ✅
   - move (变更记录) ✅
   - summary --by-group ✅
   - 现有功能不受影响 ✅
   - 测试数据已清理 ✅
