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
import ctypes
import ctypes.util
import json
import os
import signal
import sys
import time
from pathlib import Path

MAX_TEXT_CHARS = 1_000_000
POLL_MS_DEFAULT = 40          # request file / XFixes event check interval
TEXT_POLL_MS_FALLBACK = 250   # text-compare polling when XFixes is unavailable


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


class XFixesWatcher:
    """Report every CLIPBOARD ownership change via the XFixes extension.

    Text comparison alone misses a remote re-copy of the *same* text (e.g. the
    user copies A remotely, copies B locally, then copies A remotely again: the
    X clipboard text never changed, so nothing would be synced).  XFixes emits
    an event on every SetSelectionOwner, so each copy is seen.  It also means
    the clipboard text is only fetched when something actually changed.
    """

    class _SelectionNotify(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_int),
            ("serial", ctypes.c_ulong),
            ("send_event", ctypes.c_int),
            ("display", ctypes.c_void_p),
            ("window", ctypes.c_ulong),
            ("subtype", ctypes.c_int),
            ("owner", ctypes.c_ulong),
            ("selection", ctypes.c_ulong),
            ("timestamp", ctypes.c_ulong),
            ("selection_timestamp", ctypes.c_ulong),
        ]

    class _XEvent(ctypes.Union):
        _fields_ = [("type", ctypes.c_int), ("pad", ctypes.c_long * 24)]

    def __init__(self, display: str) -> None:
        x11_name = ctypes.util.find_library("X11") or "libX11.so.6"
        xfixes_name = ctypes.util.find_library("Xfixes") or "libXfixes.so.3"
        self.x11 = ctypes.CDLL(x11_name)
        self.xfixes = ctypes.CDLL(xfixes_name)

        x11, xfixes = self.x11, self.xfixes
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XGetSelectionOwner.restype = ctypes.c_ulong
        x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        x11.XFlush.argtypes = [ctypes.c_void_p]
        xfixes.XFixesQueryExtension.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        xfixes.XFixesSelectSelectionInput.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]

        self.dpy = x11.XOpenDisplay(display.encode())
        if not self.dpy:
            raise RuntimeError(f"XOpenDisplay({display}) failed")
        event_base, error_base = ctypes.c_int(), ctypes.c_int()
        if not xfixes.XFixesQueryExtension(self.dpy, ctypes.byref(event_base),
                                           ctypes.byref(error_base)):
            x11.XCloseDisplay(self.dpy)
            raise RuntimeError("XFixes extension not available")
        self.notify_type = event_base.value  # + XFixesSelectionNotify (0)
        self.atom = x11.XInternAtom(self.dpy, b"CLIPBOARD", 0)
        # XFixesSetSelectionOwnerNotifyMask = 1
        xfixes.XFixesSelectSelectionInput(
            self.dpy, x11.XDefaultRootWindow(self.dpy), self.atom, 1)
        x11.XFlush(self.dpy)
        self._event = self._XEvent()

    def current_owner(self) -> int:
        return int(self.x11.XGetSelectionOwner(self.dpy, self.atom))

    def drain(self) -> list[int]:
        """Return the owner window of every ownership change since last call."""
        owners = []
        while self.x11.XPending(self.dpy) > 0:
            self.x11.XNextEvent(self.dpy, ctypes.byref(self._event))
            if self._event.type == self.notify_type:
                ev = ctypes.cast(ctypes.byref(self._event),
                                 ctypes.POINTER(self._SelectionNotify)).contents
                owners.append(int(ev.owner))
        return owners


class ClipboardBridge:
    def __init__(self, display: str, state_path: Path, request_path: Path,
                 poll_ms: int = POLL_MS_DEFAULT) -> None:
        self.display = display
        self.state_path = state_path
        self.request_path = request_path
        self.poll_ms = max(20, poll_ms)

        self.root = None
        self.text = ""
        self.version = 0
        self.last_request_id = None
        self.last_applied_text = None
        self._lock_handle = None
        self.watcher: XFixesWatcher | None = None
        self.own_owner = 0          # X window Tk uses to own CLIPBOARD
        self._request_sig = None    # (mtime_ns, size, inode) of request file
        self._last_text_poll = 0.0

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
        if self.watcher is not None:
            # Tk's `update` syncs with the server, so the owner is ours now.
            # Remember Tk's clipboard window to ignore the echo of our writes.
            owner = self.watcher.current_owner()
            if owner:
                self.own_owner = owner

    # ── state file ──────────────────────────────────────────────────────
    def _publish(self) -> None:
        atomic_write_json(self.state_path, {
            "version": self.version,
            "text": self.text,
            "request_id": self.last_request_id,
            "pid": os.getpid(),
            "updated": time.time(),
        })

    def _request_changed(self) -> bool:
        """Cheap stat() check so the request file is only parsed when written."""
        try:
            st = self.request_path.stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._request_sig:
            return False
        self._request_sig = sig
        return True

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
        if not self._request_changed():
            return
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
        if self.watcher is not None:
            self._detect_via_xfixes()
            return
        now = time.monotonic()
        if (now - self._last_text_poll) * 1000 < TEXT_POLL_MS_FALLBACK:
            return
        self._last_text_poll = now
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

    def _detect_via_xfixes(self) -> None:
        try:
            owners = self.watcher.drain()
        except Exception as exc:
            log(f"XFixes watcher failed, falling back to polling: {exc!r}")
            self.watcher = None
            return
        # Only a new foreign owner means "the user copied something remotely".
        # owner == 0: the owning app exited; own_owner: echo of our own write.
        foreign = [o for o in owners if o and o != self.own_owner]
        if not foreign:
            return
        current = self._read_x_clipboard()
        if current is None or current == "":
            return
        # Bump the version even when the text is unchanged: the user copied
        # again and expects the local clipboard to receive it again.
        self.text = current
        self.version += 1
        self._publish()
        log(f"remote clipboard copied ({len(current)} chars), version={self.version}")

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

        try:
            self.watcher = XFixesWatcher(self.display)
            log("using XFixes selection events")
        except Exception as exc:
            self.watcher = None
            log(f"XFixes unavailable ({exc!r}); polling clipboard text instead")

        # Do not apply a request left over from a previous daemon instance.
        self._request_changed()
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
