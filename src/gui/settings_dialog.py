"""Settings dialog: API keys + worker concurrency."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
    QWidget,
)

from . import keys


class _KeyRow(QWidget):
    """Single (provider, key) row with show/hide and test button."""

    changed = Signal()

    def __init__(self, provider: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.provider = provider
        h = QHBoxLayout(self); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)

        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText(f"{keys.ENV_VARS[provider]} value")
        existing = keys.get(provider) or ""
        if existing:
            self.edit.setText(existing)
        h.addWidget(self.edit, 1)

        self.show_btn = QPushButton("Show")
        self.show_btn.setObjectName("GhostBtn")
        self.show_btn.setCheckable(True)
        self.show_btn.toggled.connect(self._toggle_show)
        h.addWidget(self.show_btn)

        self.test_btn = QPushButton("Test")
        self.test_btn.setObjectName("GhostBtn")
        self.test_btn.clicked.connect(self._test_key)
        h.addWidget(self.test_btn)

        self.status = QLabel("")
        self.status.setObjectName("KeyStatus")
        h.addWidget(self.status)

        self.edit.textChanged.connect(lambda _: self.changed.emit())

    def _toggle_show(self, on: bool) -> None:
        self.edit.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        self.show_btn.setText("Hide" if on else "Show")

    def _test_key(self) -> None:
        v = self.edit.text().strip()
        if not v:
            self.status.setText("(empty)")
            return
        self.status.setText("testing…")
        QApplication = None
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        ok, err = _ping_provider(self.provider, v)
        if ok:
            self.status.setText("✓ ok")
            self.status.setStyleSheet("color: #137333;")
        else:
            self.status.setText(f"✕ {err[:40]}")
            self.status.setStyleSheet("color: #b3261e;")

    def value(self) -> str:
        return self.edit.text().strip()


class SettingsDialog(QDialog):
    """API keys + concurrency. Saves to OS keychain on Save."""

    def __init__(self, workers: int = 4, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(560, 420)
        self._initial_workers = workers
        self._build()

    def _build(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12); v.setSpacing(10)

        title = QLabel("API keys")
        title.setObjectName("SectionLabel")
        f = title.font(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        v.addWidget(title)

        hint = QLabel(
            "Keys are stored in the system keychain (macOS Keychain, "
            "Windows Credential Manager, Linux Secret Service). They are "
            "never written to disk in plain text.")
        hint.setObjectName("FormHint"); hint.setWordWrap(True)
        v.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.rows: dict[str, _KeyRow] = {}
        for p in keys.PROVIDERS:
            row = _KeyRow(p)
            self.rows[p] = row
            form.addRow(keys.LABELS[p], row)
        v.addLayout(form)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        v.addWidget(sep)

        title2 = QLabel("Concurrency")
        title2.setObjectName("SectionLabel")
        f = title2.font(); f.setBold(True); f.setPointSize(14)
        title2.setFont(f)
        v.addWidget(title2)

        form2 = QFormLayout()
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(self._initial_workers)
        form2.addRow("Parallel workers", self.workers_spin)
        hint2 = QLabel(
            "How many transcribe/translate API calls run at once. Higher = "
            "faster but more concurrent rate-limit pressure.")
        hint2.setObjectName("FormHint"); hint2.setWordWrap(True)
        form2.addRow("", hint2)
        v.addLayout(form2)

        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _on_save(self) -> None:
        for p, row in self.rows.items():
            v = row.value()
            if v:
                try:
                    keys.set(p, v)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "Keychain error",
                                        f"Failed to save {p}: {exc}")
                    return
            else:
                keys.delete(p)
        self.accept()

    @property
    def workers(self) -> int:
        return int(self.workers_spin.value())


# --------------------------------------------------------------------------- #
# Lightweight key health-check                                                 #
# --------------------------------------------------------------------------- #

def _ping_provider(provider: str, key: str) -> tuple[bool, str]:
    """Tiny 1-token-ish request to confirm the key is valid."""
    import os
    prior = os.environ.get(keys.ENV_VARS[provider])
    os.environ[keys.ENV_VARS[provider]] = key
    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2,
                messages=[{"role": "user", "content": "hi"}])
        elif provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=key)
            client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=2,
                messages=[{"role": "user", "content": "hi"}])
        elif provider == "google":
            from google import genai
            client = genai.Client(api_key=key)
            client.models.generate_content(
                model="gemini-2.0-flash",
                contents=["hi"])
        else:
            return False, "unknown provider"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if prior is None:
            os.environ.pop(keys.ENV_VARS[provider], None)
        else:
            os.environ[keys.ENV_VARS[provider]] = prior
