# dmgbuild settings for Book Translator.
# Invoked from build_mac.sh per-arch, e.g.
#   APP_PATH_FOR_DMG="dist/Book Translator (Apple Silicon).app" \
#   DMG_VOLUME_NAME="Book Translator (Apple Silicon)" \
#   dmgbuild -s build/dmg_settings.py "Book Translator (Apple Silicon)" out.dmg
#
# https://dmgbuild.readthedocs.io/en/latest/settings.html
import os
from pathlib import Path

# Path to the .app PyInstaller produced. Overridden via env so the same
# settings file builds both the arm64 and x86_64 DMGs.
APP_PATH = Path(os.environ.get(
    "APP_PATH_FOR_DMG", "dist/Book Translator.app")).resolve()

# Volume label shown in Finder when the DMG is mounted.
volume_name = os.environ.get("DMG_VOLUME_NAME", "Book Translator")

# Format: UDZO is the standard read-only compressed image, supported on
# every macOS version that runs the app.
format = "UDZO"

# Files / symlinks placed in the DMG root.
files = [str(APP_PATH)]
symlinks = {"Applications": "/Applications"}

# Visual layout when the user opens the DMG.
icon_size = 128
text_size = 14
window_rect = ((100, 100), (540, 380))
icon_locations = {
    APP_PATH.name: (140, 180),
    "Applications": (400, 180),
}

# Side-by-side: app icon on the left, an arrow to Applications on the right.
# We rely on Finder's default white background — adding a custom image is
# fiddly to keep universal2 and adds little for a small home-built app.
background = None
