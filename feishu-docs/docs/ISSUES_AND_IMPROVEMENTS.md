# 飞书文档上传 — 踩坑经验与改进方案

> 整理自 session `feishu.ST.1772872297`（飞书端上传 SKILL.md）和 `webchat_1773055137`（Web 端上传国产化讨论提纲）

---

## 一、踩坑问题汇总

### 问题 1: 飞书表格行数限制（最多 9 行）

**现象**: 含 11 行（1 header + 10 data）的表格在 `create` 阶段直接返回 `invalid param`，无明确错误信息。

**根因**: 飞书 `CreateDocumentBlockChildren` API 创建 table block 时，单次创建的表格行数有上限（实测为 9 行，含 header）。

**影响**: 超过 9 行的表格直接创建失败，且错误信息不明确（只说 `invalid param`），导致定位困难。

**当时的解决方式**: 手动将超过 9 行的表格拆分为多个小表格后重新上传。

**改进方案**: 
- 在 `_write_table_block()` 中自动检测行数，超过 9 行时自动拆分为多个表格
- 拆分策略：header 行复制到每个子表格，数据行按 8 行一组拆分
- 拆分时在表格间插入提示文本（如 "（续表）"）

---

### 问题 2: 表格 Cell 写入触发 Rate Limit

**现象**: 大量表格的文档写入时，表格 cell 写入 API 频繁返回 HTTP 429 或飞书 code `99991400`（rate limited），导致 JSON 解析错误（API 返回空响应）。

**根因**: 每个表格的每个 cell 都需要一次独立的 HTTP API 调用。一个 7×9 的表格就需要 63 次 API 调用。文档中有 21 个表格时，总 API 调用量巨大，很容易触发飞书的频率限制。

**影响**: 
- 单个 cell 写入失败时，如果 retry 逻辑不够健壮，整个表格写入中断
- 多个表格连续写入时，前一个表格的 cell 写入还在触发 rate limit，后一个表格的创建也会失败
- JSON 解析错误（空响应）导致脚本直接 crash

**当时的解决方式**: 
- 将文档按表格边界拆分为 24 个 chunk 文件
- 用 subagent 执行 bash 脚本逐 chunk 写入，每个 chunk 之间加 5 秒延迟
- 总耗时约 10 分钟

**改进方案**:
- 在 `_write_table_block()` 中：表格间自动加延迟（如 2-3 秒）
- 增加表格创建步骤（Step 1）的 retry 逻辑（目前只有 cell 写入有 retry）
- 处理 API 返回空响应的情况（当前直接 crash）
- 考虑批量化：如果飞书 API 支持，一次写多个 cell 减少调用次数

---

### 问题 3: Markdown 表格解析曾完全不支持

**现象**: 早期版本中，Markdown 表格被当作纯文本段落写入飞书，完全没有表格效果。

**根因**: `md_to_blocks.py` 最初没有实现表格解析逻辑。

**影响**: 包含表格的文档在飞书上显示为乱码般的管道符文本。

**修复**: Phase 3 中新增了完整的表格解析 → 飞书 table block 转换逻辑。

**经验**: 新增 Markdown 语法支持时，应在 SKILL.md 中明确标注支持和不支持的语法。

---

### 问题 4: 大文档一次性写入失败

**现象**: 500+ 行的 Markdown 文档使用 `create-and-write` 一次性写入时，中途因 rate limit 失败，但已写入的部分无法回退，导致文档内容不完整。

**根因**: 
- `_write_blocks_to_doc()` 是线性执行的，中途失败则已写入的 block 留在文档中
- 没有事务机制（写入不是原子操作）
- 表格写入是 API 密集型操作，大文档很容易触发 rate limit

**影响**: 需要手动清空文档重新写入，或者出现重复内容。

**当时的解决方式**: 手动拆分文档为小段，逐段写入。

**改进方案**:
- 脚本层面：自动将大文档按"安全边界"（表格前后、章节标题处）拆分为 chunk
- 每个 chunk 写入后加适当延迟
- 失败时记录已写入的位置，支持断点续传
- 提供 `--chunk-delay` 参数控制写入节奏

---

### 问题 5: 调试写入导致内容重复

**现象**: 在调试过程中，多次 `write` 操作（append 模式）导致文档中出现重复内容（从开头到 8.3 重复了两遍）。

**根因**: 
- 第一次写入部分内容用于调试
- 后续 subagent 用 append 模式写入完整内容
- 两次写入叠加导致重复

**影响**: 用户看到的文档有大量重复内容。

**当时的解决方式**: 用 `delete-blocks` 清空文档后重新写入。

**改进方案**:
- `create-and-write` 应该是幂等的（新文档无此问题）
- 对于已有文档的重写场景，应默认使用 `overwrite` 而非 `append`
- 在 SKILL.md 中强调：调试写入后如需重新上传，务必先清空或用 overwrite

---

### 问题 6: 命令参数名记忆错误

**现象**: Agent 在调用脚本时，经常用错参数名，如 `--document-id`（不存在）代替 `--doc`，`add-collaborator`（不存在）代替 `add-member`。

**根因**: SKILL.md 文档中的参数说明不够突出，Agent（LLM）容易"脑补"参数名。

**影响**: 第一次调用失败，需要重试。

**改进方案**:
- SKILL.md 中每个命令都给出完整的复制可用的示例
- 参数名使用最直观的命名（当前 `--doc` 已经够简洁）
- 考虑增加参数别名（如 `--document-id` 作为 `--doc` 的 alias）

---

### 问题 7: Agent 在功能实现前就尝试使用（时序问题）

**现象**: Agent 在早期版本（Phase 3 之前）就尝试使用 `--mode overwrite` 参数，当时该功能尚未实现。

**根因**: LLM 根据语义推测参数应该存在，但当时脚本确实未实现。该功能后续在 Phase 3+ 中已实现（`write --mode overwrite`），现在可以正常使用。

**影响**: 当时调用失败需要重试；现在已不是问题。

**经验**: 
- SKILL.md 应及时更新，明确标注当前支持和不支持的功能
- SKILL.md 开头加"快速参考卡片"，列出所有可用命令和参数，减少 LLM 猜测

---

### 问题 8: 飞书应用文档创建在"应用根目录"

**现象**: 不指定 `--folder` 时，文档创建在飞书应用的根目录（"应用"文件夹下），而非用户个人空间。

**根因**: 飞书 API 使用 tenant_access_token（应用身份），创建的文档归属于应用。

**影响**: 用户需要通过 `add-member` 手动添加自己为协作者才能看到和编辑文档。

**改进方案**:
- `create-and-write` 默认自动添加用户为协作者（可通过配置获取用户 open_id）
- 或在 SKILL.md 中强调：创建文档后必须 add-member

---

### 问题 9: exec 命令超时

**现象**: 逐 chunk 写入大文档时，24 个 chunk × (写入时间 + 延迟) 超过了 exec 的默认超时限制。

**根因**: exec 工具默认超时较短，大文档写入是分钟级操作。

**影响**: 写入中途被 kill，需要重新来过。

**当时的解决方式**: 改用 subagent + bash 脚本执行。

**经验**:
- exec 工具支持手动指定 `timeout` 参数，最大可到 600s，应充分利用此特性
- SKILL.md 中应提示：大文档写入时建议 `exec timeout=600`
- 脚本自身也应改进：自动分段 + 进度反馈 + 断点续传，减少总耗时
- 对于预估超过 600s 的超大文档（20+ 表格），仍建议使用 subagent

---

### 问题 10: 嵌套代码块解析混乱（Bug #4）✅ 已修复

**现象**: 当 Markdown 中有嵌套代码块时（如用 ```````` 包裹 ```````），解析器无法正确匹配结束标记，导致后续内容全部被吞入代码块。

**根因**: `md_to_blocks.py` 代码块结束标记使用 `startswith('```')` 匹配，无法区分 ```````` 和 ```````。所有以 3 个 backtick 开头的行都会被当作结束标记。

**影响**: 嵌套代码块（如展示 Markdown 代码块语法的文档）解析完全错误，后续所有内容被吞入代码块。

**修复方案**:
- 记录开始 fence 的 backtick 数量（`fence_len`）
- 结束标记：行 strip 后以 ≥ fence_len 个 backtick 开头，且之后无非空字符
- 修改文件：`scripts/md_to_blocks.py`

---

### 问题 11: `read --format blocks` 无法读取表格 cell 文本（Bug #7）✅ 已修复

**现象**: `read --format blocks` 返回的表格 block 中，cell 的文本内容为空。

**根因**:
- `_block_to_dict()` 只处理了 text/heading/code 等 block_type，没有处理 table（block_type=31）
- `_read_blocks()` 只做了一层 list，没有按 parent-child 关系组织 table cell 内容
- 飞书 API 返回的 block 列表已包含所有 block（含 table cell 和 cell 内的 text block），但脚本未利用这些数据

**影响**: 使用 `read --format blocks` 获取的表格内容为空，无法用于后续编辑操作。

**修复方案**:
- `_block_to_dict()` 增加 table (block_type=31) 属性提取（row_size/column_size/header_row/cells）
- `_read_blocks()` 构建 block_id → block_dict 查找表，对 table block 的 cells 递归查找子 block 文本
- 输出新增 `table.cell_contents` 数组
- 修改文件：`scripts/feishu_doc.py`

---

### 问题 12: `read --format blocks` JSON 输出不稳定（Bug #9）✅ 已修复

**现象**: 某些情况下 blocks 输出无法被 JSON 解析，`json.dumps` 抛出 TypeError。

**根因**:
- `_block_to_dict()` 中某些属性是 lark-oapi 对象（如 TextElementStyle、Link 等）而非基础类型
- `json.dumps` 无法序列化这些对象，导致整个输出失败
- 没有 try-except 保护，错误直接 crash

**影响**: Agent 无法解析 `read --format blocks` 的输出，后续操作中断。

**修复方案**:
- 新增 `_safe_serialize()` 辅助函数：递归转换所有值为 JSON 可序列化的基础类型
- `_block_to_dict()` 返回值经过 `_safe_serialize` 处理
- `_read_blocks()` 的 `json.dumps` 增加 try-except 保护，失败时 fallback 到 `_safe_serialize` 再试
- 修改文件：`scripts/feishu_doc.py`

---

## 二、改进方案优先级

### P0 — 必须修复（影响基本可用性）

| # | 改进 | 说明 |
|---|------|------|
| P0-1 | **表格行数自动拆分** | 超过 9 行的表格自动拆分为多个子表格，header 复制 |
| P0-2 | **表格创建 retry** | `_write_table_block()` Step 1 增加 retry + 空响应处理 |
| P0-3 | **表格间自动延迟** | 连续写入多个表格时，表格之间加 2-3 秒延迟 |

### P1 — 重要改进（提升大文档体验）

| # | 改进 | 说明 |
|---|------|------|
| P1-1 | **大文档自动分段写入** | 自动按章节/表格边界拆分，逐段写入 + 延迟 |
| P1-2 | **写入进度反馈** | 每写完一段输出进度（如 `[5/24] Section 2.1 written`） |
| P1-3 | **断点续传** | 记录已写入位置，失败后可从断点继续 |
| P1-4 | **create-and-write 自动添加协作者** | 支持 `--add-member ou_xxx` 参数，创建后自动添加 |

### P2 — 体验优化

| # | 改进 | 说明 |
|---|------|------|
| P2-1 | **参数别名** | `--document-id` → `--doc`，`--comment-id` → `--comment` 等 |
| P2-2 | **SKILL.md 快速参考卡片** | 顶部增加所有命令的一行式速查 |
| P2-3 | **错误信息增强** | 表格创建失败时输出具体原因（如"行数超限"） |
| P2-4 | **SKILL.md 大文档提示** | 提示大文档写入建议 `exec timeout=600`；超大文档（20+ 表格）建议 subagent |

---

## 三、具体实施方案

### P0-1: 表格行数自动拆分

修改 `md_to_blocks.py` 中的 `_parse_table()` 函数：

```python
MAX_TABLE_ROWS = 9  # 飞书限制，含 header

def _parse_table(lines, i):
    # ... 现有解析逻辑 ...
    
    if len(rows) > MAX_TABLE_ROWS:
        # 拆分为多个子表格
        return _split_table(rows, col_count, column_widths), next_i
    else:
        return [table_block], next_i

def _split_table(rows, col_count, column_widths):
    """将超过 MAX_TABLE_ROWS 的表格拆分为多个子表格"""
    header = rows[0]
    data_rows = rows[1:]
    max_data_per_table = MAX_TABLE_ROWS - 1  # 减去 header
    
    blocks = []
    for chunk_start in range(0, len(data_rows), max_data_per_table):
        chunk = data_rows[chunk_start:chunk_start + max_data_per_table]
        sub_rows = [header] + chunk
        
        if chunk_start > 0:
            # 在续表前加提示
            blocks.append(_make_text_block(BLOCK_TYPE_TEXT, "（续表）"))
        
        blocks.append({
            "block_type": BLOCK_TYPE_TABLE,
            "table": {
                "rows": sub_rows,
                "column_size": col_count,
                "header_row": True,
                "column_widths": column_widths,
            }
        })
    return blocks
```

### P0-2 + P0-3: 表格创建 retry + 表格间延迟

修改 `feishu_doc.py` 中的 `_write_table_block()` 和 `_write_blocks_to_doc()`：

```python
# _write_table_block() Step 1 增加 retry
max_retries = 3
for attempt in range(max_retries):
    response = client.docx.v1.document_block_children.create(request)
    if response.success():
        break
    if response.code == 99991400 or "rate" in (response.msg or "").lower():
        wait = 2 * (2 ** attempt)
        time.sleep(wait)
    else:
        # 非限流错误，直接返回失败
        break

# _write_blocks_to_doc() 中表格间加延迟
TABLE_DELAY = 3  # 秒
for seg_type, seg_data in segments:
    if seg_type == "table":
        time.sleep(TABLE_DELAY)
        ok = _write_table_block(...)
```

### P1-1: 大文档自动分段写入

修改 `_write_blocks_to_doc()` 增加分段逻辑：

```python
CHUNK_SIZE = 30  # 每批最多 30 个 block
CHUNK_DELAY = 2  # 每批之间延迟 2 秒

# 将 segments 进一步按 CHUNK_SIZE 分组
# 表格单独算一个 chunk
# 每个 chunk 写入后 sleep(CHUNK_DELAY)
```

### P1-4: create-and-write 自动添加协作者

```python
# cmd_create_and_write() 增加 --add-member 参数
if args.add_member:
    # 创建文档后自动添加协作者
    _add_member(client, doc_id, args.add_member, "full_access")
```

---

*整理日期: 2026-03-09*
*来源 session: feishu.ST.1772872297, webchat_1773055137*
