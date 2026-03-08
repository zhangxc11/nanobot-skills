#!/bin/bash
# restart_gateway.sh — 通过 web-chat worker API 间接重启 gateway
#
# 原理：
#   1. 获取当前 gateway PID
#   2. 向 worker execute-stream 发送重启指令
#   3. web-chat worker（独立进程）中的 agent 执行 kill + double-fork 启动新 gateway
#
# 用法：
#   bash restart_gateway.sh [webserver_port] [worker_port]
#
# ⚠️ 适用于飞书/Telegram channel（agent 运行在 gateway 进程内，不能直接 kill 自己）
# ⚠️ cli/web channel 请直接用 restart_gateway_direct.sh

set -e

WEBSERVER_PORT="${1:-8081}"
WORKER_PORT="${2:-8082}"
WEBSERVER_URL="http://127.0.0.1:${WEBSERVER_PORT}"
WORKER_URL="http://127.0.0.1:${WORKER_PORT}"
NANOBOT_BIN=$(which nanobot 2>/dev/null || echo "/Users/zhangxingcheng/Documents/code/workspace/nanobot/venv311/bin/nanobot")
GATEWAY_LOG="/tmp/nanobot-gateway.log"

echo "=== Restart Gateway via Web-Chat Worker API ==="

# --- Robust gateway process discovery ---
find_gateway_pids() {
    local pids=""
    pids=$(pgrep -f "nanobot gateway" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        local filtered=""
        for p in $pids; do
            local cmd
            cmd=$(ps -o command= -p "$p" 2>/dev/null || true)
            if echo "$cmd" | grep -qE "[Pp]ython.*nanobot.*gateway"; then
                filtered="${filtered}${filtered:+ }${p}"
            fi
        done
        pids="$filtered"
    fi
    echo "$pids"
}

# Step 0: 检查 web-chat 是否可达
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${WEBSERVER_URL}/api/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "❌ Error: Web-chat webserver is not running at ${WEBSERVER_URL}"
    exit 1
fi
echo "✅ Web-chat webserver is reachable"

# Step 1: 获取当前 gateway PID
GATEWAY_PID=$(find_gateway_pids | head -1)
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

# Build the restart message — instruct the agent to use the direct script
RESTART_MSG=$(python3 -c "
import json
msg = '''请立即执行以下操作重启 nanobot gateway（不要询问确认，直接执行）：

bash ~/.nanobot/workspace/skills/restart-gateway/scripts/restart_gateway_direct.sh restart

执行完毕后报告结果。'''
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

    NEW_PID=$(find_gateway_pids | head -1)

    if [ -z "$NEW_PID" ]; then
        echo "   [${ELAPSED}s] Gateway process not found, waiting..."
        continue
    elif [ "$NEW_PID" = "$GATEWAY_PID" ]; then
        echo "   [${ELAPSED}s] Old gateway (PID ${GATEWAY_PID}) still running, waiting..."
        continue
    else
        echo "✅ Gateway restarted successfully!"
        echo "   Old PID: ${GATEWAY_PID:-unknown}"
        echo "   New PID: ${NEW_PID}"
        echo "   Took ~${ELAPSED}s"
        kill $CURL_PID 2>/dev/null || true
        exit 0
    fi
done

# 超时
NEW_PID=$(find_gateway_pids | head -1)
if [ -z "$NEW_PID" ]; then
    echo "❌ Gateway did not restart within ${MAX_WAIT}s"
    exit 1
elif [ "$NEW_PID" = "$GATEWAY_PID" ]; then
    echo "❌ Gateway PID unchanged (${NEW_PID}) after ${MAX_WAIT}s — restart may have failed"
    exit 1
else
    echo "✅ Gateway restarted (detected at timeout check)!"
    echo "   Old PID: ${GATEWAY_PID:-unknown}"
    echo "   New PID: ${NEW_PID}"
fi

kill $CURL_PID 2>/dev/null || true
