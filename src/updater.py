"""
Auto-update via GitHub Releases (public repo).

Two channels:
  stable — /releases/latest  (only non-pre-releases)
  dev    — /releases          (first item, includes pre-releases)

Flow:
  1. Fetch release metadata via GitHub API
  2. Download version.json from that specific release
  3. Compare __version__ with remote; if different → UpdateInfo
  4. For each file: sha256(local) vs version.json — download only what changed
  5. Write update.bat → user clicks "Перезапустить" → files swapped → new version starts
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.version import __version__

REPO = "shcharoeby/game-capture-agent"
_API_BASE = f"https://api.github.com/repos/{REPO}"
_DL_BASE  = f"https://github.com/{REPO}/releases/download"

_UPDATABLE_FILES = ["game-capture.exe", "ffmpeg.exe"]


@dataclass
class UpdateInfo:
    version:   str
    changelog: str
    tag:       str             # e.g. "v1.0.1" or "v1.0.1-dev.20260911"
    channel:   str             # "stable" | "dev"
    files_to_download: list[str]
    total_bytes: int
    _file_meta: dict = field(repr=False, default_factory=dict)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(os.path.dirname(os.path.abspath(__file__))).parent


def _fetch_json(url: str, timeout: float) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"game-capture-agent/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_release_meta(channel: str, timeout: float) -> dict | None:
    """
    Returns a GitHub release object (dict) for the requested channel.
    stable → /releases/latest   (pre-releases excluded by GitHub)
    dev    → /releases[0]       (most recent including pre-releases)
    """
    try:
        if channel == "dev":
            releases = _fetch_json(f"{_API_BASE}/releases", timeout)
            if not isinstance(releases, list) or not releases:
                return None
            return releases[0]
        else:
            result = _fetch_json(f"{_API_BASE}/releases/latest", timeout)
            return result if isinstance(result, dict) else None
    except Exception:
        return None


def check_for_update(
    channel: str = "stable",
    timeout: float = 10.0,
) -> UpdateInfo | None:
    """
    Blocking — run in a background thread.
    Returns UpdateInfo if a different version is available, None otherwise.
    Version comparison is by string equality: any difference triggers an update,
    supporting downgrades when the user switches from dev → stable.
    """
    if platform.system() != "Windows":
        return None

    release = _get_release_meta(channel, timeout)
    if not release:
        return None

    tag = release.get("tag_name", "")          # e.g. "v1.0.1-dev.20260911"
    remote_ver = tag.lstrip("v")               # "1.0.1-dev.20260911"
    if not remote_ver:
        return None

    if remote_ver == __version__:
        return None                            # already on this exact version

    # Download version.json from this specific release tag
    try:
        vj_url = f"{_DL_BASE}/{tag}/version.json"
        req = urllib.request.Request(
            vj_url,
            headers={"User-Agent": f"game-capture-agent/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            meta = json.loads(resp.read())
    except Exception:
        return None

    install = _install_dir()
    files_meta: dict = meta.get("files", {})
    to_download: list[str] = []
    total_bytes = 0

    for fname in _UPDATABLE_FILES:
        if fname not in files_meta:
            continue
        remote_sha = files_meta[fname].get("sha256", "")
        size       = files_meta[fname].get("size", 0)
        local_path = install / fname
        if local_path.exists() and _sha256_file(local_path) == remote_sha:
            continue                           # file unchanged
        to_download.append(fname)
        total_bytes += size

    if not to_download:
        return None                            # all files already match

    return UpdateInfo(
        version=remote_ver,
        changelog=meta.get("changelog", ""),
        tag=tag,
        channel=channel,
        files_to_download=to_download,
        total_bytes=total_bytes,
        _file_meta=files_meta,
    )


class Updater:
    """
    Downloads staged update files in a background thread.

    Callbacks (called from worker thread — marshal to GUI via Qt signals):
      on_progress(downloaded_bytes, total_bytes)
      on_ready()
      on_error(msg)
    """

    def __init__(
        self,
        info: UpdateInfo,
        on_progress: Callable[[int, int], None] | None = None,
        on_ready:    Callable[[], None]          | None = None,
        on_error:    Callable[[str], None]       | None = None,
    ):
        self._info        = info
        self._on_progress = on_progress
        self._on_ready    = on_ready
        self._on_error    = on_error
        self._cancelled   = False
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
        install    = _install_dir()
        downloaded = 0
        try:
            for fname in self._info.files_to_download:
                if self._cancelled:
                    return
                url  = f"{_DL_BASE}/{self._info.tag}/{fname}"
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
            total      = self._info.total_bytes
            chunk_size = 1 << 16  # 64 KB
            received   = 0
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
        bat   = install / "update.bat"
        # Wait up to 15s for game-capture.exe to fully exit (PyInstaller cleanup
        # can take several seconds; 2s was too short and move silently failed).
        lines = [
            "@echo off",
            "set /a attempts=0",
            ":wait_loop",
            "timeout /t 1 /nobreak >nul",
            "set /a attempts+=1",
            'tasklist /fi "imagename eq game-capture.exe" 2>nul | find /i "game-capture.exe" >nul',
            "if not errorlevel 1 (",
            "  if %attempts% lss 15 goto wait_loop",
            ")",
        ]
        for fname in _UPDATABLE_FILES:
            lines.append(f'if exist "{fname}.new" move /y "{fname}.new" "{fname}"')
        # Use explorer.exe to launch the updated app so that it persists as
        # the parent process — cmd.exe would exit before PyInstaller can query
        # it, causing "Security validation failure: failed to obtain executable
        # path for parent process".
        lines += ['explorer.exe "%~dp0game-capture.exe"', 'del "%~f0"']
        bat.write_text("\r\n".join(lines), encoding="ascii")

    @staticmethod
    def launch_and_exit() -> None:
        install = _install_dir()
        bat = install / "update.bat"
        if bat.exists():
            os.startfile(str(bat))   # type: ignore[attr-defined]
        sys.exit(0)
