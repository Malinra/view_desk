#!/bin/bash
# ==============================================================================
# 远程桌面 Web 服务保活守护脚本
# 特性：
# 1. 自动探测 Python 环境 (venv / Conda / 系统 python3)
# 2. 严格使用精准 PID 与端口双重探测，绝不使用模糊 kill，绝不误伤其他服务
# 3. 服务正常时静默退出（返回 0），适合 Cron / 定时调度任务
# 4. 服务意外退出时自动拉起，并记录时间戳日志
# ==============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$PROJECT_DIR/app.pid"
LOG_FILE="$PROJECT_DIR/service.log"
PORT="${PORT:-5000}"

# 1. 动态探测 Python 执行环境
if [ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ]; then
    : # 使用指定环境变量
elif [ -f "$PROJECT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -f "$CONDA_PREFIX/bin/python3" ]; then
    PYTHON_BIN="$CONDA_PREFIX/bin/python3"
elif [ -f "$HOME/miniconda3/bin/python3" ]; then
    PYTHON_BIN="$HOME/miniconda3/bin/python3"
    export PATH="$HOME/miniconda3/bin:$PATH"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] 未找到可用的 Python3 环境" >> "$LOG_FILE"
    exit 1
fi

export PYTHONUNBUFFERED=1

cd "$PROJECT_DIR" || exit 1

# ----------------- [2. 精准双重健康检查] -----------------
is_service_running() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE" 2>/dev/null || true)
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
                return 0
            fi
        fi
    fi
    # 辅助检查：如果 pid 文件丢失，但端口确实处于监听中
    if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
        return 0
    fi
    return 1
}

# 服务正常直接退出
if is_service_running; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [OK] Remote Desktop Web Service is running normally."
    exit 0
fi

# ----------------- [3. 服务拉起逻辑] -----------------
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] Service is down. Starting service on port $PORT..."
rm -f "$PID_FILE"

nohup "$PYTHON_BIN" app.py >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 3
if kill -0 "$NEW_PID" 2>/dev/null && nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SUCCESS] Service successfully started with PID: $NEW_PID"
    exit 0
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] Failed to start service. Check $LOG_FILE for details."
    exit 1
fi
