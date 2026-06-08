"""QApplication bootstrap. Usage: `python run_gui.py`."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow

APP_NAME = "Book Translator"
ICON_PATH = Path(__file__).resolve().parent / "resources" / "book.svg"


def _fix_macos_menubar_name(name: str) -> None:
    """Try to override the macOS menubar app name (defaults to "Python ...")
    when running from a non-bundled script. Best-effort; silently no-ops if
    PyObjC isn't available."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle  # type: ignore
    except ImportError:
        return
    bundle = NSBundle.mainBundle()
    if not bundle:
        return
    # The localized dictionary wins if present; otherwise fall back to the
    # plain info dictionary. Both are mutable when accessed via PyObjC.
    info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
    if info is None:
        return
    info["CFBundleName"] = name
    info["CFBundleDisplayName"] = name


def main() -> int:
    # High-DPI is on by default in Qt6, but be explicit about the policy
    # so Retina Macs and 4K Linux look right.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # Set the macOS bundle name BEFORE QApplication starts — once Qt has
    # initialised the NSApplication, it caches the menubar label.
    _fix_macos_menubar_name(APP_NAME)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setStyle("Fusion")

    if ICON_PATH.exists():
        icon = QIcon(str(ICON_PATH))
        app.setWindowIcon(icon)

    # System font on Mac is SF Pro; Qt picks it up by default.
    f = QFont()
    f.setPointSize(13)
    app.setFont(f)

    style_path = Path(__file__).resolve().parent / "style.qss"
    if style_path.exists():
        app.setStyleSheet(style_path.read_text(encoding="utf-8"))

    win = MainWindow()
    win.show()
    win.maybe_import_env()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
