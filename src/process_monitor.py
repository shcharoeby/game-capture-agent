"""Game process monitor — polls running processes every N seconds."""
import platform
import threading
import time
from dataclasses import dataclass
from typing import Callable

import psutil


@dataclass
class GameProcess:
    pid: int
    name: str
    window_title: str = ""


def _get_window_title(pid: int) -> str:
    """Try to get the main window title for a process (best-effort)."""
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            import ctypes.wintypes

            EnumWindows = ctypes.windll.user32.EnumWindows
            GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
            GetWindowTextW = ctypes.windll.user32.GetWindowTextW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible

            titles: list[str] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def enum_cb(hwnd, _):
                if not IsWindowVisible(hwnd):
                    return True
                found_pid = ctypes.wintypes.DWORD()
                GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value == pid:
                    buf = ctypes.create_unicode_buffer(512)
                    GetWindowTextW(hwnd, buf, 512)
                    if buf.value:
                        titles.append(buf.value)
                return True

            EnumWindows(enum_cb, 0)
            return titles[0] if titles else ""

        elif system == "Darwin":
            # macOS: use Quartz / AppKit — optional, best-effort
            try:
                from AppKit import NSWorkspace  # type: ignore
                ws = NSWorkspace.sharedWorkspace()
                for app in ws.runningApplications():
                    if app.processIdentifier() == pid:
                        return app.localizedName() or ""
            except ImportError:
                pass
    except Exception:
        pass
    return ""


def _get_window_rect(pid: int) -> tuple[int, int, int, int] | None:
    """
    Return (screen_x, screen_y, width, height) of the game window client area.
    None if the window can't be found or is zero-sized.
    """
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        found = wt.HWND(0)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            p = wt.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                found.value = hwnd
                return False
            return True

        ctypes.windll.user32.EnumWindows(_cb, 0)
        hwnd = found.value
        if not hwnd:
            return None

        rect = wt.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom
        if w <= 0 or h <= 0:
            return None

        pt = wt.POINT()
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return (pt.x, pt.y, w, h)
    except Exception:
        return None


class ProcessMonitor:
    """
    Polls the list of running processes for configured game executables.

    Callbacks:
      on_game_found(proc: GameProcess) — called when game process appears
      on_game_lost(proc: GameProcess)  — called when game process disappears
    """

    def __init__(
        self,
        process_names: list[str],
        poll_interval: float = 3.0,
        on_game_found: Callable[[GameProcess], None] | None = None,
        on_game_lost: Callable[[GameProcess], None] | None = None,
    ):
        # On non-Windows systems process names don't have .exe — match both variants
        names_lower = {n.lower() for n in process_names}
        if platform.system() != "Windows":
            names_lower |= {n.lower().removesuffix(".exe") for n in process_names}
        self._names = names_lower
        self._interval = poll_interval
        self._on_found = on_game_found
        self._on_lost = on_game_lost

        self._lock = threading.Lock()
        self._current: GameProcess | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------

    @property
    def current_game(self) -> GameProcess | None:
        with self._lock:
            return self._current

    @property
    def is_game_running(self) -> bool:
        return self.current_game is not None

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ProcessMonitor")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            found = self._find_game()
            with self._lock:
                prev = self._current
                self._current = found

            if found and not prev:
                if self._on_found:
                    self._on_found(found)
            elif not found and prev:
                if self._on_lost:
                    self._on_lost(prev)

            time.sleep(self._interval)

    def _find_game(self) -> GameProcess | None:
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    name = (proc.info["name"] or "").lower()
                    if name in self._names:
                        pid = proc.info["pid"]
                        title = _get_window_title(pid)
                        return GameProcess(pid=pid, name=proc.info["name"], window_title=title)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return None
