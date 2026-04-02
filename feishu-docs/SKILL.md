---
name: feishu-docs
description: "飞书文档操作：创建、读取、写入飞书文档，批注管理（列出/回复/解决/创建），权限管理。支持 Markdown 内容自动转换为飞书文档格式（含表格）。当用户要求创建文档、写报告、整理内容到飞书、处理批注时使用。"
---

# 飞书文档 Skill

通过飞书开放平台 API 操作飞书文档。支持创建文档、写入 Markdown 内容（含表格）、**局部编辑**、读取文档、批注管理、权限管理。

## 脚本位置

```
skills/feishu-docs/scripts/feishu_doc.py
```

---

## 命令速查

| 命令 | 完整语法 |
|------|----------|
| create | `python3 feishu_doc.py create --title TITLE [--folder TOKEN] [--app ST\|lab]` |
| write | `python3 feishu_doc.py write --doc DOC_ID --markdown TEXT \| --markdown-file FILE [--mode append\|overwrite] [--resume-from N] [--app ST\|lab]` |
| read | `python3 feishu_doc.py read --doc DOC_ID [--format raw\|blocks] [--app ST\|lab]` |
| create-and-write | `python3 feishu_doc.py create-and-write --title TITLE --markdown TEXT \| --markdown-file FILE [--folder TOKEN] [--add-member ou_xxx] [--member-perm full_access\|edit\|view] [--resume-from N] [--app ST\|lab]` |
| patch-block | `python3 feishu_doc.py patch-block --doc DOC_ID --block BLOCK_ID --text TEXT [--app ST\|lab]` |
| delete-blocks | `python3 feishu_doc.py delete-blocks --doc DOC_ID --start N --end N [--parent BLOCK_ID] [--app ST\|lab]` |
| insert-blocks | `python3 feishu_doc.py insert-blocks --doc DOC_ID --index N --markdown TEXT \| --markdown-file FILE [--parent BLOCK_ID] [--app ST\|lab]` |
| list-comments | `python3 feishu_doc.py list-comments --doc DOC_ID [--status all\|solved\|unsolved] [--app ST\|lab]` |
| reply-comment | `python3 feishu_doc.py reply-comment --doc DOC_ID --comment COMMENT_ID --text TEXT [--app ST\|lab]` |
| resolve-comment | `python3 feishu_doc.py resolve-comment --doc DOC_ID --comment COMMENT_ID [--app ST\|lab]` |
| add-comment | `python3 feishu_doc.py add-comment --doc DOC_ID --text TEXT [--quote QUOTE] [--is-whole] [--app ST\|lab]` |
| add-member | `python3 feishu_doc.py add-member --doc DOC_ID --open-id ou_xxx [--perm full_access\|edit\|view] [--app ST\|lab]` |

---

## ⚠️ 编辑原则：优先局部编辑，避免 overwrite

> **修改已有文档时，应优先使用局部编辑命令（patch-block / delete-blocks / insert-blocks），而非 overwrite 全量覆盖。**
>
> - overwrite 会丢失飞书的历史编辑记录，无法查看修改前后对比
> - 局部编辑保留完整编辑历史，支持飞书的版本回溯功能
> - 仅在"需要彻底重写整个文档"的极端场景下才使用 `--mode overwrite`

### 推荐的编辑工作流

```
1. read --format blocks   → 获取文档结构（block ID、index、内容）
2. patch-block             → 原地更新某个 block 的文本
3. delete-blocks           → 删除不需要的 block 范围
4. insert-blocks           → 在指定位置插入新内容
```

---

## 命令一览

| 命令 | 用途 | 场景 |
|---|---|---|
| `create` | 创建空白文档 | 新建文档 |
| `create-and-write` | 创建文档并写入 | 新建+写入（最常用） |
| `read` | 读取文档内容 | 查看内容、获取 block 结构 |
| `write` | 追加/覆盖写入 | 向已有文档追加内容 |
| **`patch-block`** | **原地更新 block** | **修改某段文字（推荐）** |
| **`delete-blocks`** | **删除 block 范围** | **删除某些段落（推荐）** |
| **`insert-blocks`** | **在指定位置插入** | **在两段之间插入新内容（推荐）** |
| `list-comments` | 列出批注 | 查看文档批注 |
| `reply-comment` | 回复批注 | 回复某条批注 |
| `resolve-comment` | 解决批注 | 标记批注已处理 |
| `add-comment` | 创建批注 | 对文档添加批注 |
| `add-member` | 添加协作者 | 授予文档权限 |

---

## 局部编辑命令（推荐）

### 读取文档结构

编辑前必须先读取 block 结构，获取目标 block 的 ID 和 index：

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py read --doc DOC_ID --format blocks
```

输出示例（关注 page block 的 children 顺序）：
```
Page block children:
  [0] type=3  id=doxcnXXX  "一级标题"
  [1] type=2  id=doxcnYYY  "正文段落"
  [2] type=31 id=doxcnZZZ  [TABLE 3x4]  (含 cell 文本内容)
  [3] type=4  id=doxcnAAA  "二级标题"
```

> **表格读取**：`read --format blocks` 支持读取表格 cell 的文本内容（table 属性、cell_contents 字段），可用于诊断表格写入结果。

### patch-block — 原地更新 block 内容

**最常用的编辑方式**。直接修改某个 block 的文本，不影响其他内容。

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py patch-block \
  --doc DOC_ID \
  --block BLOCK_ID \
  --text "新的文本内容，支持 **加粗** 和 *斜体*"
```

- `--block` 是目标 block 的 ID（从 `read --format blocks` 获取）
- `--text` 支持行内 Markdown 格式（bold, italic, code, link, strikethrough）
- 适用于 text / heading / bullet / ordered 等文本类 block

### delete-blocks — 删除指定范围的 block

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py delete-blocks \
  --doc DOC_ID \
  --start 2 \
  --end 4
```

- `--start` 和 `--end` 是 page block children 的 index（0-based，end 不包含）
- 上例删除 index 2 和 3 的 block（共 2 个）
- `--parent BLOCK_ID` 可选，指定父 block（默认为 page block）

### insert-blocks — 在指定位置插入新内容

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py insert-blocks \
  --doc DOC_ID \
  --index 2 \
  --markdown "## 新章节\n\n这是在 index 2 位置插入的内容。"
```

从文件读取：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py insert-blocks \
  --doc DOC_ID \
  --index 2 \
  --markdown-file /path/to/content.md
```

- `--index` 是插入位置（0-based，插入到该 index 之前）
- `--parent BLOCK_ID` 可选，指定父 block（默认为 page block）
- 支持完整 Markdown 语法（含表格）

### 局部编辑示例：替换某段内容

```bash
# 1. 读取文档结构
python3 feishu_doc.py read --doc DOC_ID --format blocks
# 输出: [0] heading "旧标题"  [1] text "旧内容"  [2] text "保留内容"

# 2. 删除 index 0~1 的旧内容
python3 feishu_doc.py delete-blocks --doc DOC_ID --start 0 --end 2

# 3. 在 index 0 插入新内容（原来的 [2] 现在变成了 [0]，新内容插在最前面）
python3 feishu_doc.py insert-blocks --doc DOC_ID --index 0 --markdown "# 新标题\n\n新内容"
```

---

## 创建与写入命令

### 创建空白文档

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py create --title "文档标题"
```

可选参数：
- `--folder TOKEN` — 指定目标文件夹 token（不指定则创建在应用根目录）

### 创建文档并写入内容（最常用）

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py create-and-write --title "文档标题" --markdown "# 内容标题\n\n正文内容"
```

从文件读取内容：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py create-and-write --title "文档标题" --markdown-file /path/to/content.md
```

可选参数：
- `--folder TOKEN` — 指定目标文件夹 token
- `--add-member ou_xxx` — 创建文档后自动添加协作者（在写入内容前执行）
- `--member-perm full_access|edit|view` — 协作者权限（默认 full_access，需配合 `--add-member` 使用）
- `--resume-from N` — 断点续传，从第 N 个 chunk 继续写入（0-based）

### 向已有文档追加内容

```bash
# 追加模式（默认）— 在文档末尾追加内容
python3 skills/feishu-docs/scripts/feishu_doc.py write --doc DOC_ID --markdown "追加的内容"
```

从文件读取：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py write --doc DOC_ID --markdown-file /path/to/content.md
```

可选参数：
- `--mode append|overwrite` — 写入模式（默认 append 追加）
- `--resume-from N` — 断点续传，从第 N 个 chunk 继续写入（0-based，写入失败时 stderr 会提示具体值）

### ⚠️ 覆盖写入（慎用）

```bash
# 覆盖模式 — 先清空文档所有内容，再写入新内容
python3 skills/feishu-docs/scripts/feishu_doc.py write --doc DOC_ID --mode overwrite --markdown "替换的内容"
```

> **⚠️ 警告**：`--mode overwrite` 会删除文档所有现有内容并丢失编辑历史。
> 仅在确实需要彻底重写整个文档时使用。绝大多数场景应使用局部编辑命令替代。

### 读取文档内容

纯文本格式：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py read --doc DOC_ID
```

Block 结构格式（编辑前必读）：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py read --doc DOC_ID --format blocks
```

---

## 批注管理

### 列出文档批注

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py list-comments --doc DOC_ID
```

可选参数：
- `--status all|solved|unsolved` — 过滤批注状态（默认 all）

### 回复批注

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py reply-comment --doc DOC_ID --comment COMMENT_ID --text "回复内容"
```

### 解决批注

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py resolve-comment --doc DOC_ID --comment COMMENT_ID
```

### 创建批注

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py add-comment --doc DOC_ID --quote "引用的文档文本" --text "批注内容"
```

对整个文档添加批注（无需引用特定文本）：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py add-comment --doc DOC_ID --text "批注内容" --is-whole
```

### 添加协作者

```bash
python3 skills/feishu-docs/scripts/feishu_doc.py add-member --doc DOC_ID --open-id ou_xxx --perm full_access
```

可选参数：
- `--perm full_access|edit|view` — 权限级别（默认 full_access）

---

## 输出格式

所有命令输出 JSON，包含 `success` 字段：

成功示例：
```json
{
  "success": true,
  "document_id": "doxcnXXXXXX",
  "blocks_written": 15,
  "url": "https://feishu.cn/docx/doxcnXXXXXX"
}
```

失败示例：
```json
{
  "success": false,
  "error": "[99991663] No permission"
}
```

## 支持的 Markdown 语法

| Markdown | 飞书效果 |
|---|---|
| `# H1` ~ `######### H9` | 标题 1-9 |
| 普通段落 | 文本 |
| `- item` | 无序列表 |
| `1. item` | 有序列表 |
| `` ```lang ... ``` `` | 代码块（支持语言高亮，支持嵌套：外层用 ```````` 可包裹内部 ``````` ） |
| `> quote` | 引用 |
| `- [ ] todo` / `- [x] done` | 待办事项 |
| `---` | 分割线 |
| `\| A \| B \|` + `\|---\|---\|` | **表格**（含表头，支持单元格内联样式，**列宽自动计算**） |
| `**bold**` | 加粗 |
| `*italic*` | 斜体 |
| `` `code` `` | 行内代码 |
| `~~strike~~` | 删除线 |
| `[text](url)` | 链接 |

### ⚠️ 暂不支持的语法

- 嵌套列表（多级缩进）
- 图片 `![alt](url)`
- HTML 标签
- 脚注
- 表格单元格合并

## 使用技巧

### 长内容建议用文件方式

当 Markdown 内容较长时，先写入临时文件再用 `--markdown-file` 传入：

```bash
# 1. 将内容写入临时文件
# 2. 调用命令
python3 skills/feishu-docs/scripts/feishu_doc.py create-and-write \
  --title "周报" \
  --markdown-file /tmp/weekly_report.md
```

### 批注工作流

典型的批注处理流程：
```bash
# 1. 列出未解决的批注
python3 feishu_doc.py list-comments --doc DOC_ID --status unsolved

# 2. 根据批注内容局部更新文档
python3 feishu_doc.py patch-block --doc DOC_ID --block BLOCK_ID --text "修改后的内容"

# 3. 回复批注说明已处理
python3 feishu_doc.py reply-comment --doc DOC_ID --comment COMMENT_ID --text "已处理"

# 4. 标记批注为已解决
python3 feishu_doc.py resolve-comment --doc DOC_ID --comment COMMENT_ID
```

### 指定飞书应用

默认使用 ST 应用，可通过 `--app` 切换：
```bash
python3 skills/feishu-docs/scripts/feishu_doc.py --app lab create --title "测试"
```

### 大文档写入建议

- 脚本会**自动分段写入**（按安全边界拆分为 chunk），每段间有延迟，进度输出到 stderr
- 建议 exec 设置 `timeout=600`（exec 工具支持手动指定超时，最大 600s）
- 含 20+ 表格的超大文档，总耗时可能超过 10 分钟，建议使用 **subagent** 执行
- 写入失败时，stderr 会输出断点续传命令提示，使用 `--resume-from N` 从失败位置继续
- 示例：
  ```bash
  # stderr 输出: ERROR: Failed at chunk 8/12. Resume with: --resume-from 8
  # 续传命令:
  python3 feishu_doc.py write --doc DOC_ID --markdown-file /tmp/content.md --resume-from 8
  ```

## 安全说明

- 脚本从 `~/.nanobot/config.json` 自动加载飞书应用凭证
- **appSecret 不会输出到 stdout**，仅在脚本进程内使用
- Agent 不直接接触密钥

## 权限要求

飞书应用需开通以下权限：
- `docx:document:create` — 创建文档
- `docx:document:write_only` — 写入文档
- `docx:document:readonly` — 读取文档
- `drive:drive:comment` — 批注操作（读取/创建/回复/解决）
- `drive:drive:permission` — 权限管理（添加协作者）

## 项目文档

- 需求文档: `skills/feishu-docs/docs/REQUIREMENTS.md`
- 架构文档: `skills/feishu-docs/docs/ARCHITECTURE.md`
- 开发日志: `skills/feishu-docs/docs/DEVLOG.md`

## 使用约束

- 飞书文档先写 MD 给用户确认再上传
