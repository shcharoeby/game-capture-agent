"""Debug calibration tool — GUI and orchestration."""
import json
import os
import platform
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import psutil

from src.debug.recorders import (
    FileActivityRecorder,
    MemoryDiffRecorder,
    PixelGridRecorder,
    ScreenshotRecorder,
)
from src.process_monitor import ProcessMonitor, GameProcess


# Quick-event hotkey bindings: (PyQt key string, event name)
QUICK_EVENTS = [
    ("F1", "turn_start"),
    ("F2", "turn_end"),
    ("F3", "combat_start"),
    ("F4", "combat_end"),
    ("F5", "hero_death"),
    ("F6", "city_capture"),
]
MANUAL_HOTKEY = "F9"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_unix_ms() -> int:
    return int(time.time() * 1000)


def _find_game_dir(proc: GameProcess) -> str | None:
    """Try to find the directory where the game executable lives."""
    try:
        p = psutil.Process(proc.pid)
        exe = p.exe()
        return str(Path(exe).parent)
    except Exception:
        return None


def _find_window_bbox(pid: int) -> tuple[int, int, int, int] | None:
    """Return (left, top, width, height) of the game window."""
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            import ctypes.wintypes

            result = [None]

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            def enum_cb(hwnd, _):
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                found_pid = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(found_pid))
                if found_pid.value == pid:
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
                    point = ctypes.wintypes.POINT(0, 0)
                    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
                    result[0] = (point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)
                    return False  # stop enum
                return True

            ctypes.windll.user32.EnumWindows(enum_cb, 0)
            return result[0]
    except Exception:
        pass
    # Fallback: full primary monitor
    import mss
    with mss.mss() as sct:
        m = sct.monitors[1]
        return m["left"], m["top"], m["width"], m["height"]


class _Signals(QObject):
    game_found = pyqtSignal(str, int)   # name, pid
    game_lost = pyqtSignal()
    event_marked = pyqtSignal(str)      # event name


class DebugSession:
    """Manages one debug recording session."""

    def __init__(self, session_dir: Path, process: GameProcess):
        self._session_dir = session_dir
        self._process = process
        self._start_time = time.monotonic()
        self._user_events_path = session_dir / "user_events.jsonl"
        self._user_file = open(self._user_events_path, "a", encoding="utf-8", buffering=1)
        self._recorders: list = []
        self._running = False

        # Discover window bbox
        bbox = _find_window_bbox(process.pid)
        if bbox is None:
            bbox = (0, 0, 1920, 1080)
        left, top, w, h = bbox

        # Screenshot recorder
        self._screenshot_rec = ScreenshotRecorder(session_dir)
        self._screenshot_rec.set_window_bbox(left, top, w, h)
        self._recorders.append(self._screenshot_rec)

        # Pixel grid
        self._pixel_rec = PixelGridRecorder(session_dir)
        self._pixel_rec.set_window_bbox(left, top, w, h)
        self._recorders.append(self._pixel_rec)

        # Memory diff
        self._memory_rec = MemoryDiffRecorder(session_dir, process.pid)
        self._recorders.append(self._memory_rec)

        # File activity — watch game directory
        game_dir = _find_game_dir(process)
        if game_dir:
            self._file_rec = FileActivityRecorder(session_dir, game_dir)
            self._recorders.append(self._file_rec)
        else:
            self._file_rec = None

        # Write session metadata
        self._write_meta(left, top, w, h)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        for rec in self._recorders:
            rec.start()

    def stop(self) -> None:
        self._running = False
        for rec in self._recorders:
            rec.stop()
        self._user_file.flush()
        self._user_file.close()

    def mark_event(self, event_name: str, note: str = "") -> None:
        unix_ms = _now_unix_ms()
        record = {
            "abs": _now_iso(),
            "unix_ms": unix_ms,
            "event": event_name,
            "note": note,
        }
        self._user_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Trigger screenshot burst
        self._screenshot_rec.burst(unix_ms)

    def elapsed_str(self) -> str:
        elapsed = int(time.monotonic() - self._start_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def size_mb(self) -> float:
        total = 0
        for root, _, files in os.walk(self._session_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total / (1024 * 1024)

    def _write_meta(self, left: int, top: int, w: int, h: int) -> None:
        meta = {
            "session_start": _now_iso(),
            "process_name": self._process.name,
            "process_pid": self._process.pid,
            "window": {"left": left, "top": top, "width": w, "height": h},
            "os": platform.system(),
            "os_version": platform.version(),
        }
        (self._session_dir / "session_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class DebugMainWindow(QMainWindow):
    def __init__(self, process_names: list[str]):
        super().__init__()
        self.setWindowTitle("Debug Capture")
        self.setMinimumWidth(420)

        self._signals = _Signals()
        self._signals.game_found.connect(self._on_game_found)
        self._signals.game_lost.connect(self._on_game_lost)
        self._signals.event_marked.connect(self._on_event_marked)

        self._session: DebugSession | None = None
        self._session_dir: Path | None = None
        self._process: GameProcess | None = None

        self._process_monitor = ProcessMonitor(
            process_names,
            poll_interval=3.0,
            on_game_found=lambda p: self._signals.game_found.emit(p.name, p.pid),
            on_game_lost=lambda p: self._signals.game_lost.emit(),
        )

        self._build_ui()
        self._register_hotkeys()
        self._process_monitor.start()

        # Update timer for elapsed/size display
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(2000)
        self._update_timer.timeout.connect(self._refresh_stats)
        self._update_timer.start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # Game status
        game_row = QHBoxLayout()
        game_row.addWidget(QLabel("Игра:"))
        self._game_label = QLabel("ожидание...")
        self._game_label.setStyleSheet("color: #888;")
        game_row.addWidget(self._game_label)
        game_row.addStretch()
        layout.addLayout(game_row)

        # Recording status
        rec_row = QHBoxLayout()
        rec_row.addWidget(QLabel("Запись:"))
        self._rec_label = QLabel("не активна")
        self._rec_label.setStyleSheet("color: #888;")
        rec_row.addWidget(self._rec_label)
        rec_row.addStretch()
        layout.addLayout(rec_row)

        # Recorder summary
        layout.addWidget(self._make_separator())
        self._lbl_screenshots = QLabel("Скриншоты:   каждые 200 мс")
        self._lbl_pixels = QLabel("Пиксели:     каждые 500 мс")
        self._lbl_memory = QLabel("Память:      каждые 1 сек")
        self._lbl_files = QLabel("Файлы игры:  каждые 2 сек")
        for lbl in (self._lbl_screenshots, self._lbl_pixels, self._lbl_memory, self._lbl_files):
            lbl.setStyleSheet("color: #aaa;")
            layout.addWidget(lbl)

        layout.addWidget(self._make_separator())

        # Hotkey info
        hotkey_label = QLabel("Hotkey:  F9 — ввод события вручную")
        layout.addWidget(hotkey_label)
        quick_label = QLabel("Быстрые:  F1 turn_start  F2 turn_end  F3 combat_start\n"
                             "           F4 combat_end  F5 hero_death  F6 city_capture")
        quick_label.setFont(QFont("Courier", 9))
        layout.addWidget(quick_label)

        layout.addWidget(self._make_separator())

        # Event log
        layout.addWidget(QLabel("Последние метки:"))
        self._event_log = QTextEdit()
        self._event_log.setReadOnly(True)
        self._event_log.setFixedHeight(100)
        self._event_log.setFont(QFont("Courier", 9))
        layout.addWidget(self._event_log)

        # Buttons
        layout.addWidget(self._make_separator())
        btn_row = QHBoxLayout()
        self._btn_stop = QPushButton("■  Стоп и сохранить")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._stop_session)
        btn_row.addWidget(self._btn_stop)

        self._btn_open = QPushButton("📁  Открыть папку")
        self._btn_open.setEnabled(False)
        self._btn_open.clicked.connect(self._open_folder)
        btn_row.addWidget(self._btn_open)
        layout.addLayout(btn_row)

    def _make_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ------------------------------------------------------------------
    # Hotkeys
    # ------------------------------------------------------------------

    def _register_hotkeys(self) -> None:
        # Quick event hotkeys
        for key_str, event_name in QUICK_EVENTS:
            shortcut = QShortcut(QKeySequence(key_str), self)
            shortcut.activated.connect(lambda ev=event_name: self._mark_event(ev))

        # Manual entry
        manual_shortcut = QShortcut(QKeySequence(MANUAL_HOTKEY), self)
        manual_shortcut.activated.connect(self._manual_event_dialog)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_game_found(self, name: str, pid: int) -> None:
        self._game_label.setText(f"● {name}  PID: {pid}")
        self._game_label.setStyleSheet("color: #22cc44;")

        # Start recording session automatically
        if self._session is None:
            proc = self._process_monitor.current_game
            if proc:
                self._start_session(proc)

    def _on_game_lost(self) -> None:
        self._game_label.setText("процесс завершён")
        self._game_label.setStyleSheet("color: #cc6622;")

    def _on_event_marked(self, event_name: str) -> None:
        now_str = datetime.now().strftime("%H:%M:%S")
        self._event_log.append(f"{now_str}  {event_name}")

    def _start_session(self, proc: GameProcess) -> None:
        self._process = proc
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # Default output next to this script
        base = Path(__file__).parent.parent.parent / "debug-sessions"
        base.mkdir(exist_ok=True)
        session_dir = base / f"debug-session-{ts}"
        session_dir.mkdir()
        self._session_dir = session_dir

        self._session = DebugSession(session_dir, proc)
        self._session.start()

        self._btn_stop.setEnabled(True)
        self._btn_open.setEnabled(True)

        # Update recorder labels
        color = "#22cc44"
        for lbl in (self._lbl_screenshots, self._lbl_pixels, self._lbl_memory, self._lbl_files):
            lbl.setStyleSheet(f"color: {color};")

    def _stop_session(self) -> None:
        if self._session:
            self._session.stop()
            self._session = None
        self._rec_label.setText("сохранено")
        self._btn_stop.setEnabled(False)

    def _open_folder(self) -> None:
        if not self._session_dir:
            return
        path = str(self._session_dir)
        if platform.system() == "Windows":
            os.startfile(path)  # type: ignore
        elif platform.system() == "Darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])

    def _mark_event(self, event_name: str, note: str = "") -> None:
        if self._session:
            self._session.mark_event(event_name, note)
        self._signals.event_marked.emit(event_name)

    def _manual_event_dialog(self) -> None:
        text, ok = QInputDialog.getText(self, "Событие", "Название события:")
        if ok and text.strip():
            note_text, ok2 = QInputDialog.getText(self, "Заметка", "Заметка (необязательно):")
            self._mark_event(text.strip(), note_text.strip() if ok2 else "")

    def _refresh_stats(self) -> None:
        if self._session:
            elapsed = self._session.elapsed_str()
            size = self._session.size_mb()
            self._rec_label.setText(f"● {elapsed}  ~{size:.0f} МБ")
            self._rec_label.setStyleSheet("color: #cc2222;")

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        if self._session:
            self._session.stop()
        self._process_monitor.stop()
        event.accept()


def run_debug_app(process_names: list[str]) -> None:
    app = QApplication.instance() or QApplication([])
    window = DebugMainWindow(process_names)
    window.show()
    app.exec()
