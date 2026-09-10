"""Main application entry point — Game Capture Agent."""
import sys
import threading
import time

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
from src.window_capture import WindowCapturePipe


def _make_capture_config(cfg: Config, window_title: str = "", window_pid: int = 0) -> CaptureConfig:
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
    )


class App:
    def __init__(self):
        self._cfg = Config()
        self._logger: EventLogger | None = None
        self._capture: CaptureManager | None = None
        self._proc_audio: ProcessAudioCapture | None = None
        self._win_capture: WindowCapturePipe | None = None
        self._current_game: GameProcess | None = None
        self._startup_timer: threading.Timer | None = None
        self._focus_lost_at: float | None = None

        self._qt_app = QApplication(sys.argv)
        self._qt_app.setQuitOnLastWindowClosed(False)

        self._window = MainWindow(
            config=self._cfg,
            on_start_stream=self._user_start_stream,
            on_stop_stream=self._user_stop_stream,
            on_settings_saved=self._on_settings_saved,
            on_game_changed=self._on_game_changed,
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

        # Check for updates in background 30 s after startup
        t = threading.Timer(30.0, self._check_update)
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
        self._tray.set_state(TrayState.LIVE)

        delay = self._cfg.game.get("startup_delay_sec", 5)
        if delay > 0:
            self._startup_timer = threading.Timer(delay, self._auto_start_stream, args=(proc,))
            self._startup_timer.daemon = True
            self._startup_timer.start()
        else:
            self._auto_start_stream(proc)

    def _on_game_lost(self, proc: GameProcess) -> None:
        self._current_game = None
        self._window.notify_game_status("Ожидание...", found=False)

        if self._startup_timer:
            self._startup_timer.cancel()
            self._startup_timer = None

        if self._capture and self._capture.is_running:
            self._stop_stream(reason="game_closed")

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def _auto_start_stream(self, proc: GameProcess) -> None:
        if self._capture and self._capture.is_running:
            return
        key  = self._cfg.stream.get("key", "").strip()
        host = self._cfg.stream.get("host", "").strip()
        if not key or not host:
            self._window.notify_game_status(
                f"{proc.name} обнаружена — укажите ключ и сервер для автозапуска",
                found=True,
            )
            return
        # Re-query title — game window may not have been ready at detection time
        title = _get_window_title(proc.pid) or proc.window_title
        self._start_stream(title, proc.pid)

    def _user_start_stream(self) -> None:
        title = self._current_game.window_title if self._current_game else ""
        pid   = self._current_game.pid if self._current_game else 0
        self._start_stream(title, pid)

    def _user_stop_stream(self) -> None:
        if self._capture and self._capture.is_running:
            self._stop_stream(reason="user_stopped")

    def _start_stream(self, window_title: str = "", window_pid: int = 0) -> None:
        if self._logger:
            self._logger.close()
        self._logger = EventLogger(
            output_dir=self._cfg.log_dir,
            stream_key=self._cfg.stream.get("key", ""),
        )
        self._logger.add_listener(self._window.notify_event)

        # ── Per-window video capture (PrintWindow, z-order independent) ────────
        # If the game PID is known, start a WindowCapturePipe so that FFmpeg reads
        # raw BGRx frames that come directly from the game window's framebuffer —
        # not from screen coordinates.  Falls back to gdigrab desktop if it fails.
        if window_pid:
            try:
                self._win_capture = WindowCapturePipe(
                    pid=window_pid,
                    fps=self._cfg.video["fps"],
                    on_focus_change=self._on_game_focus_change,
                )
                self._win_capture.start()
                self._logger.log(
                    "video_source", mode="window_capture",
                    pid=window_pid,
                    size=f"{self._win_capture.width}x{self._win_capture.height}",
                )
            except Exception as exc:
                self._logger.log("video_source", mode="gdigrab_fallback", reason=str(exc))
                self._win_capture = None

        # ── Per-process audio capture (Windows 10 2004+) ─────────────────────
        audio_pipe = ""
        if window_pid and is_process_loopback_supported():
            try:
                self._proc_audio = ProcessAudioCapture(pid=window_pid)
                self._proc_audio.start()
                audio_pipe = self._proc_audio.pipe_name
                self._logger.log("audio_source", mode="process_loopback", pid=window_pid)
            except Exception as exc:
                self._logger.log("audio_source", mode="fallback", reason=str(exc))
                self._proc_audio = None

        cap_cfg = _make_capture_config(self._cfg, window_title, window_pid)
        cap_cfg.audio_pipe_name = audio_pipe
        if self._win_capture:
            cap_cfg.video_pipe_name = self._win_capture.pipe_name
            cap_cfg.video_width     = self._win_capture.width
            cap_cfg.video_height    = self._win_capture.height

        self._capture = CaptureManager(
            cfg=cap_cfg,
            on_stats=self._window.notify_stats,
            on_bitrate_change=self._on_bitrate_change,
            on_error=self._on_capture_error,
            on_stopped=self._on_capture_stopped,
        )
        self._capture.start()

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
            self._logger.close()
            self._logger = None

        self._window.notify_stream_status(False)
        self._tray.set_state(TrayState.IDLE)

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
        self._window.notify_stream_status(False)
        self._tray.set_state(TrayState.ERROR)

    def _on_capture_stopped(self) -> None:
        self._window.notify_stream_status(False)

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
        info = check_for_update()
        if info:
            skipped = self._cfg.get("update", "skipped_version", default="")
            if info.version != skipped:
                self._window.notify_update_available(info)

    def _quit(self) -> None:
        if self._capture and self._capture.is_running:
            self._stop_stream(reason="app_quit")
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
