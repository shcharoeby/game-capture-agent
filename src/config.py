"""Config loading and saving via YAML."""
import os
import copy
import platform
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Known games catalogue
# Each entry: display_name -> {platform: [exe_names]}
# On non-Windows the .exe suffix is stripped automatically by process_monitor.
# ---------------------------------------------------------------------------
KNOWN_GAMES: dict[str, dict[str, list[str]]] = {
    "Heroes of Might and Magic: Olden Era": {
        "windows": ["HeroesOldenEra.exe"],
        "darwin":  ["HeroesOldenEra"],
        "linux":   ["HeroesOldenEra"],
    },
}


def process_names_for_game(game_name: str) -> list[str]:
    """Return the process name list for the current OS given a game name."""
    entry = KNOWN_GAMES.get(game_name)
    if not entry:
        return []
    sys_key = platform.system().lower()  # "windows" | "darwin" | "linux"
    return list(entry.get(sys_key, entry.get("windows", [])))


def get_data_dir() -> Path:
    """
    Returns the platform-appropriate user-data directory.

    Windows : %APPDATA%\\GameCapture        (C:\\Users\\...\\AppData\\Roaming\\GameCapture)
    macOS   : ~/Library/Application Support/GameCapture
    Linux   : ~/.config/GameCapture
    """
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "GameCapture"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "GameCapture"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "GameCapture"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_GAME = "Heroes of Might and Magic: Olden Era"

DEFAULTS: dict[str, Any] = {
    "stream": {
        "key": "",
        "host": "stream.oldenleague.ru",
        "protocol": "srt",
        "srt_latency_ms": 3000,
    },
    "video": {
        "fps": 60,
        "resolution": "1920x1080",
        "bitrate_kbps": 6000,
        "preset": "medium",
        "tune": "film",
    },
    "audio": {
        "enabled": True,
        "device": "",
        "codec": "aac",
        "bitrate_kbps": 192,
    },
    "buffer": {
        "max_sec": 30,
        "reduce_threshold_sec": 15,
        "min_bitrate_kbps": 2000,
        "step_kbps": 500,
        "recovery_pause_sec": 30,
    },
    "game": {
        "selected": _DEFAULT_GAME,
        "startup_delay_sec": 5,
    },
    "log": {
        "show_in_ui_last_n": 20,
    },
    "update": {
        "skipped_version": "",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively, returning new dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Application configuration backed by a YAML file in the user data directory."""

    def __init__(self, path: str | Path | None = None):
        self._data_dir = get_data_dir()

        if path is None:
            path = self._data_dir / "config.yaml"
        self.path = Path(path)
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load config from disk, filling missing keys with defaults."""
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                on_disk = yaml.safe_load(f) or {}
            self._data = _deep_merge(DEFAULTS, on_disk)
        else:
            self._data = copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        """Persist current config to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, allow_unicode=True, default_flow_style=False)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def stream(self) -> dict:
        return self._data["stream"]

    @property
    def video(self) -> dict:
        return self._data["video"]

    @property
    def audio(self) -> dict:
        return self._data["audio"]

    @property
    def buffer(self) -> dict:
        return self._data["buffer"]

    @property
    def game(self) -> dict:
        return self._data["game"]

    @property
    def log(self) -> dict:
        return self._data["log"]

    @property
    def log_dir(self) -> Path:
        """Resolved path to the log directory (always inside data_dir)."""
        return self._data_dir / "logs"

    @property
    def selected_game(self) -> str:
        return self._data["game"].get("selected", _DEFAULT_GAME)

    @property
    def active_process_names(self) -> list[str]:
        """Process names to watch for the currently selected game."""
        return process_names_for_game(self.selected_game)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Dot-path accessor: config.get('stream', 'key')."""
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, *keys_and_value) -> None:
        """Set nested value: config.set('stream', 'key', 'abc123')."""
        *keys, value = keys_and_value
        node = self._data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)
