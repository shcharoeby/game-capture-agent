"""Main application entry point — Game Capture Agent."""
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from src.capture import CaptureConfig, CaptureManager, CaptureStats
from src.config import Config
from src.event_logger import EventLogger
from src.ffmpeg_utils import find_ffmpeg
from src.gui.main_window import MainWindow
from src.gui.tray import TrayIcon, TrayState
from src.process_audio import ProcessAudioCapture, is_process_loopback_supported
from src.process_monitor import GameProcess, ProcessMonitor, _get_window_title
from src.updater import check_for_update
from src.window_capture import WindowCapturePipe, make_window_capture


def _make_capture_config(
    cfg: Config,
    window_title: str = "",
    window_pid: int = 0,
    stream_enabled: bool = True,
    record_path: str = "",
) -> CaptureConfig:
    try:
        ffmpeg_path = find_ffmpeg()
    except RuntimeError:
        ffmpeg_path = "ffmpeg"  # will fail with a clear error in CaptureManager

    return CaptureConfig(
        host=cfg.stream["host"],
        key=cfg.stream["key"],
        protocol=cfg.stream["protocol"],
        srt_latency_ms=cfg.stream["srt_latency_ms"],
        fps=cfg.video["fps"],
        resolution=cfg.video["resolution"],
        bitrate_kbps=cfg.video["bitrate_kbps"],
        preset=cfg.video["preset"],
        tune=cfg.video["tune"],
        audio_enabled=cfg.audio["enabled"],
        audio_device=cfg.audio["device"],
        audio_codec=cfg.audio["codec"],
        audio_bitrate_kbps=cfg.audio["bitrate_kbps"],
        max_buffer_sec=cfg.buffer["max_sec"],
        reduce_threshold_sec=cfg.buffer["reduce_threshold_sec"],
        min_bitrate_kbps=cfg.buffer["min_bitrate_kbps"],
        step_kbps=cfg.buffer["step_kbps"],
        recovery_pause_sec=cfg.buffer["recovery_pause_sec"],
        window_title=window_title,
        window_pid=window_pid,
        ffmpeg_path=ffmpeg_path,
        stream_enabled=stream_enabled,
        record_path=record_path,
        keyframe_interval_sec=cfg.get("record", "keyframe_interval_sec", default=2),
    )


class App:
    def __init__(self):
        self._cfg = Config()
        self._logger: EventLogger | None = None
        self._capture: CaptureManager | None = None
        self._proc_audio: ProcessAudioCapture | None = None
        self._win_capture: WindowCapturePipe | None = None
        self._current_game: GameProcess | None = None
        self._focus_lost_at: float | None = None

        # Recording state
        self._is_streaming: bool = False
        self._is_recording: bool = False
        self._current_record_path: str = ""

        self._qt_app = QApplication(sys.argv)
        self._qt_app.setQuitOnLastWindowClosed(False)

        self._window = MainWindow(
            config=self._cfg,
            on_start_stream=self._user_start_stream,
            on_stop_stream=self._user_stop_stream,
            on_start_recording=self._user_start_recording,
            on_stop_recording=self._user_stop_recording,
            on_open_recordings_folder=self._open_recordings_folder,
            on_settings_saved=self._on_settings_saved,
            on_game_changed=self._on_game_changed,
            on_channel_changed=self._on_channel_changed,
            on_check_update=self._user_check_update,
            on_quit=self._quit,
        )

        self._tray = TrayIcon(
            on_open=self._window.show,
            on_stop_stream=self._user_stop_stream,
            on_quit=self._quit,
        )
        self._tray.start()

        self._process_monitor = self._make_process_monitor()
        self._process_monitor.start()

        # Check for updates in background 3 s after startup
        t = threading.Timer(3.0, self._check_update)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------
    # Process monitor factory
    # ------------------------------------------------------------------

    def _make_process_monitor(self) -> ProcessMonitor:
        names = self._cfg.active_process_names
        return ProcessMonitor(
            process_names=names,
            poll_interval=3.0,
            on_game_found=self._on_game_found,
            on_game_lost=self._on_game_lost,
        )

    # ------------------------------------------------------------------
    # Process monitor callbacks
    # ------------------------------------------------------------------

    def _on_game_found(self, proc: GameProcess) -> None:
        self._current_game = proc
        self._window.notify_game_status(
            f"{proc.name} обнаружена (PID {proc.pid})", found=True
        )

    def _on_game_lost(self, proc: GameProcess) -> None:
        self._current_game = None
        self._window.notify_game_status("Ожидание...", found=False)

        if self._is_recording:
            self._user_stop_recording()
        if self._is_streaming:
            self._stop_stream(reason="game_closed")

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def _user_start_stream(self) -> None:
        pid   = self._current_game.pid if self._current_game else 0
        title = (_get_window_title(pid) or self._current_game.window_title) if self._current_game else ""
        self._start_stream(title, pid)

    def _user_stop_stream(self) -> None:
        if self._capture and self._capture.is_running:
            self._stop_stream(reason="user_stopped")

    def _log_platform_info(self) -> None:
        """Log platform diagnostics at the start of every session."""
        if not self._logger:
            return
        try:
            win_build = sys.getwindowsversion().build if sys.platform == "win32" else 0
        except Exception:
            win_build = 0
        from src.wgc_capture import is_wgc_supported
        self._logger.log(
            "platform_info",
            python=sys.version.split()[0],
            windows_build=win_build,
            wgc_supported=is_wgc_supported(),
            process_loopback_supported=is_process_loopback_supported(),
        )

    def _make_record_path(self) -> str:
        """Return a timestamped MP4 path in the configured recordings folder."""
        folder_cfg = self._cfg.get("record", "folder", default="")
        if folder_cfg:
            folder = Path(folder_cfg)
        else:
            folder = self._cfg._data_dir / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(folder / f"rec_{ts}.mp4")

    def _launch_capture(self, window_title: str, window_pid: int) -> None:
        """(Re)create and start CaptureManager with current stream+record state."""
        cap_cfg = _make_capture_config(
            self._cfg,
            window_title,
            window_pid,
            stream_enabled=self._is_streaming,
            record_path=self._current_record_path,
        )
        # Wire up previously opened AV pipes
        if hasattr(self, "_pending_audio_pipe"):
            cap_cfg.audio_pipe_name        = self._pending_audio_pipe
            cap_cfg.audio_pipe_sample_rate = getattr(self, "_pending_audio_sample_rate", 48000)
            cap_cfg.audio_pipe_channels    = getattr(self, "_pending_audio_channels", 2)
        if self._win_capture:
            cap_cfg.video_pipe_name = self._win_capture.pipe_name
            cap_cfg.video_width     = self._win_capture.width
            cap_cfg.video_height    = self._win_capture.height

        if self._capture:
            self._capture.stop()
        self._capture = CaptureManager(
            cfg=cap_cfg,
            on_stats=self._window.notify_stats,
            on_bitrate_change=self._on_bitrate_change,
            on_error=self._on_capture_error,
            on_stopped=self._on_capture_stopped,
        )
        self._capture.start()

    def _start_stream(self, window_title: str = "", window_pid: int = 0) -> None:
        self._stream_window_title = window_title
        self._stream_window_pid   = window_pid

        if self._logger:
            self._logger.close()
        self._logger = EventLogger(
            output_dir=self._cfg.log_dir,
            stream_key=self._cfg.stream.get("key", ""),
        )
        self._logger.add_listener(self._window.notify_event)
        self._window.notify_log_path(str(self._logger.log_path))
        self._log_platform_info()

        # ── Per-window video capture ──────────────────────────────────────────
        if window_pid and not self._win_capture:
            try:
                self._win_capture = make_window_capture(
                    pid=window_pid,
                    fps=self._cfg.video["fps"],
                    on_focus_change=self._on_game_focus_change,
                )
                self._win_capture.start()
                capture_type = (
                    "wgc" if type(self._win_capture).__name__ == "WgcCapturePipe"
                    else "bitblt"
                )
                self._logger.log(
                    "video_source",
                    mode="window_capture",
                    capture_type=capture_type,
                    pid=window_pid,
                    size=f"{self._win_capture.width}x{self._win_capture.height}",
                )
            except Exception as exc:
                self._logger.log("video_source", mode="gdigrab_fallback", reason=str(exc))
                self._win_capture = None

        # ── Per-process audio capture (Windows 10 2004+) ─────────────────────
        self._pending_audio_pipe = ""
        self._pending_audio_sample_rate = 48000
        self._pending_audio_channels = 2
        if window_pid and is_process_loopback_supported() and not self._proc_audio:
            try:
                self._proc_audio = ProcessAudioCapture(pid=window_pid)
                self._proc_audio.start()
                self._pending_audio_pipe = self._proc_audio.pipe_name
                self._pending_audio_sample_rate = self._proc_audio.sample_rate
                self._pending_audio_channels = self._proc_audio.channels
                self._logger.log(
                    "audio_source", mode="process_loopback", pid=window_pid,
                    sample_rate=self._proc_audio.sample_rate,
                    channels=self._proc_audio.channels,
                )
            except Exception as exc:
                self._logger.log(
                    "audio_source", mode="process_loopback_failed",
                    pid=window_pid, error=str(exc),
                )
                self._proc_audio = None
        elif not is_process_loopback_supported():
            self._logger.log(
                "audio_source", mode="process_loopback_unavailable",
                reason="win10_2004_required",
            )

        self._is_streaming = True
        self._launch_capture(window_title, window_pid)

        self._logger.log(
            "capture_source",
            window_title=window_title or "(primary monitor)",
            pid=window_pid,
        )
        self._logger.mark_stream_start(
            key=self._cfg.stream.get("key", ""),
            bitrate=self._cfg.video["bitrate_kbps"],
        )
        self._window.notify_stream_status(True)
        self._tray.set_state(TrayState.LIVE)

    def _stop_stream(self, reason: str = "unknown") -> None:
        self._is_streaming = False

        if self._is_recording:
            # Keep FFmpeg running for recording, restart without streaming
            self._launch_capture(
                getattr(self, "_stream_window_title", ""),
                getattr(self, "_stream_window_pid", 0),
            )
        else:
            if self._capture:
                self._capture.stop()
                self._capture = None
            if self._win_capture:
                self._win_capture.stop()
                self._win_capture = None
            if self._proc_audio:
                self._proc_audio.stop()
                self._proc_audio = None

        if self._logger:
            self._logger.mark_stream_end(reason=reason)
            if not self._is_recording:
                self._logger.close()
                self._logger = None

        self._window.notify_stream_status(False)
        self._tray.set_state(TrayState.IDLE)

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def _user_start_recording(self) -> None:
        self._current_record_path = self._make_record_path()
        self._is_recording = True

        pid   = getattr(self, "_stream_window_pid", 0) or (self._current_game.pid if self._current_game else 0)
        title = getattr(self, "_stream_window_title", "")

        if self._is_streaming:
            # Restart FFmpeg with tee muxer (stream + record)
            self._launch_capture(title, pid)
        else:
            # Recording only — need AV capture infrastructure if not already up
            if not self._logger:
                self._logger = EventLogger(
                    output_dir=self._cfg.log_dir,
                    stream_key=self._cfg.stream.get("key", ""),
                )
                self._logger.add_listener(self._window.notify_event)
                self._window.notify_log_path(str(self._logger.log_path))
                self._log_platform_info()

            if pid and not self._win_capture:
                try:
                    self._win_capture = make_window_capture(
                        pid=pid,
                        fps=self._cfg.video["fps"],
                        on_focus_change=self._on_game_focus_change,
                    )
                    self._win_capture.start()
                    self._stream_window_pid   = pid
                    self._stream_window_title = title
                    capture_type = (
                        "wgc" if type(self._win_capture).__name__ == "WgcCapturePipe"
                        else "bitblt"
                    )
                    self._logger.log(
                        "video_source",
                        mode="window_capture",
                        capture_type=capture_type,
                        pid=pid,
                        size=f"{self._win_capture.width}x{self._win_capture.height}",
                    )
                except Exception as exc:
                    self._logger.log("video_source", mode="gdigrab_fallback", reason=str(exc))
                    self._win_capture = None

            self._pending_audio_pipe = ""
            self._pending_audio_sample_rate = 48000
            self._pending_audio_channels = 2
            if pid and is_process_loopback_supported() and not self._proc_audio:
                try:
                    self._proc_audio = ProcessAudioCapture(pid=pid)
                    self._proc_audio.start()
                    self._pending_audio_pipe = self._proc_audio.pipe_name
                    self._pending_audio_sample_rate = self._proc_audio.sample_rate
                    self._pending_audio_channels = self._proc_audio.channels
                    self._logger.log(
                        "audio_source", mode="process_loopback", pid=pid,
                        sample_rate=self._proc_audio.sample_rate,
                        channels=self._proc_audio.channels,
                    )
                except Exception as exc:
                    self._logger.log(
                        "audio_source", mode="process_loopback_failed",
                        pid=pid, error=str(exc),
                    )
                    self._proc_audio = None

            self._launch_capture(title, pid)

        if self._logger:
            self._logger.log("record_start", path=self._current_record_path)

        self._window.notify_recording_status(True)

    def _user_stop_recording(self) -> None:
        self._is_recording = False
        old_path = self._current_record_path
        self._current_record_path = ""

        if self._is_streaming:
            # Keep FFmpeg running, restart without record
            self._launch_capture(
                getattr(self, "_stream_window_title", ""),
                getattr(self, "_stream_window_pid", 0),
            )
        else:
            if self._capture:
                self._capture.stop()
                self._capture = None
            if self._win_capture:
                self._win_capture.stop()
                self._win_capture = None
            if self._proc_audio:
                self._proc_audio.stop()
                self._proc_audio = None

        if self._logger:
            self._logger.log("record_stop", path=old_path)
            if not self._is_streaming:
                self._logger.close()
                self._logger = None

        self._window.notify_recording_status(False)

    def _open_recordings_folder(self) -> None:
        folder_cfg = self._cfg.get("record", "folder", default="")
        if folder_cfg:
            folder = Path(folder_cfg)
        else:
            folder = self._cfg._data_dir / "recordings"
        folder.mkdir(parents=True, exist_ok=True)
        import platform
        system = platform.system()
        if system == "Windows":
            subprocess.Popen(["explorer", str(folder)])
        elif system == "Darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_bitrate_change(self, from_kbps: int, to_kbps: int, reason: str) -> None:
        if self._logger:
            self._logger.log(
                "bitrate_reduced" if to_kbps < from_kbps else "bitrate_recovered",
                **{"from": from_kbps, "to": to_kbps, "reason": reason},
            )

    def _on_capture_error(self, message: str) -> None:
        if self._logger:
            self._logger.log("capture_error", message=message)
        self._is_streaming = False
        self._is_recording = False
        self._current_record_path = ""
        self._window.notify_stream_status(False)
        self._window.notify_recording_status(False)
        self._tray.set_state(TrayState.ERROR)

    def _on_capture_stopped(self) -> None:
        was_recording = self._is_recording
        self._is_streaming = False
        self._is_recording = False
        self._current_record_path = ""
        self._window.notify_stream_status(False)
        if was_recording:
            self._window.notify_recording_status(False)

    def _on_game_changed(self, game_name: str) -> None:
        """User picked a different game from the dropdown."""
        self._process_monitor.stop()
        self._current_game = None
        self._process_monitor = self._make_process_monitor()
        self._process_monitor.start()

    def _on_settings_saved(self) -> None:
        self._process_monitor.stop()
        self._process_monitor = self._make_process_monitor()
        self._process_monitor.start()

    # ------------------------------------------------------------------

    def _on_game_focus_change(self, is_focused: bool) -> None:
        """Called from the WindowCapturePipe thread on every focus transition."""
        if not self._logger:
            return
        now = time.monotonic()
        if is_focused:
            away_sec = round(now - self._focus_lost_at, 1) if self._focus_lost_at is not None else 0.0
            self._logger.log("focus_gained", away_sec=away_sec)
            self._focus_lost_at = None
        else:
            self._focus_lost_at = now
            self._logger.log("focus_lost")

    def _check_update(self) -> None:
        channel = self._cfg.get("update", "channel", default="stable")
        info = check_for_update(channel=channel)
        if info:
            skipped = self._cfg.get("update", "skipped_version", default="")
            if info.version != skipped:
                self._window.notify_update_available(info)

    def _user_check_update(self) -> None:
        """Called when user manually clicks the update button."""
        def _run():
            channel = self._cfg.get("update", "channel", default="stable")
            info = check_for_update(channel=channel)
            if info:
                # Reset skipped version so the banner always shows on manual check
                self._cfg.set("update", "skipped_version", "")
                self._window.notify_update_available(info)
            self._window.notify_check_update_done(info is not None)
        t = threading.Thread(target=_run, daemon=True, name="UpdateCheckManual")
        t.start()

    def _on_channel_changed(self, channel: str) -> None:
        """Called when user switches update channel in settings."""
        # Reset skipped version so the new channel's release isn't suppressed
        self._cfg.set("update", "skipped_version", "")
        self._cfg.save()
        # Trigger immediate check in background
        t = threading.Thread(target=self._check_update, daemon=True, name="UpdateCheck")
        t.start()

    def _quit(self) -> None:
        if self._is_recording:
            self._user_stop_recording()
        if self._is_streaming:
            self._stop_stream(reason="app_quit")
        elif self._capture and self._capture.is_running:
            self._capture.stop()
            self._capture = None
        if self._win_capture:
            self._win_capture.stop()
            self._win_capture = None
        if self._proc_audio:
            self._proc_audio.stop()
            self._proc_audio = None
        self._process_monitor.stop()
        self._tray.stop()
        self._qt_app.quit()

    def run(self) -> int:
        self._window.show()
        return self._qt_app.exec()


def main() -> None:
    if "--enable-stereo-mix" in sys.argv:
        from src.audio_utils import enable_stereo_mix
        ok, _msg = enable_stereo_mix()
        sys.exit(0 if ok else 1)

    app = App()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
