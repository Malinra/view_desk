#!/bin/bash
# ==============================================================================
# start.sh – 一键启动 Web 远程桌面服务
# 自动探测 Python 环境 (venv / Conda / 系统 python3)
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-5000}"

# 1. 动态探测 Python 执行环境
#    仅“存在 python3”不够：venv/conda 可能没有安装 Flask / pam，
#    必须验证依赖，否则服务会在 import 阶段直接退出。
python_has_deps() {
    "$1" -c 'import flask, pam' >/dev/null 2>&1
}

pick_python() {
    local candidates=()
    local py
    if [ -n "${PYTHON_BIN:-}" ]; then
        candidates+=("$PYTHON_BIN")
    fi
    if [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
        candidates+=("$SCRIPT_DIR/venv/bin/python3")
    fi
    if [ -n "${CONDA_PREFIX:-}" ] && [ -f "$CONDA_PREFIX/bin/python3" ]; then
        candidates+=("$CONDA_PREFIX/bin/python3")
    fi
    if [ -f "$HOME/miniconda3/bin/python3" ]; then
        candidates+=("$HOME/miniconda3/bin/python3")
    fi
    if command -v python3 >/dev/null 2>&1; then
        candidates+=("$(command -v python3)")
    fi

    for py in "${candidates[@]}"; do
        if [ -x "$py" ] && python_has_deps "$py"; then
            PYTHON_BIN="$py"
            return 0
        fi
    done
    return 1
}

if ! pick_python; then
    echo "❌ 未找到已安装 Flask / python-pam 的 Python 环境"
    echo "   请先安装依赖，例如："
    echo "     $SCRIPT_DIR/venv/bin/pip install -r $SCRIPT_DIR/requirements.txt"
    echo "   或指定可用解释器：PYTHON_BIN=/path/to/python3 ./start.sh"
    exit 1
fi
# 使用 conda 解释器时把其 bin 放到 PATH，便于找到 websockify
case "$PYTHON_BIN" in
    "$HOME/miniconda3/bin/python3"|"$HOME"/miniconda3/envs/*/bin/python3)
        export PATH="$(dirname "$PYTHON_BIN"):$PATH" ;;
esac

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
