# 飞书文档 Skill — 需求文档

## 概述

为 nanobot 提供飞书文档的创建和编辑能力。通过 `lark-oapi` SDK 调用飞书开放平台 API，实现在飞书上创建、读取、编辑文档。

## 背景

- nanobot gateway 已接入飞书（ST / lab 两个应用），具备 IM 聊天能力
- ST 应用已开通文档相关权限（docx、drive、sheets、bitable、wiki）
- `lark-oapi` SDK 已安装，包含完整的 docx API 模块
- 以独立 Skill 形式实现，不修改 nanobot 核心代码

## 已开通权限（ST 应用）

### 文档类
- `docx:document` — 文档管理
- `docx:document:create` — 创建文档
- `docx:document:readonly` — 读取文档
- `docx:document:write_only` — 写入文档
- `docx:document.block:convert` — Block 转换
- `docs:document.content:read` — 读取文档内容

### 云盘类
- `drive:drive.metadata:readonly` — 云盘元数据只读
- `drive:drive:version` — 版本管理
- `drive:drive:version:readonly` — 版本只读

### 表格类
- `sheets:spreadsheet` — 电子表格读写
- `sheets:spreadsheet:create` — 创建电子表格
- `sheets:spreadsheet:readonly` — 只读电子表格
- `sheets:spreadsheet.meta:read` — 表格元数据读取
- `sheets:spreadsheet.meta:write_only` — 表格元数据写入

### 多维表格
- `bitable:app` — 多维表格读写
- `bitable:app:readonly` — 多维表格只读

### 知识库
- `wiki:wiki:readonly` — 知识库只读

## 功能需求

### Phase 1: 核心文档操作（MVP）

#### F1.1 创建文档
- 创建空白飞书文档，支持指定标题
- 支持指定目标文件夹（folder_token）
- 返回文档 URL 和 document_id

#### F1.2 写入文档内容
- 向文档追加内容（Markdown → 飞书 Block）
- 支持的 Block 类型：
  - 文本段落（text）
  - 标题（heading1 ~ heading9）
  - 无序列表（bullet）
  - 有序列表（ordered）
  - 代码块（code）
  - 引用（quote）
  - 待办事项（todo）
  - 分割线（divider）
- Markdown 到 Block 的自动转换

#### F1.3 读取文档内容
- 读取文档纯文本内容（raw_content）
- 读取文档 Block 结构（list blocks）

#### F1.4 创建并写入（一步到位）
- 创建文档 + 写入 Markdown 内容的组合命令
- 最常用的场景：一句话生成一篇文档

### Phase 2: 批注（Comment）操作

#### F2.1 列出文档批注
- 列出指定文档的所有批注（含回复内容）
- 支持过滤已解决/未解决的批注
- 返回批注 ID、内容、回复列表、解决状态

#### F2.2 回复批注
- 对指定批注添加回复
- 支持纯文本内容

#### F2.3 解决批注
- 将指定批注标记为已解决

#### F2.4 创建批注
- 在文档上创建新批注
- 需要指定 quote（引用的文档内容）和批注正文

#### F2.5 权限管理（协作者）
- 为文档添加协作者（通过 open_id）
- 支持设置权限级别（full_access / edit / view）

### Phase 3: 覆盖写入 + 表格支持

#### F3.1 write 命令覆盖模式
- `write --doc DOC_ID --mode overwrite` — 先清空文档所有子 block，再写入新内容
- 默认行为（`--mode append`）保持不变：追加到文档末尾
- `create-and-write` 命令不需要 mode 参数（新文档本身为空）
- 清空逻辑：调用 `BatchDeleteDocumentBlockChildren` API，传入 `start_index=0, end_index=子block数量`

#### F3.2 Markdown 表格 → 飞书 Table Block
- 解析标准 Markdown 表格语法（`| col1 | col2 |` + `|---|---|`）
- 生成飞书 `table` block（block_type=31）
- 表格属性：row_size, column_size, header_row=true
- 单元格内容支持 inline 样式（bold, italic, code, link）
- 飞书表格创建方式：先创建空 table block，再向每个 cell 写入内容

#### F3.3 文档说明完善
- SKILL.md 明确标注 write 命令默认为追加模式
- SKILL.md 明确列出支持和不支持的 Markdown 语法
- ARCHITECTURE.md 更新表格相关 API 说明

### Phase 4: 局部编辑（Block 级别操作） ✅

> **设计原则**：修改已有文档时优先使用局部编辑，避免 overwrite 全量覆盖。overwrite 会丢失飞书编辑历史，无法查看修改前后对比。

#### F4.1 原地更新 Block 内容（patch-block） ✅
- 通过 block_id 定位目标 block，原地更新其文本内容
- 支持行内 Markdown 格式（bold, italic, code, link, strikethrough）
- 适用于 text / heading / bullet / ordered 等文本类 block
- 使用飞书 HTTP PATCH API（`update_text_elements`），SDK 的 PatchDocumentBlock 参数格式不兼容
- 保留飞书完整编辑历史

#### F4.2 删除指定范围的 Block（delete-blocks） ✅
- 按 index 范围删除 page block 的子 block（start_index inclusive, end_index exclusive）
- 支持指定 parent block（默认为 page block = doc_id）
- 使用 SDK `BatchDeleteDocumentBlockChildren` API

#### F4.3 在指定位置插入内容（insert-blocks） ✅
- 在指定 index 位置插入 Markdown 内容（0-based，插入到该 index 之前）
- 支持完整 Markdown 语法（含表格）
- 支持 `--markdown` 和 `--markdown-file` 两种输入方式
- 支持指定 parent block
- 使用 SDK `CreateDocumentBlockChildren` 的 `index` 参数

#### F4.4 推荐编辑工作流
- 编辑前必须先 `read --format blocks` 获取文档结构（block ID + index）
- 优先 patch-block（修改）→ delete-blocks（删除）→ insert-blocks（插入）
- 仅在需要彻底重写整个文档时才使用 `write --mode overwrite`

### Phase 5: 高级功能（后续）

#### F5.1 表格列宽自动计算 ✅
- 创建表格时自动根据单元格内容计算合适的列宽
- CJK 字符按 2 宽度单位计算，ASCII 按 1 宽度单位
- 使用平方根比例分配（压缩长短列差异，视觉更均衡）
- 默认总宽 600px，单列最小 80px，最大 400px
- 创建表格后通过 PATCH API（`update_table_property`）逐列设置宽度
- 解决之前所有列默认 100px 导致表格过窄、内容换行过多的问题

#### F5.2 表格 Cell 写入健壮性 ✅
- 表格 cell 写入增加 HTTP 429 指数退避重试（最多 5 次）
- 写入 cell 内容后删除飞书自动生成的默认空 text block（避免多余空行）
- insert-blocks 命令支持表格的 index 定位（之前硬编码为追加到末尾）

#### F5.3 电子表格操作（后续）
- 创建电子表格
- 读写单元格数据

#### F5.4 多维表格操作（后续）
- 读写多维表格记录

## 技术约束

### 安全性
- **appSecret 不得暴露给 LLM** — 脚本从 config.json 加载凭证，agent 不直接接触密钥
- 脚本中硬编码 config.json 路径，自行读取 appId/appSecret

### 兼容性
- 使用 ST 应用的凭证（config.json 中 feishu 数组第一个 name="ST" 的条目）
- 与 gateway 的 WebSocket 连接无冲突（独立 HTTP Client）

### 依赖
- Python 3.11+
- lark-oapi（已安装在 nanobot venv311 中）

### Phase 6: P0 健壮性修复

> 详细背景见 `docs/ISSUES_AND_IMPROVEMENTS.md`

#### F6.1 表格行数自动拆分（P0-1）
- 飞书 API 创建表格时单次最多 9 行（含 header），超过返回 `invalid param`
- `md_to_blocks.py` 的 `_parse_table()` 自动检测行数，超过 9 行时拆分为多个子表格
- 拆分策略：header 行复制到每个子表格，数据行按 8 行一组，续表间加"（续表）"提示文本

#### F6.2 表格创建 retry + 空响应处理（P0-2）
- `feishu_doc.py` 的 `_write_table_block()` Step 1（创建空表格）增加 retry 逻辑（最多 3 次，指数退避）
- 处理 API 返回空响应的情况（当前 JSON 解析 crash）
- 识别 rate limit 错误码（99991400 或 HTTP 429）

#### F6.3 表格间自动延迟（P0-3）
- `feishu_doc.py` 的 `_write_blocks_to_doc()` 连续写入多个表格时，表格之间自动加 3 秒延迟
- 只有表格需要延迟，普通文本 block 不需要

### Phase 7: P1 大文档体验改进

> 详细背景见 `docs/ISSUES_AND_IMPROVEMENTS.md`

#### F7.1 大文档自动分段写入（P1-1）
- `_write_blocks_to_doc()` 自动将 block_dicts 按"安全边界"分段写入
- 安全边界：表格前后、heading 前
- 每段（chunk）最多 30 个 regular blocks，表格单独算一个 chunk
- 每段写完后加 1-2 秒延迟，表格间延迟保持 P0-3 的 3 秒
- 不破坏现有表格间延迟逻辑

#### F7.2 写入进度反馈（P1-2）
- 每写完一个 chunk/segment，输出进度到 stderr
- 格式: `[3/12] Writing chunk 3 (5 blocks + 1 table)...`
- 完成时: `[12/12] All chunks written successfully.`
- 使用 `print(..., file=sys.stderr)`，不影响 stdout JSON 输出

#### F7.3 断点续传（P1-3）
- 新增 `--resume-from` 参数（chunk 序号），从指定 chunk 开始写入
- 写入失败时输出已完成 chunk 数和续传命令到 stderr
- resume 时使用 append 模式，从上次失败位置继续
- write 和 create-and-write 子命令均支持

#### F7.4 create-and-write 自动添加协作者（P1-4）
- `create-and-write` 新增 `--add-member` 参数（open_id）
- 可选 `--member-perm` 参数（默认 `full_access`）
- 创建文档后、写入内容前自动添加协作者
- 提取 `_add_member()` 内部函数，`cmd_add_member` 改为 wrapper

### Phase 8: P2 改进

#### F8.1 SKILL.md 全面更新（P2-A）
- 在 SKILL.md 顶部增加"命令速查"表格，每个命令一行列出完整参数签名
- 确保每个命令的参数完整枚举，特别是 P1 新增的 `--resume-from`、`--add-member`、`--member-perm`
- 新增"大文档写入建议"使用技巧
- 逐个检查所有 12+ 个命令的参数说明完整性

#### F8.2 错误信息增强（P2-B）
- `_write_table_block()` Step 1 表格创建失败时，错误信息包含表格维度（rows, cols）
- 错误码 1770001（invalid param）且行数 > 9 时，额外提示"行数超限"
- 错误码 99991400（rate limit）时，提示"触发频率限制，将自动重试"

### Phase 9: 写入工具健壮性修复 ✅

> TODO: 28895c39
> 状态: 已完成
> 详情: [requirements/phase9-write-robustness.md](requirements/phase9-write-robustness.md)

#### F9.1 嵌套列表 schema mismatch 修复 ✅
- 目录(TOC)中的嵌套列表（缩进子项）导致 `[1770006] schema mismatch`
- 飞书 API 不支持嵌套列表的 `children` 字段
- `md_to_blocks.py` 转换器自动扁平化嵌套列表

#### F9.2 文档内锚点链接降级 ✅
- `[text](#anchor)` 格式的锚点链接导致写入失败
- 转换器自动将锚点链接降级为纯文本

#### F9.3 大文档写入超时优化 ✅
- 884 行文档写入过程中 600s 超时
- CHUNK_MAX_BLOCKS 30→50，chunk 间 delay 1s→0.5s

#### F9.4 续传内容重复修复 ✅
- 超时后续传导致文档开头出现重复内容
- 改进 resume hint：提供 skip/retry 两个选项 + blocks_written 计数

#### F9.5 加粗标题格式异常修复 ✅
- 整行加粗标题（如 **方案 2: 后台执行**）格式渲染异常
- 全行加粗检测为独立 block，段落收集器遇到全行加粗时停止

## Issues

### Bug #4: 嵌套代码块解析混乱 ✅ (已修复)
- `md_to_blocks.py` 代码块解析使用 `startswith('```')` 匹配结束标记，无法区分不同 backtick 数量的 fence
- 修复：记录开始 fence 的 backtick 数量，结束标记必须匹配相同或更多数量

### Bug #7: `read --format blocks` 无法读取表格 cell 文本 ✅ (已修复)
- `_block_to_dict()` 未处理 table（block_type=31）的属性
- `_read_blocks()` 未按 parent-child 关系组织表格 cell 内容
- 修复：`_block_to_dict` 增加 table 属性提取；`_read_blocks` 构建 block 查找表并解析 cell 文本

### Bug #9: `read --format blocks` JSON 输出不稳定 ✅ (已修复)
- `_block_to_dict` 中某些属性可能是 lark-oapi 对象，`json.dumps` 无法序列化
- 修复：新增 `_safe_serialize()` 函数确保所有值为基础类型；`_read_blocks` 的 `json.dumps` 增加 try-except 保护

---

*创建日期: 2026-02-28*
