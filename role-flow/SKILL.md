# role-flow — 多角色协同框架

> 纯文档 skill：定义角色（Role）、编排模式（Pattern）和协同方法论。

---

## 一、这是什么

role-flow 是一套**多角色协同框架**，用于将复杂任务拆分给多个独立角色执行，并通过交叉检查保证产出质量。

核心思路很简单：**LLM 不能自己检查自己的产出**。就像代码需要他人 review 一样，每个角色的产出都必须由另一个独立角色检查。role-flow 通过定义角色和编排模式，将这个理念系统化。

## 二、核心概念

### Role（角色）— 原子执行单元

Role 定义了一个独立的职责和行为规范。每个 Role：
- 有明确的职责边界（做什么、不做什么）
- 有规范的输入输出格式
- **必须由独立 subagent 执行**

角色是框架的最小单元，可以被不同 Pattern 复用。

> 角色定义文件位于 `roles/*/ROLE.md`。

### Pattern（模式）— 角色的编排组合

Pattern 定义了一组角色如何组合、按什么规则流转、怎样交叉检查。每个 Pattern：
- 指定参与的角色组合
- 定义角色间的流转规则（pass/fail 后去哪）
- 通过 **Cross-Check 覆盖矩阵** 自证每个产出都有另一角色检查
- 声明验收标准和 Auditor 要求

Pattern 是具体的协同方案。不同场景选用不同 Pattern。

> 模式定义文件位于 `patterns/*.md`，所有 Pattern 遵循 `patterns/_TEMPLATE.md` 的统一结构。

### Role 与 Pattern 的关系

```
Role   = 积木块（独立定义，可复用）
Pattern = 搭建方案（选哪些积木、怎么拼装）
```

两者都是可扩展的 — 可以新增 Role 来定义新职责，也可以新增 Pattern 来定义新的协同方案。

---

## 三、核心方法论

### 方法论 1：Cross-Check 原则

> **任何 LLM 角色的产出，都必须有另一个独立 LLM 角色检查。**

这是贯穿每个环节的设计理念，不是流程末尾的审计关卡，也不是某个固定步骤名。

**为什么需要**：同一个 LLM session 内"切换视角"本质上是自己检查自己，无法发现自身盲区。独立角色拥有独立上下文，才能形成真正的交叉检查。

**如何落地**：每个 Pattern 必须提供一张 **Cross-Check 覆盖矩阵**，逐行列出"谁的产出 → 谁来检查 → 检查什么"，无遗漏即合规。

例如，在 Dev Pipeline 模式中，cross-check 是这样覆盖的：

| 产出角色 | 产出内容 | Cross-Check 角色 |
|---------|---------|-----------------|
| Architect | 设计方案 + 验收方案 | Architect Review |
| Developer | 代码 + 单元测试 | Architect（代码审查） |
| Tester | 测试报告 + 证据 | Architect（测试审查） |
| 整体流程 | 流程完整性 | Auditor + Retrospective |

> Architect 对任务全貌最清楚，因此在 Dev Pipeline 中由它承担代码审查和测试审查的 cross-check 职责。但这只是该 Pattern 的具体设计，其他 Pattern 可以有不同的 cross-check 安排，只要覆盖矩阵无遗漏即可。

### 方法论 2：独立审计原则

流程执行完成后，必须有独立角色回溯检查所有环节是否完整发生。两个角色各司其职：

| 角色 | 面向 | 职责 | 是否阻塞交付 |
|------|------|------|-------------|
| **Auditor** | 当前任务 | 流程是否合规 — 环节完整、cross-check 覆盖、流转合规 | ✅ 是 |
| **Retrospective** | 未来改进 | 经验教训沉淀、改进建议 | ❌ 否（可选执行） |

Auditor 只查流程，不碰质量。质量由各环节的 cross-check 角色负责。

### 方法论 3：独立执行原则

> **每个角色必须由独立 subagent 执行。**

不论调用方是主 session 还是 dispatcher，都必须 spawn 独立 subagent 来扮演每个角色。

**同一 session 内切换角色 = 自己检查自己 = 违反 cross-check 原则。**

**同一任务中，同一角色应复用同一 session（follow_up），保持上下文连贯。** 例如 Architect 设计方案被 Review 打回后需要修改，应 follow_up 原来的 Architect subagent 而非重新 spawn 一个新的 — 新 session 没有之前的设计上下文，会导致信息丢失和重复工作。

---

## 四、框架级硬约束

> ⛔ 以下两条是不可违反的框架级约束，所有 Pattern 和调用方都必须遵守。

| # | 约束 | 说明 |
|---|------|------|
| **1** | **每个角色产出必须有另一个角色 cross-check** | 通过 Pattern 的覆盖矩阵自证 |
| **2** | **流程完成后必须有 Auditor 检查流程完整性** | 调用方负责确保 Auditor 被执行 |

---

## 五、使用指南

1. **选择 Pattern** — 根据任务场景选择合适的 Pattern（读 `patterns/*.md`）
2. **按 Pattern 流转** — 按 Pattern 定义的角色组合和流转规则，为每个角色 spawn 独立 subagent
3. **注入角色定义** — 每个 subagent 读取对应 `roles/*/ROLE.md`，按职责执行并产出报告
4. **根据 verdict 流转** — pass → 进入下一角色；fail → follow_up 对应角色的 subagent 重做
5. **收尾** — 流程结束前必须执行 Auditor（见下方 ATTENTION）；复杂任务建议执行 Retrospective 沉淀经验

### 调度方注意事项

#### 派发 Auditor 时：必须指定 pattern 文件路径

调度方（主 session / Dispatcher）在 spawn Auditor subagent 时，**必须在 prompt 中指定对应的 pattern 文件路径**，让 Auditor 自己从 pattern 获取标准流程作为审计 baseline。

✅ 正确做法：
```
请按照 role-flow/patterns/dev-pipeline.md 中定义的标准流程进行审计。
```

❌ 错误做法：在 prompt 中自己列出流程步骤（调度方可能遗漏环节，导致 Auditor 以不完整的流程为 baseline）。

> **教训来源**：2026-04-06 Phase 2 审计失效 — 调度方 prompt 中漏掉了 Architect[代码审查] 和 Architect[测试审查]，Auditor 以不完整的流程为 baseline，未能发现环节缺失。详见 `roles/auditor/experience.md`。

---

## 六、扩展指南

### 新增 Role

**约束条件**：
- 职责边界必须明确 — 做什么、不做什么，不能与现有角色职责重叠
- 必须定义规范的输入输出格式
- 必须能被独立 subagent 执行（自包含，不依赖同 session 的其他角色上下文）

**步骤**：
1. 复制 `roles/_TEMPLATE/` 目录到 `roles/<role-name>/`，基于模板填写 `ROLE.md` 和 `experience.md`
2. 参考现有角色文件的格式，写明：职责描述、输入要求、输出格式、行为规范
3. 在本文件的 **角色索引** 中登记
4. 在至少一个 Pattern 中引用该角色（否则角色定义了也没有使用场景）

### 新增 Pattern

**约束条件**：
- 必须遵循 `patterns/_TEMPLATE.md` 的统一结构
- 必须提供 **Cross-Check 覆盖矩阵**，自证满足框架硬约束 1（每个产出都有 cross-check）
- 必须包含 **⚠️ ATTENTION: Auditor 要求** 段，满足框架硬约束 2
- 只能引用已定义的 Role（如需新角色，先按上述步骤新增 Role）

**步骤**：
1. 复制 `patterns/_TEMPLATE.md` 到 `patterns/<pattern-name>.md`
2. 填写各节内容：适用场景、角色组合、流转规则、Cross-Check 覆盖矩阵、验收标准
3. **逐行检查覆盖矩阵** — 确保无遗漏（有豁免必须标注条件和理由）
4. 写明 ⚠️ ATTENTION: Auditor 要求
5. 在本文件的 **模式索引** 中登记

> ⚠️ **角色与 Pattern 的耦合** — 角色的输入输出、各阶段职责与 Pattern 的阶段定义存在关联。新增或修改 Pattern 时，检查涉及角色的定义是否需要同步更新；修改角色定义时，也检查引用该角色的 Pattern 是否受影响。

---

## 七、角色索引

### 已实现（6 个）

| 角色 | 文件 | 一句话描述 |
|------|------|-----------|
| Architect | `roles/architect/ROLE.md` | 方案设计 + 代码审查 + 测试审查（质量把关全归它） |
| Architect Review | `roles/architect-review/ROLE.md` | 开发前评审架构完整性和可行性 |
| Developer | `roles/developer/ROLE.md` | 代码实现 + 单元测试 + 文档三件套 |
| Tester | `roles/tester/ROLE.md` | 执行验收测试（方案模式/自由模式） |
| Auditor | `roles/auditor/ROLE.md` | 流程完整性审计（只查流程，不碰质量） |
| Retrospective | `roles/retrospective/ROLE.md` | 经验沉淀 + 改进建议（面向未来，不阻塞交付） |

### 预留（待扩充）

| 角色 | 文件 | 一句话描述 |
|------|------|-----------|
| Product Designer | `roles/product-designer/ROLE.md` | 🚧 需求分析 + 产品方案设计 |
| Product Review | `roles/product-review/ROLE.md` | 🚧 产品方案评审 |

---

## 八、模式索引（3 个 Pattern + 模板）

| 模式 | 文件 | 适用场景 |
|------|------|---------|
| _TEMPLATE | `patterns/_TEMPLATE.md` | 所有 Pattern 遵循的统一结构模板 |
| Dev Pipeline | `patterns/dev-pipeline.md` | 标准多角色开发流水线 |
| Brain Trust | `patterns/brain-trust.md` | 多视角智囊团评审 |
| Self Check | `patterns/self-check.md` | 低风险场景的单步自检 |

---

## ⚠️ ATTENTION: Auditor 单点风险

**问题**：role-flow 是纯文档框架，没有代码强制力，无法保证调用方一定会执行 Auditor。

**role-flow 的做法**：在 SKILL.md 和每个 Pattern 中写明确的 ATTENTION 提醒。

**底线**：跳过 Auditor 的风险由调用方承担，调用方应自行建立机制确保 Auditor 被执行。role-flow 已尽到提醒义务。
