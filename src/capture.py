"""FFmpeg-based capture and streaming manager with adaptive bitrate control."""
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class CaptureStats:
    bitrate_kbps: int = 0
    speed: float = 0.0
    fps: float = 0.0
    buffer_sec: float = 0.0  # estimated buffer fill
    is_live: bool = False
    uptime_sec: float = 0.0


@dataclass
class CaptureConfig:
    # Stream
    host: str = "stream.oldenleague.ru"
    key: str = ""
    protocol: str = "srt"       # rtmp | srt
    srt_latency_ms: int = 3000
    # Video
    fps: int = 60
    resolution: str = "1920x1080"
    bitrate_kbps: int = 6000
    preset: str = "medium"
    tune: str = "film"
    # Audio
    audio_enabled: bool = True
    audio_device: str = ""
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 192
    # Per-process audio pipe (set by ProcessAudioCapture; overrides dshow when non-empty)
    audio_pipe_name: str = ""
    # Buffer / adaptive
    max_buffer_sec: float = 30.0
    reduce_threshold_sec: float = 15.0
    min_bitrate_kbps: int = 2000
    step_kbps: int = 500
    recovery_pause_sec: float = 30.0
    # Window capture
    window_title: str = ""
    window_pid: int = 0
    # Raw video pipe from WindowCapturePipe (set by App._start_stream when PID known)
    video_pipe_name: str = ""
    video_width: int = 0
    video_height: int = 0
    # FFmpeg binary path
    ffmpeg_path: str = "ffmpeg"


def _build_ffmpeg_cmd(cfg: CaptureConfig, bitrate_kbps: int) -> list[str]:
    """Build the FFmpeg argument list for capture + encode + stream."""
    system = platform.system()
    # -loglevel error: suppress info/warning noise; errors still go to stderr
    cmd = [cfg.ffmpeg_path, "-hide_banner", "-loglevel", "error"]

    # ---- Video input ----
    # IMPORTANT: all input options (-framerate, -offset_x, etc.) MUST come
    # before -i for FFmpeg demuxers.
    #
    # Strategy on Windows:
    #   • video_pipe_name set → rawvideo from WindowCapturePipe (PrintWindow-based).
    #     PrintWindow captures the window's own framebuffer, independent of z-order —
    #     same approach as OBS "BitBlt" window capture.
    #   • fallback → gdigrab limited to the primary monitor.
    vf_parts: list[str] = []  # video filter chain built up below

    if system == "Windows":
        if cfg.video_pipe_name and cfg.video_width and cfg.video_height:
            # Raw BGRx frames from WindowCapturePipe.
            cmd += [
                "-f", "rawvideo",
                "-pix_fmt", "bgr0",
                "-s", f"{cfg.video_width}x{cfg.video_height}",
                "-r", str(cfg.fps),
                "-i", cfg.video_pipe_name,
            ]
        else:
            # Fallback: gdigrab on primary monitor (no game window found yet).
            try:
                import ctypes
                w = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
                h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
                cmd += ["-f", "gdigrab", "-framerate", str(cfg.fps),
                        "-offset_x", "0", "-offset_y", "0",
                        "-video_size", f"{w}x{h}", "-i", "desktop"]
            except Exception:
                cmd += ["-f", "gdigrab", "-framerate", str(cfg.fps), "-i", "desktop"]
    elif system == "Darwin":
        # avfoundation: screen index 1, no audio here (added separately)
        cmd += [
            "-f", "avfoundation",
            "-framerate", str(cfg.fps),
            "-capture_cursor", "0",
            "-i", "1:none",
        ]
    else:
        # Linux fallback (x11grab)
        cmd += ["-f", "x11grab", "-r", str(cfg.fps), "-i", ":0.0"]

    # ---- Audio input ----
    # Priority 1: per-process loopback pipe (ProcessAudioCapture)
    # Priority 2: dshow device (Stereo Mix / selected device)
    # macOS: avfoundation virtual device
    audio_active = False
    if cfg.audio_pipe_name:
        # Raw PCM float32 LE from our Python WASAPI capture thread
        cmd += ["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", cfg.audio_pipe_name]
        audio_active = True
    elif cfg.audio_enabled and cfg.audio_device:
        if system == "Windows":
            cmd += ["-f", "dshow", "-i", f"audio={cfg.audio_device}"]
            audio_active = True
        elif system == "Darwin":
            cmd += ["-f", "avfoundation", "-i", f"none:{cfg.audio_device}"]
            audio_active = True

    # ---- Video encoding ----
    bufsize = bitrate_kbps * 2
    if cfg.resolution and cfg.resolution != "source":
        vf_parts.append(f"scale={cfg.resolution.replace('x', ':')}")
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
    cmd += [

        "-c:v", "libx264",
        "-preset", cfg.preset,
        "-tune", cfg.tune,
        "-b:v", f"{bitrate_kbps}k",
        "-minrate", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps}k",
        "-bufsize", f"{bufsize}k",
        "-pix_fmt", "yuv420p",
        "-g", str(cfg.fps * 2),  # keyframe every 2 seconds
    ]

    # ---- Audio encoding ----
    if audio_active:
        cmd += [
            "-c:a", cfg.audio_codec,
            "-b:a", f"{cfg.audio_bitrate_kbps}k",
            "-ar", "44100",
        ]
    else:
        cmd += ["-an"]

    # ---- Progress reporting ----
    cmd += ["-progress", "pipe:2", "-stats_period", "1"]

    # ---- Output / stream URL ----
    if cfg.protocol == "rtmp":
        url = f"rtmp://{cfg.host}:1935/live/{cfg.key}"
        cmd += ["-f", "flv", url]
    else:
        url = (
            f"srt://{cfg.host}:8890"
            f"?streamid=publish:live/{cfg.key}&latency={cfg.srt_latency_ms}"
        )
        cmd += ["-f", "mpegts", url]

    return cmd


_PROGRESS_RE = re.compile(
    r"(?:bitrate=\s*(?P<bitrate>[\d.]+)kbits/s)"
    r"|(?:speed=\s*(?P<speed>[\d.]+)x)"
    r"|(?:fps=\s*(?P<fps>[\d.]+))"
)


def _parse_progress_line(line: str) -> dict:
    """Extract metrics from a single FFmpeg progress/stats line."""
    result = {}
    for m in _PROGRESS_RE.finditer(line):
        if m.group("bitrate"):
            result["bitrate_kbps"] = float(m.group("bitrate"))
        if m.group("speed"):
            result["speed"] = float(m.group("speed"))
        if m.group("fps"):
            result["fps"] = float(m.group("fps"))
    return result


class CaptureManager:
    """
    Manages the FFmpeg subprocess lifecycle and adaptive bitrate control.

    Callbacks:
      on_stats(stats: CaptureStats)          — called ~every second with live metrics
      on_bitrate_change(from_kbps, to_kbps, reason)
      on_error(message: str)
      on_stopped()
    """

    def __init__(
        self,
        cfg: CaptureConfig,
        on_stats: Callable[[CaptureStats], None] | None = None,
        on_bitrate_change: Callable[[int, int, str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_stopped: Callable[[], None] | None = None,
    ):
        self._cfg = cfg
        self._on_stats = on_stats
        self._on_bitrate_change = on_bitrate_change
        self._on_error = on_error
        self._on_stopped = on_stopped

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._current_bitrate = cfg.bitrate_kbps
        self._start_time: float = 0.0

        # Buffer / recovery state
        self._buffer_sec: float = 0.0
        self._speed_history: list[tuple[float, float]] = []  # (timestamp, speed)
        self._low_buffer_since: float | None = None
        self._last_recovery_step: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_bitrate_kbps(self) -> int:
        return self._current_bitrate

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._current_bitrate = self._cfg.bitrate_kbps
        self._start_time = time.monotonic()
        self._launch_ffmpeg()

    def stop(self) -> None:
        self._running = False
        self._terminate_ffmpeg()

    def update_config(self, cfg: CaptureConfig) -> None:
        """Hot-update config (applied on next FFmpeg restart)."""
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _launch_ffmpeg(self) -> None:
        cmd = _build_ffmpeg_cmd(self._cfg, self._current_bitrate)
        kwargs: dict = dict(
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError:
            if self._on_error:
                self._on_error(f"FFmpeg не найден: {self._cfg.ffmpeg_path}")
            self._running = False
            return

        t = threading.Thread(target=self._reader_loop, daemon=True, name="FFmpegReader")
        t.start()

    def _terminate_ffmpeg(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = None

    def _restart_with_bitrate(self, new_bitrate: int, reason: str) -> None:
        old = self._current_bitrate
        self._current_bitrate = max(new_bitrate, self._cfg.min_bitrate_kbps)
        if self._on_bitrate_change:
            self._on_bitrate_change(old, self._current_bitrate, reason)
        self._terminate_ffmpeg()
        if self._running:
            self._launch_ffmpeg()

    def _reader_loop(self) -> None:
        """Read FFmpeg stderr, parse progress, drive adaptive logic."""
        proc = self._proc
        if not proc or not proc.stderr:
            return

        error_lines: list[str] = []

        for line in proc.stderr:
            if not self._running:
                break
            stripped = line.strip()
            metrics = _parse_progress_line(stripped)
            if metrics:
                self._process_metrics(metrics)
            elif stripped:
                # Collect non-progress lines as potential error output
                error_lines.append(stripped)

        # Process exited
        proc.wait()
        if self._running:
            # Unexpected exit — report last stderr lines for diagnosis
            detail = " | ".join(error_lines[-3:]) if error_lines else ""
            msg = f"FFmpeg завершился с кодом {proc.returncode}"
            if detail:
                msg += f": {detail}"
            if self._on_error:
                self._on_error(msg)
            self._running = False

        if self._on_stopped:
            self._on_stopped()

    def _process_metrics(self, metrics: dict) -> None:
        now = time.monotonic()
        speed = metrics.get("speed", 1.0)

        # Track speed history for buffer estimation
        self._speed_history.append((now, speed))
        # Keep only last 60 seconds
        cutoff = now - 60.0
        self._speed_history = [(t, s) for t, s in self._speed_history if t > cutoff]

        # Estimate buffer: accumulate only when speed is genuinely below 0.95
        # (normal FFmpeg speed fluctuates around 1.0; ignore small deviations)
        if len(self._speed_history) >= 2:
            t0, s0 = self._speed_history[-2]
            t1, s1 = self._speed_history[-1]
            dt = t1 - t0
            avg_speed = (s0 + s1) / 2.0
            if avg_speed < 0.95:
                delta = (1.0 - avg_speed) * dt  # positive = buffer growing
            else:
                delta = -dt * 0.1  # slowly drain buffer when speed is OK
            self._buffer_sec = max(0.0, self._buffer_sec + delta)

        stats = CaptureStats(
            bitrate_kbps=int(metrics.get("bitrate_kbps", self._current_bitrate)),
            speed=speed,
            fps=metrics.get("fps", 0.0),
            buffer_sec=self._buffer_sec,
            is_live=True,
            uptime_sec=now - self._start_time,
        )
        if self._on_stats:
            self._on_stats(stats)

        self._adaptive_step(now)

    def _adaptive_step(self, now: float) -> None:
        buf = self._buffer_sec
        cfg = self._cfg

        if buf >= cfg.max_buffer_sec:
            # Emergency: drop to minimum immediately
            if self._current_bitrate > cfg.min_bitrate_kbps:
                self._restart_with_bitrate(cfg.min_bitrate_kbps, "buffer_max")
                self._buffer_sec = 0.0
        elif buf >= 25.0:
            # Critical: immediate step down
            new_br = self._current_bitrate - cfg.step_kbps
            if new_br >= cfg.min_bitrate_kbps:
                self._restart_with_bitrate(new_br, "buffer_25s")
                self._buffer_sec = 0.0
        elif buf >= cfg.reduce_threshold_sec:
            # High: step down
            new_br = self._current_bitrate - cfg.step_kbps
            if new_br >= cfg.min_bitrate_kbps:
                self._restart_with_bitrate(new_br, "buffer_15s")
                self._buffer_sec = 0.0
        elif buf < 5.0:
            # Buffer is low — start recovery timer
            if self._low_buffer_since is None:
                self._low_buffer_since = now
            elif (
                now - self._low_buffer_since >= 60.0
                and self._current_bitrate < cfg.bitrate_kbps
                and now - self._last_recovery_step >= cfg.recovery_pause_sec
            ):
                new_br = min(self._current_bitrate + cfg.step_kbps, cfg.bitrate_kbps)
                self._restart_with_bitrate(new_br, "recovery")
                self._last_recovery_step = now
        else:
            # Buffer between 5–15: reset recovery timer
            self._low_buffer_since = None
