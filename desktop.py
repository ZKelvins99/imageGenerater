"""Desktop entry point: run the FastAPI backend in a background thread and
host the web UI in a native window via pywebview (WebView2 on Windows).

Packaged (PyInstaller) this is the process entry point. In development you can
run it directly with ``python desktop.py`` after installing pywebview.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import socket
import sys
import threading
import time

import uvicorn
from app.paths import base_dir
from app.services import config_service


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


# Win32 user32 helpers. desktop.py is Windows-only and not imported elsewhere,
# but guard the DLL load so a stray import on another OS degrades gracefully.
try:
    _user32 = ctypes.windll.user32
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    _user32.SendMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _user32.GetWindowRect.restype = ctypes.c_bool
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.MonitorFromWindow.restype = ctypes.c_void_p
    _user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetMonitorInfoW.restype = ctypes.c_bool
    _user32.GetMonitorInfoW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MONITORINFO),
    ]
    _user32.GetDpiForWindow.restype = ctypes.c_uint
    _user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
    _user32.SetWindowPos.restype = ctypes.c_bool
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    _user32.MessageBoxW.restype = ctypes.c_int
    _user32.MessageBoxW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
except Exception:  # pragma: no cover - non-Windows import guard
    _user32 = None


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _SingleInstance:
    """Best-effort single-instance guard via an exclusive lock file."""

    def __init__(self) -> None:
        self._path = base_dir() / ".instance.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode("ascii"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass


def _start_server(port: int) -> tuple[uvicorn.Server, threading.Thread]:
    from app.main import create_app

    config_service.ensure_dirs()
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server, thread


def _wait_until_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_is_up(port):
            return True
        time.sleep(0.1)
    return False


def _server_is_up(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _ensure_webview() -> None:
    """Import pywebview lazily and give a clear hint if it is missing."""
    try:
        import webview  # noqa: F401
    except ImportError as exc:  # pragma: no cover - packaging/desktop only
        raise SystemExit(
            "pywebview 未安装。桌面模式请先执行：uv pip install pywebview platformdirs"
        ) from exc


class DesktopApi:
    """JS bridge for the frameless window controls.

    Exposed to the page as ``window.pywebview.api.*``. Window chrome is hidden,
    so the UI drives minimize / maximize-restore / close from the custom
    titlebar, and drag / edge-resize through Win32 on the underlying HWND.

    Drag and resize use ``GetWindowRect`` + ``SetWindowPos``, driven by mouse
    deltas from JS. Both Win32 calls are thread-safe, so this works directly
    from pywebview's JS-bridge worker thread (no ``ReleaseCapture`` /
    ``WM_NCLBUTTONDOWN``, which are unreliable here because they depend on
    mouse-capture ownership inside the WebView2 child window). This is the same
    mechanism the working maximize path uses.
    """

    _MONITOR_DEFAULTTONEAREST = 2
    _SWP_NOZORDER = 0x0004
    _MIN_WIDTH = 960
    _MIN_HEIGHT = 640

    def __init__(self) -> None:
        self._window = None
        self._maximized = False
        self._restore_rect: tuple[int, int, int, int] | None = None

    def bind(self, window) -> None:
        self._window = window

    @property
    def _hwnd(self) -> int | None:
        w = self._window
        if w is None:
            return None
        native = getattr(w, "native", None)
        if native is None:
            return None
        try:
            return int(native.Handle.ToInt64())
        except Exception:
            return None

    # -- window controls -----------------------------------------------------
    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> bool:
        hwnd = self._hwnd
        if hwnd is None or _user32 is None:
            return False
        if self._maximized:
            if self._restore_rect:
                self._set_rect(hwnd, self._restore_rect)
            self._maximized = False
            return False
        self._restore_rect = self._get_rect(hwnd)
        work = self._work_area(hwnd)
        if work:
            self._set_rect(hwnd, work)
        self._maximized = True
        return True

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def move_by(self, dx: int, dy: int) -> None:
        """Translate the window by a mouse delta (CSS px → physical px)."""
        hwnd = self._hwnd
        if hwnd is None or _user32 is None:
            return
        r = self._get_rect(hwnd)
        if not r:
            return
        dx_p, dy_p = self._physical_delta(hwnd, dx, dy)
        self._maximized = False
        self._set_rect(hwnd, (r[0] + dx_p, r[1] + dy_p, r[2] + dx_p, r[3] + dy_p))

    def resize_by(self, dx: int, dy: int, edge: str) -> None:
        """Resize by a mouse delta, anchored on the dragged edge/corner."""
        hwnd = self._hwnd
        if hwnd is None or _user32 is None:
            return
        r = self._get_rect(hwnd)
        if not r:
            return
        dx_p, dy_p = self._physical_delta(hwnd, dx, dy)
        left, top, right, bottom = r
        if "left" in edge:
            left += dx_p
        if "right" in edge:
            right += dx_p
        if "top" in edge:
            top += dy_p
        if "bottom" in edge:
            bottom += dy_p
        # Enforce minimum size, preferring to keep the dragged edge honest.
        if right - left < self._MIN_WIDTH:
            if "left" in edge:
                left = right - self._MIN_WIDTH
            else:
                right = left + self._MIN_WIDTH
        if bottom - top < self._MIN_HEIGHT:
            if "top" in edge:
                top = bottom - self._MIN_HEIGHT
            else:
                bottom = top + self._MIN_HEIGHT
        self._maximized = False
        self._set_rect(hwnd, (left, top, right, bottom))

    # -- Win32 helpers -------------------------------------------------------
    def _physical_delta(self, hwnd: int, dx: int, dy: int) -> tuple[int, int]:
        """Convert CSS-pixel deltas (from JS screenX/screenY) to physical px.

        On a DPI-scaled display the browser reports screen coordinates in CSS
        pixels while SetWindowPos takes physical pixels; without conversion the
        window lags the cursor by the scale factor.
        """
        try:
            dpi = _user32.GetDpiForWindow(hwnd)
            scale = dpi / 96.0 if dpi else 1.0
        except Exception:
            scale = 1.0
        return int(round(dx * scale)), int(round(dy * scale))

    def _get_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        r = _RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
        return None

    def _work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        mon = _user32.MonitorFromWindow(hwnd, self._MONITOR_DEFAULTTONEAREST)
        if mon and _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
        return None

    def _set_rect(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = rect
        _user32.SetWindowPos(
            hwnd,
            0,
            left,
            top,
            right - left,
            bottom - top,
            self._SWP_NOZORDER,
        )


def main() -> None:
    _ensure_webview()
    import webview

    lock = _SingleInstance()
    if not lock.acquire():
        if _user32 is not None:
            _user32.MessageBoxW(0, "ImageGenerater 已在运行中。", "ImageGenerater", 0x40)
        return
    atexit.register(lock.release)

    port = _pick_free_port()
    server, thread = _start_server(port)

    if not _wait_until_ready(port):
        server.should_exit = True
        thread.join(timeout=5)
        raise SystemExit("后端服务启动失败，请查看日志。")

    url = f"http://127.0.0.1:{port}/"

    api = DesktopApi()
    window = webview.create_window(
        "ImageGenerater",
        url,
        js_api=api,
        width=1440,
        height=960,
        min_size=(960, 640),
        frameless=True,
        easy_drag=False,
    )
    api.bind(window)

    def _on_closed() -> None:
        server.should_exit = True

    window.events.closed += _on_closed

    try:
        webview.start(debug=False)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    # Keep the working directory from confusing relative-path assumptions when
    # launched from a shortcut; all paths resolve via app.paths anyway.
    sys.exit(main())
