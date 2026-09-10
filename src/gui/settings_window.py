"""Settings dialog window."""
import subprocess
import sys
import threading

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.config import Config


def _list_audio_devices(ffmpeg_path: str = "ffmpeg") -> list[str]:
    """List available dshow audio devices via FFmpeg (Windows) or return empty list."""
    devices: list[str] = []
    if sys.platform != "win32":
        return devices
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-f", "dshow",
             "-list_devices", "true", "-i", "dummy"],
            capture_output=True, text=True, timeout=8,
        )
        # FFmpeg prints device list to stderr; audio devices appear after
        # the line "DirectShow audio devices"
        in_audio = False
        for line in result.stderr.splitlines():
            if "audio devices" in line.lower():
                in_audio = True
                continue
            if in_audio and '"' in line:
                # Skip "Alternative name" annotations that follow each device
                if "alternative name" in line.lower():
                    continue
                # Lines look like:  [dshow @ ...] "Device Name"
                start = line.index('"') + 1
                end = line.rindex('"')
                name = line[start:end]
                if name:
                    devices.append(name)
    except Exception:
        pass
    return devices


class SettingsWindow(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(860)
        self.setMinimumHeight(480)
        self.setModal(True)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # ── Two-column body ───────────────────────────────────────────────────
        cols = QHBoxLayout()
        cols.setSpacing(12)
        outer.addLayout(cols)

        left = QVBoxLayout()
        left.setSpacing(8)
        right = QVBoxLayout()
        right.setSpacing(8)
        cols.addLayout(left, stretch=1)
        cols.addLayout(right, stretch=1)

        # ════ LEFT COLUMN ════════════════════════════════════════════════════

        # ---- SERVER ----
        srv_box = QGroupBox("СЕРВЕР")
        srv_form = QFormLayout(srv_box)

        proto_layout = QHBoxLayout()
        self._rb_rtmp = QRadioButton("RTMP")
        self._rb_srt = QRadioButton("SRT")
        proto_layout.addWidget(self._rb_rtmp)
        proto_layout.addWidget(self._rb_srt)
        proto_layout.addStretch()
        srv_form.addRow("Протокол:", proto_layout)

        self._host_edit = QLineEdit()
        srv_form.addRow("Адрес:", self._host_edit)

        key_layout = QHBoxLayout()
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._btn_show_key = QPushButton("👁")
        self._btn_show_key.setFixedWidth(32)
        self._btn_show_key.setCheckable(True)
        self._btn_show_key.toggled.connect(self._toggle_key_visibility)
        key_layout.addWidget(self._key_edit)
        key_layout.addWidget(self._btn_show_key)
        srv_form.addRow("Ключ:", key_layout)

        self._srt_latency = QSpinBox()
        self._srt_latency.setRange(100, 10000)
        self._srt_latency.setSuffix(" мс")
        srv_form.addRow("SRT задержка:", self._srt_latency)
        left.addWidget(srv_box)

        # ---- VIDEO QUALITY ----
        vid_box = QGroupBox("КАЧЕСТВО ВИДЕО")
        vid_form = QFormLayout(vid_box)

        self._res_combo = QComboBox()
        self._res_combo.addItems(["1920x1080", "1280x720", "source"])
        vid_form.addRow("Разрешение:", self._res_combo)

        self._fps_combo = QComboBox()
        self._fps_combo.addItems(["60", "30", "25"])
        vid_form.addRow("FPS:", self._fps_combo)

        self._bitrate_spin = QSpinBox()
        self._bitrate_spin.setRange(500, 50000)
        self._bitrate_spin.setSuffix(" кбит/с")
        vid_form.addRow("Битрейт:", self._bitrate_spin)

        self._preset_combo = QComboBox()
        self._preset_combo.addItems(["slow", "medium", "fast", "faster", "ultrafast"])
        vid_form.addRow("Пресет x264:", self._preset_combo)

        self._tune_combo = QComboBox()
        self._tune_combo.addItems(["film", "animation", "grain", "none"])
        vid_form.addRow("Tune:", self._tune_combo)
        left.addWidget(vid_box)

        # ---- GAME ----
        game_box = QGroupBox("ИГРА")
        game_form = QFormLayout(game_box)

        self._process_names_edit = QLineEdit()
        self._process_names_edit.setPlaceholderText("heroes3.exe, MMH55.exe")
        game_form.addRow("Процессы:", self._process_names_edit)

        self._startup_delay = QSpinBox()
        self._startup_delay.setRange(0, 60)
        self._startup_delay.setSuffix(" сек")
        game_form.addRow("Задержка старта:", self._startup_delay)
        left.addWidget(game_box)

        left.addStretch()

        # ════ RIGHT COLUMN ════════════════════════════════════════════════════

        # ---- BUFFER & ADAPTATION ----
        buf_box = QGroupBox("БУФЕР И АДАПТАЦИЯ")
        buf_form = QFormLayout(buf_box)

        self._max_buf = QSpinBox()
        self._max_buf.setRange(5, 120)
        self._max_buf.setSuffix(" сек")
        buf_form.addRow("Макс. буфер:", self._max_buf)

        self._reduce_thresh = QSpinBox()
        self._reduce_thresh.setRange(5, 60)
        self._reduce_thresh.setSuffix(" сек")
        buf_form.addRow("Порог снижения:", self._reduce_thresh)

        self._min_bitrate = QSpinBox()
        self._min_bitrate.setRange(500, 10000)
        self._min_bitrate.setSuffix(" кбит/с")
        buf_form.addRow("Мин. битрейт:", self._min_bitrate)

        self._step_kbps = QSpinBox()
        self._step_kbps.setRange(100, 2000)
        self._step_kbps.setSuffix(" кбит/с")
        buf_form.addRow("Шаг снижения:", self._step_kbps)

        self._recovery_pause = QSpinBox()
        self._recovery_pause.setRange(10, 300)
        self._recovery_pause.setSuffix(" сек")
        buf_form.addRow("Пауза восстановления:", self._recovery_pause)
        right.addWidget(buf_box)

        # ---- AUDIO ----
        aud_box = QGroupBox("АУДИО")
        aud_form = QFormLayout(aud_box)

        from src.process_audio import is_process_loopback_supported
        if is_process_loopback_supported():
            proc_audio_note = QLabel("✔ Звук захватывается только из игры (Windows 10 2004+)")
            proc_audio_note.setStyleSheet("color: #22aa44; font-size: 10px;")
        else:
            build = 0
            try:
                build = sys.getwindowsversion().build
            except Exception:
                pass
            msg = (
                f"✘ Захват только из игры недоступен (сборка {build}, нужна ≥ 19041).\n"
                "Используйте Стерео микшер."
            ) if build else "✘ Захват только из игры недоступен на этой платформе."
            proc_audio_note = QLabel(msg)
            proc_audio_note.setStyleSheet("color: #cc6600; font-size: 10px;")
        proc_audio_note.setWordWrap(True)
        aud_form.addRow("", proc_audio_note)

        device_row = QHBoxLayout()
        self._audio_device_combo = QComboBox()
        self._audio_device_combo.addItem("(отключено)")
        self._audio_device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._audio_device_combo.setMinimumWidth(180)
        device_row.addWidget(self._audio_device_combo)
        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedWidth(28)
        btn_refresh.setToolTip("Обновить список устройств")
        btn_refresh.clicked.connect(self._refresh_audio_devices)
        device_row.addWidget(btn_refresh)
        aud_form.addRow("Источник:", device_row)

        hint = QLabel(
            "Если список пуст: правой кнопкой по значку звука → Параметры звука → "
            "Запись → Показать отключённые → включите «Стерео микшер»."
        )
        hint.setStyleSheet("color: #888; font-size: 10px;")
        hint.setWordWrap(True)
        aud_form.addRow("", hint)

        self._audio_codec_combo = QComboBox()
        self._audio_codec_combo.addItems(["aac", "mp3"])
        aud_form.addRow("Кодек:", self._audio_codec_combo)

        self._audio_bitrate_combo = QComboBox()
        self._audio_bitrate_combo.addItems(["128", "192", "256", "320"])
        self._audio_bitrate_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        aud_form.addRow("Битрейт (кбс):", self._audio_bitrate_combo)
        right.addWidget(aud_box)

        right.addStretch()

        # Populate devices after dialog is shown (non-blocking)
        QTimer.singleShot(0, self._refresh_audio_devices_async)

        # ── Buttons (full width) ──────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _load_values(self) -> None:
        cfg = self._config

        # Server
        protocol = cfg.stream.get("protocol", "srt")
        if protocol == "rtmp":
            self._rb_rtmp.setChecked(True)
        else:
            self._rb_srt.setChecked(True)
        self._host_edit.setText(cfg.stream.get("host", ""))
        self._key_edit.setText(cfg.stream.get("key", ""))
        self._srt_latency.setValue(cfg.stream.get("srt_latency_ms", 3000))

        # Video
        res = cfg.video.get("resolution", "1920x1080")
        idx = self._res_combo.findText(res)
        self._res_combo.setCurrentIndex(idx if idx >= 0 else 0)

        fps_str = str(cfg.video.get("fps", 60))
        idx = self._fps_combo.findText(fps_str)
        self._fps_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self._bitrate_spin.setValue(cfg.video.get("bitrate_kbps", 6000))

        preset = cfg.video.get("preset", "medium")
        idx = self._preset_combo.findText(preset)
        self._preset_combo.setCurrentIndex(idx if idx >= 0 else 1)

        tune = cfg.video.get("tune", "film")
        idx = self._tune_combo.findText(tune)
        self._tune_combo.setCurrentIndex(idx if idx >= 0 else 0)

        # Buffer
        self._max_buf.setValue(cfg.buffer.get("max_sec", 30))
        self._reduce_thresh.setValue(cfg.buffer.get("reduce_threshold_sec", 15))
        self._min_bitrate.setValue(cfg.buffer.get("min_bitrate_kbps", 2000))
        self._step_kbps.setValue(cfg.buffer.get("step_kbps", 500))
        self._recovery_pause.setValue(cfg.buffer.get("recovery_pause_sec", 30))

        # Audio — restore saved device after list is populated
        device = cfg.audio.get("device", "")
        idx = self._audio_device_combo.findText(device)
        self._audio_device_combo.setCurrentIndex(idx if idx >= 0 else 0)  # 0 = "(отключено)"

        codec = cfg.audio.get("codec", "aac")
        idx = self._audio_codec_combo.findText(codec)
        self._audio_codec_combo.setCurrentIndex(idx if idx >= 0 else 0)

        ab_str = str(cfg.audio.get("bitrate_kbps", 192))
        idx = self._audio_bitrate_combo.findText(ab_str)
        self._audio_bitrate_combo.setCurrentIndex(idx if idx >= 0 else 1)

        # Game
        names = cfg.game.get("process_names", [])
        self._process_names_edit.setText(", ".join(names))
        self._startup_delay.setValue(cfg.game.get("startup_delay_sec", 5))

    def _save(self) -> None:
        cfg = self._config

        # Server
        cfg.set("stream", "protocol", "rtmp" if self._rb_rtmp.isChecked() else "srt")
        cfg.set("stream", "host", self._host_edit.text().strip())
        cfg.set("stream", "key", self._key_edit.text().strip())
        cfg.set("stream", "srt_latency_ms", self._srt_latency.value())

        # Video
        cfg.set("video", "resolution", self._res_combo.currentText())
        cfg.set("video", "fps", int(self._fps_combo.currentText()))
        cfg.set("video", "bitrate_kbps", self._bitrate_spin.value())
        cfg.set("video", "preset", self._preset_combo.currentText())
        cfg.set("video", "tune", self._tune_combo.currentText())

        # Buffer
        cfg.set("buffer", "max_sec", self._max_buf.value())
        cfg.set("buffer", "reduce_threshold_sec", self._reduce_thresh.value())
        cfg.set("buffer", "min_bitrate_kbps", self._min_bitrate.value())
        cfg.set("buffer", "step_kbps", self._step_kbps.value())
        cfg.set("buffer", "recovery_pause_sec", self._recovery_pause.value())

        # Audio — empty string = disabled (no device selected)
        device_text = self._audio_device_combo.currentText()
        is_disabled = device_text == "(отключено)"
        cfg.set("audio", "enabled", not is_disabled)
        cfg.set("audio", "device", "" if is_disabled else device_text)
        cfg.set("audio", "codec", self._audio_codec_combo.currentText())
        cfg.set("audio", "bitrate_kbps", int(self._audio_bitrate_combo.currentText()))

        # Game
        raw = self._process_names_edit.text()
        names = [n.strip() for n in raw.split(",") if n.strip()]
        cfg.set("game", "process_names", names)
        cfg.set("game", "startup_delay_sec", self._startup_delay.value())

        cfg.save()
        self.accept()

    def _refresh_audio_devices_async(self) -> None:
        """Start background thread to list audio devices without blocking the UI."""
        self._audio_device_combo.setEnabled(False)
        saved = self._audio_device_combo.currentText()

        def worker():
            from src.ffmpeg_utils import find_ffmpeg
            try:
                ffmpeg = find_ffmpeg()
            except RuntimeError:
                ffmpeg = "ffmpeg"
            devices = _list_audio_devices(ffmpeg)
            # Schedule UI update back on main thread
            QTimer.singleShot(0, lambda: self._apply_audio_devices(devices, saved))

        threading.Thread(target=worker, daemon=True, name="AudioDeviceList").start()

    def _apply_audio_devices(self, devices: list[str], restore: str) -> None:
        """Update combo box with discovered devices (called on main thread)."""
        self._audio_device_combo.clear()
        self._audio_device_combo.addItem("(отключено)")
        for d in devices:
            self._audio_device_combo.addItem(d)
        self._audio_device_combo.setEnabled(True)
        idx = self._audio_device_combo.findText(restore)
        self._audio_device_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_audio_devices(self) -> None:
        """Refresh button handler — run async device listing."""
        self._refresh_audio_devices_async()

    def _toggle_key_visibility(self, checked: bool) -> None:
        self._key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

