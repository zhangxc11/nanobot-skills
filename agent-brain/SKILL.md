---
name: agent-brain
description: >
  智能体思维核心（L3）：周期性心跳思考，态势感知、分析判断、反思改进。
  由 cron_task 触发，不需要手动调用。
---

# Agent Brain — 心跳思考系统

智能体的周期性思维核心。通过 cron 驱动 LLM 进行态势感知、分析判断、反思改进，并将思考成果记录到 journal。

## 概述

心跳系统是智能体自主思考的最小可行版本（Phase 1）：
- **只读**：不消费 INBOX、不创建任务、不调用 CIL engine
- **周期性**：由 cron_task 按 heartbeat-config.yaml 配置的频率触发
- **自反思**：每次心跳包含对自身的反思和改进建议
- **Token 约束**：单次心跳 ≤8K token

## 脚本用法

### heartbeat_runner.py — 心跳入口

```bash
# 步骤 1：态势采集 + prompt 准备
python3 scripts/heartbeat_runner.py prepare [--data-dir PATH] [--workspace PATH]
# 输出 JSON: {status: "ready"|"skip", awareness, prompt_template, heartbeat_id, state}

# 步骤 2：记录 LLM 思考结果
python3 scripts/heartbeat_runner.py record --input-file /tmp/heartbeat-result.json [--data-dir PATH] [--heartbeat-type regular] [--heartbeat-id HB-xxx]
# 也支持 stdin: echo '{"analysis":...}' | python3 scripts/heartbeat_runner.py record

# 步骤 3（异常时）：记录错误
python3 scripts/heartbeat_runner.py record-error --error "错误描述" [--data-dir PATH]
```

### awareness_snapshot.py — 态势感知

```bash
python3 scripts/awareness_snapshot.py [--workspace PATH] [--data-dir PATH] [--json]
# 从 4 个数据源采集态势（BRIEFING、INBOX、todo、CIL 日报）
# 输出 ≤2K 字符摘要，同时写入 awareness-cache.txt
```

### journal_helper.py — Journal 读写

```bash
# 追加条目
python3 scripts/journal_helper.py append --input-file result.json [--data-dir PATH]

# 读取当日 journal
python3 scripts/journal_helper.py today [--data-dir PATH]

# 读取近期摘要（用于 prompt 注入）
python3 scripts/journal_helper.py recent [--days 3] [--data-dir PATH]

# 统计改进建议条目
python3 scripts/journal_helper.py stats [--days 2] [--data-dir PATH]
```

## 数据目录

```
data/brain/mind/
├── heartbeat-config.yaml     # 心跳配置（profiles + limits）
├── heartbeat-state.json      # 心跳运行状态
├── heartbeat.lock            # 运行时状态文件锁（自动创建/删除）
├── awareness-cache.txt       # 最新态势快照
└── journal/
    ├── YYYY-MM-DD.md         # 按日 journal
    └── archive/              # 归档
```

## 配置说明

`heartbeat-config.yaml` 结构：

```yaml
mode: trial                    # 当前 profile 名称
profiles:
  trial:
    cron_expr: "..."
    description: "试运行"
  standard:
    cron_expr: "..."
    description: "稳定运行"
limits:
  max_regular: 10              # 每天最大常规心跳次数
  max_total: 20
timezone: "Asia/Shanghai"
```

## Journal 格式

每条 journal 条目包含四段：

```markdown
## HH:MM 常规心跳 [HB-YYYYMMDD-HHMM]

### 态势分析
（对当前态势的分析判断）

### 反思
（这次心跳自身做得怎样）

### 改进建议
- [ ] 标题: 描述 [scope] (priority)

### 摘要
（一句话总结）

---
```

## cron 配置

心跳由 cron_task 触发，注册示例：

```python
cron_task(
    action="add",
    owner="agent-brain",
    name="heartbeat-regular",
    message="...",  # 三步指令：prepare → 五阶段思考 → record
    cron_expr="0,30 8-22 * * *",
    tz="Asia/Shanghai"
)
```

## Dev 环境

通过 `BRAIN_DATA_DIR` 环境变量隔离 dev 数据：

```bash
export BRAIN_DATA_DIR=/path/to/dev-workdir/data/brain/mind
python3 scripts/heartbeat_runner.py prepare
```

Dev 环境的 journal、state、lock 均写入 dev 数据目录，不影响 production。

## 并发防重入

使用状态文件锁（heartbeat.lock）防止并发心跳：
- `prepare` 获取锁（写入 PID + 时间戳）
- `record` / `record-error` 释放锁（删除文件）
- 锁超过 15 分钟视为僵尸锁，自动释放

## 错误处理

| 场景 | 处理方式 |
|------|---------|
| 数据源采集失败 | 跳过该数据源，标记 degraded |
| LLM 输出非 JSON | 宽松解析，降级为纯文本记录 |
| consecutive_errors ≥ 3 | prepare 阶段直接 skip |
| journal 写入失败 | 备份到 /tmp |
| state.json 损坏 | 重建默认状态 |
