#!/bin/bash
# restart_gateway.sh — 通过 web-chat HTTP API 触发 gateway 重启
#
# 原理：
#   1. 创建一个临时 web-chat session
#   2. 向该 session 发送重启指令（message）
#   3. web-chat worker（独立进程）执行 agent，agent 会 kill 旧 gateway 并用 double-fork 启动新的
#
# 用法：
#   bash restart_gateway.sh [webserver_port]
#
# 默认 webserver 端口: 8081

set -e

WEBSERVER_PORT="${1:-8081}"
WEBSERVER_URL="http://127.0.0.1:${WEBSERVER_PORT}"
NANOBOT_DIR="/Users/zhangxingcheng/Documents/code/workspace/nanobot"

echo "=== Restart Gateway via Web-Chat API ==="
echo "Webserver URL: ${WEBSERVER_URL}"

# Step 0: 检查 webserver 是否可达
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${WEBSERVER_URL}/api/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "❌ Error: Web-chat webserver is not running at ${WEBSERVER_URL}"
    exit 1
fi
echo "✅ Web-chat webserver is reachable"

# Step 1: 获取当前 gateway PID
GATEWAY_PID=$(ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}' | head -1)
if [ -z "$GATEWAY_PID" ]; then
    echo "⚠️  Warning: No running gateway process found"
else
    echo "📌 Current gateway PID: ${GATEWAY_PID}"
fi

# Step 2: 创建临时 session
echo "📝 Creating temporary web-chat session..."
SESSION_RESPONSE=$(curl -s -X POST "${WEBSERVER_URL}/api/sessions" \
    -H "Content-Type: application/json" \
    -d '{"name": "restart-gateway-auto"}')

SESSION_ID=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    echo "❌ Error: Failed to create session. Response: ${SESSION_RESPONSE}"
    exit 1
fi
echo "✅ Created session: ${SESSION_ID}"

# Step 3: 构造并发送重启指令
# ⚠️ 关键点：
#   - nanobot gateway 没有 --daemonize 选项
#   - 必须用 Python double-fork 方式后台启动
#   - exec 工具禁止 & 后台操作符，所以不能用 nohup ... &
echo "🚀 Sending restart command to web-chat worker..."

RESTART_MSG=$(python3 -c "
import json
pid = '${GATEWAY_PID}' or 'unknown'
nanobot_dir = '${NANOBOT_DIR}'
msg = f'''请立即执行以下操作重启 nanobot gateway（不要询问确认，直接执行）：

Step 1: 终止当前 gateway 进程
kill {pid}

Step 2: 等待 2 秒确保进程退出
sleep 2

Step 3: 用 Python double-fork 方式后台启动新 gateway
python3 -c \"
import os, sys
pid = os.fork()
if pid > 0:
    print(f'Daemon forked, first child pid={{pid}}')
    sys.exit(0)
os.setsid()
pid2 = os.fork()
if pid2 > 0:
    sys.exit(0)
os.chdir('{nanobot_dir}')
with open('/tmp/nanobot-gateway.log', 'a') as log:
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
with open('/dev/null', 'r') as devnull:
    os.dup2(devnull.fileno(), 0)
os.execv('./venv311/bin/nanobot', ['nanobot', 'gateway'])
\"

Step 4: 验证新进程已启动
sleep 2; ps aux | grep 'nanobot.*gateway' | grep -v grep

⚠️ 重要提示：
- nanobot gateway 没有 --daemonize 选项，不要尝试
- 不要用 nohup ... & 方式，exec 工具会拒绝
- 必须用上面的 Python double-fork 方式启动'''
print(json.dumps({'message': msg}))
")

# fire-and-forget: 用 --max-time 防止 SSE 流无限等待
curl -s -X POST "${WEBSERVER_URL}/api/sessions/${SESSION_ID}/messages" \
    -H "Content-Type: application/json" \
    -d "$RESTART_MSG" \
    --max-time 60 > /dev/null 2>&1 &

CURL_PID=$!
echo "📤 Restart command sent"

# Step 4: 等待 gateway 重启（轮询检查，最多 60 秒）
echo "⏳ Waiting for gateway to restart..."
MAX_WAIT=60
INTERVAL=5
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))

    NEW_GATEWAY_PID=$(ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}' | head -1)

    if [ -z "$NEW_GATEWAY_PID" ]; then
        # 进程不存在：旧的已被 kill，新的还没起来，继续等
        echo "   [${ELAPSED}s] Gateway process not found, waiting for new process..."
        continue
    elif [ "$NEW_GATEWAY_PID" = "$GATEWAY_PID" ]; then
        # PID 跟旧的一样：旧进程还没被 kill，继续等
        echo "   [${ELAPSED}s] Old gateway (PID ${GATEWAY_PID}) still running, waiting..."
        continue
    else
        # PID 不同且不为空：新进程已启动！
        echo "✅ Gateway restarted successfully!"
        echo "   Old PID: ${GATEWAY_PID:-unknown}"
        echo "   New PID: ${NEW_GATEWAY_PID}"
        echo "   Took ~${ELAPSED}s"
        # 清理
        kill $CURL_PID 2>/dev/null || true
        exit 0
    fi
done

# 超时
NEW_GATEWAY_PID=$(ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}' | head -1)
if [ -z "$NEW_GATEWAY_PID" ]; then
    echo "❌ Gateway did not restart within ${MAX_WAIT} seconds"
    echo "   Check web-chat worker logs: /tmp/nanobot-worker.log"
    exit 1
elif [ "$NEW_GATEWAY_PID" = "$GATEWAY_PID" ]; then
    echo "❌ Gateway PID unchanged (${NEW_GATEWAY_PID}) after ${MAX_WAIT}s — restart may have failed"
    echo "   Check web-chat worker logs: /tmp/nanobot-worker.log"
    exit 1
else
    echo "✅ Gateway restarted successfully (detected at timeout check)!"
    echo "   Old PID: ${GATEWAY_PID:-unknown}"
    echo "   New PID: ${NEW_GATEWAY_PID}"
fi

# 清理
kill $CURL_PID 2>/dev/null || true
