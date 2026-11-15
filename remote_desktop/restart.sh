#!/bin/bash
# ==============================================================================
# restart.sh - 专属重启远程桌面服务
# 严格只操作本项目目录下的 app.py 进程，绝不误伤系统其他任务。
# 兼容 ss 无法显示 PID 的受限环境（通过 /proc 扫描 + app.pid 兜底）。
# ==============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

PORT="${PORT:-5000}"
PID_FILE="$PROJECT_DIR/app.pid"

echo "[1/3] 正在探测 $PORT 端口服务进程..."

collect_pids() {
    {
        # 1) ss 能给出 PID 时优先使用
        ss -tlnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | cut -d'=' -f2 || true
        # 2) app.pid 记录的 PID
        if [ -f "$PID_FILE" ]; then
            cat "$PID_FILE" 2>/dev/null || true
        fi
        # 3) 扫描 /proc，仅匹配本项目目录下运行的 app.py
        for proc in /proc/[0-9]*; do
            pid="${proc#/proc/}"
            [ "$pid" = "$$" ] && continue
            cwd="$(readlink "$proc/cwd" 2>/dev/null || true)"
            [ "$cwd" = "$PROJECT_DIR" ] || continue
            cmd="$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)"
            case "$cmd" in
                *python*" app.py"*|*python*"/app.py"*) echo "$pid" ;;
            esac
        done
    } | sort -u
}

mapfile -t TARGET_PIDS < <(collect_pids)

if [ "${#TARGET_PIDS[@]}" -gt 0 ]; then
    for pid in "${TARGET_PIDS[@]}"; do
        [ -n "$pid" ] || continue
        echo "  -> 找到运行中的服务 PID: $pid，正在平滑停止..."
        kill "$pid" 2>/dev/null || true
    done
    sleep 2
    for pid in "${TARGET_PIDS[@]}"; do
        [ -n "$pid" ] || continue
        if kill -0 "$pid" 2>/dev/null; then
            echo "  -> PID $pid 未退出，执行强制停止"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    echo "  -> 旧服务已停止。"
else
    echo "  -> 当前 $PORT 端口未被本项目服务占用。"
fi

echo "[2/3] 清理旧 PID 标记..."
rm -f "$PID_FILE"

echo "[3/3] 正在调用保活守护程序重新拉起新代码服务..."
"$PROJECT_DIR/keepalive_daemon.sh"

echo "=============================================="
echo " 重启完成！最新状态如下："
ss -tlnp 2>/dev/null | grep ":$PORT " || true
echo "=============================================="
