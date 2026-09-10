"""
Per-window video capture using Win32 PrintWindow API.

PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT=0x02) asks the window to render its
client area into a GDI DC — independent of z-order and occlusion.  DirectX 9/10/11
content in windowed mode is captured correctly (same as OBS "BitBlt" window capture).
If the window is minimised → black frame; stream continues uninterrupted.

Frames are written as raw BGRx (32-bit, top-down) to a Windows named pipe that
FFmpeg reads with: -f rawvideo -pix_fmt bgr0 -s WxH -r FPS -i \\.\pipe\game_video_PID
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Callable


# PrintWindow: capture hardware-accelerated / DirectX content, not just GDI
_PW_RENDERFULLCONTENT = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wt.DWORD),
        ("biWidth",         wt.LONG),
        ("biHeight",        wt.LONG),  # negative = top-down
        ("biPlanes",        wt.WORD),
        ("biBitCount",      wt.WORD),
        ("biCompression",   wt.DWORD),
        ("biSizeImage",     wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed",       wt.DWORD),
        ("biClrImportant",  wt.DWORD),
    ]


def _find_hwnd(pid: int) -> int:
    """Return the HWND of the first visible top-level window for the given PID, or 0."""
    found = wt.HWND(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        p = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            found.value = hwnd
            return False  # stop enumeration
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    return found.value


class WindowCapturePipe:
    """
    Captures a game window (by PID) using PrintWindow and streams raw BGRx frames
    to a Windows named pipe for FFmpeg to consume as rawvideo input.

    - Captures the window's own framebuffer → unaffected by z-order / occlusion.
    - Alt-tabbed game still captured correctly (window still has its framebuffer).
    - Minimised window → black frames (FFmpeg keeps running, stream shows black).
    - Works for DX9/10/11 in windowed mode (same approach as OBS BitBlt capture).
    """

    PIPE_BASE = r"\\.\pipe\game_video_"

    def __init__(
        self,
        pid: int,
        fps: int = 60,
        on_focus_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._pid = pid
        self._fps = fps
        self._on_focus_change = on_focus_change
        self.pipe_name = f"{self.PIPE_BASE}{pid}"
        self.width = 0
        self.height = 0
        self._stop_event = threading.Event()
        self._pipe_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_error: str | None = None

    def start(self) -> None:
        """Start capture thread; blocks until the pipe is ready (≤5 s)."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"WinCapture-{self._pid}"
        )
        self._thread.start()
        if not self._pipe_ready.wait(timeout=5.0):
            raise RuntimeError("WindowCapturePipe: timed out finding game window")
        if self._init_error:
            raise RuntimeError(f"WindowCapturePipe: {self._init_error}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._capture()
        except Exception as exc:
            if not self._pipe_ready.is_set():
                self._init_error = str(exc)
                self._pipe_ready.set()

    def _capture(self) -> None:
        hwnd = _find_hwnd(self._pid)
        if not hwnd:
            raise RuntimeError(f"No visible window for PID {self._pid}")

        rect = wt.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom
        if w <= 0 or h <= 0:
            raise RuntimeError(f"Game window has zero client area ({w}×{h})")

        self.width  = w
        self.height = h

        # ── GDI objects ───────────────────────────────────────────────────────
        screen_dc = ctypes.windll.user32.GetDC(0)
        mem_dc    = ctypes.windll.gdi32.CreateCompatibleDC(screen_dc)
        bitmap    = ctypes.windll.gdi32.CreateCompatibleBitmap(screen_dc, w, h)
        ctypes.windll.gdi32.SelectObject(mem_dc, bitmap)
        ctypes.windll.user32.ReleaseDC(0, screen_dc)

        bih = _BITMAPINFOHEADER()
        bih.biSize        = ctypes.sizeof(bih)
        bih.biWidth       = w
        bih.biHeight      = -h      # negative = top-down scan order
        bih.biPlanes      = 1
        bih.biBitCount    = 32
        bih.biCompression = 0       # BI_RGB — no compression
        bih.biSizeImage   = w * h * 4

        frame_buf = (ctypes.c_byte * (w * h * 4))()

        # ── Named pipe ────────────────────────────────────────────────────────
        # Large buffer (8 MB) to absorb frame bursts without stalling.
        pipe_h = ctypes.windll.kernel32.CreateNamedPipeW(
            self.pipe_name,
            0x00000002,           # PIPE_ACCESS_OUTBOUND
            0x00000000,           # PIPE_TYPE_BYTE | PIPE_WAIT
            1, 8 << 20, 8 << 20, 0, None,
        )
        if pipe_h == _INVALID_HANDLE_VALUE:
            raise OSError(f"CreateNamedPipe failed: error {ctypes.GetLastError()}")

        try:
            self._pipe_ready.set()   # unblock start() — dimensions known, pipe exists
            ctypes.windll.kernel32.ConnectNamedPipe(pipe_h, None)  # wait for FFmpeg

            interval  = 1.0 / self._fps
            next_tick = time.monotonic()
            written   = wt.DWORD(0)

            # Focus tracking
            _GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
            _focused: bool | None = None   # unknown at start

            while not self._stop_event.is_set():
                # Detect focus changes (cheap call, once per frame is fine)
                if self._on_focus_change is not None:
                    is_focused = (_GetForegroundWindow() == hwnd)
                    if is_focused != _focused:
                        _focused = is_focused
                        try:
                            self._on_focus_change(is_focused)
                        except Exception:
                            pass

                # Ask the window to render itself into mem_dc.
                # PW_RENDERFULLCONTENT ensures DX surfaces are included.
                ctypes.windll.user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT)

                # Copy the bitmap pixels (top-down BGRx) into frame_buf.
                ctypes.windll.gdi32.GetDIBits(
                    mem_dc, bitmap, 0, h, frame_buf, ctypes.byref(bih), 0
                )

                # Write one raw frame to the pipe for FFmpeg to consume.
                ok = ctypes.windll.kernel32.WriteFile(
                    pipe_h, frame_buf, len(frame_buf), ctypes.byref(written), None
                )
                if not ok:
                    break   # FFmpeg disconnected (stream stopped)

                # Pace to target FPS.
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        finally:
            ctypes.windll.gdi32.DeleteObject(bitmap)
            ctypes.windll.gdi32.DeleteDC(mem_dc)
            ctypes.windll.kernel32.CloseHandle(pipe_h)
