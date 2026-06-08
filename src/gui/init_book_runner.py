"""QThread wrapper + progress dialog for src/init_book.py.

`init_book` makes several LLM calls and (for PDFs) renders sample pages,
so it can take a half-minute to a few minutes. We run it on a background
thread and show a small modal dialog with the pages being fetched so the
user knows something's happening.
"""

from __future__ import annotations

import sys as _sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from . import keys, models  # noqa: E402

import init_book as init_book_mod  # noqa: E402

# A reasonable default ranking for the init step. Cheapest/fastest first;
# Anthropic is excellent at this task too. We exclude OpenAI from defaults
# only because gpt-5 is the most expensive of the three for vision-heavy
# tool use, but it's still selectable.
INIT_PROVIDER_RANKING = ("google", "anthropic", "openai")


class InitBookPreflightDialog(QDialog):
    """Modal pre-flight before init_book runs.

    Lets the user pick a provider and target language, or skip the
    auto-detect step. On accept(), `provider`, `target_lang`, and `skip`
    reflect their choices.
    """

    def __init__(self, input_path: Path,
                 available_providers: list[str],
                 default_provider: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up new book")
        self.setModal(True)
        self.resize(520, 320)
        self.skip = False

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)

        heading = QLabel("Auto-detect book details?")
        f = heading.font(); f.setPointSize(16); f.setWeight(QFont.DemiBold)
        heading.setFont(f)
        v.addWidget(heading)

        sub = QLabel(
            f"I can read {input_path.name} and pre-fill the title, author, "
            f"language, page range, and book context for you. It takes 30 "
            f"seconds to a couple of minutes and uses your selected AI "
            f"provider's tokens. You can also skip and fill everything in "
            f"by hand.")
        sub.setObjectName("FormHint"); sub.setWordWrap(True)
        v.addWidget(sub)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.target_lang = QLineEdit("English")
        form.addRow("Translate into", self.target_lang)

        self.provider = QComboBox()
        if not available_providers:
            available_providers = list(INIT_PROVIDER_RANKING)
        for p in available_providers:
            self.provider.addItem(models.PROVIDER_LABELS.get(p, p), p)
        # Select the default if it's in the list.
        idx = self.provider.findData(default_provider)
        if idx >= 0:
            self.provider.setCurrentIndex(idx)
        form.addRow("AI provider", self.provider)
        v.addLayout(form)

        hint = QLabel(
            "Google Gemini is the cheapest and fastest for this step. "
            "Claude tends to be the most accurate on hard scans.")
        hint.setObjectName("FormHint"); hint.setWordWrap(True)
        v.addWidget(hint)

        v.addStretch(1)

        bb = QDialogButtonBox()
        skip_btn = bb.addButton("Skip — fill in manually",
                                QDialogButtonBox.RejectRole)
        skip_btn.clicked.connect(self._on_skip)
        ok_btn = bb.addButton("Auto-detect", QDialogButtonBox.AcceptRole)
        ok_btn.setObjectName("PrimaryBtn")
        ok_btn.clicked.connect(self.accept)
        v.addWidget(bb)

    def _on_skip(self) -> None:
        self.skip = True
        self.reject()

    @property
    def chosen_provider(self) -> str:
        return self.provider.currentData() or "google"

    @property
    def chosen_target_lang(self) -> str:
        return self.target_lang.text().strip() or "English"


class InitBookWorker(QObject):
    """Runs init_book.init_book() on a QThread.

    Signals:
        progress(kind, message): per-step update — kind in
            {"render", "fetch", "call", "done"}.
        finished(ok, error, detected): terminal. `detected` is the dict
            of fields written to yaml; empty on failure.
    """

    progress = Signal(str, str)
    finished = Signal(bool, str, dict)

    def __init__(self, pdf: Path, out: Path, target_lang: str,
                 provider: str, model: Optional[str] = None) -> None:
        super().__init__()
        self.pdf = pdf
        self.out = out
        self.target_lang = target_lang
        self.provider = provider
        self.model = model
        self._thread: Optional[QThread] = None
        self._restore_env: dict[str, str] = {}

    # ----- public API -----
    def start(self) -> None:
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        self.finished.connect(self._thread.quit)
        self._thread.finished.connect(self.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @property
    def thread_done(self):
        return self._thread.finished if self._thread is not None else None

    # ----- worker body -----
    def _run(self) -> None:  # pragma: no cover - exercised via GUI
        self._restore_env = keys.apply_to_env()

        def cb(kind: str, message: str) -> None:
            self.progress.emit(kind, message)

        ok = False
        error = ""
        detected: dict = {}
        try:
            cb("call", "starting…")
            detected = init_book_mod.init_book(
                self.pdf, self.out,
                self.target_lang, self.provider, self.model,
                progress_cb=cb,
            )
            ok = True
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - SDK / network errors vary
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        finally:
            import os
            for k, v in self._restore_env.items():
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)
            self.finished.emit(ok, error, detected or {})


class InitBookProgressDialog(QDialog):
    """Modal "Detecting book metadata…" dialog. Shown while the worker
    runs. Auto-closes on success; on failure, surfaces the error and
    lets the user either retry or fall back to the empty skeleton.

    `result_ok` reflects whether the detection succeeded; the parent
    reads it after exec() returns to decide what to do next.

    Signals:
        retry_requested(): user clicked Retry. The parent should spawn
            a fresh InitBookWorker and reconnect its signals to this
            dialog's slots (on_progress / on_finished).
    """

    retry_requested = Signal()

    def __init__(self, pdf_name: str, target_lang: str, provider_label: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Setting up new book")
        self.setModal(True)
        self.resize(560, 360)
        self.result_ok = False
        self.error_text = ""
        self._provider_label = provider_label
        self._pdf_name = pdf_name

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)

        self.heading = QLabel(f"Reading {pdf_name}…")
        f = self.heading.font(); f.setPointSize(16); f.setWeight(QFont.DemiBold)
        self.heading.setFont(f)
        v.addWidget(self.heading)

        self._default_sub = (
            f"Using {provider_label} to detect the title, author, language, "
            f"page range, and other settings. This usually takes 30 seconds "
            f"to a couple of minutes — the model may flip through several "
            f"sample pages.")
        self.sub = QLabel(self._default_sub)
        self.sub.setObjectName("FormHint"); self.sub.setWordWrap(True)
        v.addWidget(self.sub)

        self.status = QLabel("starting…")
        self.status.setObjectName("StageDetail")
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        ff = QFont("Menlo"); ff.setStyleHint(QFont.TypeWriter)
        ff.setPointSize(11)
        self.log.setFont(ff)
        v.addWidget(self.log, 1)

        self.buttons = QDialogButtonBox()
        self.cancel_btn = self.buttons.addButton(
            "Skip — fill in manually", QDialogButtonBox.RejectRole)
        self.retry_btn = self.buttons.addButton(
            "Retry", QDialogButtonBox.ActionRole)
        self.retry_btn.setObjectName("PrimaryBtn")
        self.retry_btn.setVisible(False)
        self.ok_btn = self.buttons.addButton(
            "Continue", QDialogButtonBox.AcceptRole)
        self.ok_btn.setObjectName("PrimaryBtn")
        self.ok_btn.setEnabled(False)
        self.ok_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.retry_btn.clicked.connect(self._on_retry)
        self.ok_btn.clicked.connect(self.accept)
        v.addWidget(self.buttons)

        self._cancelled = False

    # ----- public slots -----

    def on_progress(self, kind: str, message: str) -> None:
        self.status.setText(message)
        self.log.appendPlainText(message)

    def on_finished(self, ok: bool, error: str, detected: dict) -> None:
        if self._cancelled:
            return  # already closed
        if ok:
            self.result_ok = True
            # Auto-accept on success — no point asking the user to click
            # Continue when there's nothing to review on this dialog.
            self.accept()
            return
        # Failure: surface a friendly headline and offer Retry. The
        # "Skip" button changes label so the user knows it now means
        # "give up and use the blank form".
        self.error_text = error or "unknown error"
        headline, sub, retryable = _humanize_error(self.error_text)
        self.heading.setText(headline)
        self.sub.setText(sub)
        # Show the relevant first line on the status pill; full text
        # (incl. traceback) stays in the log for diagnostics.
        first_meaningful = next(
            (ln for ln in self.error_text.splitlines() if ln.strip()),
            "unknown error")
        self.status.setText(first_meaningful)
        if error:
            self.log.appendPlainText("\nERROR:\n" + error)
        self.retry_btn.setVisible(retryable)
        self.retry_btn.setEnabled(retryable)
        self.cancel_btn.setText("Continue with blank form")

    # ----- internals -----

    def _on_cancel(self) -> None:
        self._cancelled = True
        self.reject()

    def _on_retry(self) -> None:
        # Reset the dialog to its initial state and ask the parent to
        # spawn a fresh worker.
        self.heading.setText(f"Reading {self._pdf_name}…")
        self.sub.setText(self._default_sub)
        self.status.setText("retrying…")
        self.log.appendPlainText("\n--- retrying ---")
        self.retry_btn.setVisible(False)
        self.cancel_btn.setText("Skip — fill in manually")
        self.error_text = ""
        self.retry_requested.emit()


# --------------------------------------------------------------------------- #
# Error humanization                                                          #
# --------------------------------------------------------------------------- #

# Heuristic markers for retryable transient failures. We default to
# retryable=True for anything not matched (network blips, timeouts) so a
# Retry button is always offered when there's at least a chance.
_PERMANENT_MARKERS = (
    "AuthenticationError",      # OpenAI / Anthropic SDK
    "PermissionDenied",         # Google
    "InvalidArgument",          # Google — usually a coding bug
    "FileNotFoundError",
    "FileExistsError",
    "ValueError",
    "401",
    "403",
    "API key",
)

_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "ServerError", "RateLimit", "Overloaded",
    "429", "Timeout", "Timed out", "Connection",
)


def _humanize_error(raw: str) -> tuple[str, str, bool]:
    """Return (headline, subtitle, retryable) given the raw worker error."""
    s = raw or ""
    low = s.lower()
    if "503" in s or "unavailable" in low or "overloaded" in low \
            or "rate limit" in low or "429" in s:
        return (
            "The AI service is busy right now",
            "Spikes in demand are usually temporary. You can retry in a "
            "moment, or skip the auto-detect and fill in the form by hand.",
            True,
        )
    if "timeout" in low or "timed out" in low or "connection" in low:
        return (
            "Network hiccup",
            "Couldn't reach the AI service. Check your internet and try "
            "again, or skip and fill in the form by hand.",
            True,
        )
    if any(m in s for m in ("401", "403", "AuthenticationError",
                            "PermissionDenied", "API key")):
        return (
            "Authentication failed",
            "Your API key was rejected. Open Settings to check it, then "
            "retry, or skip the auto-detect for now.",
            True,
        )
    # Default: unknown error. Allow retry unless the marker says permanent.
    retryable = not any(m in s for m in (
        "FileNotFoundError", "FileExistsError", "ValueError"))
    return (
        "Couldn't auto-detect this book",
        ("You can retry, or continue with a blank metadata form and fill "
         "the fields in by hand."
         if retryable else
         "You can continue with a blank metadata form and fill the "
         "fields in by hand."),
        retryable,
    )
