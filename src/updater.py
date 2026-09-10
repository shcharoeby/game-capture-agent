"""
Auto-update via GitHub Releases (public repo).

Flow:
  1. fetch version.json from the latest release  (tiny, ~300 bytes)
  2. compare embedded __version__ with remote version
  3. for each file: sha256(local) vs version.json — download only what changed
  4. write update.bat next to the exe, show UI prompt
  5. user clicks "Перезапустить" → app exits → bat swaps files → new version starts
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
import threading
import urllib.request
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.version import __version__

REPO = "shcharoeby/game-capture-agent"
VERSION_JSON_URL = (
    f"https://github.com/{REPO}/releases/latest/download/version.json"
)
DOWNLOAD_BASE = f"https://github.com/{REPO}/releases/latest/download/"

# Files we know how to update (exe names are Windows-only; mac/linux TBD)
_UPDATABLE_FILES = ["game-capture.exe", "capture-debug.exe", "ffmpeg.exe"]


@dataclass
class UpdateInfo:
    version: str
    changelog: str
    files_to_download: list[str]      # filenames that differ from local
    total_bytes: int                   # sum of sizes for progress reporting
    _file_meta: dict = field(repr=False, default_factory=dict)  # {name: {sha256, size}}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _install_dir() -> Path:
    """Directory where the running exe lives."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # Running from source — use the project root
    return Path(os.path.dirname(os.path.abspath(__file__))).parent


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return (0,)


def check_for_update(timeout: float = 10.0) -> UpdateInfo | None:
    """
    Blocking call — run in a background thread.

    Returns UpdateInfo if a newer version is available, None otherwise.
    """
    if platform.system() != "Windows":
        return None          # installer / updater only on Windows for now

    try:
        req = urllib.request.Request(
            VERSION_JSON_URL,
            headers={"User-Agent": f"game-capture-agent/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta = json.loads(resp.read())
    except Exception:
        return None

    remote_ver = meta.get("version", "")
    if not remote_ver:
        return None

    if _version_tuple(remote_ver) <= _version_tuple(__version__):
        return None          # already up to date

    install = _install_dir()
    files_meta: dict = meta.get("files", {})
    to_download: list[str] = []
    total_bytes = 0

    for fname in _UPDATABLE_FILES:
        if fname not in files_meta:
            continue
        remote_sha = files_meta[fname].get("sha256", "")
        size = files_meta[fname].get("size", 0)
        local_path = install / fname
        if local_path.exists():
            local_sha = _sha256_file(local_path)
            if local_sha == remote_sha:
                continue    # file unchanged — skip
        to_download.append(fname)
        total_bytes += size

    if not to_download:
        return None          # all files already match even though version tag differs

    return UpdateInfo(
        version=remote_ver,
        changelog=meta.get("changelog", ""),
        files_to_download=to_download,
        total_bytes=total_bytes,
        _file_meta=files_meta,
    )


class Updater:
    """
    Manages the download and staging of an update.

    Callbacks (all called on the worker thread — marshal to GUI via Qt signals):
      on_progress(downloaded_bytes, total_bytes)
      on_ready()       — all files downloaded, restart to apply
      on_error(msg)
    """

    def __init__(
        self,
        info: UpdateInfo,
        on_progress: Callable[[int, int], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self._info = info
        self._on_progress = on_progress
        self._on_ready = on_ready
        self._on_error = on_error
        self._cancelled = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="Updater"
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled = True

    # ── internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        install = _install_dir()
        downloaded = 0

        try:
            for fname in self._info.files_to_download:
                if self._cancelled:
                    return
                url = DOWNLOAD_BASE + fname
                dest = install / (fname + ".new")
                self._download_file(url, dest, downloaded)
                downloaded += self._info._file_meta.get(fname, {}).get("size", 0)

            if self._cancelled:
                return

            self._write_update_bat(install)
            if self._on_ready:
                self._on_ready()

        except Exception as exc:
            if self._on_error:
                self._on_error(str(exc))

    def _download_file(self, url: str, dest: Path, base_downloaded: int) -> None:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"game-capture-agent/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = self._info.total_bytes
            chunk_size = 1 << 16  # 64 KB
            received = 0
            with open(dest, "wb") as f:
                while not self._cancelled:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    if self._on_progress:
                        self._on_progress(base_downloaded + received, total)

    @staticmethod
    def _write_update_bat(install: Path) -> None:
        bat = install / "update.bat"
        lines = [
            "@echo off",
            "timeout /t 2 /nobreak >nul",
        ]
        for fname in _UPDATABLE_FILES:
            src = fname + ".new"
            lines.append(f'if exist "{src}" move /y "{src}" "{fname}"')
        lines += [
            'start "" "%~dp0game-capture.exe"',
            'del "%~f0"',
        ]
        bat.write_text("\r\n".join(lines), encoding="ascii")

    @staticmethod
    def launch_and_exit() -> None:
        """Run update.bat and quit the current process."""
        install = _install_dir()
        bat = install / "update.bat"
        if bat.exists():
            os.startfile(str(bat))   # type: ignore[attr-defined]
        sys.exit(0)
