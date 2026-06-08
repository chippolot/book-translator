#!/bin/bash
# Convert an SVG (or PNG) source to a macOS .icns icon bundle using only
# built-in macOS tooling (`sips`, `iconutil`) — no Homebrew dependency.
#
# Usage: make_icns.sh <source.svg|source.png> <output.icns>
#
# Idempotent: skips work if the output is newer than the source.

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <source.svg|source.png> <output.icns>" >&2
    exit 1
fi

SRC="$1"
OUT="$2"

if [ ! -f "$SRC" ]; then
    echo "source not found: $SRC" >&2
    exit 1
fi

# Skip if up to date.
if [ -f "$OUT" ] && [ "$OUT" -nt "$SRC" ]; then
    echo "up to date: $OUT"
    exit 0
fi

# `sips` can't read SVG directly. If the source is SVG, rasterize once with
# Qt's svgtopng-equivalent via Python (PySide6 is already a build dep),
# then iconutil consumes PNG sources.
SRCDIR="$(cd "$(dirname "$SRC")" && pwd)"
SRCNAME="$(basename "$SRC")"
EXT="${SRCNAME##*.}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ICONSET="$WORK/icon.iconset"
mkdir -p "$ICONSET"

if [ "$EXT" = "svg" ] || [ "$EXT" = "SVG" ]; then
    # Rasterize the SVG to a 1024×1024 PNG using PySide6's QSvgRenderer
    # (already installed in .venv as a hard dep).
    PNG_MASTER="$WORK/master.png"
    "${PYTHON_BIN:-.venv/bin/python}" - "$SRC" "$PNG_MASTER" <<'PYEOF'
import sys
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

src, out = sys.argv[1], sys.argv[2]
QApplication([])
renderer = QSvgRenderer(src)
size = 1024
image = QImage(size, size, QImage.Format_ARGB32)
image.fill(Qt.transparent)
painter = QPainter(image)
painter.setRenderHint(QPainter.Antialiasing, True)
painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
renderer.render(painter)
painter.end()
image.save(out, "PNG")
print(f"master PNG: {out}")
PYEOF
else
    PNG_MASTER="$SRC"
fi

# Standard macOS iconset sizes (1× and 2× variants).
for spec in \
    "16 icon_16x16.png" \
    "32 icon_16x16@2x.png" \
    "32 icon_32x32.png" \
    "64 icon_32x32@2x.png" \
    "128 icon_128x128.png" \
    "256 icon_128x128@2x.png" \
    "256 icon_256x256.png" \
    "512 icon_256x256@2x.png" \
    "512 icon_512x512.png" \
    "1024 icon_512x512@2x.png"
do
    SIZE="${spec%% *}"
    NAME="${spec#* }"
    sips -z "$SIZE" "$SIZE" "$PNG_MASTER" --out "$ICONSET/$NAME" >/dev/null
done

iconutil -c icns "$ICONSET" -o "$OUT"
echo "wrote: $OUT"
