#!/usr/bin/env bash
# Build executables for the current platform.
#
# Windows (run from WSL2):  ./build.sh
# macOS:                    ./build.sh
#
# FFmpeg must be pre-placed in vendor/ before building:
#   Windows: vendor/ffmpeg.exe
#   macOS:   vendor/ffmpeg
# See vendor/README.md for download instructions.

set -e
cd "$(dirname "$0")"

PLATFORM="$(uname -s)"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [ "$PLATFORM" = "Linux" ]; then
    # WSL2 — build Windows exe via py.exe
    DIST_WIN="D:\\share\\game-capture-agent"
    DIST_MNT="/mnt/d/share/game-capture-agent"

    # Require ffmpeg.exe in vendor/
    if [ ! -f "vendor/ffmpeg.exe" ]; then
        echo "ERROR: vendor/ffmpeg.exe not found. See vendor/README.md."
        exit 1
    fi

    mkdir -p "$DIST_MNT"

    echo "=== Building game-capture.exe ==="
    py.exe -m PyInstaller game_capture.spec \
        --distpath "$DIST_WIN" \
        --workpath "$(pwd)/build/game_capture" \
        --noconfirm

    echo "=== Building capture-debug.exe ==="
    py.exe -m PyInstaller capture_debug.spec \
        --distpath "$DIST_WIN" \
        --workpath "$(pwd)/build/capture_debug" \
        --noconfirm

    echo "=== Copying bundled files ==="
    cp vendor/ffmpeg.exe  "$DIST_MNT/ffmpeg.exe"
    mkdir -p "$DIST_MNT/templates"

    echo ""
    echo "Done! D:\\share\\game-capture-agent:"
    ls -lh "$DIST_MNT/"

elif [ "$PLATFORM" = "Darwin" ]; then
    DIST_MAC="$(pwd)/dist/game-capture-agent"

    # Require ffmpeg in vendor/
    if [ ! -f "vendor/ffmpeg" ]; then
        echo "ERROR: vendor/ffmpeg not found. See vendor/README.md."
        exit 1
    fi

    mkdir -p "$DIST_MAC"

    echo "=== Building game-capture.app ==="
    python3 -m PyInstaller game_capture_mac.spec \
        --distpath "$DIST_MAC" \
        --workpath "$(pwd)/build/game_capture" \
        --noconfirm

    echo "=== Building capture-debug ==="
    python3 -m PyInstaller capture_debug_mac.spec \
        --distpath "$DIST_MAC" \
        --workpath "$(pwd)/build/capture_debug" \
        --noconfirm

    echo "=== Copying bundled files ==="
    cp vendor/ffmpeg      "$DIST_MAC/ffmpeg"
    chmod +x              "$DIST_MAC/ffmpeg"
    mkdir -p "$DIST_MAC/templates"

    echo ""
    echo "Done! $DIST_MAC:"
    ls -lh "$DIST_MAC/"

else
    echo "Unsupported platform: $PLATFORM"
    exit 1
fi
