"""Main application window."""
import time
from typing import Callable

from PyQt6.QtCore import QTimer, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.capture import CaptureStats
from src.config import Config, KNOWN_GAMES
from src.gui.settings_window import SettingsWindow
from src.version import __version__


class _Signals(QObject):
    """Cross-thread signal emitter."""
    stats_updated = pyqtSignal(object)           # CaptureStats
    event_logged = pyqtSignal(dict)              # event record
    game_status_changed = pyqtSignal(str, bool)  # (label, found)
    stream_status_changed = pyqtSignal(bool)     # is_live
    recording_status_changed = pyqtSignal(bool)  # is_recording
    update_available = pyqtSignal(str, str)      # (version, changelog)
    update_progress = pyqtSignal(int, int)       # (downloaded, total)
    update_ready = pyqtSignal()
    update_error = pyqtSignal(str)
    check_update_done = pyqtSignal(bool)         # found_update


class _StatusIndicator(QWidget):
    """Colored circle + label widget."""

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._dot = QLabel("●")
        self._dot.setFixedWidth(16)
        font = QFont()
        font.setPointSize(14)
        self._dot.setFont(font)
        layout.addWidget(self._dot)

        self._label = QLabel(label)
        layout.addWidget(self._label)
        layout.addStretch()

        self.set_inactive()

    def set_active(self, color: str = "#22cc44") -> None:
        self._dot.setStyleSheet(f"color: {color};")

    def set_inactive(self) -> None:
        self._dot.setStyleSheet("color: #666666;")

    def set_label(self, text: str) -> None:
        self._label.setText(text)


class MainWindow(QMainWindow):
    # Emitted when user changes the game selection
    game_selected = pyqtSignal(str)  # game display name

    def __init__(
        self,
        config: Config,
        on_start_stream: Callable[[], None] | None = None,
        on_stop_stream: Callable[[], None] | None = None,
        on_start_recording: Callable[[], None] | None = None,
        on_stop_recording: Callable[[], None] | None = None,
        on_open_recordings_folder: Callable[[], None] | None = None,
        on_settings_saved: Callable[[], None] | None = None,
        on_game_changed: Callable[[str], None] | None = None,
        on_channel_changed: Callable[[str], None] | None = None,
        on_check_update: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ):
        super().__init__()
        self._config = config
        self._on_start_stream = on_start_stream
        self._on_stop_stream = on_stop_stream
        self._on_start_recording = on_start_recording
        self._on_stop_recording = on_stop_recording
        self._on_open_recordings_folder = on_open_recordings_folder
        self._on_settings_saved = on_settings_saved
        self._on_game_changed = on_game_changed
        self._on_channel_changed = on_channel_changed
        self._on_check_update = on_check_update
        self._on_quit = on_quit

        self._signals = _Signals()
        self._signals.stats_updated.connect(self._on_stats)
        self._signals.event_logged.connect(self._on_event)
        self._signals.game_status_changed.connect(self._on_game_status)
        self._signals.stream_status_changed.connect(self._on_stream_status)
        self._signals.recording_status_changed.connect(self._on_recording_status)
        self._signals.update_available.connect(self._on_update_available)
        self._signals.update_progress.connect(self._on_update_progress)
        self._signals.update_ready.connect(self._on_update_ready)
        self._signals.update_error.connect(self._on_update_error)
        self._signals.check_update_done.connect(self._on_check_update_done)

        self._updater = None   # set by notify_update_available → download

        self._is_live = False
        self._is_recording = False
        self._stream_start_mono: float | None = None
        self._record_start_mono: float | None = None
        self._pulse_on = True

        self._build_ui()
        self._setup_timers()

    # ------------------------------------------------------------------
    # Thread-safe signals (called from any thread)
    # ------------------------------------------------------------------

    def notify_stats(self, stats: CaptureStats) -> None:
        self._signals.stats_updated.emit(stats)

    def notify_event(self, record: dict) -> None:
        self._signals.event_logged.emit(record)

    def notify_game_status(self, label: str, found: bool) -> None:
        self._signals.game_status_changed.emit(label, found)

    def notify_stream_status(self, is_live: bool) -> None:
        self._signals.stream_status_changed.emit(is_live)

    def notify_recording_status(self, is_recording: bool) -> None:
        self._signals.recording_status_changed.emit(is_recording)

    def notify_update_available(self, info) -> None:
        """Called from background thread when a new version is found."""
        self._signals.update_available.emit(info.version, info.changelog)
        self._pending_update_info = info

    def notify_update_progress(self, downloaded: int, total: int) -> None:
        self._signals.update_progress.emit(downloaded, total)

    def notify_update_ready(self) -> None:
        self._signals.update_ready.emit()

    def notify_update_error(self, msg: str) -> None:
        self._signals.update_error.emit(msg)

    def notify_check_update_done(self, found: bool) -> None:
        self._signals.check_update_done.emit(found)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle(f"OldenLeague Game Capture  v{__version__}")
        self.setMinimumWidth(500)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # --- Game selector ---
        game_row = QHBoxLayout()
        game_lbl = QLabel("Игра:")
        game_lbl.setFixedWidth(60)
        game_row.addWidget(game_lbl)
        self._game_combo = QComboBox()
        for name in KNOWN_GAMES:
            self._game_combo.addItem(name)
        # Set current selection from config
        idx = self._game_combo.findText(self._config.selected_game)
        if idx >= 0:
            self._game_combo.setCurrentIndex(idx)
        self._game_combo.currentTextChanged.connect(self._on_game_combo_changed)
        game_row.addWidget(self._game_combo)
        layout.addLayout(game_row)

        # --- Status block ---
        self._game_indicator = _StatusIndicator("Ожидание...")
        layout.addWidget(self._build_label_row("Статус:", self._game_indicator))

        self._stream_indicator = _StatusIndicator("Не активен")
        layout.addWidget(self._build_label_row("Стрим:", self._stream_indicator))

        self._record_indicator = _StatusIndicator("Не активна")
        layout.addWidget(self._build_label_row("Запись:", self._record_indicator))

        # Buffer progress bar
        buf_row = QHBoxLayout()
        buf_label = QLabel("Буфер:")
        buf_label.setFixedWidth(60)
        buf_row.addWidget(buf_label)
        self._buffer_bar = QProgressBar()
        self._buffer_bar.setRange(0, 300)  # 0–30 sec * 10
        self._buffer_bar.setValue(0)
        self._buffer_bar.setTextVisible(False)
        self._buffer_bar.setFixedHeight(16)
        buf_row.addWidget(self._buffer_bar)
        self._buffer_label = QLabel("0.0 сек")
        self._buffer_label.setFixedWidth(60)
        buf_row.addWidget(self._buffer_label)
        layout.addLayout(buf_row)

        # Audio VU meter
        aud_row = QHBoxLayout()
        aud_label = QLabel("Аудио:")
        aud_label.setFixedWidth(60)
        aud_row.addWidget(aud_label)
        self._audio_bar = QProgressBar()
        self._audio_bar.setRange(0, 100)
        self._audio_bar.setValue(0)
        self._audio_bar.setTextVisible(False)
        self._audio_bar.setFixedHeight(16)
        aud_row.addWidget(self._audio_bar)
        self._audio_label = QLabel("-∞ dB")
        self._audio_label.setFixedWidth(60)
        aud_row.addWidget(self._audio_label)
        layout.addLayout(aud_row)

        # Separator
        layout.addWidget(self._make_separator())

        # --- Connection fields ---
        server_row = QHBoxLayout()
        server_row.addWidget(QLabel("Сервер:"))
        self._server_edit = QLineEdit()
        self._server_edit.setText(self._config.stream.get("host", ""))
        self._server_edit.setPlaceholderText("stream.oldenleague.ru")
        server_row.addWidget(self._server_edit)
        layout.addLayout(server_row)

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("Ключ:"))
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setText(self._config.stream.get("key", ""))
        self._key_edit.setPlaceholderText("stream key")
        self._key_edit.editingFinished.connect(self._save_key)
        key_row.addWidget(self._key_edit)
        self._btn_show_key = QPushButton("👁")
        self._btn_show_key.setFixedWidth(32)
        self._btn_show_key.setCheckable(True)
        self._btn_show_key.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self._btn_show_key)
        layout.addLayout(key_row)

        # --- Buttons ---
        layout.addWidget(self._make_separator())
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("▶  Начать стрим")
        self._btn_start.setFixedHeight(36)
        self._btn_start.clicked.connect(self._toggle_stream)
        btn_row.addWidget(self._btn_start)

        self._btn_record = QPushButton("⏺  Запись")
        self._btn_record.setFixedHeight(36)
        self._btn_record.clicked.connect(self._toggle_recording)
        btn_row.addWidget(self._btn_record)

        self._btn_open_folder = QPushButton("📁")
        self._btn_open_folder.setFixedWidth(36)
        self._btn_open_folder.setFixedHeight(36)
        self._btn_open_folder.setToolTip("Открыть папку с записями")
        self._btn_open_folder.clicked.connect(self._open_recordings_folder)
        btn_row.addWidget(self._btn_open_folder)

        self._btn_check_update = QPushButton("🔄")
        self._btn_check_update.setFixedWidth(36)
        self._btn_check_update.setFixedHeight(36)
        self._btn_check_update.setToolTip("Проверить наличие обновлений")
        self._btn_check_update.clicked.connect(self._trigger_check_update)
        btn_row.addWidget(self._btn_check_update)

        btn_settings = QPushButton("⚙  Настройки")
        btn_settings.setFixedHeight(36)
        btn_settings.clicked.connect(self._open_settings)
        btn_row.addWidget(btn_settings)
        layout.addLayout(btn_row)

        # --- Event log ---
        layout.addWidget(self._make_separator())
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Последние события:"))
        log_header.addStretch()
        btn_copy_log = QPushButton("📋 Копировать")
        btn_copy_log.setFixedHeight(22)
        btn_copy_log.clicked.connect(self._copy_event_log)
        log_header.addWidget(btn_copy_log)
        layout.addLayout(log_header)
        self._event_log = QTextEdit()
        self._event_log.setReadOnly(True)
        self._event_log.setFixedHeight(120)
        self._event_log.setFont(QFont("Courier", 9))
        layout.addWidget(self._event_log)

        # --- Update banner (hidden by default) ---
        self._update_banner = QFrame()
        self._update_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self._update_banner.setStyleSheet(
            "QFrame { background: #1a3a1a; border: 1px solid #2a6a2a; border-radius: 4px; }"
        )
        self._update_banner.setVisible(False)
        update_layout = QVBoxLayout(self._update_banner)
        update_layout.setContentsMargins(8, 6, 8, 6)
        update_layout.setSpacing(4)

        self._update_label = QLabel()
        self._update_label.setStyleSheet("color: #88ff88; font-weight: bold;")
        self._update_changelog = QLabel()
        self._update_changelog.setStyleSheet("color: #aaaaaa; font-size: 10px;")
        self._update_changelog.setWordWrap(True)
        update_layout.addWidget(self._update_label)
        update_layout.addWidget(self._update_changelog)

        self._update_progress = QProgressBar()
        self._update_progress.setFixedHeight(12)
        self._update_progress.setTextVisible(False)
        self._update_progress.setVisible(False)
        update_layout.addWidget(self._update_progress)

        btn_row = QHBoxLayout()
        self._btn_update = QPushButton("⬇  Обновить")
        self._btn_update.setFixedHeight(28)
        self._btn_update.clicked.connect(self._start_update_download)
        btn_row.addWidget(self._btn_update)

        self._btn_restart_update = QPushButton("↺  Перезапустить для обновления")
        self._btn_restart_update.setFixedHeight(28)
        self._btn_restart_update.setVisible(False)
        self._btn_restart_update.clicked.connect(self._apply_update)
        btn_row.addWidget(self._btn_restart_update)

        self._btn_skip_update = QPushButton("Пропустить")
        self._btn_skip_update.setFixedHeight(28)
        self._btn_skip_update.clicked.connect(self._skip_update)
        btn_row.addWidget(self._btn_skip_update)
        update_layout.addLayout(btn_row)

        layout.addWidget(self._update_banner)

    def _build_label_row(self, label: str, widget: QWidget) -> QWidget:
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(60)
        row.addWidget(lbl)
        row.addWidget(widget)
        w = QWidget()
        w.setLayout(row)
        return w

    def _make_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ------------------------------------------------------------------
    # Timers
    # ------------------------------------------------------------------

    def _setup_timers(self) -> None:
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(800)
        self._pulse_timer.timeout.connect(self._pulse_live)

        self._uptime_timer = QTimer(self)
        self._uptime_timer.setInterval(1000)
        self._uptime_timer.timeout.connect(self._update_uptime)

    def _pulse_live(self) -> None:
        if not self._is_live:
            return
        self._pulse_on = not self._pulse_on
        color = "#cc2222" if self._pulse_on else "#661111"
        self._stream_indicator.set_active(color)

    def _update_uptime(self) -> None:
        now = time.monotonic()
        if self._stream_start_mono is not None:
            elapsed = int(now - self._stream_start_mono)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            br = getattr(self, "_last_bitrate", 0)
            self._stream_indicator.set_label(f"LIVE  {h:02d}:{m:02d}:{s:02d}  {br} kbps")
        if self._record_start_mono is not None:
            elapsed = int(now - self._record_start_mono)
            h = elapsed // 3600
            m = (elapsed % 3600) // 60
            s = elapsed % 60
            self._record_indicator.set_label(f"REC  {h:02d}:{m:02d}:{s:02d}")

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_stats(self, stats: CaptureStats) -> None:
        self._last_bitrate = stats.bitrate_kbps

        buf_val = int(stats.buffer_sec * 10)
        self._buffer_bar.setValue(min(buf_val, 300))
        self._buffer_label.setText(f"{stats.buffer_sec:.1f} сек")

        if stats.buffer_sec < 10:
            style = "QProgressBar::chunk { background: #22cc44; }"
        elif stats.buffer_sec < 20:
            style = "QProgressBar::chunk { background: #e8a020; }"
        else:
            style = "QProgressBar::chunk { background: #cc2222; }"
        self._buffer_bar.setStyleSheet(style)

        speed_pct = int(min(stats.speed, 1.5) / 1.5 * 100)
        self._audio_bar.setValue(speed_pct)

    def _on_event(self, record: dict) -> None:
        abs_time = record.get("abs", "")
        time_part = abs_time[11:19] if len(abs_time) >= 19 else abs_time
        event = record.get("event", "")
        extra = {k: v for k, v in record.items() if k not in ("abs", "rel", "event")}
        extra_str = "  " + str(extra) if extra else ""
        line = f"{time_part}  {event}{extra_str}"

        self._event_log.append(line)
        max_lines = self._config.log.get("show_in_ui_last_n", 20)
        doc = self._event_log.document()
        while doc.blockCount() > max_lines:
            cursor = self._event_log.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def _on_game_status(self, label: str, found: bool) -> None:
        self._game_indicator.set_label(label)
        if found:
            self._game_indicator.set_active("#22cc44")
        else:
            self._game_indicator.set_inactive()

    def _on_stream_status(self, is_live: bool) -> None:
        self._is_live = is_live
        if is_live:
            self._stream_start_mono = time.monotonic()
            self._uptime_timer.start()
            self._pulse_timer.start()
            self._btn_start.setText("■  Остановить стрим")
            self._btn_start.setStyleSheet("background: #8b1a1a; color: white;")
            self._game_combo.setEnabled(False)
        else:
            self._stream_start_mono = None
            self._pulse_timer.stop()
            self._stream_indicator.set_inactive()
            self._stream_indicator.set_label("Не активен")
            self._btn_start.setText("▶  Начать стрим")
            self._btn_start.setStyleSheet("")
            self._game_combo.setEnabled(True)
            if not self._is_recording:
                self._uptime_timer.stop()

    def _on_recording_status(self, is_recording: bool) -> None:
        self._is_recording = is_recording
        if is_recording:
            self._record_start_mono = time.monotonic()
            self._uptime_timer.start()
            self._record_indicator.set_active("#cc2222")
            self._btn_record.setText("■  Стоп запись")
            self._btn_record.setStyleSheet("background: #8b1a1a; color: white;")
        else:
            self._record_start_mono = None
            self._record_indicator.set_inactive()
            self._record_indicator.set_label("Не активна")
            self._btn_record.setText("⏺  Запись")
            self._btn_record.setStyleSheet("")
            if not self._is_live:
                self._uptime_timer.stop()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_game_combo_changed(self, game_name: str) -> None:
        self._config.set("game", "selected", game_name)
        self._config.save()
        self._game_indicator.set_label("Ожидание...")
        self._game_indicator.set_inactive()
        if self._on_game_changed:
            self._on_game_changed(game_name)

    def _toggle_stream(self) -> None:
        self._config.set("stream", "host", self._server_edit.text().strip())
        self._config.set("stream", "key", self._key_edit.text().strip())
        if self._is_live:
            if self._on_stop_stream:
                self._on_stop_stream()
        else:
            key = self._key_edit.text().strip()
            if not key:
                QMessageBox.warning(
                    self,
                    "Ключ не указан",
                    "Введите ключ трансляции перед началом стрима.",
                )
                return
            if self._on_start_stream:
                self._on_start_stream()

    def _toggle_recording(self) -> None:
        if self._is_recording:
            if self._on_stop_recording:
                self._on_stop_recording()
        else:
            if self._on_start_recording:
                self._on_start_recording()

    def _open_recordings_folder(self) -> None:
        if self._on_open_recordings_folder:
            self._on_open_recordings_folder()

    def _trigger_check_update(self) -> None:
        self._btn_check_update.setEnabled(False)
        self._btn_check_update.setText("…")
        if self._on_check_update:
            self._on_check_update()

    def _on_check_update_done(self, found: bool) -> None:
        self._btn_check_update.setEnabled(True)
        self._btn_check_update.setText("🔄")
        if not found:
            self._btn_check_update.setToolTip("Обновлений нет")
            QTimer.singleShot(
                3000,
                lambda: self._btn_check_update.setToolTip("Проверить наличие обновлений"),
            )

    def _copy_event_log(self) -> None:
        QApplication.clipboard().setText(self._event_log.toPlainText())

    def _open_settings(self) -> None:
        dlg = SettingsWindow(self._config, on_channel_changed=self._on_channel_changed, parent=self)
        if dlg.exec():
            self._server_edit.setText(self._config.stream.get("host", ""))
            self._key_edit.setText(self._config.stream.get("key", ""))
            if self._on_settings_saved:
                self._on_settings_saved()

    def _save_key(self) -> None:
        key = self._key_edit.text().strip()
        self._config.set("stream", "key", key)
        self._config.save()

    def _toggle_key_visibility(self, checked: bool) -> None:
        self._key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    # ------------------------------------------------------------------
    # Update slots (run on main thread via Qt signals)
    # ------------------------------------------------------------------

    def _on_update_available(self, version: str, changelog: str) -> None:
        self._update_label.setText(f"Доступно обновление  v{version}")
        self._update_changelog.setText(changelog or "")
        self._update_banner.setVisible(True)

    def _on_update_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            self._update_progress.setMaximum(total)
            self._update_progress.setValue(downloaded)
            mb_done = downloaded / (1 << 20)
            mb_total = total / (1 << 20)
            self._update_label.setText(
                f"Загрузка обновления…  {mb_done:.0f} / {mb_total:.0f} МБ"
            )

    def _on_update_ready(self) -> None:
        self._update_label.setText("Обновление загружено")
        self._update_progress.setVisible(False)
        self._btn_update.setVisible(False)
        self._btn_skip_update.setVisible(False)
        self._btn_restart_update.setVisible(True)

    def _on_update_error(self, msg: str) -> None:
        self._update_label.setText(f"Ошибка обновления: {msg}")
        self._update_progress.setVisible(False)
        self._btn_update.setVisible(True)

    def _start_update_download(self) -> None:
        from src.updater import Updater
        info = getattr(self, "_pending_update_info", None)
        if not info:
            return
        self._btn_update.setEnabled(False)
        self._update_progress.setVisible(True)
        self._updater = Updater(
            info,
            on_progress=self.notify_update_progress,
            on_ready=self.notify_update_ready,
            on_error=self.notify_update_error,
        )
        self._updater.start()

    def _skip_update(self) -> None:
        self._update_banner.setVisible(False)

    def _apply_update(self) -> None:
        from src.updater import Updater
        Updater.launch_and_exit()

    # ------------------------------------------------------------------

    def closeEvent(self, event):
        if self._is_live or self._is_recording:
            parts = []
            if self._is_live:
                parts.append("стрим")
            if self._is_recording:
                parts.append("запись")
            what = " и ".join(parts)
            reply = QMessageBox.question(
                self,
                "Завершить?",
                f"Идёт {what}. Остановить и выйти?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
        if self._on_quit:
            self._on_quit()
