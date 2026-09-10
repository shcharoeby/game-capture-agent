"""FFmpeg binary resolution — finds ffmpeg next to the exe or in PATH."""
import platform
import shutil
import sys
from pathlib import Path


def find_ffmpeg() -> str:
    """
    Return the path to the ffmpeg binary.

    Search order:
      1. Next to the running executable (works for PyInstaller bundles and
         for running from source if ffmpeg is placed in the project root).
      2. System PATH.

    Raises RuntimeError if ffmpeg is not found anywhere.
    """
    system = platform.system()
    exe_name = "ffmpeg.exe" if system == "Windows" else "ffmpeg"

    # --- 1. Next to our executable ---
    if getattr(sys, "frozen", False):
        # PyInstaller bundle: sys.executable is the actual .exe / .app binary
        candidates = [Path(sys.executable).parent / exe_name]
    else:
        # Running from source: look in project root (one level up from src/)
        src_dir = Path(__file__).parent
        project_root = src_dir.parent
        candidates = [project_root / exe_name]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    # --- 2. System PATH ---
    found = shutil.which(exe_name) or shutil.which("ffmpeg")
    if found:
        return found

    raise RuntimeError(
        f"FFmpeg не найден. Положите {exe_name} рядом с приложением."
    )
