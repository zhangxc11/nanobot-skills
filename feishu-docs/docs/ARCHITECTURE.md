# 飞书文档 Skill — 架构文档

## 项目结构

```
skills/feishu-docs/
├── SKILL.md                    # Skill 入口（nanobot 加载）
├── docs/
│   ├── REQUIREMENTS.md         # 需求文档
│   ├── ARCHITECTURE.md         # 架构文档（本文件）
│   └── DEVLOG.md               # 开发日志
├── scripts/
│   ├── feishu_doc.py           # 核心 Python 脚本（统一入口）
│   └── md_to_blocks.py         # Markdown → 飞书 Block 转换器
└── tests/
    └── test_md_to_blocks.py    # Markdown 转换器单元测试
```

## 架构设计

### 整体方案

采用 **Python CLI 脚本** 作为 Skill 实现方式：
- Agent 通过 `exec` 工具调用 Python 脚本
- 脚本自行从 `~/.nanobot/config.json` 读取飞书凭证
- 脚本使用 `lark-oapi` SDK 调用飞书 API
- 输出结果到 stdout，供 agent 解析

### 核心组件

#### 1. `feishu_doc.py` — 统一 CLI 入口

```
用法:
  python3 feishu_doc.py create --title "标题" [--folder TOKEN]
  python3 feishu_doc.py write --doc DOC_ID --markdown "# 内容"
  python3 feishu_doc.py write --doc DOC_ID --markdown-file path/to/file.md
  python3 feishu_doc.py write --doc DOC_ID --mode overwrite --markdown "# 替换内容"
  python3 feishu_doc.py read --doc DOC_ID [--format raw|blocks]
  python3 feishu_doc.py create-and-write --title "标题" --markdown "# 内容" [--folder TOKEN]
  python3 feishu_doc.py create-and-write --title "标题" --markdown-file path/to/file.md [--folder TOKEN]
  python3 feishu_doc.py patch-block --doc DOC_ID --block BLOCK_ID --text "新内容"
  python3 feishu_doc.py delete-blocks --doc DOC_ID --start 0 --end 2 [--parent BLOCK_ID]
  python3 feishu_doc.py insert-blocks --doc DOC_ID --index 1 --markdown "# 插入内容" [--parent BLOCK_ID]
  python3 feishu_doc.py list-comments --doc DOC_ID [--status all|solved|unsolved]
  python3 feishu_doc.py reply-comment --doc DOC_ID --comment COMMENT_ID --text "回复内容"
  python3 feishu_doc.py resolve-comment --doc DOC_ID --comment COMMENT_ID
  python3 feishu_doc.py add-comment --doc DOC_ID --quote "引用文本" --text "批注内容"
  python3 feishu_doc.py add-member --doc DOC_ID --open-id OPEN_ID [--perm full_access|edit|view]
```

子命令:
- `create` — 创建空白文档
- `write` — 向已有文档追加/覆盖写入内容
- `read` — 读取文档内容（raw 纯文本 / blocks 结构）
- `create-and-write` — 创建文档并写入内容（组合命令）
- `patch-block` — **原地更新**指定 block 的文本内容（局部编辑）
- `delete-blocks` — **删除**指定 index 范围的子 block（局部编辑）
- `insert-blocks` — 在指定 index 位置**插入** Markdown 内容（局部编辑）
- `list-comments` — 列出文档批注
- `reply-comment` — 回复指定批注
- `resolve-comment` — 标记批注为已解决
- `add-comment` — 在文档上创建新批注
- `add-member` — 为文档添加协作者

#### 2. `md_to_blocks.py` — Markdown → 飞书 Block 转换器

将 Markdown 文本解析为飞书 Block 数据结构（JSON），支持：

| Markdown 语法 | 飞书 Block 类型 | block_type 值 |
|---|---|---|
| 普通段落 | text | 2 |
| `# H1` | heading1 | 3 |
| `## H2` | heading2 | 4 |
| `### H3` | heading3 | 5 |
| `#### H4` ~ `######### H9` | heading4~9 | 6~11 |
| `- item` | bullet | 12 |
| `1. item` | ordered | 13 |
| `` ```code``` `` | code | 14 |
| `> quote` | quote | 15 |
| `- [ ] todo` / `- [x] todo` | todo | 17 |
| `---` | divider | 22 |

文本内联样式支持：
| Markdown | 飞书 TextElementStyle |
|---|---|
| `**bold**` | bold: true |
| `*italic*` | italic: true |
| `~~strike~~` | strikethrough: true |
| `` `code` `` | inline_code: true |
| `[text](url)` | link.url |

### 数据流

```
Agent (exec)
  → python3 feishu_doc.py <command> <args>
    → 读取 ~/.nanobot/config.json 获取 appId/appSecret
    → 初始化 lark.Client (tenant_access_token)
    → [如果有 markdown] md_to_blocks.py 转换
    → 调用飞书 API
    → 输出 JSON 结果到 stdout
  ← Agent 解析结果
```

### 凭证管理

```python
# feishu_doc.py 内部
config = json.load(open(os.path.expanduser("~/.nanobot/config.json")))
feishu_apps = config["channels"]["feishu"]
# 找到 name="ST" 的应用
st_app = next(app for app in feishu_apps if app.get("name") == "ST")
app_id = st_app["appId"]
app_secret = st_app["appSecret"]
```

**安全保证**：
- appSecret 仅在脚本进程内使用，不输出到 stdout
- Agent 不直接接触密钥，只调用脚本命令

### 飞书 API 调用链

#### 创建文档
```
POST /open-apis/docx/v1/documents
Body: { "title": "...", "folder_token": "..." }
Response: { "document": { "document_id": "...", "title": "...", "revision_id": 1 } }
```

#### 写入 Block
```
POST /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children
Body: { "children": [ Block, Block, ... ] }
```
- `block_id` = `document_id`（根 Block = 文档本身）

#### 读取文档
```
GET /open-apis/docx/v1/documents/{document_id}/raw_content
Response: { "content": "纯文本内容" }
```

```
GET /open-apis/docx/v1/documents/{document_id}/blocks
Response: { "items": [ Block, Block, ... ] }
```

#### 列出批注
```
GET /open-apis/drive/v1/files/{file_token}/comments?file_type=docx
Response: { "items": [ { "comment_id", "reply_list": { "replies": [...] }, "is_solved" } ] }
```

#### 回复批注
```
POST /open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies?file_type=docx
Body: { "content": { "elements": [{ "type": "text_run", "text_run": { "text": "..." } }] } }
```
- SDK 中无 CreateFileCommentReply，使用 requests 直接调用 HTTP API

#### 解决批注
```
PATCH /open-apis/drive/v1/files/{file_token}/comments/{comment_id}?file_type=docx
Body: { "is_solved": true }
```

#### 创建批注
```
POST /open-apis/drive/v1/files/{file_token}/comments?file_type=docx
Body: { "content": { "elements": [...] }, "quote": "引用文本" }
```
- SDK 中 CreateFileComment 需要 quote（引用的文档原文）和 content（批注正文）

#### 添加协作者
```
POST /open-apis/drive/v1/permissions/{token}/members?type=docx
Body: { "member_type": "openid", "member_id": "ou_xxx", "perm": "full_access" }
```

#### 局部编辑 — 原地更新 Block 内容
```
PATCH /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}
Body: {
  "update_text_elements": {
    "elements": [{ "text_run": { "content": "新内容", "text_element_style": { "bold": true } } }]
  }
}
Response: { "code": 0, "data": { "block": {...}, "document_revision_id": 29 } }
```
- 使用 HTTP PATCH API 而非 SDK（SDK 的 `PatchDocumentBlock` 传 `Block` 对象会返回 `invalid param`）
- `update_text_elements.elements` 格式与 Block 创建时的 `text.elements` 一致
- 复用 `_get_tenant_token()` 获取 tenant_access_token

#### 局部编辑 — 删除指定范围的 Block
```
DELETE /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children/batch_delete
Body: { "start_index": 0, "end_index": 2 }
```
- 使用 SDK `BatchDeleteDocumentBlockChildren`（与 overwrite 的 `_clear_document` 共用同一 API）
- `block_id` 为父 block ID（默认为 page block = doc_id）
- `start_index` 包含，`end_index` 不包含

#### 局部编辑 — 在指定位置插入 Block
```
POST /open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children
Body: { "children": [Block, ...], "index": 1 }
```
- 使用 SDK `CreateDocumentBlockChildren`，与 `write` 追加逻辑共用
- 追加时 `index=-1`（末尾），局部插入时 `index` 为 0-based 位置
- 表格插入暂不支持指定 index（受限于两步创建流程），会追加到文档末尾

#### 更新表格列宽
```
PATCH /open-apis/docx/v1/documents/{document_id}/blocks/{table_block_id}
Body: {
  "update_table_property": {
    "column_width": 280,
    "column_index": 1
  }
}
Response: { "code": 0, "data": { "block": {...}, "document_revision_id": N } }
```
- 每次只能更新一列的宽度，需逐列调用
- `column_index` 为 0-based 列号
- `column_width` 为像素值（整数）
- 创建表格后自动调用，列宽由 `md_to_blocks._calculate_column_widths()` 计算

### 列宽计算算法

```
输入: 表格所有行的单元格内容
输出: 每列的像素宽度列表

1. 计算每列最大显示宽度（CJK字符=2, ASCII=1, 去除Markdown标记）
2. 对每列宽度取平方根（压缩长短列差异）
3. 按平方根比例分配总宽度（默认600px）
4. Clamp到 [80, 400] px 范围
```

**为什么用平方根比例？**
- 线性比例会导致短内容列过窄、长内容列过宽
- 平方根压缩了比例差异，更接近人类手动调整的视觉效果
- 参考手动调整的表格：分类列(5字符)→207px vs 含义列(35字符)→192px，比值远小于内容比

## 设计决策

### 为什么用 Python 脚本而不是 Shell 脚本？
- 飞书 Block 数据结构复杂（嵌套 JSON），Shell 处理困难
- `lark-oapi` 是 Python SDK，直接调用最方便
- Markdown 解析需要正则/状态机，Python 更合适

### 为什么用 CLI 而不是 HTTP 服务？
- Skill 是 agent 按需调用，不需要常驻进程
- CLI 更简单，无需管理服务生命周期
- 每次调用独立初始化 Client，无状态，无冲突

### 为什么选择 ST 应用？
- ST 应用已开通文档权限
- 可通过 `--app` 参数扩展支持其他应用

### 为什么 patch-block 用 HTTP API 而非 SDK？
- SDK 的 `PatchDocumentBlockRequest` 接受 `Block` 对象作为 `request_body`，但飞书服务端实际期望的是 `update_text_elements` 结构
- 直接传 `Block` 对象会返回 `[1770001] invalid param`
- HTTP PATCH API 可以精确控制 JSON body 格式，成功率 100%
- 复用 `_get_tenant_token()` helper（与 reply-comment、add-comment 共用）

### 为什么优先局部编辑而非 overwrite？
- overwrite 会删除所有现有 block 再重写，飞书编辑历史中只能看到"全部删除+全部新增"
- 局部编辑（patch/delete/insert）保留完整的编辑历史，飞书版本对比可以精确看到每个 block 的变更
- 局部编辑更安全：误操作只影响目标 block，不会丢失整个文档内容
- overwrite 仅作为极端场景的后备方案保留

### 表格行数自动拆分

飞书 API 创建表格时单次最多 9 行（含 header），`md_to_blocks.py` 的 `_parse_table()` 在解析完表格后检测行数：
- 行数 ≤ 9：返回单个 table block（原有逻辑不变）
- 行数 > 9：调用 `_split_table()` 拆分为多个子表格
  - header 行复制到每个子表格
  - 数据行按 8 行一组（MAX_TABLE_ROWS - 1）
  - 续表间插入 `（续表）` 提示文本 block
  - 返回 block 列表（table + text + table + ...）

### 表格创建 retry + 空响应处理

`_write_table_block()` Step 1（SDK 创建空表格）增加 retry：
- 最多 3 次重试，指数退避（`2 * 2^attempt` 秒）
- 识别 rate limit：API code 99991400 或 HTTP 429
- 空响应处理：`response.raw` 或 `response.data` 为空时不 crash，返回 False

### 表格间自动延迟

`_write_blocks_to_doc()` 写入 table segment 前检查是否已有表格写入：
- 第一个表格不延迟
- 后续表格写入前 sleep 3 秒
- 普通文本 block 不触发延迟

### 大文档自动分段写入

`_write_blocks_to_doc()` 将 block_dicts 按"安全边界"拆分为 chunk 后逐段写入：

**分段策略**：
1. 先按 block 类型分成 segment（连续 regular blocks 为一组，每个 table 独立一组）
2. 对 regular segment 进一步按"安全边界"拆分：heading 前断开
3. 每个 chunk 最多 30 个 regular blocks（`CHUNK_MAX_BLOCKS = 30`）
4. 表格单独算一个 chunk

**延迟策略**：
- 表格 chunk 间保持 P0-3 的 3 秒延迟
- 普通 chunk 间加 1 秒延迟（`CHUNK_DELAY = 1`）
- 第一个 chunk 不延迟

### 写入进度反馈

每写完一个 chunk 输出进度到 stderr（不影响 stdout JSON）：
- 格式: `[3/12] Writing chunk 3 (5 blocks)...` 或 `[3/12] Writing chunk 3 (table)...`
- 完成: `[12/12] All chunks written successfully.`

### 断点续传

`_write_blocks_to_doc()` 接受 `resume_from` 参数（默认 0）：
- 跳过序号 < resume_from 的 chunk
- 失败时输出: `ERROR: Failed at chunk 8/12. Resume with: --resume-from 8`
- write 和 create-and-write 子命令通过 `--resume-from` argparse 参数传递

### create-and-write 自动添加协作者

提取 `_add_member(client, doc_id, open_id, perm)` 内部函数：
- `cmd_add_member(args)` 改为调用 `_add_member()` 的 wrapper
- `cmd_create_and_write()` 在创建文档后、写入内容前调用 `_add_member()`
- 通过 `--add-member` 和 `--member-perm` 参数传递

### 表格创建错误信息增强（P2-B）

`_write_table_block()` Step 1 失败时的错误信息改进：
- 错误信息包含表格维度：`Table create failed (rows=11, cols=7): [code] msg`
- 错误码 `1770001`（invalid param）且行数 > 9 时，追加提示：`NOTE: 飞书限制单次创建最多 9 行`
- 错误码 `99991400`（rate limit）时，追加提示：`触发频率限制，将自动重试`
- 作为防御性提示，即使 P0-1 已自动拆分，仍在错误信息中给出诊断线索

### 嵌套代码块解析（Bug #4）

`md_to_blocks.py` 的代码块解析支持 N-backtick fence（N ≥ 3）：
- 记录开始 fence 的 backtick 数量（`fence_len`）
- 结束标记：行 strip 后以 ≥ fence_len 个 backtick 开头，且之后无非空字符
- 支持 4 backtick 包裹 3 backtick 等嵌套场景
- 无结束标记时，剩余内容全部作为代码块内容

### read blocks 表格内容读取（Bug #7）

`_block_to_dict()` 增加对 table block（block_type=31）的处理：
- 提取 `block.table.property`（row_size, column_size, header_row）
- 提取 `block.table.cells`（cell block ID 列表）

`_read_blocks()` 增加 parent-child 关系解析：
- 构建 block_id → block_dict 查找表
- 对 table block 的 cells，递归查找 cell → child text block 的内容
- 输出 `table.cell_contents` 数组，包含每个 cell 的文本内容

### read blocks JSON 序列化健壮性（Bug #9）

新增 `_safe_serialize()` 辅助函数：
- 递归处理 dict/list/基础类型
- lark-oapi 对象（有 `__dict__`）转换为 dict（排除 `_` 前缀属性）
- 其他未知类型降级为 `str()`
- `_block_to_dict()` 最后对 result 做 `_safe_serialize` 处理
- `_read_blocks()` 的 `json.dumps` 增加 try-except，失败时输出有意义的错误到 stderr

---

*创建日期: 2026-02-28*
