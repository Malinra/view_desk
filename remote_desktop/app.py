#!/usr/bin/env python3
"""
Web-based Remote Desktop Gateway
==================================
流程：
  1. 用户访问 /  → 显示登录页
  2. POST /login → 验证 Linux 账号/密码（PAM 或 shadow 文件）
  3. 验证通过后：
       a. 为该用户启动 TigerVNC 会话（如果未运行）
       b. 为该 VNC 会话启动独立的 websockify 进程
       c. 返回 noVNC 页面（自动连接到 WebSocket，分辨率自适应客户端）
  4. GET /logout → 结束会话、停止 VNC + websockify
"""

import os
import sys
import shutil
import socket
import re
import signal
import subprocess
import tempfile
import threading
import time
import hashlib
import secrets
import logging
from pathlib import Path
from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, abort, send_from_directory
)
import pam   # python-pam; fallback: shadow-file check

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # 每次重启随机，可改为固定值

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────── 配置 ───────────────────────────
NOVNC_PATH   = Path("/opt/novnc")          # noVNC 静态文件目录
VNC_BASE_PORT = 5900                        # VNC 基础端口 (display :10 -> 5910)
WS_BASE_PORT  = 26900                       # websockify 监听端口起始（避开默认的 6900）
MAX_USERS     = 10                          # 最多同时在线用户数
VNC_GEOMETRY  = "1920x1080"                 # 初始分辨率（noVNC 会按浏览器缩放）
VNC_DEPTH     = "24"
XSTARTUP      = Path(__file__).parent / "xstartup"
SESSION_TIMEOUT = 3600                      # 会话超时 1 小时

# 运行中的会话 {username: {"vnc_display": int, "vnc_pid": int, "ws_port": int, "ws_proc": Popen}}
active_sessions: dict = {}
sessions_lock = threading.Lock()

# ─────────────────────────── 认证 ───────────────────────────
def authenticate(username: str, password: str) -> bool:
    """先尝试 PAM，失败则回退到 shadow 文件比对。"""
    # 安全：用户名只允许合法 Linux 用户名字符
    if not re.fullmatch(r"[a-z_][a-z0-9_\-]{0,31}", username):
        return False
    try:
        p = pam.pam()
        ok = p.authenticate(username, password, service="login")
        log.info("PAM auth for %s: %s", username, ok)
        return ok
    except Exception as e:
        log.warning("PAM auth failed (%s), trying fallback: %s", username, e)

    # Fallback：读取 /etc/shadow（需要 root 权限或 shadow 组）
    try:
        import spwd, crypt
        entry = spwd.getspnam(username)
        hashed = crypt.crypt(password, entry.sp_pwdp)
        return secrets.compare_digest(hashed, entry.sp_pwdp)
    except Exception as e2:
        log.error("Shadow fallback also failed: %s", e2)
        return False

# ─────────────────────────── VNC 管理 ───────────────────────────
def _find_free_display() -> int:
    """找一个未被占用的 VNC display 编号（从 10 开始，避开系统已有桌面）。"""
    import socket
    used = {s["vnc_display"] for s in active_sessions.values()}
    for d in range(10, 100):
        if d not in used and not Path(f"/tmp/.X{d}-lock").exists():
            # 严格测试对应 TCP 端口是否被占用
            p = VNC_BASE_PORT + d
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('127.0.0.1', p))
                sock.close()
                return d
            except OSError:
                sock.close()
                continue
    raise RuntimeError("No free VNC display available")

def _find_free_ws_port() -> int:
    """找一个未被占用的 websockify 端口（从 26900 开始）。"""
    import socket
    used = {s["ws_port"] for s in active_sessions.values()}
    for p in range(WS_BASE_PORT, WS_BASE_PORT + MAX_USERS):
        if p not in used:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('0.0.0.0', p))
                sock.close()
                return p
            except OSError:
                sock.close()
                continue
    raise RuntimeError("No free websockify port available")

def start_vnc_session(username: str) -> dict:
    """为 username 启动 TigerVNC + websockify，返回会话信息。"""
    with sessions_lock:
        if username in active_sessions:
            return active_sessions[username]

        display = _find_free_display()
        vnc_port = VNC_BASE_PORT + display
        ws_port  = _find_free_ws_port()

        # 写入无密码的 VNC passwd 文件（连接走 WebSocket，不暴露 VNC 端口到公网）
        vnc_passwd_dir  = Path(f"/tmp/vnc_{username}")
        vnc_passwd_dir.mkdir(mode=0o700, exist_ok=True)
        vnc_passwd_file = vnc_passwd_dir / "passwd"
        # 生成随机内部密码（8字节）
        internal_pass = secrets.token_hex(4)       # 8 hex chars
        subprocess.run(
            ["vncpasswd", "-f"],
            input=f"{internal_pass}\n{internal_pass}\n",
            stdout=open(vnc_passwd_file, "wb"),
            text=True, check=True
        )
        vnc_passwd_file.chmod(0o600)

        # 确保 xstartup 脚本存在（启动 XFCE/GNOME/xterm）
        _ensure_xstartup()

        # 启动 x0vncserver 直接镜像抓取真实屏幕 DISPLAY=:0
        env = os.environ.copy()
        env["USER"] = username
        env["HOME"] = str(Path(f"/home/{username}"))
        env["DISPLAY"] = ":0"
        
        # 寻找真实桌面的 Xauthority 凭据
        xauth_candidates = [
            "/run/user/1000/gdm/Xauthority",
            str(Path(f"/home/{username}/.Xauthority")),
            os.environ.get("XAUTHORITY", "")
        ]
        xauth_path = next((p for p in xauth_candidates if p and Path(p).exists()), "")
        if xauth_path:
            env["XAUTHORITY"] = xauth_path

        x0vnc_bin = shutil.which("x0vncserver") or "/usr/bin/x0vncserver"
        vnc_cmd = [
            x0vnc_bin,
            "-display", ":0",
            "-rfbport", str(vnc_port),
            "-rfbauth", str(vnc_passwd_file),
            "-localhost", "yes",   # 仅本机监听，安全
            "-SecurityTypes", "VncAuth"
        ]
        log.info("Starting x0vncserver (Real Display :0): %s", " ".join(vnc_cmd))
        proc = subprocess.Popen(vnc_cmd, env=env)

        # 等待端口就绪
        connected = False
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", vnc_port), timeout=0.5):
                    connected = True
                    break
            except (socket.error, ConnectionRefusedError):
                time.sleep(0.5)

        if not connected:
            raise RuntimeError(f"VNC server on port {vnc_port} failed to start")

        # 寻址 websockify 可执行程序
        websockify_bin = shutil.which("websockify")
        if not websockify_bin:
            for candidate in [
                str(Path(sys.executable).parent / "websockify"),
                str(Path.home() / "miniconda3/bin/websockify"),
                "/usr/local/bin/websockify",
                "/usr/bin/websockify"
            ]:
                if Path(candidate).exists():
                    websockify_bin = candidate
                    break

        if websockify_bin:
            ws_cmd = [
                websockify_bin, "--web", str(NOVNC_PATH),
                str(ws_port), f"127.0.0.1:{vnc_port}"
            ]
        else:
            # 保底使用当前 python 解释器的模块执行模式
            ws_cmd = [
                sys.executable, "-m", "websockify", "--web", str(NOVNC_PATH),
                str(ws_port), f"127.0.0.1:{vnc_port}"
            ]

        log.info("Starting websockify: %s", " ".join(ws_cmd))
        ws_proc = subprocess.Popen(ws_cmd)
        time.sleep(1)
        if ws_proc.poll() is not None:
            raise RuntimeError(f"websockify failed to start (port {ws_port} in use?)")

        info = {
            "vnc_display": display,
            "vnc_port":    vnc_port,
            "vnc_proc":    proc,
            "ws_port":     ws_port,
            "ws_proc":     ws_proc,
            "internal_pass": internal_pass,
            "last_active": time.time(),
        }
        active_sessions[username] = info
        log.info("Session started for %s: display=:%d ws_port=%d", username, display, ws_port)
        return info

def stop_vnc_session(username: str):
    """停止用户的 x0vncserver + websockify 会话。"""
    with sessions_lock:
        info = active_sessions.pop(username, None)
    if not info:
        return
    # 停 websockify
    try:
        info["ws_proc"].terminate()
        info["ws_proc"].wait(timeout=5)
    except Exception:
        pass
    # 停 x0vncserver
    if "vnc_proc" in info:
        try:
            info["vnc_proc"].terminate()
            info["vnc_proc"].wait(timeout=5)
        except Exception:
            pass
    log.info("Session stopped for %s", username)

def _ensure_xstartup():
    """如果 xstartup 不存在则创建一个默认的（优先 XFCE，回退 xterm）。"""
    if XSTARTUP.exists():
        return
    content = """\
#!/bin/sh
# xstartup for TigerVNC
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
export XKL_XMODMAP_DISABLE=1

# 尝试启动桌面环境（按优先级）
if command -v startxfce4 >/dev/null 2>&1; then
    exec startxfce4
elif command -v gnome-session >/dev/null 2>&1; then
    exec gnome-session
elif command -v startkde >/dev/null 2>&1; then
    exec startkde
else
    # 最终回退：只启动一个终端
    xterm &
    exec openbox-session 2>/dev/null || exec fluxbox 2>/dev/null || wait
fi
"""
    XSTARTUP.write_text(content)
    XSTARTUP.chmod(0o755)
    log.info("Created xstartup at %s", XSTARTUP)

# ─────────────────────────── 路由 ───────────────────────────
@app.route("/", methods=["GET"])
def index():
    if "username" in session:
        return redirect(url_for("desktop"))
    return render_template("login.html")

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        return render_template("login.html", error="请填写用户名和密码")

    if len(active_sessions) >= MAX_USERS and username not in active_sessions:
        return render_template("login.html", error="当前在线用户已满，请稍后再试")

    if not authenticate(username, password):
        log.warning("Failed login attempt for user: %s from %s", username, request.remote_addr)
        return render_template("login.html", error="用户名或密码错误")

    try:
        info = start_vnc_session(username)
    except Exception as e:
        log.error("Failed to start VNC session for %s: %s", username, e)
        return render_template("login.html", error=f"无法启动桌面会话：{e}")

    session["username"]  = username
    session["ws_port"]   = info["ws_port"]
    session["vnc_pass"]  = info["internal_pass"]
    session.permanent    = True
    app.permanent_session_lifetime = SESSION_TIMEOUT

    return redirect(url_for("desktop"))

@app.route("/desktop")
def desktop():
    if "username" not in session:
        return redirect(url_for("index"))
    username = session["username"]
    # 更新活跃时间
    with sessions_lock:
        if username in active_sessions:
            active_sessions[username]["last_active"] = time.time()
    return render_template("desktop.html",
                           username=username,
                           ws_port=session["ws_port"],
                           vnc_pass=session["vnc_pass"])

@app.route("/logout")
def logout():
    username = session.pop("username", None)
    session.clear()
    if username:
        stop_vnc_session(username)
    return redirect(url_for("index"))

@app.route("/novnc_ws/<path:filename>")
def novnc_static(filename):
    """服务 noVNC 静态文件（JS / CSS / HTML）。
    desktop.html 中引用的 /novnc_ws/core/rfb.js 由此路由提供。
    """
    return send_from_directory(str(NOVNC_PATH), filename)

@app.route("/api/keepalive", methods=["POST"])
def keepalive():
    """前端每 60 秒调用一次，更新活跃时间。"""
    username = session.get("username")
    if username:
        with sessions_lock:
            if username in active_sessions:
                active_sessions[username]["last_active"] = time.time()
    return jsonify({"ok": True})

# ─────────────────────────── 超时清理 ───────────────────────────
def _cleanup_idle_sessions():
    """后台线程：每 5 分钟检查一次，清理超时会话。"""
    while True:
        time.sleep(300)
        now = time.time()
        expired = [u for u, s in active_sessions.items()
                   if now - s["last_active"] > SESSION_TIMEOUT]
        for u in expired:
            log.info("Cleaning up idle session for %s", u)
            stop_vnc_session(u)

threading.Thread(target=_cleanup_idle_sessions, daemon=True).start()

# ─────────────────────────── 入口 ───────────────────────────
if __name__ == "__main__":
    # 仅监听内网，端口 5000
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
