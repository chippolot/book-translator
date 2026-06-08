#!/bin/bash
# Single-arch Mac .app build: icon → PyInstaller → ad-hoc sign → DMG.
#
# Run for each arch (or use build_both.sh):
#     TARGET_ARCH=arm64  ./build/build_mac.sh
#     TARGET_ARCH=x86_64 ./build/build_mac.sh
#
# Prereqs (one-time): ./build/setup_build_venvs.sh  — creates the
# .venv-arm64 and .venv-x86_64 used by this script.
#
# Output: dist/Book Translator (<Arch label>).app/.dmg

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

TARGET_ARCH="${TARGET_ARCH:-arm64}"
case "$TARGET_ARCH" in
    arm64)  ARCH_LABEL="Apple Silicon" ;;
    x86_64) ARCH_LABEL="Intel" ;;
    *) echo "TARGET_ARCH must be arm64 or x86_64 (got $TARGET_ARCH)" >&2; exit 1 ;;
esac

VENV_DIR="$ROOT/.venv-$TARGET_ARCH"
VENV_PY="$VENV_DIR/bin/python"
VENV_BIN="$VENV_DIR/bin"
if [ ! -x "$VENV_PY" ]; then
    echo "Expected venv at $VENV_DIR. Run ./build/setup_build_venvs.sh first." >&2
    exit 1
fi

APP_NAME="Book Translator"
APP_LABEL="$APP_NAME ($ARCH_LABEL)"
APP_PATH="dist/$APP_LABEL.app"
DMG_PATH="dist/$APP_LABEL.dmg"

# -------------------------------------------------------------------------
# 1. Icon (idempotent, arch-independent).
# -------------------------------------------------------------------------
echo "==> Generating icon"
PYTHON_BIN="$VENV_PY" "$ROOT/build/make_icns.sh" \
    "src/gui/resources/book.svg" \
    "build/book.icns"

# -------------------------------------------------------------------------
# 2. PyInstaller (runs under the matching arch so the bundled libpython
#    and stdlib .so files match TARGET_ARCH).
# -------------------------------------------------------------------------
echo "==> Building .app for $TARGET_ARCH"
WORKPATH="build/_pyinstaller-$TARGET_ARCH"
DIST_DIR="dist/$TARGET_ARCH"
rm -rf "$WORKPATH" "$DIST_DIR" "$APP_PATH"

TARGET_ARCH="$TARGET_ARCH" \
APP_NAME="$APP_LABEL" \
    arch -"$TARGET_ARCH" "$VENV_BIN/pyinstaller" \
        --noconfirm \
        --clean \
        --workpath "$WORKPATH" \
        --distpath "$DIST_DIR" \
        build/book_translator.spec

if [ ! -d "$DIST_DIR/$APP_LABEL.app" ]; then
    echo "ERROR: PyInstaller did not produce $DIST_DIR/$APP_LABEL.app" >&2
    exit 2
fi

# Move into the canonical dist/ root.
rm -rf "$APP_PATH"
mv "$DIST_DIR/$APP_LABEL.app" "$APP_PATH"
rm -rf "$DIST_DIR"

# -------------------------------------------------------------------------
# 3. Ad-hoc sign (required on Apple Silicon; harmless on Intel).
# -------------------------------------------------------------------------
echo "==> Ad-hoc code signing"
# We deliberately omit `--options runtime`: the Hardened Runtime enforces
# library validation that requires every loaded dylib to share a Team ID
# with the main executable. Pre-built wheels carry their own Team IDs and
# ad-hoc re-signing can't change those, so hardened runtime causes
# "different Team IDs" launch failures. Hardened runtime is only useful
# with notarization; this is an ad-hoc local build.
codesign --remove-signature "$APP_PATH" 2>/dev/null || true
codesign --sign - --deep --force --timestamp=none "$APP_PATH"
codesign --verify --strict --deep "$APP_PATH"

# -------------------------------------------------------------------------
# 4. DMG.
# -------------------------------------------------------------------------
echo "==> Building DMG"
rm -f "$DMG_PATH"
# Tell dmg_settings.py which app to package via env var.
APP_PATH_FOR_DMG="$APP_PATH" \
DMG_VOLUME_NAME="$APP_LABEL" \
    "$VENV_BIN/dmgbuild" \
        -s build/dmg_settings.py \
        "$APP_LABEL" \
        "$DMG_PATH"

# -------------------------------------------------------------------------
echo
echo "===================================================================="
echo "Built ($TARGET_ARCH):"
echo "  $APP_PATH   ($(du -sh "$APP_PATH" | cut -f1))"
echo "  $DMG_PATH   ($(du -sh "$DMG_PATH" | cut -f1))"
echo "===================================================================="
