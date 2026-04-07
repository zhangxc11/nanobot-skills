# Architect — 经验积累

> 本文件记录该角色在实际执行中积累的经验教训，由 Retrospective 回流写入。
> Architect 在设计方案时应主动参考本文件，避免重复踩坑。

---

## 一、测试方案设计

### 1.1 E2E 测试定义必须具体到可执行级别（M-010 教训）

**背景**：Phase 4 (T-20260404-001) 中，Architect 验收方案对 E2E 定义模糊（只写了"dry-run 全流程"），导致 Developer 用函数调用实现"E2E"，Code Review/Tester/Test Review/Auditor 六个角色全部在模糊定义上滑过，无人质疑。最终用户发现所谓 E2E 只是构造 mock → 调函数 → 检查返回值，而非真实环境端到端。

**经验**：Architect 的验收方案中，每条 E2E 测试必须明确指定：
- **执行方式**：CLI 命令 / API 调用 / UI 操作（具体到命令或 URL）
- **执行环境**：dev（localhost:9081/9082）/ prod（localhost:8081/8082）
- **数据流**：真实文件系统+真实 session / mock 数据（明确哪些是真实的哪些是 mock 的）
- **验证手段**：检查文件是否生成 / 检查 YAML 状态变更 / 截图对比 / 日志关键字

**注意事项**：
- ❌ 禁止写"E2E 全流程验证"这种模糊描述
- ❌ 禁止写"端到端测试"但不指定是 CLI 还是函数调用
- ✅ 应写成："在 dev 环境执行 `python3 skills/xxx-dev/scripts/trigger_scheduler.py --task T-xxx`，验证任务从 queued → executing → 角色流转 → done 全链路，检查 `data/brain/tasks/T-xxx.yaml` 状态字段变更"

### 1.2 测试方案必须与设计方案同步产出

**背景**：多次观察到 Architect 先完成设计方案后补测试方案，导致测试方案成为设计方案的"附属品"，缺乏独立思考。Phase 4 的 E2E gap 就是这个模式的典型后果。

**经验**：Architect 交付物必须同时包含设计方案和验收方案（acceptance_plan），两者同等重要。验收方案不是设计方案的附录，而是独立的质量契约。

**注意事项**：
- acceptance_plan 的每一项必须包含 `step_id`、`description`、`category`、`expected_result` 四个字段
- category 建议覆盖：review（代码审查）、unit（单元测试）、e2e（端到端）、doc（文档）
- E2E 类别的 step 必须满足 1.1 中的具体性要求

### 1.3 LLM Agent 验证倾向性——系统性风险

**背景**：在多个任务中反复观察到，Agent（包括 Tester、Test Review、Auditor）倾向于做容易的验证（函数调用、单元测试、代码审查）而非有效的验证（真实环境端到端）。这不是个别 Agent 的问题，是 LLM 的系统性倾向。

**经验**：Architect 在设计验收方案时，必须预设"后续角色会走捷径"的假设，用方案的具体性来约束执行。验收方案越模糊，后续角色偷懒的空间越大。

**注意事项**：
- Architect 是质量链条的源头，验收方案的质量决定了整条链路的质量上限
- 如果 Architect 写了模糊的 E2E 描述，Tester 做函数级测试是"合规"的——问题在 Architect 不在 Tester
- Dev E2E 验证建议分两轮：第一轮手动跑通确认可行，第二轮由 Tester 独立执行确认可复现

### 1.4 SPA 应用的 E2E 测试特殊要求（T-012 教训）

**背景**：T-012 Topic 页面重构中，前端使用 Zustand activeTab 做 tab 切换（SPA 内部状态），不改变 URL。E2E 脚本用 URL 路由测试无法验证 tab 切换是否生效。Tester 截图后未校验内容差异，多张截图实际是同一画面。

**经验**：对 SPA tab-based 导航的 E2E 测试：
- 不能依赖 URL 变化验证导航
- E2E 脚本应在每次操作后截图，并立即计算 MD5
- 连续两张截图 MD5 相同 → 说明操作未生效，应中断报错

**注意事项**：
- Architect 设计前端相关验收方案时，必须了解目标页面的导航机制（URL 路由 vs 内部状态）
- 前端开发必须截图测试，截图必须有 MD5 唯一性校验
- Test Review 的 MD5 唯一性校验是当前最有效的前端测试质控手段

### 1.5 acceptance_plan 的 E2E 步骤必须包含四要素（T-001/T-002 教训）

**背景**：T-001 中 AP 13 条全部通过但 3 个真实 bug 全部漏检，T-002 中 AP-10 E2E 深度不确定导致 conditional_pass。两次问题的共同根因是 AP 中 E2E 步骤缺少具体的执行方式和验证手段，Tester 只能做 happy-path。

**经验**：每个 category="e2e" 的 AP 步骤必须包含四要素：
- `exec_method` — 具体执行命令或操作
- `exec_env` — 执行环境（dev/prod/CI）
- `data_flow` — 真实数据还是 mock
- `verify_cmd` — 具体的验证命令或检查步骤

**注意事项**：
- 这四个字段已加入 acceptance_plan 的输出格式，仅 E2E 步骤必填
- Architect 交付前必须完成 E2E 自检清单（见 ROLE.md A+ 节）
- 如果 AP 全是 happy-path 没有边界测试，说明 AP 设计不足——不能指望 Tester 自行补充所有边界场景

---

## 二、Dev 环境与验证流程

### 2.1 Dev 环境验证不依赖代码 push 到 origin

**背景**：用户纠正了一个常见误解——以为 dev 环境测试需要先 push 代码到远程仓库。实际上 dev 环境验证只需要本地 dev-workdir 下有对应 feature branch 并基于该分支启动 dev 环境即可。

**经验**：Dev 环境验证流程：
1. 在 `dev-workdir/` 下的 git 仓库中创建 feature branch 并完成开发
2. 基于该 feature branch 启动 dev 环境（端口 9081/9082）
3. 在 dev 环境中执行 E2E 测试
4. 测试通过后，代码才拉回 prod 分支（merge to main）

**注意事项**：
- Architect 设计方案中涉及 dev 环境测试时，不要写"push 到 origin 后在 dev 环境测试"
- 正确描述："在 dev-workdir feature branch 上启动 dev 环境测试"
- 测试通过 → merge to main → 部署 prod，顺序不能乱

### 2.2 验收硬标准——不可协商

**背景**：用户多次明确要求，所有开发任务的验收必须满足以下硬标准，不存在例外。

**经验**：
1. 所有开发必须在 dev 环境实际部署并实测通过
2. 由 Tester 在实际场景测试（非只看代码或跑单元测试）
3. 前端开发必须截图测试
4. 以上全部通过才能验收

**注意事项**：
- Architect 设计验收方案时，必须包含 dev 环境部署验证步骤
- 不能用"单元测试全部通过"替代 dev 环境实测
- 不能用"代码审查无问题"替代实际运行验证

### 2.3 涉及外部 API 的改造——先试通再正式开发

**背景**：飞书 API 改造过程中，直接进入正式开发流程后发现 API 行为与文档不一致，导致大量返工和超时浪费。

**经验**：当设计方案涉及外部 API（飞书、GitHub、第三方服务等）调用时，Architect 应在方案中增加"API 可行性验证"前置步骤：
1. 先用独立脚本试通目标 API（确认认证、参数、返回格式）
2. 记录实际 API 行为与文档的差异
3. 基于实际验证结果再进入正式设计和开发

**注意事项**：
- 这个步骤应该在 Architect 设计阶段就安排，而非等 Developer 开发时才发现问题
- 验证脚本应保留作为后续 E2E 测试的基础

---

## 三、handle-completion 与调度器交互

### 3.1 acceptance_plan 格式必须是 list

**背景**：handle-completion 期望 `acceptance_plan` 为 list 类型，每项包含 `step_id`/`description`/`category`/`expected_result`。但 Architect 可能输出 dict 格式（含 `tests` 数组，每项带 `id`/`type`/`title`/`steps`/`pass_criteria`），导致后续流程解析失败。

**经验**：Architect 报告中的 acceptance_plan 必须严格遵循以下格式：
```yaml
acceptance_plan:
  - step_id: "AP-01"
    description: "验证 xxx 功能"
    category: "e2e"
    expected_result: "xxx 状态变为 yyy"
  - step_id: "AP-02"
    ...
```

**注意事项**：
- ❌ 不要输出 dict 格式（`{tests: [{id, type, title, steps, pass_criteria}]}`）
- ❌ 不要输出嵌套结构
- ✅ 扁平 list，每项四个字段，step_id 用 "AP-01" 格式

### 3.2 verdict 只能用 pass/fail/blocked/partial

**背景**：handle-completion 的 `valid_verdicts` 只有 `pass`/`fail`/`blocked`/`partial` 四个值。Architect 曾使用 `"approved"` 作为 verdict，被调度器当作"无报告"处理（Bug #23），导致流程卡住。

**经验**：所有角色报告的 verdict 字段只能使用以下四个值之一：
- `pass` — 通过
- `fail` — 失败
- `blocked` — 阻塞
- `partial` — 部分完成

**注意事项**：
- ❌ 不要用 `approved`、`accepted`、`completed`、`done` 等看似合理但不在白名单中的值
- 这个约束适用于所有角色，不只是 Architect

### 3.3 worker_instructions 必须是 string 类型

**背景**：handle-completion 中多处对 `worker_instructions` 调用 `.strip()`，要求为 string 类型。Architect 可能输出 list（多条指令），导致 `.strip()` 报错。

**经验**：Architect 报告中的 `worker_instructions` 必须是单个 string，不能是 list。如有多条指令，用换行符拼接为一个 string。

**注意事项**：
- ❌ `worker_instructions: ["指令1", "指令2"]`
- ✅ `worker_instructions: "1. 指令1\n2. 指令2\n3. 指令3"`

---

## 四、代码与路径规范

### 4.1 Dev Skill 调用必须通过 symlink 路径

**背景**：Phase 6 E2E 测试中发现，直接运行 `python3 dev-workdir/_nanobot-skills/task-dispatcher/scripts/xxx.py` 会导致 `_path_config.py` 中的 `Path(__file__).absolute()` 解析到真实路径而非 symlink 路径，`SKILL_NAME` 变成 `task-dispatcher` 而非 `task-dispatcher-dev`，生成的 dispatcher prompt 指向 prod skill。

**经验**：设计方案中涉及 dev skill 测试时，必须明确指定通过 symlink 路径调用：
- ✅ `python3 skills/task-dispatcher-dev/scripts/xxx.py`（通过 symlink）
- ❌ `python3 dev-workdir/_nanobot-skills/task-dispatcher/scripts/xxx.py`（直接路径）

**注意事项**：
- 这是 Skill 自洽原则的体现——通过 skill 调用就使用它自己的代码
- Architect 在设计 E2E 测试步骤时，所有脚本路径都应使用 `skills/xxx-dev/scripts/` 前缀
- 如果设计方案中出现 `dev-workdir/` 开头的直接路径调用，说明路径规范有问题

### 4.2 prod/dev scheduler.py 不同步——bug 修复需两处操作

**背景**：prod 和 dev 的 scheduler.py 行数差异约 500 行（prod 含 V6.1 8-role flow features），不是 symlink 关系（dev 使用 `_path_config.py` 做路径隔离，不能建 symlink）。

**经验**：当设计方案涉及 scheduler.py 的 bug 修复或改动时，必须在方案中明确标注：
- 改动需要同时应用到 prod 和 dev 两个文件
- 两个文件的代码上下文可能不同，不能简单 copy-paste
- 验证也需要分别在两个环境执行

**注意事项**：
- prod 路径：`_nanobot-skills/task-dispatcher/scripts/scheduler.py`
- dev 路径：`dev-workdir/_nanobot-skills/task-dispatcher/scripts/scheduler.py`
- 设计方案中应有专门的步骤描述"同步修改到 dev 环境"

### 4.3 subagent prompt 必须用完整 workspace 路径

**背景**：M-009 教训，subagent 中使用 `~/.nanobot/` 缩写路径导致路径解析失败。

**经验**：设计方案中涉及的所有文件路径，必须使用完整的绝对路径：
- ✅ `/Users/zhangxingcheng/.nanobot/workspace/...`
- ❌ `~/.nanobot/workspace/...`

**注意事项**：
- 这个要求适用于所有会被 subagent 读取的路径（设计文档、测试方案、参考文件等）
- Architect 在 worker_instructions 中引用文件时尤其需要注意

---

## 五、设计方案质量

### 5.1 设计方案必须与 ARCHITECTURE.md 设计哲学一致

**背景**：Phase 6 中发现 F2 设计文档因早于 F1 产出，路径描述违反了 P5（Skill 自洽原则）和 P8（通用机制优先），导致 13 处需要修正。

**经验**：Architect 完成设计方案后，必须对照项目的 `ARCHITECTURE.md`（如存在）进行自检，逐条核对设计哲学原则。

**注意事项**：
- 特别是跨 feature 的设计文档，需要交叉校验一致性
- 如果多个 feature 并行设计，后产出的方案可能与先产出的方案存在冲突
- 自检应作为 Architect 交付前的标准动作，不依赖 Review 角色发现

### 5.2 Dispatcher 只传调度指令+文件引用，不搬运内容

**背景**：Phase 5 中发现 dispatcher 的 exec tool 输出截断（18KB→2KB），导致 Architect 精心设计的测试方案在传递给 Tester 时丢失。根本原因是 dispatcher 试图在 prompt 中转发全量设计/测试方案内容。

**经验**：Architect 的设计方案和验收方案应写入文件，在 worker_instructions 中只引用文件路径，不要期望内容被完整传递。

**注意事项**：
- worker_instructions 应简洁（目标 < 2KB），只包含任务目标和文件引用
- 详细的设计方案、测试步骤、验收标准应存放在独立文件中
- Worker（Developer/Tester 等）应自己读取文件获取完整内容

### 5.3 快速迭代导向——不要过度规划

**背景**：用户多次强调 Agent 开发效率很高，不需要按天估工作量，不要过度规划。

**经验**：
- 每个 Phase 尽量小，快做快验证快调整
- 做了就试、试了就调
- 设计方案应聚焦"做什么"和"怎么验证"，不需要详细的时间估算和风险矩阵

**注意事项**：
- 如果一个设计方案超过 500 行，考虑是否可以拆分为更小的 Phase
- 验收方案的价值远大于详细的实施计划

---

## 六、流程与协作

### 6.1 不能给已执行中的角色加需求

**背景**：Phase 2 开发过程中，曾尝试给已在执行的 Developer 补充新需求，用户明确指出这不合理。

**经验**：需求变更应走正规流程：
- 如果 Developer 已在执行，新需求应记录到下一个 Phase 或创建新任务
- 如果需求变更影响当前设计方案的正确性，应打回 Architect 重新设计

**注意事项**：
- Architect 在设计阶段应尽量完整地收集需求，减少后续变更
- 如果确实需要变更，评估影响范围后决定是"打回重设计"还是"记入下一 Phase"

### 6.2 开发隔离必须保留 git

**背景**：M-008 教训，早期使用 dev-workdir 裸文件副本开发（cp 代码文件，无 .git），导致无法直接 push/merge，部署时只能 cp 回去。

**经验**：隔离开发必须在 git 仓库内（feature branch 或 clone），能直接 push/merge。禁止裸文件 cp 部署。

**注意事项**：
- Architect 设计方案中的开发环境描述应明确指定 git 分支策略
- 示例："在 `dev-workdir/_nanobot-skills/` 仓库中创建 `feat/xxx` 分支开发"
- ❌ 禁止出现"将文件复制到 dev-workdir 进行开发"的描述

### 6.3 部署前必须确认改动范围

**背景**：部署 web-chat 时未区分前端/后端改动，导致重启了不需要重启的服务。

**经验**：Architect 设计方案中应包含部署说明：
- 只涉及前端 → 只重启 webserver
- 涉及后端 → 也需重启 worker
- 涉及 skills/scripts → workspace 层改动，不需要重启（实时生效）

**注意事项**：
- 确认代码已合并到目标分支后再部署
- workspace 层（skills/、scripts/）的改动是实时读取的，不需要重启 prod 服务

---

*最后更新：2026-04-06，基于 Dispatcher Phase 3~6 + Phase 0 开发测试经验整理*
