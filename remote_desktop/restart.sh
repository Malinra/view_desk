#!/bin/bash
# ==============================================================================
# restart.sh - 专属重启远程桌面服务
# 严格只操作 5000 端口对应的单一进程，绝不误伤任何系统其他任务
# ==============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

PORT="${PORT:-5000}"

echo "[1/3] 正在探测 $PORT 端口服务进程..."
# 获取监听 PORT 端口的精确 PID
TARGET_PID=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -n1 | cut -d'=' -f2 || true)

if [ -n "$TARGET_PID" ]; then
    echo "  -> 找到运行中的服务 PID: $TARGET_PID，正在平滑停止..."
    kill "$TARGET_PID" 2>/dev/null || true
    sleep 2
    # 确认是否已退出
    if kill -0 "$TARGET_PID" 2>/dev/null; then
        kill -9 "$TARGET_PID" 2>/dev/null || true
    fi
    echo "  -> 旧服务已停止。"
else
    echo "  -> 当前 $PORT 端口未被占用。"
fi

echo "[2/3] 清理旧 PID 标记..."
rm -f "$PROJECT_DIR/app.pid"

echo "[3/3] 正在调用保活守护程序重新拉起新代码服务..."
"$PROJECT_DIR/keepalive_daemon.sh"

echo "=============================================="
echo " 重启完成！最新状态如下："
ss -tlnp 2>/dev/null | grep ":$PORT " || true
echo "=============================================="
