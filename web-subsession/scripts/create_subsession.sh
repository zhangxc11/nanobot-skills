#!/bin/bash
# create_subsession.sh — 通过 web-chat HTTP API 创建子 session 并发送任务
#
# 用法：
#   bash create_subsession.sh --name "task-name" --message "任务指令"
#   bash create_subsession.sh --name "task-name" --message "任务指令" --wait 60
#
# 参数：
#   --name NAME          Session 名称（默认 subsession-auto）
#   --message MSG        发送给子 session 的消息（必填）
#   --port PORT          Webserver 端口（默认 8081）
#   --wait SECONDS       等待完成的超时秒数（默认 0 = fire-and-forget）
#   --poll-interval SECS 轮询间隔秒数（默认 5，仅 --wait > 0 时有效）

set -e

# === 参数解析 ===
SESSION_NAME="subsession-auto"
MESSAGE=""
WEBSERVER_PORT=8081
WAIT_TIMEOUT=0
POLL_INTERVAL=5

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)
            SESSION_NAME="$2"
            shift 2
            ;;
        --message)
            MESSAGE="$2"
            shift 2
            ;;
        --port)
            WEBSERVER_PORT="$2"
            shift 2
            ;;
        --wait)
            WAIT_TIMEOUT="$2"
            shift 2
            ;;
        --poll-interval)
            POLL_INTERVAL="$2"
            shift 2
            ;;
        *)
            echo "❌ Unknown parameter: $1"
            echo "Usage: bash $0 --name NAME --message MSG [--port PORT] [--wait SECONDS]"
            exit 1
            ;;
    esac
done

if [ -z "$MESSAGE" ]; then
    echo "❌ Error: --message is required"
    echo "Usage: bash $0 --name NAME --message MSG [--port PORT] [--wait SECONDS]"
    exit 1
fi

WEBSERVER_URL="http://127.0.0.1:${WEBSERVER_PORT}"

echo "=== Create Web Sub-Session ==="
echo "Session Name: ${SESSION_NAME}"
echo "Webserver URL: ${WEBSERVER_URL}"
echo "Wait Timeout: ${WAIT_TIMEOUT}s"

# === Step 1: 检查 webserver 是否可达 ===
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${WEBSERVER_URL}/api/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "❌ Error: Web-chat webserver is not running at ${WEBSERVER_URL}"
    exit 1
fi
echo "✅ Web-chat webserver is reachable"

# === Step 2: 创建 session ===
echo "📝 Creating sub-session..."
SESSION_RESPONSE=$(curl -s -X POST "${WEBSERVER_URL}/api/sessions" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "import json; print(json.dumps({'name': '${SESSION_NAME}'}))")")

SESSION_ID=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
    echo "❌ Error: Failed to create session. Response: ${SESSION_RESPONSE}"
    exit 1
fi
echo "✅ Created session: ${SESSION_ID} (name: ${SESSION_NAME})"

# === Step 3: 发送消息 ===
echo "📤 Sending message to sub-session..."

# 构造 JSON payload（使用 python3 确保正确转义）
PAYLOAD=$(python3 -c "
import json, sys
msg = sys.stdin.read()
print(json.dumps({'message': msg}))
" <<< "$MESSAGE")

# 发送消息（后台执行，使用 max-time 防止无限等待）
MAX_TIME=$((WAIT_TIMEOUT > 0 ? WAIT_TIMEOUT + 30 : 120))
curl -s -X POST "${WEBSERVER_URL}/api/sessions/${SESSION_ID}/messages" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    --max-time "$MAX_TIME" > /dev/null 2>&1 &

CURL_PID=$!
echo "✅ Message sent (curl PID: ${CURL_PID})"

# === Step 4: 等待完成（可选） ===
if [ "$WAIT_TIMEOUT" -le 0 ]; then
    echo ""
    echo "🔥 Fire-and-forget mode — sub-session is running in background"
    echo "   Session ID: ${SESSION_ID}"
    echo "   Check status: curl -s ${WEBSERVER_URL}/api/sessions/${SESSION_ID}/messages"
    exit 0
fi

echo "⏳ Waiting for sub-session to complete (timeout: ${WAIT_TIMEOUT}s)..."
ELAPSED=0
while [ $ELAPSED -lt $WAIT_TIMEOUT ]; do
    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    # 检查 curl 是否还在运行（SSE 流结束 = agent 完成）
    if ! kill -0 $CURL_PID 2>/dev/null; then
        echo "✅ Sub-session completed (took ~${ELAPSED}s)"
        echo "   Session ID: ${SESSION_ID}"
        exit 0
    fi

    echo "   [${ELAPSED}s] Still running..."
done

# 超时
echo "⚠️  Timeout after ${WAIT_TIMEOUT}s — sub-session may still be running"
echo "   Session ID: ${SESSION_ID}"
echo "   Check status: curl -s ${WEBSERVER_URL}/api/sessions/${SESSION_ID}/messages"
kill $CURL_PID 2>/dev/null || true
exit 0
