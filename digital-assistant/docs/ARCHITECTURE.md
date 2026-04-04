# Digital Assistant — 架构设计文档

## 系统概览

Digital Assistant 是 nanobot 的任务管理与质量控制中枢，采用**调度器-工作者（Dispatcher-Worker）**架构模式，通过固定 session 的调度器自动派发任务给 Worker subagent 执行，并通过结构化报告机制实现流程控制。

### 核心设计原则

1. **Worker 去状态化** — Worker 只执行工作并返回结构化报告，不操控任务状态
2. **调度器集中决策** — 所有状态转换由调度器统一执行，Worker 不调用 brain_manager
3. **文件通道为主** — Worker 报告写入文件，调度器从文件读取，保证可靠性
4. **规则引擎 + LLM 兜底** — 确定性场景用规则引擎，模糊场景用 LLM 兜底
5. **多角色编排** — 支持 8 角色互检流程（architect → architect_review → developer → code_review → tester → test_review → [auditor] → retrospective），自动循环控制

## 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                         User / Feishu                        │
└────────────────────────┬────────────────────────────────────┘
                         │ 需求输入 / 审批操作
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Main Agent (Web Chat)                     │
│  - 需求对齐                                                   │
│  - 任务注册 (brain_manager.register_task)                    │
│  - 触发调度器 (trigger_scheduler.py)                         │
└────────────────────────┬────────────────────────────────────┘
                         │ spawn dispatcher
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              Dispatcher (Fixed Session)                      │
│  - 扫描任务队列 (REGISTRY.md)                                │
│  - 按优先级/依赖排序                                          │
│  - 派发任务给 Worker (spawn subagent)                        │
│  - 处理 Worker 完成通知 (handle-completion)                  │
│  - 执行决策 (状态转换/Review/通知)                           │
└────────────────────────┬────────────────────────────────────┘
                         │ spawn worker
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Worker (Subagent)                         │
│  - Developer: 实现功能 + 写测试                              │
│  - Tester: 执行测试 + 验证功能                               │
│  - 写结构化报告 (data/brain/reports/)                        │
└────────────────────────┬────────────────────────────────────┘
                         │ 报告文件
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Report Files                              │
│  {task_id}-{role}-{timestamp}.json                           │
│  - verdict: pass/fail/blocked/partial                        │
│  - summary: 执行结果描述                                      │
│  - issues: 问题列表                                          │
│  - files_changed: 变更文件                                   │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. Brain Manager (brain_manager.py)

**职责**：任务数据持久化与状态管理

**核心功能**：
- `register_task()` — 注册新任务到 REGISTRY.md
- `transition_task()` — 任务状态转换（queued → executing → review → done）
- `update_briefing()` — 更新 BRIEFING.md 摘要视图
- `get_task()` / `list_tasks()` — 任务查询
- `review add/resolve` — Review 项管理

**数据结构**：
```yaml
# data/brain/tasks/{task_id}.yaml
task_id: T-20260401-001
title: 任务标题
description: 详细描述
priority: P1  # P0/P1/P2
status: queued  # queued/executing/review/done/blocked/cancelled
template: standard-dev  # quick/standard-dev/long-task/cron-auto/batch-dev
assigned_to: null
created_at: 2026-04-01T10:00:00
updated_at: 2026-04-01T10:00:00
blocked_by: []  # 依赖的任务 ID
orchestration:  # 多角色编排状态
  iteration: 0
  current_role: null
  history: []
review:  # Review 状态
  level: L2  # L0/L1/L2/L3
  checklist: []
  status: pending  # pending/approved/rejected
```

### 2. Scheduler (scheduler.py)

**职责**：任务调度与 Worker 编排

**核心功能**：

#### 2.1 任务派发 (`run_scheduler`)
```python
def run_scheduler(dry_run=False):
    """扫描任务队列，按优先级派发任务"""
    # 1. 加载所有 queued 任务
    # 2. 过滤已阻塞任务（blocked_by 未完成）
    # 3. 按优先级排序（P0 > P1 > P2）
    # 4. 检查并发限制（全局最多 3 个 executing）
    # 5. 生成 spawn 指令（含 Worker prompt）
    # 6. 返回派发计划（JSON 格式）
```

**并发控制**：
- 全局最多 3 个 executing 任务（API 限流约束）
- 单次派发上限 3 个任务
- 依赖检查：blocked_by 中的任务必须全部 done

#### 2.2 Worker 完成处理 (`handle_worker_completion`)
```python
def handle_worker_completion(task_id, auto_detect=False):
    """处理 Worker 完成通知，执行决策"""
    # 1. 读取报告文件 (parse_worker_report)
    # 2. 规则引擎决策 (make_decision)
    # 3. 执行决策 (execute_decision)
    # 4. 生成下一步 spawn 指令（如需要）
    # 5. 返回决策结果（JSON 格式）
```

**决策流程**：
```
Worker Report (verdict)
    ↓
Rule Engine (make_decision)
    ↓
Decision (action + next_role)
    ↓
Execute (execute_decision)
    ↓
Spawn Next Worker (if needed)
```

#### 2.3 Worker Prompt 生成 (`generate_worker_prompt`)
```python
def generate_worker_prompt(task, role, prior_context=None):
    """生成 Worker 执行指令（去状态化）"""
    # 1. 加载任务信息（标题/描述/优先级）
    # 2. 加载角色指引（developer/tester）
    # 3. 加载执行规则（从 rules/{role}.md）
    # 4. 附加上下文（最近 2 轮历史）
    # 5. 附加报告模板（精简 JSON Schema）
    # 6. 返回完整 prompt（不含 brain_manager 指令）
```

**关键特性**：
- ✅ 移除所有 brain_manager 指令（Worker 去状态化）
- ✅ 报告路径使用绝对路径（避免工作目录问题）
- ✅ 报告模板放 prompt 末尾显眼位置
- ✅ 上下文只取最近 2 轮（防止 prompt 膨胀）

### 3. Worker 报告机制

#### 3.1 报告 Schema（精简版）
```json
{
  "task_id": "T-20260401-001",
  "role": "developer",
  "verdict": "pass",
  "summary": "实现了缓存优化功能，命中率从 60% 提升到 85%。修改了 cache.py 和 config.py 两个文件，新增了 LRU 淘汰策略。所有单元测试通过。",
  "issues": [
    {"description": "Redis 连接池配置需要调优"}
  ],
  "files_changed": [
    "src/cache.py",
    "src/config.py",
    "tests/test_cache.py"
  ]
}
```

**字段说明**：
- `task_id` (必填) — 任务 ID，用于校验一致性
- `role` (必填) — 角色类型（developer/tester）
- `verdict` (必填) — 执行结果（pass/fail/blocked/partial）
- `summary` (必填) — 自由文本描述，包含关键证据
- `issues` (可选) — 问题列表，每项只需 description
- `files_changed` (可选) — 变更文件列表

**设计理由**：
- 字段越少，LLM Worker 遵从率越高
- 复杂证据放 summary 自由文本，由调度器 LLM 兜底解析
- 移除 evidence/test_results/artifacts 等重字段

#### 3.2 报告文件命名
```
data/brain/reports/{task_id}-{role}-{YYYYMMDD-HHMMSS}.json
```

**示例**：
```
T-20260401-001-developer-20260401-143022.json
T-20260401-001-tester-20260401-150315.json
```

**读取策略**：
- 按 mtime 取最新文件
- 匹配 role（避免读到上一轮的报告）
- Glob 模式：`{task_id}-{role}-*.json`

### 4. 决策引擎

#### 4.1 规则引擎 (`make_decision`)

**输入**：
- `task` — 任务对象
- `report` — Worker 报告
- `orchestration` — 编排状态

**输出**：
```python
{
    "action": "dispatch_tester",  # 决策动作
    "next_role": "tester",        # 下一个角色
    "reason": "Developer 完成开发，需要 Tester 验证",
    "context": {...}              # 传递给下一个 Worker 的上下文
}
```

**决策规则表（V6.1 — 8 角色互检）**：

| PL | 当前角色 | Verdict | Action | Next Role | 备注 |
|----|---------|---------|--------|-----------|------|
| PL0 | developer | pass | mark_done | — | 快速任务 |
| PL0 | developer | fail | retry | developer | |
| PL1 | developer | pass | mark_done | — | 纯文档任务 |
| PL1 | developer | fail | retry | developer | |
| PL2 | architect | pass | dispatch | architect_review | V6.1: 开发前评审架构 |
| PL2 | architect_review | pass | dispatch | developer | |
| PL2 | architect_review | fail | dispatch | architect | 打回重新设计 |
| PL2 | developer | pass | dispatch | code_review | V6.1: 代码+测试覆盖检查 |
| PL2 | developer | fail | retry | developer | |
| PL2 | code_review | pass | dispatch | tester | |
| PL2 | code_review | fail | dispatch | developer | D11: 打回执行角色 |
| PL2 | tester | pass | dispatch | test_review | V6.1: 测试语义审查 |
| PL2 | tester | fail | dispatch | developer | |
| PL2 | test_review | pass | dispatch | retrospective | |
| PL2 | test_review | fail | dispatch | tester | D11: 打回执行角色 |
| PL2 | retrospective | pass | review_check | — | 检查 review level |
| PL2 | retrospective | fail | retro_route | — | 补齐缺失环节 |
| PL3 | (同 PL2) | — | — | — | 额外: test_review→auditor→retrospective |

**角色职责（V6.1 — 8 角色体系）**：

| 角色 | 职责 | Fail 打回目标 |
|------|------|--------------|
| architect | 规则裁决 + 方案设计 + 验收方案 | blocked |
| architect_review | 架构评审（开发前，检查设计完整性） | architect |
| developer | 实现功能 + 编写单元测试 | retry self |
| code_review | 代码审查（架构一致性 + 测试覆盖合理性） | developer |
| tester | QA 端到端测试 | developer |
| test_review | 测试语义审查（真实性/盲区/深度） | tester |
| auditor | 流程审计 + 测试质量检查（PL3 only） | developer/tester/architect |
| retrospective | 流程复盘（环节完整性检查） | 缺失角色 |

**循环控制**：
- `MAX_ITERATIONS = 5` — 总轮次上限
- `MAX_SAME_ROLE_CONSECUTIVE = 2` — 同角色连续上限

#### 4.2 LLM 兜底（Phase 2）

**触发条件**：
- verdict = partial（模糊结果）
- 报告解析失败（JSON 格式错误）
- 规则引擎无法匹配（未知场景）

**实现方式**：
```python
def llm_fallback_decision(task, report_text, orchestration):
    """LLM 分析报告并生成决策"""
    prompt = f"""
    任务: {task['title']}
    Worker 报告: {report_text}
    当前轮次: {orchestration['iteration']}
    
    请分析报告并决策下一步动作：
    1. 如果工作完成且质量合格 → mark_done
    2. 如果需要修复 → dispatch_developer
    3. 如果需要验证 → dispatch_tester
    4. 如果无法继续 → mark_blocked
    """
    response = litellm.completion(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200
    )
    return parse_llm_decision(response)
```

### 5. Dispatcher Session 管理

#### 5.1 固定 Session 复用

**设计目标**：
- 避免每次调度都创建新 session（上下文丢失）
- 支持 session 换代（防止上下文膨胀）

**实现方式**：
```python
# trigger_scheduler.py
def get_or_create_dispatcher_session():
    """获取或创建 Dispatcher session"""
    # 1. 读取 data/brain/.dispatcher_session
    # 2. 检查 session 是否存在且有效
    # 3. 检查轮次是否超过 500（换代阈值）
    # 4. 如需换代，创建新 session 并更新文件
    # 5. 返回 session_id
```

**换代策略**：
- 轮次阈值：500 轮
- 换代时机：调度器启动时检查
- 旧 session 处理：标记为 archived，不删除

#### 5.2 Dispatcher Prompt

**核心指令**：
```markdown
你是 Digital Assistant 的调度器，负责任务派发和 Worker 编排。

## 工作流程

1. **常规调度**：
   - 调用 `scheduler run` 获取派发计划
   - 解析 JSON 输出，按顺序 spawn Worker

2. **Worker 完成处理**：
   - 收到 [Subagent Result Notification] 时
   - 调用 `scheduler handle-completion --task-id T-xxx`
   - 解析决策结果，执行 action（如需 spawn 下一个 Worker）

3. **定期触发**：
   - 每 30 分钟自动触发一次（Cron 兜底）
   - 或收到用户/系统通知时立即触发

## 注意事项

- ⚠️ 不要直接调用 brain_manager 命令（由 scheduler 统一执行）
- ⚠️ handle-completion 和 run 必须分开调用（两步流程）
- ⚠️ 并发上限 3 个任务，超过时等待
```

## 工作组模板

### 模板类型

| 模板 | 适用场景 | Review 级别 | 角色编排 |
|------|---------|------------|---------|
| **quick** | 简单改动（≤10 行代码） | L0（无需 Review） | 单角色（developer） |
| **standard-dev** | 标准开发任务 | L2（code_reviewer） | 8 角色互检（PL2: 7 角色, PL3: 8 角色） |
| **long-task** | 长期任务（>1 天） | L2 | 8 角色互检 + 阶段性 Review |
| **cron-auto** | 定时任务/提醒 | L0 | 单角色（auto） |
| **batch-dev** | 批量需求编排 | L2 | 多任务并行 + 统一验收 |

### 模板匹配算法

```python
def match_template(task):
    """根据任务特征匹配模板"""
    title = task['title'].lower()
    description = task['description'].lower()
    
    # 1. 关键词匹配
    if any(kw in title for kw in ['提醒', '定时', 'cron']):
        return 'cron-auto'
    
    if any(kw in title for kw in ['批量', 'batch', '多个']):
        return 'batch-dev'
    
    if any(kw in description for kw in ['简单改动', '≤10行']):
        return 'quick'
    
    # 2. 预估工作量
    if task.get('estimated_hours', 0) > 8:
        return 'long-task'
    
    # 3. 默认标准开发
    return 'standard-dev'
```

## Review 机制

### Review 级别判定

```python
def determine_review_level(task, files_changed):
    """自动判定 Review 级别"""
    # L0: Quick 任务 / Cron-auto
    if task['template'] in ['quick', 'cron-auto']:
        return 'L0'
    
    # L1: 简单改动（≤2 文件，无接口变更）
    if len(files_changed) <= 2 and not has_interface_change(files_changed):
        return 'L1'
    
    # L3: 架构变更 / 外部发布 / 金融逻辑
    if any(kw in task['description'] for kw in ['架构', '发布', '金融']):
        return 'L3'
    
    # L2: 标准开发任务
    return 'L2'
```

### Review Checklist 生成

```yaml
# L2 级别 Checklist 示例
checklist:
  - dimension: 功能完整性
    items:
      - check: 所有需求点已实现
        status: pending
      - check: 边界条件已处理
        status: pending
  
  - dimension: 代码质量
    items:
      - check: 代码符合规范
        status: pending
      - check: 无明显性能问题
        status: pending
  
  - dimension: 测试覆盖
    items:
      - check: 单元测试通过
        status: pending
      - check: 集成测试通过
        status: pending
```

## 飞书集成

### 通知格式

```python
def format_feishu_notification(task, event):
    """格式化飞书通知"""
    emoji_map = {
        'review': '📋',
        'done': '✅',
        'blocked': '🚫',
        'executing': '⚙️'
    }
    
    return f"""
{emoji_map[event]} 任务 {task['task_id']} 进入 {event} 状态

📌 {task['title']}
🔖 优先级: {task['priority']}
👤 负责人: {task.get('assigned_to', '未分配')}

操作提示:
- 回复 "T-{task['task_id']} approve" 批准
- 回复 "T-{task['task_id']} reject 原因" 拒绝
- 回复 "T-{task['task_id']} comment 内容" 添加评论
"""
```

### 回复解析

```python
def parse_feishu_reply(message):
    """解析飞书回复指令"""
    # 格式: T-{task_id} {action} [note]
    pattern = r'T-(\d{8}-\d{3})\s+(approve|reject|defer|pause|cancel|resume|comment)\s*(.*)'
    match = re.match(pattern, message, re.IGNORECASE)
    
    if match:
        return {
            'task_id': f'T-{match.group(1)}',
            'action': match.group(2).lower(),
            'note': match.group(3).strip()
        }
    return None
```

## 数据流图

```
User Input
    ↓
Main Agent (需求对齐)
    ↓
brain_manager.register_task()
    ↓
data/brain/tasks/{task_id}.yaml (status=queued)
    ↓
data/brain/REGISTRY.md (索引更新)
    ↓
trigger_scheduler.py (触发调度)
    ↓
Dispatcher Session (固定 session)
    ↓
scheduler.run() (扫描队列)
    ↓
spawn Worker (developer/tester)
    ↓
Worker 执行工作
    ↓
data/brain/reports/{task_id}-{role}-{timestamp}.json
    ↓
[Subagent Result Notification]
    ↓
scheduler.handle_completion()
    ↓
parse_worker_report() (读取报告)
    ↓
make_decision() (规则引擎)
    ↓
execute_decision() (状态转换)
    ↓
brain_manager.transition_task() / review add
    ↓
data/brain/tasks/{task_id}.yaml (status 更新)
    ↓
data/brain/BRIEFING.md (摘要更新)
    ↓
Feishu 通知 (如需要)
```

## 性能与可靠性

### 性能指标

- 调度器单次运行时间 < 10 秒（不含 Worker 执行）
- 任务状态更新延迟 < 1 秒
- BRIEFING/REGISTRY 更新延迟 < 2 秒
- 报告文件解析时间 < 100ms

### 可靠性保障

1. **报告文件持久化** — 所有 Worker 报告写入文件，不依赖内存
2. **决策日志记录** — 所有决策记录到 decisions.jsonl，可追溯
3. **失败重试机制** — 调度器失败自动重试（最多 3 次）
4. **并发安全** — Dispatcher session 串行处理消息，handle-completion 加文件锁
5. **换代机制** — Dispatcher session 超过 500 轮自动换代，防止上下文膨胀

### 容错策略

| 异常场景 | 处理方式 |
|---------|---------|
| Worker 未写报告 | parse 返回 None → mark_blocked + 通知用户 |
| 报告 JSON 格式错误 | JSONDecodeError → mark_blocked + 通知用户 |
| task_id 不匹配 | 校验失败 → mark_blocked + 通知用户 |
| 决策引擎无法匹配 | LLM 兜底（Phase 2）或 mark_blocked（MVP） |
| transition 失败 | 异常捕获 + 记录错误日志 + 不回滚已执行 action |
| Dispatcher session 失效 | 自动创建新 session 继任 |

## 扩展性设计

### 新增角色

```python
# 1. 在 rules/ 目录下创建 {role}.md
# 2. 在 make_decision() 中添加决策规则
# 3. 在 generate_worker_prompt() 中加载角色指引

# 示例：新增 code_reviewer 角色
ROLE_GUIDANCE = {
    'code_reviewer': """
    你是代码审查员，负责审查代码质量和安全性。
    
    审查维度：
    1. 代码规范
    2. 性能问题
    3. 安全漏洞
    4. 可维护性
    
    审查通过标准：
    - 无 P0/P1 级别问题
    - P2 问题有明确修复计划
    """
}
```

### 新增模板

```python
# 在 match_template() 中添加匹配规则
# 在 determine_review_level() 中定义 Review 级别
# 在 make_decision() 中添加模板特定逻辑

# 示例：新增 hotfix 模板
if 'hotfix' in task['title'].lower():
    return 'hotfix'  # 紧急修复，跳过 Tester，直接 Review
```

## 相关文档

- [REQUIREMENTS.md](./REQUIREMENTS.md) — 需求文档
- [DEVLOG.md](./DEVLOG.md) — 开发日志
- [../SKILL.md](../SKILL.md) — Skill 使用手册
- [D-20260331-001](../../data/brain/designs/D-20260331-001.md) — 调度器多角色编排设计
- [D-20260331-002](../../data/brain/designs/D-20260331-002-summary.md) — Worker 规则按需加载设计

---

## Phase 1 止血：流程失效整改 (2026-04-01)

### 背景与动机

dev-workflow 合规审计（2026-04-01）发现合规率仅 19.4%。根因分析揭示四大绕过模式：
1. **调度器无智囊团硬卡点** — standard-dev 任务直接派 developer，跳过 architect
2. **L0 审批未检查文档** — 只检查 Worker verdict，不验证文档三件套
3. **Worker 规则未强制** — 文档要求依赖 Worker "自觉"，紧急修复时被跳过
4. **审计滞后** — 事后审计发现问题时已积累数日

整改方案经架构师→设计师→评审三角色独立评审，评审结论为 **Conditional Go**（解决 3 个 Must Fix 后可实施）。

### 架构变更概览

```
                        ┌─────────────────────────────────┐
                        │     run_scheduler() 派发循环      │
                        └──────────────┬──────────────────┘
                                       │
                              ┌────────▼────────┐
                              │ check_design_gate│  ◄── 新增：设计门禁
                              │  (Phase 1 卡点)  │
                              └────────┬────────┘
                                       │
                           ┌───────────┼───────────┐
                           │ pass                   │ fail
                           ▼                        ▼
                    role=developer           role=architect
                    (正常派发)               (强制走设计流程)
                           │
                           ▼
              ┌─────────────────────────┐
              │  Worker 执行 + 提交报告   │
              └────────────┬────────────┘
                           │
                  ┌────────▼────────┐
                  │  make_decision() │
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              │ developer pass          │ tester pass
              ▼                         ▼
     ┌─────────────────┐      ┌─────────────────┐
     │check_doc_triplet│      │check_doc_triplet│  ◄── 新增：文档检查
     └────────┬────────┘      └────────┬────────┘
              │                        │
         ┌────┼────┐              ┌────┼────┐
         │ok       │missing       │ok       │missing
         ▼         ▼              ▼         ▼
    dispatch   dispatch_back  mark_done  promote_to
    tester     developer      (L0/L1)    review
               (补文档)
               │
          retry >= MAX_DOC_RETRY?
               │
          ┌────┼────┐
          │no       │yes
          ▼         ▼
      打回补文档  升级人工审核
```

### 1. 设计门禁机制 (Design Gate)

**位置**: `scheduler.py` L366-412 — `check_design_gate()`

**目的**: 阻止无设计文档的 standard-dev 任务直接派发 developer，强制先走 architect 流程。

**检查链**（按优先级顺序）:

| 检查项 | 通过条件 | 说明 |
|--------|---------|------|
| Feature flag | `DESIGN_GATE_ENABLED=0` | 全局关闭门禁 |
| 模板豁免 | template ∈ {quick, cron-auto} | 轻量任务不需要设计 |
| design_ref | task.design_ref 或 task.design_doc 存在 | 已关联设计文档 |
| Architect 报告 | `{task_id}-architect-*.json` 存在 | 已走过 architect 流程 |
| Orchestration 历史 | history 中有 role=architect 记录 | 历史编排中包含 architect |
| Emergency 豁免 | task.emergency = true | 紧急修复，事后补文档 |
| needs_design 标记 | task.needs_design = false | 显式标记不需要设计 |

**集成点**: `run_scheduler()` L1722-1728，在 dispatch 前调用。未通过时将 `initial_role` 从 developer 切换为 architect，并在 transition note 中记录原因。

**设计决策**: Phase 1 不引入新的 `design` 状态到状态机，通过 role 切换实现最小侵入。Phase 2 将引入 `design` 状态。

### 2. 文档三件套检查 (Doc Triplet Check)

**位置**: `scheduler.py` L413-505 — `check_doc_triplet()`

**目的**: 在 developer pass 和 tester pass 决策点验证文档完整性，缺失时阻止自动流转。

**检查逻辑**:

1. **Report 扫描**: 遍历 `REPORTS_DIR/{task_id}-*.json`，检查 `files_changed` 中是否包含 DEVLOG/ARCHITECTURE/REQUIREMENTS
2. **当前 Report**: 如果传入了当前 report，同步检查其 `files_changed`
3. **文件系统兜底**: 从 report 中提取项目目录，直接扫描文件系统中的 docs/ 目录（解决 reviewer 提出的 Must Fix：不仅依赖 report JSON）

**最低要求**:
- **DEVLOG.md**: 必须存在（Worker 必须记录做了什么）
- **ARCHITECTURE.md 或 design_ref**: 至少有一个（设计文档可通过 design_ref 替代）

**豁免规则**:
- `DOC_TRIPLET_CHECK_ENABLED=0`: 全局关闭
- template ∈ {quick, cron-auto}: 不要求三件套
- task.emergency = true: 标记 doc_debt，不阻塞

**集成点**:

| 决策分支 | 位置 | 行为 |
|---------|------|------|
| developer pass (standard-dev) | L830-857 | 缺文档 → 打回补文档 / 超过重试上限 → 升级人工审核 |
| tester pass (L0/L1) | L794-806 | 缺文档 → 升级 review level（不自动通过） |

### 3. 打回循环防护 (Doc Retry Counter)

**位置**: `scheduler.py` L506-516 — `_count_doc_retries()`

**目的**: 防止 developer 被无限打回补文档（reviewer Must Fix 项）。

**机制**:
- 扫描 task.orchestration.history 中包含 "missing docs" 或 "文档三件套不完整" 的记录
- `MAX_DOC_RETRY = 2`：最多打回 2 次
- 超过上限后 → `promote_to_review`（升级人工审核），summary 中注明已打回次数

**设计决策**: 选择 2 次上限而非 1 次，给 Worker 一次补救机会，同时避免过度循环浪费资源。

### 4. Worker Prompt 强化

**位置**: `scheduler.py` L1267-1279 — `generate_worker_prompt_v2()` 内 developer 指导部分

**变更**: 对非 quick/cron-auto 模板的 developer 任务，在 prompt 中硬编码：

1. **文档三件套要求**（标注 MUST）:
   - DEVLOG.md — 开发日志（Phase + checkbox + 关键决策）
   - ARCHITECTURE.md — 方案文档（有 design_ref 可跳过）
   - REQUIREMENTS.md — 需求文档（任务描述充分可跳过）
   - 明确警告："⚠️ 调度器会检查文档完整性，缺少文档的报告会被打回"

2. **Git Commit 规范**（reviewer Must Fix：Git commit 关联 Task ID）:
   - 格式: `feat(task_id): 描述`
   - 示例: `feat(T-20260401-003): add design gate check`

### 5. Feature Flag 回滚机制

**位置**: `scheduler.py` L358-366

**目的**: 所有新增卡点可通过环境变量一键关闭，实现快速回滚（reviewer Must Fix 项）。

| Flag | 环境变量 | 默认值 | 控制范围 |
|------|---------|--------|---------|
| `DESIGN_GATE_ENABLED` | `DESIGN_GATE_ENABLED` | `"1"` (开启) | `check_design_gate()` |
| `DOC_TRIPLET_CHECK_ENABLED` | `DOC_TRIPLET_CHECK_ENABLED` | `"1"` (开启) | `check_doc_triplet()` |
| `TEST_EVIDENCE_ENABLED` | `TEST_EVIDENCE_ENABLED` | `"1"` (开启) | tester pass 时的 test_evidence 校验 |
| `CROSS_CHECK_ENABLED` | `CROSS_CHECK_ENABLED` | `"1"` (开启) | 全局 cross-check 总开关（关闭则跳过所有 cross-check 校验） |
| `NOTIFICATION_VALIDATE_ENABLED` | `NOTIFICATION_VALIDATE_ENABLED` | `"1"` (开启) | `validate_notification()` 通知发送前状态一致性校验 |
| `IRREVERSIBLE_CONFIRM_ENABLED` | `IRREVERSIBLE_CONFIRM_ENABLED` | `"1"` (开启) | 飞书回复 cancel/reject 操作二次确认规则注入 |

**关闭方式**: `export DESIGN_GATE_ENABLED=0` 或 `export DOC_TRIPLET_CHECK_ENABLED=0` 或 `export TEST_EVIDENCE_ENABLED=0`
Cross-check 相关: `export CROSS_CHECK_ENABLED=0`（总开关）或单独关闭 `NOTIFICATION_VALIDATE_ENABLED=0` / `IRREVERSIBLE_CONFIRM_ENABLED=0`

**设计决策**: 使用环境变量而非配置文件，因为：
- 无需修改代码或配置文件即可回滚
- 支持进程级别的快速切换
- 与现有部署方式（shell 脚本启动）兼容

### 6. 规则层级调整

#### STD-002 升级: L2 RECOMMENDED → L0 MUST

**文件**: `rules/standard-dev.md` L5-10

**影响**: rule_loader 对 L0 规则注入完整规则文本（而非仅标题），Worker 无法忽略。增加调度器检查警告。

#### G-006 新增: standard-dev 任务必须有设计文档

**文件**: `rules/global.md` L27-30

**内容**: 描述设计门禁机制、architect 重定向行为、emergency 豁免条件。

### 7. 测试证据验证机制 (T-20260401-009)

**位置**: `scheduler.py` make_decision() tester pass 分支

**目的**: 强制 tester 提供测试执行证据（test_evidence），防止未经真实测试就报 pass（G-003 违规）。

**验证逻辑**:
1. tester verdict=pass 时，检查报告中 `test_evidence` 或 `test_results` 字段
2. 要求为非空 list，每项必须有 `type` 和 `result` 字段
3. 无证据 → 打回 tester 补充（最多 MAX_EVIDENCE_RETRY=2 次）
4. 超限 → promote_to_review 升级人工审核
5. fail/blocked/partial 不受影响

**辅助函数**: `_count_evidence_retries(task)` — 统计历史中 evidence 打回次数

**Tester Guidance**: `_generate_tester_guidance()` 已追加 test_evidence 格式说明

**Feature Flag**: `TEST_EVIDENCE_ENABLED`（默认开启，设 `"0"` 关闭）

### 8. Cross Check 整改 Phase 1 (T-20260402-002)

**设计文档**: [cross-check-remediation-designer.md](../../data/brain/designs/cross-check-remediation-designer.md)

#### 8.1 Git commit-msg Hook（环节 13 代码上线）

**位置**: `scripts/git_hooks/commit-msg` + `scripts/install_git_hooks.sh`

**目的**: 强制所有 commit message 包含 Task ID，实现代码变更与任务的可追溯性。

**校验规则**:
- commit message 必须匹配 `T-[0-9]{8}-[0-9]{1,3}` 格式
- 豁免: Merge/Revert/Initial commit 自动跳过
- 绕过: `SKIP_TASK_ID_CHECK=1` 环境变量或 `git commit --no-verify`

**安装**: `bash scripts/install_git_hooks.sh`（默认安装到 nanobot + web-chat 两仓库）

#### 8.2 飞书回复不可逆操作二次确认（环节 11 飞书回复路由）

**位置**: `trigger_scheduler.py` L65-87, L340, L422

**目的**: cancel/reject 等不可逆操作必须经用户二次确认后才执行，防止误操作。

**流程**:
1. 用户发送 "T-001 取消"
2. 调度器回复确认提示: "⚠️ 确认取消 [T-001]？此操作不可逆。回复'确认取消 001'执行"
3. 用户回复 "确认取消 001" → 调度器执行
4. 每次不可逆操作记录到 decisions.jsonl

**Feature Flag**: `IRREVERSIBLE_CONFIRM_ENABLED`

#### 8.3 validate_notification() 通知验证（环节 10 飞书通知内容）

**位置**: `scheduler.py` L1126-1183（函数定义）, L1240-1249（调用点）

**目的**: 通知发送前独立验证通知内容与 task 实际状态一致，防止通知内容与任务状态不匹配。

**校验维度**:
1. 状态关键词一致性（review/done/blocked 对应关键词）
2. Task ID 存在性（full 或 short 形式）
3. 任务标题存在性
4. Review 通知包含 Go/NoGo 操作指引

**失败处理**: 记录 `notification_validation_failed` 到 decisions.jsonl，不阻塞发送

**Feature Flags**: `CROSS_CHECK_ENABLED`（总开关）+ `NOTIFICATION_VALIDATE_ENABLED`

### 整改效果与后续规划

**Phase 1 止血效果**:
- 新的 standard-dev 任务无设计文档 → 自动派 architect（不再跳过）
- developer pass 但缺文档 → 打回补文档（不再直接流转 tester）
- tester pass 但缺文档 → L0/L1 不自动通过（升级 review）
- tester pass 但无测试证据 → 打回补充 test_evidence（超限升级 review）
- 所有卡点可通过 feature flag 一键关闭（安全回滚）

**Phase 2 加固规划**（1-2 周）:
- 任务类型识别重构（`classify_task_type()` 三层分类：显式→规则→LLM）
- 新增 task type: test-only / doc-only / hotfix
- `design` 状态加入状态机（queued → design → executing）
- 实时审计触发（`audit_task_compliance()` 在 handle_worker_completion 中执行）
- 合规率目标: 新任务 ≥80%

**Phase 3 自动化规划**（1-2 月）:
- 文档生成自动化（模板引擎 + LLM 填充）
- 流程可视化监控（Dashboard + 卡点告警）
- 规则引擎 + LLM 混合决策
