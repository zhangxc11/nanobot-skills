# 单 Turn 工具调用限制应对

## 问题背景

Agent 每个 turn（一次 LLM 推理 → 工具调用 → 下一次推理的循环）有 `max_iterations` 限制（默认 30）。长程任务中，单个 turn 容易因以下原因耗尽配额：

- 需要读取大量文件来理解上下文
- 需要执行多步操作（修改 → 测试 → 查看结果 → 再修改）
- subagent 也有独立的 iteration 限制

## 技巧 1: Subagent 分担工具密集型工作

**原则**：主 session 是指挥官，subagent 是执行者。

### 适合委派的工作

| 工作类型 | 示例 |
|----------|------|
| 多文件分析 | "读取 src/ 下所有 .py 文件，找出所有 TODO 注释" |
| 文档撰写 | "基于以下要点写一份分析报告到 path/to/report.md" |
| 数据收集 | "统计最近 7 天的 llm-logs，按 session 分组汇总" |
| 代码修改 | "按照以下方案修改 memory.py：{详细方案}" |
| 测试执行 | "运行测试套件，报告失败的测试和错误信息" |

### 不适合委派的工作

- 需要与用户实时交互的决策
- 依赖上一步结果的串行推理（subagent 无法访问主 session 上下文）
- 需要跨多个 subagent 协调的工作（用主 session 协调）

### Subagent 任务描述要自包含

```
# ❌ 差：依赖隐式上下文
spawn(task="修复那个 bug")

# ✅ 好：自包含，所有信息都在任务描述中
spawn(task="""
修改 dev-workdir/nanobot/nanobot/agent/memory.py:
1. 在 _sanitize_slice() 方法中，Pass 3 的逻辑从"裁剪 tool_calls"改为"从 keep 段补入 tool_results"
2. 具体改法：遍历 slice 中每个 assistant 的 tool_calls，检查是否有匹配的 tool_result...
3. 修改完成后运行 python3 -m py_compile memory.py 验证语法
4. 报告：修改了哪些行，py_compile 结果
""")
```

## 技巧 2: 及时返回刷新轮次

**核心原理**：每次 agent 返回给用户（或被 subagent 结果唤醒）时，工具轮次计数器重置。

### 模式 A: Spawn 后立即返回

```
Turn 1 (30 iterations):
  分析问题 → 制定方案 → spawn(subagent) → 返回 "已启动，等待结果"
  
# subagent 完成后触发新 turn

Turn 2 (30 iterations，重置):
  review subagent 结果 → 下一步操作...
```

### 模式 B: 阶段性返回

对于需要多阶段执行的工作，主动在阶段间返回：

```
Turn 1: 数据收集阶段 → 返回 "数据收集完成，发现 X 个问题，继续分析？"
Turn 2: 分析阶段 → 返回 "分析完成，建议方案 A/B，请确认"
Turn 3: 执行阶段 → 返回 "修改完成，验证结果..."
```

### 模式 C: 用户触发继续

如果 agent 在一个 turn 中接近轮次上限，应主动返回：

```
"当前已完成 X，还需要 Y。请回复任意内容继续。"
```

## 技巧 3: 批量操作合并

### 用 exec 合并多次文件读取

```bash
# 一次 exec 读取多个文件的关键部分
exec("head -50 a.py; echo '---'; grep -n 'def ' b.py; echo '---'; wc -l c.py")
```

### 用 exec 合并多次搜索

```bash
# 一次 exec 搜索多个模式
exec("grep -rn 'consolidat' src/ --include='*.py' | head -30")
```

### 用脚本封装多步操作

```bash
# 将分析流程封装为脚本
write_file("tmp/check.sh", """#!/bin/bash
echo "=== Git Status ==="
git status --short
echo "=== Recent Commits ==="
git log --oneline -10
echo "=== Test Results ==="
python3 -m pytest tests/ -q 2>&1 | tail -20
echo "=== File Sizes ==="
wc -l src/**/*.py | sort -rn | head -10
""")
exec("bash tmp/check.sh")
```

## 技巧 4: 预判轮次消耗

在开始一个操作序列前，预估需要的工具调用次数：

| 操作 | 预估轮次 |
|------|----------|
| 读 1 个文件 | 1 |
| exec 1 个命令 | 1 |
| edit_file 1 次 | 1 |
| 读文件 + 修改 + 验证 | 3 |
| spawn subagent | 1（但后续 turn 重置） |
| 完整的"分析→修改→测试"循环 | 5-8 |

如果预估超过剩余轮次的 70%，考虑：
1. 委派给 subagent
2. 封装为脚本
3. 先返回，下个 turn 继续

## 技巧 5: Subagent 的 max_iterations 调整

复杂任务可以给 subagent 更多轮次：

```python
spawn(
    task="详细分析 10 个文件...",
    max_iterations=50,   # 默认 30，复杂任务可调高
    max_tokens=16384     # 代码生成任务需要更多 token
)
```

但注意：轮次越多，subagent 运行时间越长，token 消耗越大。
