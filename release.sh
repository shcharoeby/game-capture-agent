#!/usr/bin/env bash
# Create a GitHub release with smart-diff assets and an Inno Setup installer.
#
# Usage:
#   ./release.sh <version> "<changelog>"
#
# Example:
#   ./release.sh 1.2.3 "Исправлен захват звука, улучшена стабильность"
#
# Requirements:
#   - gh CLI authenticated (gh auth login)
#   - Inno Setup 6 installed at default path on Windows
#   - ./build.sh must have been run first

set -e
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
VERSION="${1:?Usage: ./release.sh <version> \"<changelog>\"}"
CHANGELOG="${2:-Обновление}"
TAG="v${VERSION}"

DIST_MNT="/mnt/d/share/game-capture-agent"
DIST_WIN="D:\\share\\game-capture-agent"
# Search common install locations (winget installs to AppData when no admin)
ISCC=""
for candidate in \
    "/mnt/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
    "/mnt/c/Program Files/Inno Setup 6/ISCC.exe" \
    "/mnt/c/Users/$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n')/AppData/Local/Programs/Inno Setup 6/ISCC.exe"
do
    if [ -f "$candidate" ]; then
        ISCC="$candidate"
        break
    fi
done

echo "=== Release ${TAG} ==="

# ---------------------------------------------------------------------------
# 1. Patch src/version.py
# ---------------------------------------------------------------------------
echo "--- Patching src/version.py ---"
cat > src/version.py <<EOF
"""Application version — single source of truth."""
__version__ = "${VERSION}"
EOF

# ---------------------------------------------------------------------------
# 2. Build executables
# ---------------------------------------------------------------------------
echo "--- Building exes ---"
./build.sh

# ---------------------------------------------------------------------------
# 3. Compute sha256 for each updatable file
# ---------------------------------------------------------------------------
echo "--- Computing checksums ---"

sha256_of() {
    sha256sum "$1" | awk '{print $1}'
}

EXE_SHA=$(sha256_of "$DIST_MNT/game-capture.exe")
DBG_SHA=$(sha256_of "$DIST_MNT/capture-debug.exe")
FFM_SHA=$(sha256_of "$DIST_MNT/ffmpeg.exe")

EXE_SIZE=$(stat -c%s "$DIST_MNT/game-capture.exe")
DBG_SIZE=$(stat -c%s "$DIST_MNT/capture-debug.exe")
FFM_SIZE=$(stat -c%s "$DIST_MNT/ffmpeg.exe")

# ---------------------------------------------------------------------------
# 4. Generate version.json
# ---------------------------------------------------------------------------
echo "--- Generating version.json ---"
CHANGELOG_ESC=$(echo "$CHANGELOG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip(), ensure_ascii=False))")

cat > "$DIST_MNT/version.json" <<EOF
{
  "version": "${VERSION}",
  "changelog": ${CHANGELOG_ESC},
  "files": {
    "game-capture.exe":  { "sha256": "${EXE_SHA}", "size": ${EXE_SIZE} },
    "capture-debug.exe": { "sha256": "${DBG_SHA}", "size": ${DBG_SIZE} },
    "ffmpeg.exe":        { "sha256": "${FFM_SHA}", "size": ${FFM_SIZE} }
  }
}
EOF

echo "version.json:"
cat "$DIST_MNT/version.json"

# ---------------------------------------------------------------------------
# 5. Build Inno Setup installer
# ---------------------------------------------------------------------------
if [ -f "$ISCC" ]; then
    echo "--- Building installer with Inno Setup ---"
    "$ISCC" installer/setup.iss \
        "/DMyAppVersion=${VERSION}" \
        "/DSourceDir=${DIST_WIN}"
    INSTALLER="$DIST_MNT/GameCapture-Setup-${VERSION}.exe"
    echo "Installer: $INSTALLER"
else
    echo "WARNING: Inno Setup not found at '$ISCC' — skipping installer build"
    echo "         Install Inno Setup 6 from https://jrsoftware.org/isinfo.php"
    INSTALLER=""
fi

# ---------------------------------------------------------------------------
# 6. Commit version bump
# ---------------------------------------------------------------------------
echo "--- Committing version bump ---"
git add src/version.py
git commit -m "chore: bump version to ${VERSION}"
git push

# ---------------------------------------------------------------------------
# 7. Create GitHub Release and upload assets
# ---------------------------------------------------------------------------
echo "--- Creating GitHub Release ${TAG} ---"
gh release create "${TAG}" \
    --title "v${VERSION}" \
    --notes "${CHANGELOG}" \
    "$DIST_MNT/version.json" \
    "$DIST_MNT/game-capture.exe" \
    "$DIST_MNT/capture-debug.exe" \
    "$DIST_MNT/ffmpeg.exe"

if [ -n "$INSTALLER" ] && [ -f "$INSTALLER" ]; then
    gh release upload "${TAG}" "$INSTALLER"
fi

echo ""
echo "=== Done! Release ${TAG} published ==="
echo "    https://github.com/shcharoeby/game-capture-agent/releases/tag/${TAG}"
