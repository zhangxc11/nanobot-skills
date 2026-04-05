# nanobot 开发专用约束

以下约束仅在开发 nanobot 相关仓库时适用。

## Dev 环境（核心仓库）

- Dev 任务用 curl 调 dev web-chat API (9081/9082)，fire-and-forget 不用 `--wait`
- spawn subagent 跑的是 prod 环境
- dev-workdir 只能 checkout 切换分支不能 merge
- Claude Code 默认在 prod 目录提交，dev-workdir 需 format-patch + apply 同步
- 启动 dev gateway 前必须先停 prod gateway
- 验证改动时不要搞坏 dev 运行环境

## Dev 环境（Skill 类仓库）

Skill 类仓库（`_nanobot-skills`、`feishu-docs`、`feishu-parser` 等）的 dev/prod 隔离方式与核心仓库不同，利用 nanobot 的 skill 名称机制实现。

### 实施步骤

1. **开发**：在 `dev-workdir/` 下 clone skill 仓库，切 feature branch 开发
   ```bash
   cd ~/.nanobot/workspace/dev-workdir
   git clone git@github.com:zhangxc11/nanobot-skills.git _nanobot-skills
   cd _nanobot-skills && git checkout -b feat/xxx
   ```

2. **创建 Dev Symlink**：在 `skills/` 目录下创建 `xxx-dev` 的 symlink，指向 dev-workdir 中对应的 skill 目录
   ```bash
   cd ~/.nanobot/workspace/skills
   ln -s ../dev-workdir/_nanobot-skills/digital-assistant digital-assistant-dev
   ```
   nanobot 会自动识别为名为 `digital-assistant-dev` 的独立 skill，与 prod 的 `digital-assistant` 互不干扰。

3. **测试**：使用 prod 环境（8081/8082）验证 dev skill，不需要单独启动 dev 进程
   - 直接复用 prod 的 web-chat 服务，通过 dev skill 的 `trigger_scheduler.py` 触发调度：
     ```bash
     cd ~/.nanobot/workspace
     python3 skills/digital-assistant-dev/scripts/trigger_scheduler.py
     ```
     dev skill 的 `trigger_scheduler.py` 和 `scheduler.py` 通过 `Path(__file__).resolve()` 自动定位到 dev-workdir 下的代码，与 prod 的 `digital-assistant` 互不干扰
   - 观察完整链路：dispatcher 接收 → spawn worker → 各角色流转 → done
   - 验证 dev skill 代码的行为符合预期后，再进入步骤 4 部署

4. **部署**：验证通过后，merge feature branch 到 skill 仓库的 main 分支，prod symlink（指向 `_nanobot-skills/` 的 main）自动生效，然后删除 `-dev` symlink
   ```bash
   rm ~/.nanobot/workspace/skills/digital-assistant-dev
   ```

### 独立仓库的 Skill（对比）

独立仓库的 skill（如 `feishu-docs`、`feishu-parser`）直接 clone 到 `skills/` 目录。dev 隔离方式类似：
- 在 `dev-workdir/` clone 仓库，切 feature branch
- 创建 `feishu-docs-dev -> ../dev-workdir/nanobot-feishu-docs` 的 symlink
- 验证通过后 merge，删除 `-dev` link

### 关键约束

- **M-008**：必须在 git 仓库内开发，禁止裸文件 cp 或在非 git 目录编辑
- **Dev/Prod 隔离**：dev 和 prod 必须使用不同的 symlink/目录，不能共用
- **先验后 merge**：验证通过后才能 merge 到 main 部署 prod
- **命名约定**：dev symlink 统一用 `{skill-name}-dev` 后缀
