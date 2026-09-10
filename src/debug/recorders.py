"""Background recorders for the debug calibration tool."""
import json
import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import mss
import numpy as np
import psutil
from PIL import Image


# ---------------------------------------------------------------------------
# Screenshot recorder
# ---------------------------------------------------------------------------

class ScreenshotRecorder:
    """
    Captures game window screenshots every `interval_ms` milliseconds.
    On burst(), captures at `burst_interval_ms` for `burst_duration_sec` around the event.
    """

    def __init__(
        self,
        output_dir: Path,
        interval_ms: int = 200,
        burst_interval_ms: int = 50,
        burst_duration_sec: float = 3.0,
    ):
        self._output_dir = output_dir / "screenshots"
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._interval_ms = interval_ms
        self._burst_interval_ms = burst_interval_ms
        self._burst_duration_sec = burst_duration_sec
        self._running = False
        self._thread: threading.Thread | None = None
        self._burst_until: float = 0.0
        self._seq = 0
        self._bbox: dict | None = None  # mss monitor dict

    def set_window_bbox(self, left: int, top: int, width: int, height: int) -> None:
        self._bbox = {"left": left, "top": top, "width": width, "height": height}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScreenshotRecorder")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def burst(self, event_unix_ms: int) -> None:
        """Trigger burst capture mode around an event."""
        self._burst_until = time.monotonic() + self._burst_duration_sec

    def _loop(self) -> None:
        with mss.mss() as sct:
            while self._running:
                now_mono = time.monotonic()
                is_burst = now_mono < self._burst_until
                interval = self._burst_interval_ms if is_burst else self._interval_ms

                if self._bbox:
                    try:
                        shot = sct.grab(self._bbox)
                        unix_ms = int(time.time() * 1000)
                        self._seq += 1
                        prefix = "burst" if is_burst else ""
                        name = f"{unix_ms}_{prefix}{self._seq:04d}.png" if prefix else f"{unix_ms}_{self._seq:04d}.png"
                        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                        img.save(self._output_dir / name, optimize=False)
                    except Exception as e:
                        print(f"[screenshot] {e}")

                time.sleep(interval / 1000.0)


# ---------------------------------------------------------------------------
# Pixel grid recorder
# ---------------------------------------------------------------------------

class PixelGridRecorder:
    """
    Every `interval_ms` ms samples a 20×20 grid of pixels from the game window.
    Saves compact JSONL: {"ts": unix_ms, "grid": [[r,g,b], ...]}.
    """

    GRID_SIZE = 20

    def __init__(self, output_dir: Path, interval_ms: int = 500):
        self._path = output_dir / "pixel_grid.jsonl"
        self._interval_ms = interval_ms
        self._running = False
        self._thread: threading.Thread | None = None
        self._bbox: dict | None = None
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def set_window_bbox(self, left: int, top: int, width: int, height: int) -> None:
        self._bbox = {"left": left, "top": top, "width": width, "height": height}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="PixelGridRecorder")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._file.flush()
        self._file.close()

    def _loop(self) -> None:
        n = self.GRID_SIZE
        with mss.mss() as sct:
            while self._running:
                if self._bbox:
                    try:
                        shot = sct.grab(self._bbox)
                        img = np.array(shot)[:, :, :3]  # BGR, drop alpha
                        h, w = img.shape[:2]
                        grid = []
                        for gy in range(n):
                            for gx in range(n):
                                px = int(gx * w / n)
                                py = int(gy * h / n)
                                b, g, r = img[py, px]
                                grid.append([int(r), int(g), int(b)])
                        record = {"ts": int(time.time() * 1000), "grid": grid}
                        self._file.write(json.dumps(record) + "\n")
                    except Exception as e:
                        print(f"[pixel_grid] {e}")
                time.sleep(self._interval_ms / 1000.0)


# ---------------------------------------------------------------------------
# Memory diff recorder
# ---------------------------------------------------------------------------

def _scan_memory_windows(pid: int) -> dict[str, int]:
    """Return {hex_addr: value} for readable process memory pages (sampled)."""
    result: dict[str, int] = {}
    try:
        import ctypes
        import ctypes.wintypes as wt

        PROCESS_VM_READ = 0x0010
        PROCESS_QUERY_INFORMATION = 0x0400

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not handle:
            return result

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD),
                ("Protect", wt.DWORD),
                ("Type", wt.DWORD),
            ]

        MEM_COMMIT = 0x1000
        PAGE_READABLE = {0x02, 0x04, 0x20, 0x40}
        MAX_REGIONS = 200  # limit scan to prevent huge diffs

        addr = 0
        regions_scanned = 0
        while regions_scanned < MAX_REGIONS:
            mbi = MEMORY_BASIC_INFORMATION()
            ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            if mbi.State == MEM_COMMIT and mbi.Protect in PAGE_READABLE and mbi.RegionSize < 4 * 1024 * 1024:
                buf = (ctypes.c_char * min(mbi.RegionSize, 65536))()
                read = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, ctypes.sizeof(buf), ctypes.byref(read))
                if ok:
                    raw = bytes(buf[:read.value])
                    # Sample every 4 bytes as uint32
                    for i in range(0, min(len(raw) - 3, 4096), 4):
                        val = int.from_bytes(raw[i:i+4], "little")
                        result[hex(addr + i)] = val
                regions_scanned += 1
            addr += mbi.RegionSize if mbi.RegionSize else 0x1000

        kernel32.CloseHandle(handle)
    except Exception as e:
        print(f"[memory] scan error: {e}")
    return result


class MemoryDiffRecorder:
    """
    Scans game process memory every `interval_sec` and writes changed addresses.
    """

    def __init__(self, output_dir: Path, pid: int, interval_sec: float = 1.0):
        self._path = output_dir / "memory_diff.jsonl"
        self._pid = pid
        self._interval_sec = interval_sec
        self._running = False
        self._thread: threading.Thread | None = None
        self._prev: dict[str, int] = {}
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)
        self._enabled = platform.system() == "Windows"

    def start(self) -> None:
        if not self._enabled or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="MemoryDiffRecorder")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._file.flush()
        self._file.close()

    def _loop(self) -> None:
        while self._running:
            try:
                current = _scan_memory_windows(self._pid)
                changed = []
                for addr, val in current.items():
                    if addr in self._prev and self._prev[addr] != val:
                        changed.append({"addr": addr, "prev": self._prev[addr], "curr": val})
                if changed:
                    record = {"ts": int(time.time() * 1000), "changed": changed[:500]}  # cap at 500 changes
                    self._file.write(json.dumps(record) + "\n")
                self._prev = current
            except Exception as e:
                print(f"[memory_diff] {e}")
            time.sleep(self._interval_sec)


# ---------------------------------------------------------------------------
# File activity recorder
# ---------------------------------------------------------------------------

class FileActivityRecorder:
    """
    Polls the game directory for file modification time changes.
    """

    def __init__(self, output_dir: Path, watch_dir: str | Path, interval_sec: float = 2.0):
        self._path = output_dir / "file_activity.jsonl"
        self._watch_dir = Path(watch_dir)
        self._interval_sec = interval_sec
        self._running = False
        self._thread: threading.Thread | None = None
        self._prev_mtimes: dict[str, float] = {}
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="FileActivityRecorder")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._file.flush()
        self._file.close()

    def _loop(self) -> None:
        while self._running:
            try:
                changed = []
                for root, _, files in os.walk(self._watch_dir):
                    for fname in files:
                        fpath = os.path.join(root, fname)
                        try:
                            mtime = os.path.getmtime(fpath)
                            key = fpath
                            if key in self._prev_mtimes and self._prev_mtimes[key] != mtime:
                                changed.append({"file": fpath, "mtime": mtime})
                            self._prev_mtimes[key] = mtime
                        except OSError:
                            pass
                if changed:
                    record = {"ts": int(time.time() * 1000), "changed": changed}
                    self._file.write(json.dumps(record) + "\n")
            except Exception as e:
                print(f"[file_activity] {e}")
            time.sleep(self._interval_sec)
