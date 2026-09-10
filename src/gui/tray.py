"""System tray icon using pystray."""
import threading
from enum import Enum
from typing import Callable

from PIL import Image, ImageDraw

try:
    import pystray  # type: ignore
    _PYSTRAY_AVAILABLE = True
except ImportError:
    _PYSTRAY_AVAILABLE = False


class TrayState(Enum):
    IDLE = "idle"       # grey — waiting for game
    LIVE = "live"       # green pulsing — streaming
    ERROR = "error"     # red — error


def _make_icon_image(color: tuple[int, int, int], size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(*color, 255),
    )
    return img


_COLORS = {
    TrayState.IDLE: (120, 120, 120),
    TrayState.LIVE: (60, 200, 60),
    TrayState.ERROR: (220, 50, 50),
}


class TrayIcon:
    """System tray icon with state-based color and context menu."""

    def __init__(
        self,
        on_open: Callable[[], None] | None = None,
        on_stop_stream: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ):
        self._on_open = on_open
        self._on_stop_stream = on_stop_stream
        self._on_quit = on_quit
        self._icon: "pystray.Icon | None" = None
        self._state = TrayState.IDLE
        self._pulse_thread: threading.Thread | None = None
        self._pulse_running = False

    def start(self) -> None:
        if not _PYSTRAY_AVAILABLE:
            return
        self._icon = pystray.Icon(
            "game_capture",
            _make_icon_image(_COLORS[TrayState.IDLE]),
            "OldenLeague Game Capture",
            menu=self._build_menu(),
        )
        t = threading.Thread(target=self._icon.run, daemon=True, name="TrayIcon")
        t.start()

    def stop(self) -> None:
        self._pulse_running = False
        if self._icon:
            self._icon.stop()

    def set_state(self, state: TrayState) -> None:
        self._state = state
        self._pulse_running = False

        if not self._icon:
            return

        if state == TrayState.LIVE:
            self._start_pulse()
        else:
            color = _COLORS[state]
            self._icon.icon = _make_icon_image(color)

    def _start_pulse(self) -> None:
        self._pulse_running = True

        def _pulse():
            bright = _COLORS[TrayState.LIVE]
            dim = (30, 100, 30)
            toggle = True
            while self._pulse_running:
                color = bright if toggle else dim
                if self._icon:
                    self._icon.icon = _make_icon_image(color)
                toggle = not toggle
                threading.Event().wait(0.8)

        self._pulse_thread = threading.Thread(target=_pulse, daemon=True, name="TrayPulse")
        self._pulse_thread.start()

    def _build_menu(self) -> "pystray.Menu":
        return pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self._on_open and self._on_open()),
            pystray.MenuItem("Остановить стрим", lambda: self._on_stop_stream and self._on_stop_stream()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выйти", lambda: self._on_quit and self._on_quit()),
        )
