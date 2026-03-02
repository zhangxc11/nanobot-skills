# nanobot-skills

nanobot 辅助 skill 集合，提供开发工作流规范和服务管理能力。

## 包含的 Skills

| Skill | 说明 |
|-------|------|
| **dev-workflow** | 软件开发工作流规范（文档先行、任务拆解、Git 管理） |
| **restart-gateway** | nanobot gateway 重启（通过 web-chat worker 代理执行） |
| **restart-webchat** | nanobot web-chat 服务重启 |

## 安装方法

### 方法 1: 克隆仓库 + 软链接（推荐）

```bash
cd ~/.nanobot/workspace/skills

# 克隆整个仓库
git clone git@github.com:zhangxc11/nanobot-skills.git _nanobot-skills

# 创建软链接
ln -s _nanobot-skills/dev-workflow dev-workflow
ln -s _nanobot-skills/restart-gateway restart-gateway
ln -s _nanobot-skills/restart-webchat restart-webchat
```

### 方法 2: 直接复制需要的 skill

```bash
cd ~/.nanobot/workspace/skills
git clone git@github.com:zhangxc11/nanobot-skills.git _nanobot-skills

# 只复制需要的 skill
cp -r _nanobot-skills/dev-workflow .
cp -r _nanobot-skills/restart-gateway .
```

## 相关仓库

其他 nanobot skill 仓库的安装方法：

```bash
cd ~/.nanobot/workspace/skills
git clone git@github.com:zhangxc11/nanobot-feishu-docs.git feishu-docs
git clone git@github.com:zhangxc11/nanobot-feishu-messenger.git feishu-messenger
git clone git@github.com:zhangxc11/nanobot-feishu-parser.git feishu-parser
```
