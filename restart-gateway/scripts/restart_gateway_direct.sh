#!/bin/bash
# restart_gateway_direct.sh — 直接重启 nanobot gateway（适用于 cli / web channel）
#
# Usage:
#   ./restart_gateway_direct.sh              # restart gateway
#   ./restart_gateway_direct.sh stop         # stop gateway only
#   ./restart_gateway_direct.sh status       # show status
#
# ⚠️ 仅限 cli / web channel 使用。飞书/Telegram 请用 restart_gateway.sh（间接方式）。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_LOG_DIR="${HOME}/.nanobot/logs"
mkdir -p "${GATEWAY_LOG_DIR}"
GATEWAY_LOG="${GATEWAY_LOG_DIR}/gateway.log"
NEW_PROCESS_MAX_AGE=15

# --- Auto-detect nanobot binary ---
if [ -z "$NANOBOT_BIN" ]; then
    NANOBOT_BIN=$(which nanobot 2>/dev/null || true)
fi
if [ -z "$NANOBOT_BIN" ]; then
    echo "❌ Error: nanobot not found in PATH"
    exit 1
fi

# --- Process discovery (robust) ---
find_gateway_pids() {
    local pids=""
    # Strategy 1: match "nanobot gateway" in command line
    pids=$(pgrep -f "nanobot gateway" 2>/dev/null || true)
    # Filter out grep/editor/script processes that might match
    if [ -n "$pids" ]; then
        local filtered=""
        for p in $pids; do
            local cmd
            cmd=$(ps -o command= -p "$p" 2>/dev/null || true)
            # Only keep actual Python processes running nanobot gateway
            if echo "$cmd" | grep -qE "[Pp]ython.*nanobot.*gateway"; then
                filtered="${filtered}${filtered:+ }${p}"
            fi
        done
        pids="$filtered"
    fi
    echo "$pids"
}

# Get process elapsed time in seconds (macOS compatible)
get_process_age_seconds() {
    local pid="$1"
    local etime
    etime=$(ps -o etime= -p "$pid" 2>/dev/null | xargs) || return 1
    [ -z "$etime" ] && return 1

    local days=0 hours=0 minutes=0 seconds=0
    etime="${etime// /}"
    if [[ "$etime" == *-* ]]; then
        days="${etime%%-*}"
        etime="${etime#*-}"
    fi
    IFS=':' read -ra parts <<< "$etime"
    local n=${#parts[@]}
    if [ "$n" -eq 3 ]; then
        hours=$((10#${parts[0]})); minutes=$((10#${parts[1]})); seconds=$((10#${parts[2]}))
    elif [ "$n" -eq 2 ]; then
        minutes=$((10#${parts[0]})); seconds=$((10#${parts[1]}))
    elif [ "$n" -eq 1 ]; then
        seconds=$((10#${parts[0]}))
    fi
    echo $(( days*86400 + hours*3600 + minutes*60 + seconds ))
}

# --- Stop ---
stop_gateway() {
    local pids
    pids=$(find_gateway_pids)

    if [ -z "$pids" ]; then
        echo "Gateway not running."
        return 0
    fi

    echo "Stopping gateway (pids: ${pids})..."
    for p in $pids; do
        kill "$p" 2>/dev/null || true
    done
    sleep 2

    # Force kill if still alive
    local remaining=""
    for p in $pids; do
        if kill -0 "$p" 2>/dev/null; then
            remaining="${remaining}${remaining:+ }${p}"
        fi
    done
    if [ -n "$remaining" ]; then
        echo "Force killing remaining: ${remaining}"
        for p in $remaining; do
            kill -9 "$p" 2>/dev/null || true
        done
        sleep 1
    fi

    # Final check
    local still_alive
    still_alive=$(find_gateway_pids)
    if [ -n "$still_alive" ]; then
        echo "❌ ERROR: Gateway still running after kill! (pids: ${still_alive})"
        return 1
    fi
    echo "Gateway stopped."
}

# --- Start (Python double-fork) ---
start_gateway() {
    echo "Starting gateway..."

    # Check no existing gateway is running
    local existing
    existing=$(find_gateway_pids)
    if [ -n "$existing" ]; then
        echo "❌ ERROR: Gateway already running (pids: ${existing}). Stop it first."
        return 1
    fi

    # Python double-fork daemonize
    python3 -c "
import os, sys

def daemonize():
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    # Redirect stdio
    os.close(0); os.close(1); os.close(2)
    fd_null = os.open(os.devnull, os.O_RDWR)
    os.dup2(fd_null, 0)
    log_fd = os.open('${GATEWAY_LOG}', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.execv('${NANOBOT_BIN}', ['nanobot', 'gateway'])

daemonize()
"

    # Health check: wait for process to appear and stabilize
    local max_wait=15
    local waited=0
    while [ "$waited" -lt "$max_wait" ]; do
        sleep 1
        waited=$((waited + 1))

        local new_pids
        new_pids=$(find_gateway_pids)
        if [ -n "$new_pids" ]; then
            local first_pid
            first_pid=$(echo "$new_pids" | head -1)
            local age
            age=$(get_process_age_seconds "$first_pid" 2>/dev/null || echo "unknown")

            # Wait at least 3 seconds to confirm it's stable (not crashing immediately)
            if [ "$age" != "unknown" ] && [ "$age" -ge 3 ]; then
                echo "✅ Gateway started (pid: ${first_pid}, age: ${age}s)"
                echo "   Log: ${GATEWAY_LOG}"
                return 0
            fi
        fi
    done

    # Timeout — check one more time
    local final_pids
    final_pids=$(find_gateway_pids)
    if [ -n "$final_pids" ]; then
        local first_pid
        first_pid=$(echo "$final_pids" | head -1)
        echo "✅ Gateway started (pid: ${first_pid}, waited ${max_wait}s)"
        echo "   Log: ${GATEWAY_LOG}"
        return 0
    fi

    echo "❌ Gateway failed to start within ${max_wait}s"
    echo "   Check log: ${GATEWAY_LOG}"
    tail -5 "${GATEWAY_LOG}" 2>/dev/null || true
    return 1
}

# --- Status ---
show_status() {
    echo "=== nanobot Gateway ==="
    local pids
    pids=$(find_gateway_pids)

    if [ -z "$pids" ]; then
        echo "Gateway: ❌ stopped"
        return
    fi

    local first_pid
    first_pid=$(echo "$pids" | head -1)
    local age cmd
    age=$(get_process_age_seconds "$first_pid" 2>/dev/null || echo "?")
    cmd=$(ps -o command= -p "$first_pid" 2>/dev/null | head -c 100 || echo "?")

    echo "Gateway: ✅ running (pid: ${pids}, age: ${age}s)"
    echo "     cmd: ${cmd}"
    echo "     log: ${GATEWAY_LOG}"
}

# --- Main ---
case "${1:-restart}" in
    restart|start)
        stop_gateway
        start_gateway
        ;;
    stop)
        stop_gateway
        ;;
    status)
        show_status
        ;;
    *)
        echo "Usage: $0 [restart|stop|status]"
        exit 1
        ;;
esac
