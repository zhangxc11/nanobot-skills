# 飞书文档 Skill — 开发日志

## Phase 1: MVP — 核心文档操作

### 2026-02-28 Session 1: 项目初始化 + 核心实现 ✅

#### 任务拆解
- [x] 创建项目目录结构
- [x] 编写需求文档 (REQUIREMENTS.md)
- [x] 编写架构文档 (ARCHITECTURE.md)
- [x] 编写开发日志 (DEVLOG.md)
- [x] 实现 `md_to_blocks.py` — Markdown → 飞书 Block 转换器
- [x] 编写 `md_to_blocks` 单元测试 — 29 项全部通过
- [x] 实现 `feishu_doc.py` — 统一 CLI 入口
- [x] 编写 SKILL.md
- [x] 端到端测试
- [x] Git 初始化 + 首次提交 (3f0c81a)

#### 端到端测试结果

| 命令 | 结果 | 详情 |
|---|---|---|
| `create` | ✅ | 创建文档 `MVd5dJAmto0vuDxVEjvcxIBRnsg` |
| `write` | ✅ | 写入 14 个 blocks (标题/列表/代码/引用/分割线/待办) |
| `read --format raw` | ✅ | 正确读取纯文本内容 |
| `create-and-write` | ✅ | 一步创建 `WSrPds2S3ovK6BxvJ8xcwVMmn3c` + 写入 7 blocks |

#### 技术细节
- `lark-oapi` SDK 的 `from lark_oapi.api.docx.v1 import *` 可导入所有需要的模型类
- Block 写入使用 `document_block_children.create()`，block_id = document_id（根节点）
- 批量写入限制：每次最多 50 个 blocks
- 凭证加载：从 `~/.nanobot/config.json` 的 `channels.feishu` 数组中查找 `name="ST"` 的条目

---

## Phase 2: 批注（Comment）操作

### 2026-02-28 Session 2: 批注功能实现 ✅

#### 任务拆解
- [x] 更新需求文档 (REQUIREMENTS.md) — 新增 Phase 2 批注需求
- [x] 更新架构文档 (ARCHITECTURE.md) — 新增批注相关 API 和子命令
- [x] 实现 `list-comments` 子命令 — 列出文档批注
- [x] 实现 `reply-comment` 子命令 — 回复批注（HTTP API，SDK 缺 CreateReply）
- [x] 实现 `resolve-comment` 子命令 — 标记批注已解决
- [x] 实现 `add-comment` 子命令 — 创建新批注（HTTP API）
- [x] 实现 `add-member` 子命令 — 添加协作者
- [x] 更新 SKILL.md — 新增命令文档
- [x] 端到端测试：全部 5 个新命令测试通过
- [x] Git 提交

#### 端到端测试结果

| 命令 | 结果 | 详情 |
|---|---|---|
| `list-comments` | ✅ | 读取到 2 条批注，含回复内容和解决状态 |
| `reply-comment` | ✅ | 成功回复批注 7611786999179054038 |
| `resolve-comment` | ✅ | 成功标记批注为已解决 |
| `add-comment` | ✅ | 成功创建新批注 7611788674048527305 |
| `add-member` | (已在 session 前手动验证) | 通过 SDK CreatePermissionMember |

#### 技术细节
- `lark-oapi` SDK 缺少 `CreateFileCommentReply`，reply-comment 和 add-comment 使用 `requests` 直接调用 HTTP API
- 批注 API 均在 `drive.v1` 模块下，与文档内容 API（`docx.v1`）分属不同模块
- add-comment 需要 `reply_list.replies` 结构包裹批注正文，`quote` 为引用文本
- list-comments 返回的 reply content 是嵌套对象：`reply.content.elements[].text_run.text`

#### Bug Fix: md_to_blocks 段落间空行 (commit a822714)
- **问题**：Markdown 中两段纯文本之间的空行被忽略，飞书文档中两段紧贴
- **原因**：`markdown_to_blocks()` 中空行被直接 `continue` 跳过
- **修复**：空行在两个 text block 之间生成空 text block（前瞻判断下一个非空行是否为普通段落）
- **验证**：29 项单元测试全部通过 + 飞书文档端到端验证

---

## Phase 3: 覆盖写入 + 表格支持

### 2026-03-04 Session 1: 修复 write 覆盖 + 表格渲染

#### 问题背景
- 飞书 session `feishu.ST.1772584826` 中，AI 尝试 `--mode overwrite` 参数但不存在，每次失败后 fallback 到追加，导致文档内容重复 3 遍
- Markdown 表格语法被当作普通文本段落写入，飞书文档中无法渲染为表格

#### 任务拆解
- [x] 更新需求文档 (REQUIREMENTS.md) — 新增 Phase 3 需求
- [x] `feishu_doc.py` write 命令增加 `--mode` 参数（overwrite/append）
- [x] 实现 overwrite 逻辑：先获取文档子 block 列表，再批量删除，最后写入新内容
- [x] `md_to_blocks.py` 增加 Markdown 表格解析
- [x] `feishu_doc.py` 增加 table block 创建支持（两步：SDK创建空表格 → HTTP API填充cell）
- [x] 编写表格相关单元测试 — 9 项全部通过
- [x] 端到端测试：overwrite 模式 + 表格渲染
- [x] 更新 SKILL.md 文档
- [x] 更新 ARCHITECTURE.md（Phase 4 补齐时一并完成）
- [x] Git 提交 (6e425f8)

#### 端到端测试结果

| 命令 | 结果 | 详情 |
|---|---|---|
| `create-and-write` (含表格) | ✅ | 创建文档 `T4wrdlMsGo45y0x8FIscUAXGnyc`，5行3列表格正确渲染 |
| `write --mode overwrite` | ✅ | 清空原内容 + 写入新表格，内容完全替换 |
| `write --mode append` (默认) | ✅ | 向已有文档追加内容，不影响原内容 |

#### 技术细节
- 飞书表格 block_type=31，table_cell block_type=32
- 创建表格后 API 返回 `response.data.children[0].table.cells` 包含所有 cell block ID（flat 数组，行优先）
- SDK 的 `document_block_children.create` 写入 cell 时有 JSON 解析 bug（空响应），改用 HTTP API
- `_clear_document()` 使用 `BatchDeleteDocumentBlockChildren` API，start_index=0, end_index=N
- 代码重构：`_write_blocks_to_doc()` 统一处理 regular blocks 和 table blocks

---

## Phase 4: 局部编辑（Block 级别操作）

### 2026-03-04 Session 2: 局部编辑命令实现 ✅

#### 问题背景
- 用户反馈：对文档做 overwrite 不方便查看飞书历史编辑记录
- 需要 block 级别的局部编辑能力：原地更新、删除范围、指定位置插入
- 飞书 API 支持三种局部操作：PATCH block、BatchDelete children、CreateChildren at index

#### 任务拆解
- [x] 调研飞书 API 局部编辑能力（PATCH / BatchUpdate / BatchDelete）
- [x] 验证 HTTP PATCH API 可行性（SDK PatchDocumentBlock 参数格式不兼容）
- [x] 实现 `patch-block` 子命令 — 原地更新 block 文本
- [x] 实现 `delete-blocks` 子命令 — 删除指定 index 范围
- [x] 实现 `insert-blocks` 子命令 — 在指定位置插入 Markdown 内容
- [x] 端到端测试：3 个新命令全部通过
- [x] 运行单元测试：38/38 全部通过
- [x] 更新 SKILL.md — 优先强调局部编辑，overwrite 标注慎用
- [x] 更新 REQUIREMENTS.md — Phase 4 需求细化 + 标注已完成
- [x] 更新 ARCHITECTURE.md — 新增局部编辑 API 说明 + 子命令 + 设计决策
- [x] 更新 DEVLOG.md — 本章节 + 补全 Phase 3 遗留
- [x] Git 提交 + 推送 (12efb29 代码, 文档补齐另行提交)

#### 端到端测试结果

| 命令 | 结果 | 详情 |
|---|---|---|
| `patch-block` | ✅ | 更新 block `doxcnHMnIZi2ss4AU4SBtm28DCg` 文本，支持 **加粗** 格式 |
| `insert-blocks` | ✅ | 在 index 1 位置插入新段落，原有 block 后移 |
| `delete-blocks` | ✅ | 删除 index [1, 2) 范围的 block |
| 单元测试 | ✅ | 38/38 全部通过 |

#### 技术细节

##### SDK vs HTTP API 选型
- **SDK `PatchDocumentBlockRequest`**：接受 `Block` 对象作为 `request_body`，但飞书服务端实际期望 `update_text_elements` 结构 → 返回 `[1770001] invalid param`
- **HTTP PATCH API**：直接构造 `{"update_text_elements": {"elements": [...]}}` body → 成功
- 结论：patch-block 使用 HTTP API，delete-blocks 和 insert-blocks 使用 SDK

##### `_get_tenant_token()` 复用
- Phase 2 为 reply-comment / add-comment 创建的 helper
- Phase 4 的 patch-block 复用此 helper 获取 tenant_access_token
- 支持 `app_name` 参数，与 `--app` CLI 参数联动

##### insert-blocks 的 index 参数
- SDK `CreateDocumentBlockChildrenRequestBody.index()` 接受 0-based 位置
- 追加模式（write 命令）使用 `index=-1`（末尾）
- 局部插入使用具体 index 值
- 表格插入暂不支持指定 index（两步创建流程限制），会追加到文档末尾

##### ⚠️ 流程合规性反思
- 本次开发**未遵循 dev-workflow 规范**：直接跳到编码，未先更新需求/架构/DEVLOG
- 事后补齐了所有文档（REQUIREMENTS / ARCHITECTURE / DEVLOG）
- 教训：即使是"小改动"也应先记录任务清单，再编码

---

## Phase 4.5: 表格健壮性修复

### 2026-03-04 Session 3: 表格 Cell 写入修复 ✅

#### 问题背景
- 飞书 API 对表格 cell 写入有速率限制，快速连续写入会返回 HTTP 429
- 飞书创建表格时每个 cell 自动生成一个空 text block，写入内容后该空 block 残留导致多余空行
- insert-blocks 命令创建表格时 index 参数未传递，表格始终追加到文档末尾

#### 任务拆解
- [x] 表格 cell 写入增加 HTTP 429 指数退避重试（最多 5 次）(6aceb3c)
- [x] insert-blocks 传递正确的 index 参数到 `_write_table_block` (6aceb3c)
- [x] 写入 cell 内容后删除飞书自动生成的默认空 text block (78182e2)

#### 技术细节
- 重试策略：HTTP 429 或 API code 99991400 时，sleep `0.5 * 2^attempt` 秒后重试
- 空 block 清理：成功写入 cell 内容后，通过 `BatchDeleteDocumentBlockChildren(start_index=1, end_index=2)` 删除 index 1 的默认空 block
- `_write_table_block` 新增 `index` 参数，默认 -1（追加），支持指定位置插入

---

## Phase 5: 表格列宽自动计算

### 2026-03-05 Session 1: 列宽自动设置 ✅

#### 问题背景
- 飞书创建表格时所有列默认宽度 100px，导致表格过窄，内容换行严重
- 用户需要手动在飞书中逐列拖拽调整宽度，体验差
- 飞书 API 支持通过 `update_table_property` PATCH 接口设置列宽

#### 任务拆解
- [x] 调研飞书 API 表格列宽设置能力 — `update_table_property` 逐列更新
- [x] 修复文档 `VYRSdlzlTooO4txEtpGcGNZmnOb` 中 2 个窄表格的列宽
- [x] `md_to_blocks.py` 新增 `_estimate_display_width()` — CJK 感知字符宽度估算
- [x] `md_to_blocks.py` 新增 `_calculate_column_widths()` — 平方根比例列宽计算
- [x] `_parse_table()` 自动计算并存储 `column_widths` 到 table block dict
- [x] `feishu_doc.py` 新增 `_set_table_column_widths()` — PATCH API 逐列设置
- [x] `_write_table_block()` 新增 Step 4：创建表格后自动设置列宽
- [x] 端到端测试：创建测试文档验证列宽自动设置 ✅
- [x] 补充单元测试：18 项新增测试全部通过（56/56 总计）
- [x] 补齐 dev-workflow 文档（REQUIREMENTS / ARCHITECTURE / DEVLOG）
- [x] Git 提交 (5100586 代码, 文档补齐另行提交)

#### 端到端测试结果

| 操作 | 结果 | 详情 |
|---|---|---|
| 修复窄表格（分类表 5x3） | ✅ | [100,100,100] → [100,280,150] |
| 修复窄表格（语法表 4x2） | ✅ | [100,100] → [200,380] |
| 创建新文档含表格 | ✅ | 文档 `FtFWdQlRDoftLkxzGQacq78knjh`，列宽 [115,306,179] 和 [278,322] 自动设置 |
| 单元测试 | ✅ | 56/56 全部通过 |

#### 列宽计算算法
- 估算每列最大显示宽度（CJK=2, ASCII=1, 去除 Markdown 标记）
- 对宽度取平方根后按比例分配总宽度（默认 600px）
- Clamp 到 [80, 400] px 范围
- 平方根比例压缩长短列差异，接近人类手动调整的视觉效果

#### ⚠️ 流程合规性
- 代码实现先于文档（5100586），文档在后续 session 补齐
- 后续应严格遵循先文档后编码的流程

---

## Phase 6: P0 健壮性修复

### 2026-03-09 Session: P0 改进

#### 问题背景
- 飞书 API 创建表格限制 9 行（含 header），超过返回 invalid param
- `_write_table_block()` Step 1 无 retry，API 返回空响应时 JSON 解析 crash
- 连续写入多个表格触发 rate limit，无自动延迟

#### 任务拆解
- [x] P0-1: `md_to_blocks.py` — 表格行数自动拆分（>9 行拆分为多个子表格）
- [x] P0-1: 编写单元测试（9行不拆分、10行拆2个、17行拆3个）
- [x] P0-2: `feishu_doc.py` — `_write_table_block()` Step 1 增加 retry + 空响应处理
- [x] P0-3: `feishu_doc.py` — `_write_blocks_to_doc()` 表格间自动 3 秒延迟
- [x] P0-2/P0-3: 编写 mock 测试或语法检查
- [x] 更新文档（REQUIREMENTS / ARCHITECTURE / DEVLOG）
- [x] Git 提交

#### 实现细节

##### P0-1: 表格行数自动拆分
- 新增 `MAX_TABLE_ROWS = 9` 常量和 `_split_table()` 函数
- `_parse_table()` 检测行数 > 9 时返回 block 列表（而非单个 dict）
- `markdown_to_blocks()` 支持 `_parse_table` 返回 list（用 `isinstance` 判断）
- 拆分策略：header 复制 + 数据行按 8 行一组 + 续表间插入"（续表）"文本 block
- 每个子表格独立计算 column_widths

##### P0-2: 表格创建 retry + 空响应处理
- Step 1 创建空表格增加最多 3 次 retry，指数退避（2s, 4s）
- 识别 rate limit：API code 99991400 或 msg 含 "rate"
- `response.data` 为 None 时优雅返回 True（表格已创建，只是无 cell ID）
- Cell 写入时 HTTP 200 但 `resp.text` 为空也触发 retry

##### P0-3: 表格间自动延迟
- `_write_blocks_to_doc()` 增加 `table_written` 标志
- 第一个表格不延迟，后续表格写入前 `time.sleep(3)`
- 普通文本 block 不触发延迟

#### 测试结果
- `tests/test_md_to_blocks.py`: 63/63 通过（56 旧 + 7 新 P0-1 测试）
- `tests/test_feishu_doc_p0.py`: 6/6 通过（3 P0-2 + 3 P0-3 mock 测试）
- 总计: 69/69 全部通过

---

## Phase 7: P1 大文档体验改进

### 2026-03-09 Session: P1 改进

#### 问题背景
- 大文档（500+ 行、多表格）一次性写入容易触发 rate limit，中途失败无法恢复
- 写入过程无进度反馈，用户不知道当前进度
- 创建文档后需手动 add-member，流程繁琐

#### 任务拆解
- [x] P1-1: `feishu_doc.py` — `_write_blocks_to_doc()` 大文档自动分段写入
  - [x] 实现 `_split_into_chunks()` 函数：按安全边界（表格前后、heading 前）分段
  - [x] 每段最多 30 个 regular blocks，表格单独一个 chunk
  - [x] chunk 间加延迟（普通 1s，表格 3s）
  - [x] 编写测试
- [x] P1-2: `feishu_doc.py` — 写入进度反馈
  - [x] 每写完一个 chunk 输出进度到 stderr
  - [x] 编写测试验证 stderr 输出
- [x] P1-3: `feishu_doc.py` — 断点续传
  - [x] `_write_blocks_to_doc()` 新增 `resume_from` 参数
  - [x] write / create-and-write argparse 增加 `--resume-from`
  - [x] 失败时输出续传命令
  - [x] 编写测试
- [x] P1-4: `feishu_doc.py` — create-and-write 自动添加协作者
  - [x] 提取 `_add_member()` 内部函数
  - [x] `cmd_add_member` 改为 wrapper
  - [x] `create-and-write` 新增 `--add-member` / `--member-perm` 参数
  - [x] 编写测试
- [x] 更新文档（REQUIREMENTS / ARCHITECTURE / DEVLOG）
- [x] Git 提交

#### 实现细节

##### P1-1: 大文档自动分段写入
- 新增 `_split_into_chunks()` 函数，将 block_dicts 按安全边界拆分为 chunk 列表
- 安全边界：表格前后（每个 table 独立一个 chunk）、heading 前（heading 开始新 chunk）
- 每个 regular chunk 最多 `CHUNK_MAX_BLOCKS = 30` 个 block
- `_write_blocks_to_doc()` 重构为遍历 chunk 列表，逐 chunk 写入
- 延迟策略：普通 chunk 间 1s（`CHUNK_DELAY = 1`），表格 chunk 间 3s（`TABLE_DELAY = 3`，兼容 P0-3）

##### P1-2: 写入进度反馈
- 每写完一个 chunk 前输出进度到 stderr：`[N/total] Writing chunk N (X blocks)...` 或 `(table)`
- 全部写完后输出：`[total/total] All chunks written successfully.`
- 使用 `print(..., file=sys.stderr)`，不影响 stdout 的 JSON 输出

##### P1-3: 断点续传
- `_write_blocks_to_doc()` 新增 `resume_from` 参数（默认 0），跳过序号 < resume_from 的 chunk
- 跳过的 table chunk 仍会设置 `table_written = True`，确保后续延迟逻辑正确
- 失败时输出：`ERROR: Failed at chunk X/Y. Resume with: --resume-from X`
- write 和 create-and-write 的 argparse 均增加 `--resume-from` 参数

##### P1-4: create-and-write 自动添加协作者
- 提取 `_add_member(client, doc_id, open_id, perm)` 内部函数（返回 bool）
- `cmd_add_member(args)` 改为调用 `_add_member()` 的 wrapper
- `cmd_create_and_write()` 在创建文档后、写入内容前调用 `_add_member()`
- 添加失败不阻断写入（仅输出 warning 到 stderr）
- argparse: `--add-member` (open_id) + `--member-perm` (full_access/edit/view, 默认 full_access)

#### 测试结果
- `tests/test_md_to_blocks.py`: 63/63 通过（无变更）
- `tests/test_feishu_doc_p0.py`: 6/6 通过（回归检查）
- `tests/test_feishu_doc_p1.py`: 26/26 通过（新增）
  - TestSplitIntoChunks: 8 tests（分段逻辑）
  - TestChunkedWriteAndProgress: 5 tests（写入+进度）
  - TestResumeFrom: 5 tests（断点续传）
  - TestAddMemberExtraction: 4 tests（_add_member 提取）
  - TestCreateAndWriteAddMember: 2 tests（argparse 参数）
  - TestP0Regression: 2 tests（P0 兼容性）
- 总计: 95/95 全部通过

---

## Phase 8: P2 改进

### 2026-03-09 Session: P2 改进

#### 问题背景
- SKILL.md 缺少命令速查表，Agent 需要通读全文才能找到参数名
- P1 新增的 `--resume-from`、`--add-member`、`--member-perm` 参数未在 SKILL.md 中说明
- 大文档写入的使用建议（timeout、subagent）未记录
- 表格创建失败时错误信息不包含维度信息，难以诊断

#### 任务拆解
- [x] P2-A: SKILL.md 全面更新
  - [x] 增加"命令速查"表格（12+ 个命令的完整参数签名）
  - [x] 补充 write 命令的 `--resume-from` 参数说明
  - [x] 补充 create-and-write 的 `--add-member`、`--member-perm`、`--resume-from` 参数说明
  - [x] 新增"大文档写入建议"使用技巧
  - [x] 逐个检查所有命令参数完整性
- [x] P2-B: 错误信息增强
  - [x] `_write_table_block()` 错误信息包含表格维度
  - [x] 错误码 1770001 + 行数 > 9 时提示"行数超限"
  - [x] 错误码 99991400 时提示"频率限制"
  - [x] 编写 mock 测试验证错误信息格式
- [x] 运行全部测试确保不回归
- [x] Git 提交

#### 实现细节

##### P2-A: SKILL.md 全面更新
- 在"脚本位置"后新增"命令速查"表格，包含全部 12 个命令的完整参数签名
- write 命令新增 `--resume-from N` 参数说明
- create-and-write 命令新增 `--add-member`、`--member-perm`、`--resume-from` 参数说明
- 使用技巧新增"大文档写入建议"：自动分段、timeout=600、subagent、断点续传
- 逐个检查所有命令参数完整性，确认无遗漏

##### P2-B: 错误信息增强
- `_write_table_block()` Step 1 失败时错误信息格式改为：`Table create failed (rows=N, cols=M): [code] msg`
- 错误码 1770001 + rows > 9 时追加：`— NOTE: 飞书限制单次创建最多 9 行`
- 错误码 99991400 时追加：`— NOTE: 触发频率限制，将自动重试`
- rate limit 重试消息也包含维度和中文提示

#### 测试结果
- `tests/test_md_to_blocks.py`: 63/63 通过（无变更）
- `tests/test_feishu_doc_p0.py`: 6/6 通过（回归检查）
- `tests/test_feishu_doc_p1.py`: 26/26 通过（回归检查）
- `tests/test_feishu_doc_p2.py`: 5/5 通过（新增）
  - test_invalid_param_with_rows_over_9: 验证行数超限提示
  - test_invalid_param_with_rows_under_9: 验证行数 ≤ 9 不提示
  - test_rate_limit_retry_message_includes_dimensions: 验证重试消息含维度
  - test_rate_limit_final_failure_message: 验证最终失败消息
  - test_error_message_format_with_dimensions: 验证错误信息格式
- 总计: 100/100 全部通过

---

## Phase 9: Bug 修复 (#4, #7, #9)

### 2026-03-10 Session: 3 个遗留 Bug 修复 ✅

#### 问题背景
- Bug #4: `md_to_blocks.py` 嵌套代码块（如 ```````` 包裹 ```````）解析混乱，结束标记无法正确匹配
- Bug #7: `read --format blocks` 返回的表格 block 中 cell 文本内容为空
- Bug #9: `read --format blocks` 的 JSON 输出在某些情况下无法被 JSON 解析

#### 任务拆解
- [x] Bug #4: `md_to_blocks.py` — 嵌套代码块解析，支持 N backtick fence 匹配
  - [x] 记录开始 fence 的 backtick 数量
  - [x] 结束标记必须匹配相同或更多数量的 backtick
  - [x] 编写 7 个单元测试（3/4/5 backtick 嵌套、EOF、语言标记等）
  - [x] Git commit: `fix: #4 嵌套代码块解析 — 支持 N backtick fence 匹配`
- [x] Bug #7: `feishu_doc.py` — read blocks 表格内容读取
  - [x] `_block_to_dict()` 增加 table (block_type=31) 属性提取
  - [x] `_read_blocks()` 构建 block 查找表，按 parent-child 关系解析 cell 文本
  - [x] 编写 5 个 mock 测试（table 属性、cell 内容解析、边界情况）
  - [x] Git commit: `fix: #7 read blocks 表格内容读取`
- [x] Bug #9: `feishu_doc.py` — read blocks JSON 序列化健壮性
  - [x] 新增 `_safe_serialize()` 辅助函数（递归转换 lark-oapi 对象为基础类型）
  - [x] `_block_to_dict()` 返回值经过 `_safe_serialize` 处理
  - [x] `_read_blocks()` 的 `json.dumps` 增加 try-except 保护
  - [x] 编写 16 个测试（各种类型序列化、嵌套对象、block 序列化）
  - [x] Git commit: `fix: #9 read blocks JSON 序列化健壮性`
- [x] 更新文档（REQUIREMENTS / ARCHITECTURE / DEVLOG / ISSUES_AND_IMPROVEMENTS）
- [x] 运行全部测试确保不回归
- [x] Git commit: `docs: 更新 DEVLOG + ISSUES_AND_IMPROVEMENTS`

#### 实现细节

##### Bug #4: 嵌套代码块解析
- 原逻辑：`startswith('```')` 匹配结束标记，无法区分不同 backtick 数量
- 新逻辑：计算开始行 backtick 数量 `fence_len`，结束行必须有 ≥ fence_len 个 backtick 且之后无非空字符
- 修改文件：`scripts/md_to_blocks.py`（约 595 行附近，+12 -5 行）

##### Bug #7: read blocks 表格内容读取
- `_block_to_dict()` 新增 block_type=31 处理：提取 table.property（row_size/column_size/header_row）和 table.cells
- `_read_blocks()` 构建 `all_block_dicts` 查找表，对 table block 的 cells 递归解析子 block 文本
- 输出新增 `table.cell_contents` 数组
- 修改文件：`scripts/feishu_doc.py`（_block_to_dict + _read_blocks）

##### Bug #9: JSON 序列化健壮性
- 新增 `_safe_serialize()` 函数：递归处理 dict/list/基础类型/lark-oapi 对象/其他类型
- `_block_to_dict()` 最后 `return _safe_serialize(result)`
- `_read_blocks()` 的 `json.dumps` 增加 try-except，失败时 fallback 到 `_safe_serialize` 再试
- 修改文件：`scripts/feishu_doc.py`（新增 _safe_serialize + 修改 _block_to_dict + _read_blocks）

#### 测试结果
- `tests/test_md_to_blocks.py`: 70/70 通过（63 旧 + 7 新 Bug #4 测试）
- `tests/test_feishu_doc_p0.py`: 6/6 通过（回归检查）
- `tests/test_feishu_doc_p1.py`: 26/26 通过（回归检查）
- `tests/test_feishu_doc_p2.py`: 5/5 通过（回归检查）
- `tests/test_feishu_doc_bug7.py`: 5/5 通过（新增）
- `tests/test_feishu_doc_bug9.py`: 16/16 通过（新增）
- 总计: 128/128 全部通过

---

## Phase 9: 飞书文档写入健壮性修复

### 2026-03-25 Session: F9.1~F9.5 修复 ✅

#### 任务拆解
- [x] F9.1 嵌套列表 schema mismatch 修复
- [x] F9.2 文档内锚点链接降级
- [x] F9.3 大文档写入超时优化
- [x] F9.4 续传重复修复
- [x] F9.5 整行加粗标题格式异常修复
- [x] 编写新测试（15 项）
- [x] 回归测试（70 项原有测试通过）

#### F9.1 嵌套列表扁平化
- **根因**: 飞书 API 不支持嵌套列表的 `children` 字段，缩进子项落入段落收集器被合并为纯文本
- **修复**: 新增 `_is_list_item()` 检测任意缩进的列表项，`_collect_flat_list_items()` 收集顶级项+所有缩进子项并扁平化为同级 block
- **关键**: 子项保留原始类型（bullet/ordered），不强制统一为父项类型
- 修改文件：`scripts/md_to_blocks.py`
- 新增测试：6 项（TestNestedListFlattening）

#### F9.2 锚点链接降级
- **根因**: `[text](#anchor)` 格式的锚点链接在飞书 API 中不支持，导致写入失败
- **修复**: `_parse_inline_simple()` 中检测 `link_url.startswith('#')`，降级为纯文本（保留文字，去掉链接）
- 修改文件：`scripts/md_to_blocks.py`
- 新增测试：4 项（TestAnchorLinkDegradation）

#### F9.3 大文档超时优化
- **修复1**: `CHUNK_MAX_BLOCKS` 从 30 增至 50（匹配 BATCH_SIZE），减少 chunk 数量
- **修复2**: 常规 chunk 间 delay 从 1s 减至 0.5s
- **效果**: 对于 500 block 文档，chunk 数从 ~17 降至 ~10，总延迟从 ~16s 降至 ~4.5s
- 修改文件：`scripts/feishu_doc.py`

#### F9.4 续传重复修复
- **根因**: 失败时 resume hint 指向当前 chunk，但该 chunk 可能已部分写入（HTTP 超时但服务端已处理）
- **修复**: 改进错误消息，提供两个 resume 选项：`--resume-from N+1`（跳过失败 chunk）和 `--resume-from N`（重试，需先检查文档）；输出 `blocks_written_before_failure` 计数
- 修改文件：`scripts/feishu_doc.py`

#### F9.5 整行加粗独立 block
- **根因**: `**方案 2: 后台执行**` 后跟非空行时，段落收集器将两行合并为一个 block，导致格式异常
- **修复**: 在段落收集器前检测全行加粗（`^\*\*(.+)\*\*$`），作为独立 text block 输出；段落收集器也在遇到全行加粗时停止收集
- 修改文件：`scripts/md_to_blocks.py`
- 新增测试：5 项（TestFullLineBoldStandalone）

#### 测试结果
- `tests/test_md_to_blocks.py`: 85/85 通过（70 旧 + 6 F9.1 + 4 F9.2 + 5 F9.5）
- 总计: 85/85 全部通过

---

*开始日期: 2026-02-28*
