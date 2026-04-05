#!/bin/bash
# install_git_hooks.sh — 安装 Git hooks 到目标仓库
# 用法: ./install_git_hooks.sh [target_repo_path ...]
# 默认: 安装到 nanobot 和 web-chat 两个仓库
#
# 来源: T-20260402-002 (Cross Check 整改 Phase 1)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_SRC="${SCRIPT_DIR}/git_hooks"
WORKSPACE="${WORKSPACE:-$HOME/.nanobot/workspace}"

# 默认目标仓库
DEFAULT_REPOS=(
    "${WORKSPACE}/dev-workdir/nanobot"
    "${WORKSPACE}/dev-workdir/web-chat"
)

# 如果传入参数，使用参数作为目标；否则使用默认
if [ $# -gt 0 ]; then
    REPOS=("$@")
else
    REPOS=("${DEFAULT_REPOS[@]}")
fi

INSTALLED=0
SKIPPED=0
FAILED=0

for repo in "${REPOS[@]}"; do
    hooks_dir="${repo}/.git/hooks"

    if [ ! -d "${repo}/.git" ]; then
        echo "⚠️  跳过: ${repo} 不是 Git 仓库"
        ((SKIPPED++))
        continue
    fi

    mkdir -p "${hooks_dir}"

    for hook_src in "${HOOKS_SRC}"/*; do
        hook_name=$(basename "${hook_src}")
        hook_dst="${hooks_dir}/${hook_name}"

        # 备份已有 hook（如果不是我们的）
        if [ -f "${hook_dst}" ]; then
            if grep -q "SKIP_TASK_ID_CHECK" "${hook_dst}" 2>/dev/null; then
                echo "✅ ${repo}: ${hook_name} 已是最新版本"
                ((INSTALLED++))
                continue
            else
                backup="${hook_dst}.backup.$(date +%Y%m%d%H%M%S)"
                cp "${hook_dst}" "${backup}"
                echo "📦 ${repo}: 已备份旧 ${hook_name} → ${backup}"
            fi
        fi

        cp "${hook_src}" "${hook_dst}"
        chmod +x "${hook_dst}"
        echo "✅ ${repo}: 已安装 ${hook_name}"
        ((INSTALLED++))
    done
done

echo ""
echo "=== 安装完成 ==="
echo "  ✅ 安装/更新: ${INSTALLED}"
echo "  ⚠️  跳过: ${SKIPPED}"
echo "  ❌ 失败: ${FAILED}"
