---
name: dev-workflow
description: 软件开发工作流规范。所有代码项目（新建或维护）必须遵循此流程：文档先行（需求/架构/DEVLOG）、任务拆解、逐步开发、测试验证、Git 版本管理、分支策略。当用户要求开发新功能、修复 Bug、改进代码、创建新项目时使用。
---

# 开发工作流规范

所有代码项目统一遵循此流程，无需用户每次强调。

## 项目文档结构

### 简版（单文件模式）
适用于新项目或文档体量较小（每个文件 < 500 行）的项目：
```
project/
├── docs/
│   ├── REQUIREMENTS.md   # 需求文档（完整正文）
│   ├── ARCHITECTURE.md   # 架构设计（完整正文）
│   └── DEVLOG.md         # 开发日志（完整正文）
├── tests/
└── ...
```

### 复杂版（主文件 + 子目录模式）
适用于文档体量大（任一文件 > 800 行或 > 30KB）的成熟项目：
```
project/
├── docs/
│   ├── REQUIREMENTS.md        # 主文件：索引 + Backlog
│   ├── requirements/          # 按编号分组的需求归档
│   │   ├── s01-s10.md
│   │   └── ...
│   ├── ARCHITECTURE.md        # 主文件：架构总览 + 模块索引
│   ├── architecture/          # 按功能模块分组的架构设计
│   │   ├── core.md
│   │   └── ...
│   ├── DEVLOG.md              # 主文件：状态总览 + 索引 + 最近3个Phase
│   └── devlog/                # 按Phase编号分组的开发日志归档
│       ├── phase-01-10.md
│       └── ...
├── tests/
└── ...
```

详见 [references/doc-templates.md](references/doc-templates.md)（简版模板）和 [references/doc-split-guide.md](references/doc-split-guide.md)（升级指南）。

## 核心流程

### 新功能开发

```
1. 记录需求 → REQUIREMENTS.md 新增章节
2. 设计架构 → ARCHITECTURE.md 更新（含测试设计）
3. 拆解任务 → DEVLOG.md 写入任务清单（checkbox）
4. 开分支   → 复杂功能: git checkout -b feat/xxx；简单改动: 直接 main/local
5. 逐步实现 → 每完成一个子任务，勾选 checkbox
6. 测试验证 → 运行测试，确保全部通过（含回归）
7. Git 提交 → commit message 格式: feat/fix/docs/refactor: 描述
8. 合并分支 → git checkout main && git merge feat/xxx（如有分支）
9. 更新文档 → DEVLOG 记录结果，MEMORY.md 更新项目状态
10. 部署     → 按需重启服务
```

### Bug 修复 / 功能改进

```
1. 记录问题 → REQUIREMENTS.md 追加 Issue（带编号）或更新对应章节
2. 按需更新 → ARCHITECTURE.md（如涉及设计变更）
3. 拆解+实现 → DEVLOG.md 记录，逐步修复
4. 测试+提交 → 同上流程 6-10
```

## 分支策略

| 场景 | 策略 |
|------|------|
| 简单改动（1-2 文件） | 直接在 main/local 分支 |
| 独立功能模块 | `feat/功能名` 分支，完成后合并 |
| Bug 修复 | 视复杂度选择，通常直接 main |

## 开发纪律

1. **先读后改** — 修改文件前必须先 `read_file` 确认当前内容
2. **先测后提交** — 测试通过才能 git commit
3. **文档同步** — 代码改动必须同步更新相关文档
4. **小步快跑** — 每个子任务独立可验证，避免大爆炸式提交
5. **DEVLOG 是真相源** — 新 session 从 DEVLOG 恢复上下文，必须保持最新

## Session 恢复

每次新 session 开始开发时：

### 简版项目
1. 读 `docs/DEVLOG.md` — 找到 🔜 标记的待办任务
2. 读 `docs/REQUIREMENTS.md` — 理解当前需求
3. 按需读 `docs/ARCHITECTURE.md` — 理解技术设计
4. 继续执行未完成的任务

### 复杂版项目
1. 读 `docs/DEVLOG.md` 主文件 — 状态总览 + 最近 Phase 正文中找 🔜
2. 按需读 `docs/REQUIREMENTS.md` 主文件 — 索引表定位相关需求的归档文件
3. 按需读对应的归档子文件 — 获取完整需求/架构详情
4. 继续执行未完成的任务

> 💡 复杂版的主文件已经足够恢复上下文（~200行），只在需要深入某个具体需求/设计时才读子文件。
