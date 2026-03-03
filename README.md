# nanobot-skills

nanobot 辅助 skill 集合，提供开发工作流规范和服务管理能力。

## 包含的 Skills

| Skill | 说明 |
|-------|------|
| **dev-workflow** | 软件开发工作流规范（文档先行、任务拆解、Git 管理） |
| **restart-gateway** | nanobot gateway 重启（通过 web-chat worker 代理执行） |
| **restart-webchat** | nanobot web-chat 服务重启 |

## 安装方法

将仓库克隆到 `~/.nanobot/workspace/` 根目录（**不是** skills 目录内），然后在 skills 目录创建软链接。
这样可以避免仓库本身被 nanobot 误识别为一个 skill。

```bash
# 克隆到 workspace 根目录
cd ~/.nanobot/workspace
git clone git@github.com:zhangxc11/nanobot-skills.git _nanobot-skills

# 在 skills 目录创建软链接
cd skills
ln -s ../_nanobot-skills/dev-workflow dev-workflow
ln -s ../_nanobot-skills/restart-gateway restart-gateway
ln -s ../_nanobot-skills/restart-webchat restart-webchat
```

### 更新

```bash
cd ~/.nanobot/workspace/_nanobot-skills && git pull
```

## 目录结构

```
~/.nanobot/workspace/
├── _nanobot-skills/           # 本仓库（克隆到此处）
│   ├── dev-workflow/
│   ├── restart-gateway/
│   └── restart-webchat/
└── skills/
    ├── dev-workflow/          # → ../_nanobot-skills/dev-workflow
    ├── restart-gateway/       # → ../_nanobot-skills/restart-gateway
    └── restart-webchat/       # → ../_nanobot-skills/restart-webchat
```

## 相关仓库

其他 nanobot skill 仓库（直接克隆到 skills 目录）：

```bash
cd ~/.nanobot/workspace/skills
git clone git@github.com:zhangxc11/nanobot-feishu-docs.git feishu-docs
git clone git@github.com:zhangxc11/nanobot-feishu-messenger.git feishu-messenger
git clone git@github.com:zhangxc11/nanobot-feishu-parser.git feishu-parser
```
