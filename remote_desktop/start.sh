#!/bin/bash
# ==============================================================================
# start.sh – 一键启动 Web 远程桌面服务
# 自动探测 Python 环境 (venv / Conda / 系统 python3)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-5000}"

# 1. 动态探测 Python 执行环境
if [ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ]; then
    echo "  [INFO] 使用指定 Python: $PYTHON_BIN"
elif [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -f "$CONDA_PREFIX/bin/python3" ]; then
    PYTHON_BIN="$CONDA_PREFIX/bin/python3"
elif [ -f "$HOME/miniconda3/bin/python3" ]; then
    PYTHON_BIN="$HOME/miniconda3/bin/python3"
    export PATH="$HOME/miniconda3/bin:$PATH"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "❌ 未检测到可用的 Python3 环境，请先安装或创建虚拟环境"
    exit 1
fi

echo "============================================"
echo "  Web 远程桌面服务"
echo "  Python 环境: $("$PYTHON_BIN" --version 2>&1) ($PYTHON_BIN)"
echo "  端口: $PORT"
echo "============================================"

# 2. 检查关键系统命令
for cmd in websockify x0vncserver; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "⚠️  警告: 未在当前 PATH 中直接找到 '$cmd'，app.py 将尝试自动探测系统路径"
  fi
done

INNER_IP=$(ip route get 1 2>/dev/null | awk '{print $7}' | head -1 || hostname -I | awk '{print $1}' || echo "127.0.0.1")
echo ""
echo "  ✅ 服务启动中，请在浏览器访问："
echo "       http://${INNER_IP}:${PORT}"
echo ""

cd "$SCRIPT_DIR"
exec "$PYTHON_BIN" app.py
