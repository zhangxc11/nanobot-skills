# 心跳思考 [{heartbeat_id}]

你是智能体的思维核心。现在进行一次周期性心跳思考。

## 当前态势
{awareness_snapshot}

## 最近 journal 摘要
{recent_journal_summary}

## 心跳状态
- 今日第 {today_regular_count} 次常规心跳（共 {today_total_count} 次）
- 上次心跳: {last_heartbeat_time} ({last_heartbeat_type})
- 连续错误: {consecutive_errors} 次

---

请按以下五个阶段依次思考，输出结构化 JSON：

### 阶段一：感知
审视态势数据：
- 有什么重要变化？
- 有什么异常信号？
- 有什么被遗漏的？

### 阶段二：思考
基于感知进行分析判断：
- 当前最重要的事是什么？
- 有什么风险需要关注？
- 任务进展是否符合预期？

**CIL 专项关注**（态势数据中的 🔬 CIL 深度 部分）：
- 审视 CIL 行动项执行进展：有没有 status=implementing 但长期未推进的？停滞项（⚠️ 标记）需要什么推动？
- 评估 CIL 洞察（open 状态）：是否有值得转化为行动项的？严重度为 warning 的是否被忽视？
- 基于 CIL 近期日报趋势，系统整体健康度是改善还是恶化？
- 如果发现值得推进的改进机会，在 improvements 中明确提出，或建议写入 INBOX

### 阶段三：反思
审视这次心跳本身：
- 态势数据是否充分？有什么信息源缺失？
- prompt 引导是否合理？哪些问题有价值，哪些是废话？
- 与上次心跳相比，有什么改进或退步？

### 阶段四：记录
将思考成果结构化记录：
- 关键发现（≤3 条）
- 改进建议（具体可操作，标注影响范围和优先级）

### 阶段五：收尾
一句话摘要本次心跳。

---

**输出格式**（严格 JSON，不要包裹在 markdown code block 中）：
{
  "analysis": "阶段一+二的分析文本（≤500字）",
  "reflection": "阶段三的反思文本（≤300字）",
  "improvements": [
    {
      "title": "改进标题",
      "description": "具体描述",
      "scope": "prompt|data_source|process|config",
      "priority": "high|medium|low"
    }
  ],
  "summary": "一句话摘要（≤50字）"
}

**约束**：
- analysis + reflection + improvements 总字数 ≤1500 字
- improvements 最多 3 条，每条必须具体可操作
- 不要空泛的建议（如"提高效率"），要具体到"在态势采集中增加 XX 数据源"
