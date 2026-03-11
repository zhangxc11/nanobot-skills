#!/bin/bash
# create_subsession.sh — 通过 web-chat HTTP API 创建子 session 并发送任务
#
# 两种路径：
#   路径 A（指定 --session-key）：直接调用 worker execute-stream，自定义 session_key
#   路径 B（不指定 --session-key）：通过 webserver 创建 session，自动生成 session_key
#
# 用法：
#   # 路径 A：自定义 session_key
#   bash create_subsession.sh --session-key "webchat:worker_xxx_B8" --message "任务指令" --title "🔨 构造 B8"
#
#   # 路径 B：自动生成
#   bash create_subsession.sh --message "任务指令" --wait 60
#
# 参数：
#   --session-key KEY    自定义 session_key（使用路径 A）。不指定则用路径 B
#   --message MSG        发送给子 session 的消息（必填）
#   --title TITLE        显示名称（仅路径 A，通过 rename API 设置）
#   --port PORT          Webserver 端口（默认 8081）
#   --worker-port PORT   Worker 端口（默认 8082，仅路径 A）
#   --wait SECONDS       等待完成的超时秒数（默认 0 = fire-and-forget）
#   --poll-interval SECS 轮询间隔秒数（默认 5，仅 --wait > 0 时有效）
#   --parent SESSION_ID  父 session ID（可选，注册父子关系到 session_parents.json）

set -e

# === 参数解析 ===
SESSION_KEY=""
MESSAGE=""
TITLE=""
PARENT=""
WEBSERVER_PORT=8081
WORKER_PORT=8082
WAIT_TIMEOUT=0
POLL_INTERVAL=5

while [[ $# -gt 0 ]]; do
    case "$1" in
        --session-key)
            SESSION_KEY="$2"
            shift 2
            ;;
        --message)
            MESSAGE="$2"
            shift 2
            ;;
        --title)
            TITLE="$2"
            shift 2
            ;;
        --parent)
            PARENT="$2"
            shift 2
            ;;
        --port)
            WEBSERVER_PORT="$2"
            shift 2
            ;;
        --worker-port)
            WORKER_PORT="$2"
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
            echo "Usage: bash $0 [--session-key KEY] --message MSG [--title TITLE] [--port PORT] [--worker-port PORT] [--wait SECONDS]"
            exit 1
            ;;
    esac
done

if [ -z "$MESSAGE" ]; then
    echo "❌ Error: --message is required"
    echo "Usage: bash $0 [--session-key KEY] --message MSG [--title TITLE] [--wait SECONDS]"
    exit 1
fi

WEBSERVER_URL="http://127.0.0.1:${WEBSERVER_PORT}"
WORKER_URL="http://127.0.0.1:${WORKER_PORT}"

# === 检查服务 ===
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${WEBSERVER_URL}/api/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
    echo "❌ Error: Web-chat webserver is not running at ${WEBSERVER_URL}"
    exit 1
fi

# === 路径分支 ===
if [ -n "$SESSION_KEY" ]; then
    # ========== 路径 A：直接调用 Worker API ==========
    SESSION_ID=$(echo "$SESSION_KEY" | tr ':' '_')

    echo "=== Create Sub-Session (路径 A: Worker API) ==="
    echo "Session Key: ${SESSION_KEY}"
    echo "Session ID:  ${SESSION_ID}"
    echo "Worker URL:  ${WORKER_URL}"
    echo "Wait:        ${WAIT_TIMEOUT}s"

    # 构造 JSON payload
    PAYLOAD=$(python3 -c "
import json, sys
msg = sys.stdin.read()
print(json.dumps({'session_key': '${SESSION_KEY}', 'message': msg}))
" <<< "$MESSAGE")

    # 发送到 worker execute-stream
    MAX_TIME=$((WAIT_TIMEOUT > 0 ? WAIT_TIMEOUT + 30 : 120))
    curl -s --max-time "$MAX_TIME" -X POST "${WORKER_URL}/execute-stream" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        > /dev/null 2>&1 &
    CURL_PID=$!
    echo "✅ Task sent to worker (curl PID: ${CURL_PID})"

    # 设置显示名称（如果指定了 --title）
    if [ -n "$TITLE" ]; then
        sleep 2  # 等 session 文件创建
        RENAME_PAYLOAD=$(python3 -c "import json; print(json.dumps({'summary': '${TITLE}'}))")
        curl -s -X PATCH "${WEBSERVER_URL}/api/sessions/${SESSION_ID}" \
            -H "Content-Type: application/json" \
            -d "$RENAME_PAYLOAD" > /dev/null 2>&1 || true
        echo "📝 Title set: ${TITLE}"
    fi

    # 注册父子关系（如果指定了 --parent）
    if [ -n "$PARENT" ]; then
        sleep 1  # 等 session 文件创建
        PARENT_PAYLOAD=$(python3 -c "import json; print(json.dumps({'child': '${SESSION_ID}', 'parent': '${PARENT}'}))")
        curl -s -X POST "${WEBSERVER_URL}/api/sessions/parents" \
            -H "Content-Type: application/json" \
            -d "$PARENT_PAYLOAD" > /dev/null 2>&1 || true
        echo "🔗 Parent registered: ${SESSION_ID} → ${PARENT}"
    fi

else
    # ========== 路径 B：通过 Webserver API ==========
    echo "=== Create Sub-Session (路径 B: Webserver API) ==="
    echo "Webserver URL: ${WEBSERVER_URL}"
    echo "Wait:          ${WAIT_TIMEOUT}s"

    # 创建 session
    echo "📝 Creating session..."
    SESSION_RESPONSE=$(curl -s -X POST "${WEBSERVER_URL}/api/sessions" \
        -H "Content-Type: application/json" \
        -d '{}')

    SESSION_ID=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
    if [ -z "$SESSION_ID" ]; then
        echo "❌ Error: Failed to create session. Response: ${SESSION_RESPONSE}"
        exit 1
    fi
    SESSION_KEY=$(echo "$SESSION_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sessionKey',''))" 2>/dev/null)
    echo "✅ Created session: ${SESSION_ID} (key: ${SESSION_KEY})"

    # 发送消息
    PAYLOAD=$(python3 -c "
import json, sys
msg = sys.stdin.read()
print(json.dumps({'message': msg}))
" <<< "$MESSAGE")

    MAX_TIME=$((WAIT_TIMEOUT > 0 ? WAIT_TIMEOUT + 30 : 120))
    curl -s -X POST "${WEBSERVER_URL}/api/sessions/${SESSION_ID}/messages" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        --max-time "$MAX_TIME" > /dev/null 2>&1 &
    CURL_PID=$!
    echo "✅ Message sent (curl PID: ${CURL_PID})"

    # 注册父子关系（如果指定了 --parent）
    if [ -n "$PARENT" ]; then
        PARENT_PAYLOAD=$(python3 -c "import json; print(json.dumps({'child': '${SESSION_ID}', 'parent': '${PARENT}'}))")
        curl -s -X POST "${WEBSERVER_URL}/api/sessions/parents" \
            -H "Content-Type: application/json" \
            -d "$PARENT_PAYLOAD" > /dev/null 2>&1 || true
        echo "🔗 Parent registered: ${SESSION_ID} → ${PARENT}"
    fi
fi

# === 等待完成（可选） ===
if [ "$WAIT_TIMEOUT" -le 0 ]; then
    echo ""
    echo "🔥 Fire-and-forget — sub-session running in background"
    echo "   Session ID:  ${SESSION_ID}"
    echo "   Session Key: ${SESSION_KEY}"
    exit 0
fi

echo "⏳ Waiting for completion (timeout: ${WAIT_TIMEOUT}s)..."
ELAPSED=0
while [ $ELAPSED -lt $WAIT_TIMEOUT ]; do
    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    if ! kill -0 $CURL_PID 2>/dev/null; then
        echo "✅ Sub-session completed (took ~${ELAPSED}s)"
        echo "   Session ID: ${SESSION_ID}"
        exit 0
    fi
    echo "   [${ELAPSED}s] Still running..."
done

echo "⚠️  Timeout after ${WAIT_TIMEOUT}s — sub-session may still be running"
echo "   Session ID: ${SESSION_ID}"
kill $CURL_PID 2>/dev/null || true
exit 0
