# 调试方法论

## 两种调试模式

### 模式 A: 线上观察

```
修改代码 → 部署 → 触发目标操作 → 等待结果 → 查日志 → 分析
```

**优势**：零初始投入，使用现有基础设施
**劣势**：周期长（30-60 分钟/轮）、信息被框架层过滤、不可精确重复

**适用**：
- 问题发现初期，还不确定根因方向
- 简单问题，1-2 轮即可解决
- 需要观察真实运行时行为的场景

### 模式 B: 离线复现

```
Dump 数据 → 独立脚本直接调 API → 即时看完整响应 → 快速迭代
```

**优势**：周期短（3-5 分钟/轮）、信息完整、可精确重复
**劣势**：需要 30-60 分钟构建复现工具

**适用**：
- 线上调试 2+ 轮未解决
- 框架层报错信息可疑（可能被封装/误导）
- 需要反复测试不同参数组合

## 何时切换到离线复现

**触发条件**（满足任一即切换）：
1. 线上调试 2 轮未找到根因
2. 框架报错信息与预期不符（如超时被报为 429）
3. 问题涉及外部 API 调用，需要看原始请求/响应
4. 需要测试多种边界条件

## Dump + 复现脚本构建步骤

### Step 1: 加 Dump 逻辑

在目标代码的关键调用前，dump 完整输入数据：

```python
import json, time, pathlib

# 临时 dump 代码（调试完移除）
dump_dir = pathlib.Path("data/analysis/dumps")
dump_dir.mkdir(parents=True, exist_ok=True)
dump_path = dump_dir / f"request_{int(time.time())}.json"
dump_path.write_text(json.dumps({
    "messages": messages,
    "tools": tools,
    "max_tokens": max_tokens,
    # ... 其他参数
}, ensure_ascii=False, indent=2))
```

### Step 2: 触发一次，获取 Dump

```bash
# 触发目标操作
curl -X POST http://127.0.0.1:9081/api/sessions/{id}/messages -d '{"message": "trigger"}'

# 等待，然后查看 dump
ls -la data/analysis/dumps/
```

### Step 3: 编写复现脚本

```python
#!/usr/bin/env python3
"""独立复现脚本 — 绕过框架，直接调 API"""
import httpx, json

# 加载 dump 数据
data = json.loads(open("data/analysis/dumps/request_xxx.json").read())

# 直接调 API（绕过 litellm 等框架）
resp = httpx.post(
    "https://api.example.com/v1/messages",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json=data,
    timeout=httpx.Timeout(read=600.0)
)

print(f"Status: {resp.status_code}")
print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
```

### Step 4: 快速迭代

在复现脚本中修改参数、验证假设，每轮只需几分钟。

### Step 5: 清理

确认修复后：
1. 移除代码中的 dump 逻辑
2. 保留复现脚本（作为回归测试工具）
3. 保留有代表性的 dump 数据（作为测试数据）

## 框架报错诊断技巧

### 多层调用链的错误传播

```
应用代码 → litellm → httpx → 代理 API → 上游 API
                ↑                    ↑
          可能封装/转译          可能封装/转译
```

**常见误导**：
| 框架报错 | 可能的真实原因 |
|----------|--------------|
| 429 Rate Limit | 超时（代理层将超时包装为 429） |
| Connection Error | DNS 解析失败、代理不可用 |
| 400 Bad Request | 消息格式不合法（如孤儿 tool_use） |
| 500 Internal Error | 上游 API 过载 |

**诊断原则**：
1. 不要只看框架层的错误消息
2. 用 `curl` 或 `httpx` 直接调代理 API，看原始 HTTP 响应
3. 对比不同代理的行为（如 ppapi vs apia）
4. 检查请求 payload 本身是否合法
