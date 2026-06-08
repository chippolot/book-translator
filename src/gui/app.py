"""QApplication bootstrap. Usage: `python -m src.gui`."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main() -> int:
    # High-DPI is on by default in Qt6, but be explicit about the policy
    # so Retina Macs and 4K Linux look right.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("book-translate")
    app.setOrganizationName("book-translate")
    app.setStyle("Fusion")

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
