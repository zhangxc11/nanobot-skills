# Task Dispatcher — Brain Manager

> 任务调度与质量控制与质量控制中枢。管理任务全生命周期（创建→执行→Review→完成）、
> 工作组模板匹配、结构化 Cross-Check Review、自动调度与派发。

## 触发识别

**以下场景应加载此 Skill：**

| 场景 | 触发信号 |
|------|---------|
| 新 session 启动 | 每个 session 启动时读取 `data/brain/BRIEFING.md` 了解当前状态 |
| 用户提到任务管理 | "任务"、"进度"、"待办"、"排期"、"调度"、"review"、"审查" |
| 用户提新需求 | "帮我做..."、"开发..."、"实现..."、"修复..." → 注册任务 |
| 查看工作状态 | "现在在做什么"、"有什么待审"、"工作简报" |
| 触发调度 | "跑一下调度"、"派发任务"、"检查队列" |
| 用户回复带 T-xxx / R-xxx | 消息以 `T-001`、`R-005` 等开头 → 解析任务关联，执行对应操作 |

## 新 Session 启动流程

```
1. 读取 data/brain/BRIEFING.md → 了解当前工作状态
2. 如有紧急事项（P0 任务/待审项）→ 主动告知用户
3. 如用户有新需求 → 对齐目标后注册任务
4. 如需派发任务 → 触发调度器
```

---

## CLI 入口

```bash
python3 skills/task-dispatcher/scripts/brain_manager.py <command> <subcommand> [options]
```

环境变量 `BRAIN_DIR` 可指定数据目录（默认 `data/brain/`）。

---

## 命令参考

### task — 任务管理

| 命令 | 说明 |
|------|------|
| `task create --title <T> --type <TYPE> --priority <P> [--desc <D>] [--template <TPL>]` | 创建任务。type: quick/standard-dev/long-task/cron-auto/batch-dev。priority: P0/P1/P2。template 不指定时自动匹配。 |
| `task update <task_id> [--status S] [--title T] [--priority P] [--note N]` | 更新任务字段或状态。状态转换受有限状态机约束。 |
| `task list [--status active\|<status>]` | 列出任务。`--status active` 过滤掉 done/cancelled/dropped。 |
| `task show <task_id>` | 显示任务完整详情（YAML 输出）。 |
| `task delete <task_id>` | 删除任务文件。 |

### review — Review 管理

#### 基础 Review 操作

| 命令 | 说明 |
|------|------|
| `review add <task_id> --summary <S> --prompt <P>` | 为任务添加一个 review 项，自动生成 Review ID（R-xxx）。 |
| `review list [--format default\|brief\|detail]` | 列出所有待处理 review 项。brief=单行摘要；detail=含任务上下文和建议操作。 |
| `review notify <task_id> [--review-id <R>]` | 生成 review 通知内容（含飞书卡片格式），用于发送给审阅者。 |
| `review resolve <review_id\|task_id> --decision approved\|rejected\|deferred [--note N]` | 解决 review 项。approved 时自动触发任务状态转换（review→executing）；rejected 触发 review→revision。 |

#### Cross-Check Review（结构化审查）

| 命令 | 说明 |
|------|------|
| `review level <task_id>` | **判定 Review 级别**。根据任务特征返回 L0-L3 级别和推荐审查角色。 |
| `review checklist <task_id> --role <ROLE>` | **生成结构化 Checklist**。ROLE: code_reviewer / test_verifier / safety_checker。 |
| `review submit <task_id> --result-file <PATH>` | **提交结构化 Review 结果**。加载 YAML 结果文件，执行 schema 验证，保存结果并运行自动仲裁。 |

**Review 级别判定规则：**

| 级别 | 触发条件 | 审查角色 |
|------|---------|---------|
| L0 | template = quick / cron-auto | 无（自动通过） |
| L1 | standard-dev, ≤2 files, 无 interface change | 无（自检即可） |
| L2 | 默认级别 | code_reviewer（long-task 为 test_verifier） |
| L3 | architecture_change / external_publish / financial_logic / batch-dev / P0 | code_reviewer + test_verifier（+可选 safety_checker） |

### template — 工作组模板

| 命令 | 说明 |
|------|------|
| `template match --title <T> [--desc <D>]` | 根据标题/描述自动匹配最佳模板。 |
| `template list` | 列出所有可用模板。 |
| `template show <name>` | 显示模板完整配置。 |

### quick — 快速任务日志

| 命令 | 说明 |
|------|------|
| `quick log --title <T> [--result <R>]` | 记录快速任务结果。 |
| `quick list` | 列出今日快速任务。 |
| `quick archive` | 归档今日之前的快速任务。 |

### daily — 日维护与日报

| 命令 | 说明 |
|------|------|
| `daily maintenance` | 日维护（归档 quick-log、扫描超时待审、刷新 BRIEFING）。 |
| `daily report` | 只读日报（各状态任务数、待审数、超时待审数）。 |

### briefing / registry — 视图管理

| 命令 | 说明 |
|------|------|
| `briefing update` | 重新生成 BRIEFING.md。 |
| `registry update` | 重新生成 REGISTRY.md。 |

### decisions — 决策日志

| 命令 | 说明 |
|------|------|
| `decisions list [--limit N]` | 列出最近 N 条决策记录。 |

---

## 调度器

### 架构

```
飞书 Session（用户对话窗口）
    │ 对齐需求 → 注册任务到 REGISTRY
    │
    ├── 主动触发 → trigger_scheduler.py → 唤醒/创建 Dispatcher Session → spawn workers
    │
Cron（30min 兜底）
    │
    └── 触发 → trigger_scheduler.py → 唤醒/创建 Dispatcher Session → spawn workers
```

### 核心设计约束

1. **固定调度器 session** — 复用同一个 dispatcher session，避免每次创建新 session 的开销
2. **调度器无状态** — 只做决策（读 REGISTRY → 排序 → 输出 spawn 指令），状态全靠 REGISTRY 文件持久化
3. **单次派发上限 3 个** — 剩余任务下次调度再处理
4. **Worker 结果异步回收** — worker 完成后通过 brain_manager 更新 REGISTRY，调度器下次启动自然看到
5. **换代机制** — dispatcher session 超过 500 轮自动创建继任者，防止上下文膨胀

### 调度原则

| 原则 | 说明 |
|------|------|
| 优先级驱动 | P0 最先、P1 次之、P2 有空再做 |
| 依赖拓扑 | 有前置依赖（blocked_by）的等依赖完成 |
| 并发控制 | 全局最多 3 个 executing 任务（API 限流约束） |
| 单次上限 | 每次调度最多新派 3 个任务 |
| Review 匹配复杂度 | L0 免审、L1 自检、L2 单审、L3 交叉审 |
| 固定 session | 复用 dispatcher session，500 轮换代 |
| 快速通道 | quick 类型不走调度，直接在 session 内执行 |
| 用户时间最宝贵 | 攒批通知，不逐个打扰 |

### 调度器状态（dispatcher.json v2）

- **状态文件**: `data/brain/dispatcher.json`
- **格式**: `{"session_id": "...", "session_key": "...", "version": 2, "generation": N, "iteration_count": N, "previous_session_id": "...", ...}`
- **换代阈值**: 500 轮唤醒 **或** 1500 条消息（双重检测，先到先换代）
- **并发保护**: session 5 分钟内有活动时跳过唤醒（防止重复派发）
- **失效检测**: 通过 web-chat API 检查 session 的 `lastActiveAt`，超过 60 分钟无活动视为失效
- **换代追溯**: `generation` 字段记录第几代 session，`previous_session_id` 记录上一代
- **查看状态**: `python3 trigger_scheduler.py --status`
- **重置**: `python3 trigger_scheduler.py --reset`

### 触发方式

#### 方式 1: 飞书/CLI 主动触发

```bash
python3 skills/task-dispatcher/scripts/trigger_scheduler.py --parent "当前session_id"
```

#### 方式 2: Cron 30min 兜底

```bash
# 使用 nanobot cron tool — 极简消息，LLM 只需运行一个命令
cron(
    action="add",
    name="task-dispatcher-scheduler",
    message="请触发任务调度器：\n\npython3 skills/task-dispatcher/scripts/trigger_scheduler.py\n\n直接运行上述命令，输出 JSON 结果即可。不需要做其他事情。",
    every_seconds=1800,
)
```

#### 方式 3: Dry-run（只看不做）

```bash
python3 skills/task-dispatcher/scripts/trigger_scheduler.py --dry-run
```

### 调度器 CLI

```bash
# 直接运行调度逻辑（在 web subsession 内使用）
python3 skills/task-dispatcher/scripts/scheduler.py run [--parent SESSION_ID] [--dry-run]
python3 skills/task-dispatcher/scripts/scheduler.py record-spawn --task-id T --role R
python3 skills/task-dispatcher/scripts/scheduler.py handle-completion --task-id T
python3 skills/task-dispatcher/scripts/scheduler.py mark-done --task-id T
python3 skills/task-dispatcher/scripts/scheduler.py mark-blocked --task-id T --reason R
python3 skills/task-dispatcher/scripts/scheduler.py status
```

---

## 验收标准（按任务类别）

调度器派发任务时，会根据任务类别在 Worker prompt 中注入对应的验收要求：

| 类别 | 验收标准 |
|------|---------|
| **纯后端/脚本** | 单元测试 + 集成测试 + 实际运行确认输出 |
| **API 接口** | 单元测试 + 实际调用验证请求/响应 + 错误码测试 |
| **Web 前端/UI** | 单元测试 + **浏览器实际打开验证（必须）** + 截图证据 |
| **飞书集成** | 实际发送消息验证 + 截图或消息 ID 证据 |
| **数据处理** | 真实数据验证（非 mock）+ 边界条件 + 输出格式验证 |

> ⚠️ **纯 mock 测试不能算验收通过。** Web 前端任务必须浏览器实测+截图。

---

## 端到端流程示例

### 场景: 用户在飞书说"帮我优化缓存命中率"

```
1. [飞书 Session] 用户提需求
   ↓
2. [飞书 Session] Agent 与用户对齐目标（本质问题、期望效果）
   ↓
3. [飞书 Session] 注册任务:
   brain_manager.py task create --title "缓存命中率优化" --type standard-dev --priority P1
   → 创建 T-20260330-001 (status: queued)
   ↓
4. [飞书 Session] 触发调度:
   trigger_scheduler.py --parent "feishu.ST.xxx"
   → 唤醒已有 dispatcher session（或创建新的）
   ↓
5. [Dispatcher Session] 收到唤醒消息，运行 scheduler.py run
   → 发现 T-20260330-001 queued
   → 优先级排序 → 依赖检查 → 并发检查
   → 更新状态 queued → executing
   → 生成 worker prompt（含验收要求）
   → 输出 spawn 指令
   ↓
6. [Dispatcher Session] 创建 worker subsession
   → bash create_subsession.sh --session-key "webchat:worker_xxx_T-20260330-001" ...
   ↓
7. [Worker Subsession] 执行任务
   → 读取上下文 → 设计 → 开发 → 测试 → 自验
   → brain_manager.py task update T-20260330-001 --status review
   → brain_manager.py review add T-20260330-001 --summary "代码审查"
   → brain_manager.py briefing update
   ↓
8. [下次调度] 发现 T-20260330-001 in review + pending review
   → 报告中列出 review_pending
   ↓
9. [飞书 Session] 用户说"查看待审"
   → 读 BRIEFING → 读 review → 展示给用户
   → 用户 approve → brain_manager.py review resolve ... --decision approved
   → 状态 → done
```

---

## 数据目录结构

```
data/brain/
├── tasks/              # 任务 YAML (T-xxxxxxxx-nnn.yaml)
├── reviews/            # Review YAML (R-xxx.yaml)
├── review-results/     # 结构化 Review 结果
├── decisions.jsonl     # 决策日志
├── quick-log.jsonl     # 快速任务日志
├── dispatcher.json     # 调度器 session 状态（固定 session 模式）
├── BRIEFING.md         # 派生视图：任务简报
└── REGISTRY.md         # 派生视图：任务注册表

skills/task-dispatcher/
├── SKILL.md            # 本文件
├── scripts/
│   ├── brain_manager.py        # CLI 主程序
│   ├── scheduler.py            # 调度器核心逻辑 (v2)
│   ├── scheduler_legacy.py     # 调度器旧版本 (deprecated v1)
│   ├── trigger_scheduler.py    # 调度触发器（固定 session + 换代机制）
│   ├── review_connector.py     # Review 上下文加载
│   ├── feishu_notify.py        # 飞书通知格式化 + 回复解析
│   ├── test_brain_manager.py   # brain_manager 测试
│   ├── test_trigger_scheduler.py  # dispatcher 触发器测试
│   ├── test_scheduler_legacy.py   # 旧版调度器测试
│   ├── test_feishu_notify.py   # 飞书通知模块测试
│   └── test_templates.py       # 模板集成测试
├── templates/
│   └── *.yaml                  # 工作组模板
└── checklists/
    ├── code_review.yaml
    ├── test_verify.yaml
    └── safety_check.yaml
```

## 飞书回复路由

当用户在飞书回复中包含任务/Review 短码时，按以下流程处理：

### 解析

```bash
python3 skills/task-dispatcher/scripts/feishu_notify.py parse "<用户消息>"
```

返回 JSON：`{"ok": true, "data": {"task_id": "T-xxx", "action": "approve", "comment": "..."}}` 或 `{"ok": true, "data": null}`（无任务引用）。

### 路由规则

| action | 执行命令 |
|--------|---------|
| approve | `brain_manager.py review resolve <task_id> --decision approved` |
| reject | `brain_manager.py review resolve <task_id> --decision rejected --note "<comment>"` |
| conditional_approve | `brain_manager.py review resolve <task_id> --decision approved --note "条件: <comment>"` |
| pause | `brain_manager.py task update <task_id> --status blocked --note "用户暂停"` |
| cancel | `brain_manager.py task update <task_id> --status cancelled --note "<comment>"` |
| resume | `brain_manager.py task update <task_id> --status executing --note "用户恢复"` |
| defer | `brain_manager.py review resolve <task_id> --decision deferred` |
| comment | 加载任务上下文，作为对话继续处理 |

### 确认

操作完成后，回复用户确认（如 "✅ T-001 已批准"）。

### 通知格式

所有飞书通知使用统一格式（由 `feishu_notify.py` 生成）：
- `emoji [T-短码] 标题 — 事件类型` + 分隔线 + 内容 + 操作提示
- 短码规则：`T-001` = `T-{today}-001` 的简写
- 回复示例始终包含在通知中，方便用户直接复制

### 批量通知

```bash
# 生成所有待审项的通知
python3 skills/task-dispatcher/scripts/review_connector.py notify-all
```

---

## 输出格式

所有命令输出 JSON：

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": "错误信息"}
```
