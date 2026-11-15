#!/usr/bin/env python3
"""
X11 clipboard bridge for ViewDesk
=================================

TigerVNC's ``x0vncserver`` (the screen-scraping server used by this project,
at least up to 1.13) does not expose clipboard transfer over RFB.  This small
daemon fills that gap on the server side:

* It runs a Tk main loop on the remote X display and *owns* the X11 CLIPBOARD
  selection, so text pushed from the browser stays available to remote apps.
* It polls the selection and mirrors changes made inside the remote desktop
  (e.g. the user presses Ctrl+C in a remote application) into a JSON state
  file that the Flask gateway exposes to the browser.
* It watches a request file written by the gateway; each request carries a
  unique id and the text to put into the X clipboard.

Why a daemon instead of a one-shot ``xclip`` call?  X11 clipboard contents are
owned by a live client.  A process that exits immediately loses the selection.
Using Tk avoids an extra dependency on xclip/xsel, which are not installed by
default on many minimal images.

The state/request files live in a private directory under /tmp and are only
readable by the user running the web gateway.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

MAX_TEXT_CHARS = 1_000_000
POLL_MS_DEFAULT = 250


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def atomic_write_json(path: Path, data: dict) -> None:
    """Write *data* atomically so readers never observe a partial JSON file."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


class ClipboardBridge:
    def __init__(self, display: str, state_path: Path, request_path: Path,
                 poll_ms: int = POLL_MS_DEFAULT) -> None:
        self.display = display
        self.state_path = state_path
        self.request_path = request_path
        self.poll_ms = max(100, poll_ms)

        self.root = None
        self.text = ""
        self.version = 0
        self.last_request_id = None
        self.last_applied_text = None
        self._lock_handle = None

    # ── Tk clipboard helpers ────────────────────────────────────────────
    def _read_x_clipboard(self) -> str | None:
        """Return the current CLIPBOARD text, or None when nobody owns it."""
        try:
            return self.root.clipboard_get()
        except Exception:
            # TclError is raised when the selection does not exist (or has a
            # non-text target).  Treat it as "no new value" instead of empty.
            return None

    def _write_x_clipboard(self, text: str) -> None:
        """Become the CLIPBOARD owner and publish *text* to remote apps."""
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        # Process the selection hand-over right away.  update() is used instead
        # of update_idletasks() because selection ownership is an X event.
        self.root.update()

    # ── state file ──────────────────────────────────────────────────────
    def _publish(self) -> None:
        atomic_write_json(self.state_path, {
            "version": self.version,
            "text": self.text,
            "request_id": self.last_request_id,
            "pid": os.getpid(),
            "updated": time.time(),
        })

    def _read_request(self) -> tuple[str, str] | None:
        try:
            raw = self.request_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log(f"cannot read request file: {exc}")
            return None

        try:
            request = json.loads(raw)
        except ValueError:
            return None

        if not isinstance(request, dict):
            return None
        req_id = request.get("id")
        text = request.get("text")
        if not isinstance(req_id, str) or not isinstance(text, str):
            return None
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        return req_id, text

    # ── main loop ───────────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            self._process_request()
            self._detect_external_change()
        except Exception as exc:  # keep the bridge alive whatever happens
            log(f"tick failed: {exc!r}")
        finally:
            if self.root is not None:
                self.root.after(self.poll_ms, self._tick)

    def _process_request(self) -> None:
        request = self._read_request()
        if not request:
            return
        req_id, text = request
        if req_id == self.last_request_id:
            return

        # Always take ownership, even when the text is unchanged, so the
        # selection survives after whichever client previously owned it exits.
        self._write_x_clipboard(text)
        self.last_request_id = req_id
        self.last_applied_text = text
        if text != self.text:
            self.text = text
            self.version += 1
        self._publish()
        log(f"applied clipboard request {req_id[:8]} ({len(text)} chars)")

    def _detect_external_change(self) -> None:
        current = self._read_x_clipboard()
        if current is None:
            return

        # Ignore the echo of our own write.  Also ignore empty values: losing
        # the selection temporarily is common and must not wipe the browser
        # clipboard with an empty string.
        if current == self.text or current == "":
            return

        self.text = current
        self.version += 1
        self._publish()
        log(f"remote clipboard changed ({len(current)} chars), version={self.version}")

    def _acquire_lock(self):
        """保证同一块 X display 只有一个剪贴板守护进程。"""
        try:
            import fcntl
        except Exception:
            return True  # 非 POSIX 平台没有 flock，交给上层保证单实例

        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh

    def run(self) -> int:
        try:
            import tkinter as tk
        except Exception as exc:
            log(f"tkinter is not available: {exc!r}")
            return 2

        lock_handle = self._acquire_lock()
        if lock_handle is None:
            log("another clipboard bridge already owns this display; exiting")
            return 4
        self._lock_handle = lock_handle

        # Re-use the previous version counter so a daemon restart does not make
        # already-open pages fetch stale data.
        try:
            old = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("version"), int):
                self.version = old["version"]
        except Exception:
            pass

        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:
            log(f"cannot open X display {self.display}: {exc!r}")
            return 3

        # Do not apply a request left over from a previous daemon instance.
        existing = self._read_request()
        if existing:
            self.last_request_id = existing[0]

        current = self._read_x_clipboard()
        if current is None:
            current = ""
        self.text = current
        if self.version == 0:
            self.version = 1
        else:
            self.version += 1
        self._publish()
        log(f"clipboard bridge started on {self.display} "
            f"(version={self.version}, pid={os.getpid()})")

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                pass

        self.root.after(self.poll_ms, self._tick)
        try:
            self.root.mainloop()
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass
        log("clipboard bridge stopped")
        return 0

    def _handle_signal(self, _signum, _frame) -> None:
        if self.root is not None:
            try:
                self.root.quit()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="ViewDesk X11 clipboard bridge")
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"),
                        help="X display to attach to (default: $DISPLAY or :0)")
    parser.add_argument("--state", required=True, help="JSON state file path")
    parser.add_argument("--request", required=True, help="JSON request file path")
    parser.add_argument("--poll-ms", type=int, default=POLL_MS_DEFAULT,
                        help="clipboard polling interval in milliseconds")
    args = parser.parse_args()

    os.environ.setdefault("DISPLAY", args.display)
    state_path = Path(args.state)
    request_path = Path(args.request)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(state_path.parent, 0o700)
    except OSError:
        pass

    return ClipboardBridge(args.display, state_path, request_path, args.poll_ms).run()


if __name__ == "__main__":
    sys.exit(main())
