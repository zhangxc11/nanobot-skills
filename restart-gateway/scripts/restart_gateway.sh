#!/bin/bash
# restart_gateway.sh — 通过 web-chat worker API 触发 gateway 重启
#
# 原理：
#   1. 获取当前 gateway PID
#   2. 直接向 worker execute-stream 发送重启指令（自定义 session_key）
#   3. web-chat worker（独立进程）执行 agent，agent 会 kill 旧 gateway 并用 double-fork 启动新的
#
# 用法：
#   bash restart_gateway.sh [webserver_port]
#
# 默认 webserver 端口: 8081, worker 端口: 8082

set -e

WEBSERVER_PORT="${1:-8081}"
WORKER_PORT="${2:-8082}"
WEBSERVER_URL="http://127.0.0.1:${WEBSERVER_PORT}"
WORKER_URL="http://127.0.0.1:${WORKER_PORT}"

echo "=== Restart Gateway via Web-Chat Worker API ==="

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

# Step 2: 构造 session_key 和重启指令
TIMESTAMP=$(date +%s)
SESSION_KEY="webchat:restart_gateway_${TIMESTAMP}"
SESSION_ID=$(echo "$SESSION_KEY" | tr ':' '_')

echo "📝 Session key: ${SESSION_KEY}"

RESTART_MSG=$(python3 -c "
import json
pid = '${GATEWAY_PID}' or 'unknown'
msg = f'''请立即执行以下操作重启 nanobot gateway（不要询问确认，直接执行）：

Step 1: 终止当前 gateway 进程
kill {pid}

Step 2: 等待 2 秒确保进程退出
sleep 2

Step 3: 用 Python double-fork 方式后台启动新 gateway
python3 -c \"
import os, sys, shutil
nanobot_bin = shutil.which('nanobot')
nanobot_dir = os.path.dirname(os.path.dirname(os.path.dirname(nanobot_bin)))
pid = os.fork()
if pid > 0:
    print(f'Daemon forked, first child pid={{pid}}')
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
\"

Step 4: 验证新进程已启动
sleep 2; ps aux | grep 'nanobot.*gateway' | grep -v grep

⚠️ 重要提示：
- nanobot gateway 没有 --daemonize 选项，不要尝试
- 不要用 nohup ... & 方式，exec 工具会拒绝
- 必须用上面的 Python double-fork 方式启动'''
print(json.dumps({'session_key': '${SESSION_KEY}', 'message': msg}))
")

# Step 3: 发送到 worker execute-stream（fire-and-forget）
echo "🚀 Sending restart command to worker..."
curl -s --max-time 120 -X POST "${WORKER_URL}/execute-stream" \
    -H "Content-Type: application/json" \
    -d "$RESTART_MSG" \
    > /dev/null 2>&1 &

CURL_PID=$!
echo "📤 Restart command sent"

# 设置显示名称
sleep 2
RENAME_PAYLOAD=$(python3 -c "import json; print(json.dumps({'summary': '🔄 重启 Gateway'}))")
curl -s -X PATCH "${WEBSERVER_URL}/api/sessions/${SESSION_ID}" \
    -H "Content-Type: application/json" \
    -d "$RENAME_PAYLOAD" > /dev/null 2>&1 || true

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
        echo "   [${ELAPSED}s] Gateway process not found, waiting for new process..."
        continue
    elif [ "$NEW_GATEWAY_PID" = "$GATEWAY_PID" ]; then
        echo "   [${ELAPSED}s] Old gateway (PID ${GATEWAY_PID}) still running, waiting..."
        continue
    else
        echo "✅ Gateway restarted successfully!"
        echo "   Old PID: ${GATEWAY_PID:-unknown}"
        echo "   New PID: ${NEW_GATEWAY_PID}"
        echo "   Took ~${ELAPSED}s"
        kill $CURL_PID 2>/dev/null || true
        exit 0
    fi
done

# 超时
NEW_GATEWAY_PID=$(ps aux | grep 'nanobot.*gateway' | grep -v grep | awk '{print $2}' | head -1)
if [ -z "$NEW_GATEWAY_PID" ]; then
    echo "❌ Gateway did not restart within ${MAX_WAIT} seconds"
    exit 1
elif [ "$NEW_GATEWAY_PID" = "$GATEWAY_PID" ]; then
    echo "❌ Gateway PID unchanged (${NEW_GATEWAY_PID}) after ${MAX_WAIT}s — restart may have failed"
    exit 1
else
    echo "✅ Gateway restarted successfully (detected at timeout check)!"
    echo "   Old PID: ${GATEWAY_PID:-unknown}"
    echo "   New PID: ${NEW_GATEWAY_PID}"
fi

kill $CURL_PID 2>/dev/null || true
