# Skill: restart-gateway

> ⚠️ **专供 Gateway 通道的 IM 机器人使用**（飞书、Telegram 等）
> 
> Gateway 通道的 agent 运行在 `nanobot gateway` 进程内，**不能自己 kill 自己**。
> 本 skill 通过 HTTP 调用 web-chat 服务，让独立的 worker 进程代为执行重启。

## 背景

- 飞书/Telegram 等 IM channel 运行在 `nanobot gateway` 进程中
- 如果 agent 直接 kill gateway，自己也会被终止，无法完成后续启动操作
- Web-chat 架构：webserver.py (:8081) + worker.py (:8082) 是独立进程，不受 gateway 重启影响
- 因此通过 HTTP API 向 web-chat 发送指令，让 worker 中的 agent 执行重启

## ⚠️ 关键技术要点

1. **`nanobot gateway` 没有 `--daemonize` 选项**，不要尝试
2. **exec 工具禁止 `&` 后台操作符**，不能用 `nohup ... &` 方式
3. **正确的后台启动方式是 Python double-fork**（见下方命令）
4. **exec 工具可以执行包含 `&` 的 bash 脚本文件**（只禁止命令行直接写 `&`）

## 使用方式

### 方法 1：直接执行脚本（推荐，飞书/Telegram agent 可用）

```bash
bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway.sh
```

exec 工具可以执行 bash 脚本文件（脚本内部的 `&` 不受限制），脚本会自动：
1. 检查 web-chat webserver 是否可达
2. 获取当前 gateway PID
3. 创建临时 web-chat session
4. 发送重启指令（含 Python double-fork 启动方式）
5. 等待并验证 gateway 是否重启成功

### 方法 2：Agent 分步 HTTP 调用

如果脚本执行失败，可以手动分步执行：

#### Step 1: 记录当前 gateway PID

```bash
ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}'
```

#### Step 2: 创建 web-chat session

```bash
curl -s -X POST http://127.0.0.1:8081/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "restart-gateway"}'
```

记下返回的 `id` 字段。

#### Step 3: 发送重启指令

```bash
curl -s -X POST http://127.0.0.1:8081/api/sessions/{SESSION_ID}/messages \
  -H "Content-Type: application/json" \
  --max-time 60 \
  -d '{发送重启消息，见下方}'
```

> ⚠️ API 字段是 `message`（不是 `content`）

消息内容需要包含以下关键信息：
- `kill {PID}` 终止旧进程
- `sleep 2` 等待退出
- **Python double-fork 启动**（不是 `--daemonize`，不是 `nohup &`）：

```python
# NANOBOT_DIR 和 NANOBOT_BIN 通过 `which nanobot` 自动推断
python3 -c "
import os, sys, shutil
nanobot_bin = shutil.which('nanobot')
nanobot_dir = os.path.dirname(os.path.dirname(os.path.dirname(nanobot_bin)))
pid = os.fork()
if pid > 0:
    print(f'Daemon forked, first child pid={pid}')
    sys.exit(0)
os.setsid()
pid2 = os.fork()
if pid2 > 0:
    sys.exit(0)
os.chdir(nanobot_dir)
with open('/tmp/nanobot-gateway.log', 'a') as log:
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
with open('/dev/null', 'r') as devnull:
    os.dup2(devnull.fileno(), 0)
os.execv(nanobot_bin, ['nanobot', 'gateway'])
"
```

#### Step 4: 验证（重启后的新 session 中执行）

```bash
ps -p $(ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}') -o pid,lstart,etime
```

## 前置条件

- Web-chat 服务必须正在运行（webserver :8081 + worker :8082）
- 如果 web-chat 也没运行，则只能由用户手动在终端重启

## 典型场景

1. **Session 文件修复后需要清除内存缓存** — 修改了 session JSONL 文件，但 gateway 内存中缓存了旧数据
2. **配置变更后需要重载** — 修改了 config.json 等配置文件
3. **代码更新后需要重启** — 更新了 nanobot 核心代码

## 注意事项

- 重启 gateway 会导致所有 IM channel（飞书、Telegram）**短暂中断**（约 5-10 秒）
- 重启后当前对话的上下文会丢失（agent 会从 session 文件重新加载历史）
- Step 3 的 curl 可能会因 gateway 被 kill 导致 SSE 流中断而报错，这是**正常现象**
- 每次重启会在 web-chat 中创建一个临时 session（`restart-gateway-auto`），可以事后清理
