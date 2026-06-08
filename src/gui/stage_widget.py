"""A single stage card in the workflow board."""

from __future__ import annotations

from typing import Optional

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout,
    QWidget,
)

from .workflow_state import StageStatus, Status


STAGE_TITLES = {
    "render": "Render PDF pages",
    "transcribe": "Transcribe pages",
    "segment": "Segment into sections",
    "translate": "Translate sections",
    "assemble": "Assemble book",
}

STAGE_HINTS = {
    "render": "Rasterizes each PDF page to a PNG.",
    "transcribe": "Reads each page with a vision model and extracts source text.",
    "segment": "Groups page segments into whole sections (chapters, scenes, stories).",
    "translate": "Translates each section into the target language.",
    "assemble": "Produces the bilingual review HTML and a styled PDF.",
}

NUMERALS = ("①", "②", "③", "④", "⑤")


class StageCard(QFrame):
    """One stage. Emits high-level signals; the main window wires them up."""

    run_clicked = Signal(str)        # stage
    reset_clicked = Signal(str)
    review_clicked = Signal(str)
    edit_provider = Signal(str)
    cancel_clicked = Signal(str)

    def __init__(self, stage: str, ordinal: int,
                 show_provider: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.stage = stage
        self.setObjectName("StageCard")
        self.setFrameShape(QFrame.NoFrame)
        self._build(ordinal, show_provider)
        self._status: Status = Status.NOT_STARTED
        self._enabled_by_upstream = True

    # ----- layout -----
    def _build(self, ordinal: int, show_provider: bool) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_text = f"{NUMERALS[ordinal]}  {STAGE_TITLES[self.stage]}"
        self.title = QLabel(title_text)
        self.title.setObjectName("StageTitle")
        f = self.title.font()
        f.setPointSize(15); f.setWeight(QFont.DemiBold)
        self.title.setFont(f)
        header.addWidget(self.title)
        header.addStretch(1)
        self.pill = QLabel("○ Not started")
        self.pill.setObjectName("StatusPill")
        self.pill.setProperty("status", "not_started")
        header.addWidget(self.pill)
        outer.addLayout(header)

        self.hint = QLabel(STAGE_HINTS[self.stage])
        self.hint.setObjectName("StageHint")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

        self.detail = QLabel("")
        self.detail.setObjectName("StageDetail")
        self.detail.setWordWrap(True)
        outer.addWidget(self.detail)

        self.progress = QProgressBar()
        self.progress.setObjectName("StageProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        if show_provider:
            self.provider_label = QLabel("")
            self.provider_label.setObjectName("ProviderLine")
            outer.addWidget(self.provider_label)
        else:
            self.provider_label = None

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.run_btn = QPushButton("Start")
        self.run_btn.setObjectName("PrimaryBtn")
        self.run_btn.clicked.connect(lambda: self.run_clicked.emit(self.stage))
        btn_row.addWidget(self.run_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("DangerBtn")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(lambda: self.cancel_clicked.emit(self.stage))
        btn_row.addWidget(self.cancel_btn)

        self.review_btn = QPushButton("Review titles")
        self.review_btn.setObjectName("SecondaryBtn")
        self.review_btn.setVisible(self.stage == "segment")
        self.review_btn.clicked.connect(lambda: self.review_clicked.emit(self.stage))
        btn_row.addWidget(self.review_btn)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("GhostBtn")
        self.reset_btn.clicked.connect(lambda: self.reset_clicked.emit(self.stage))
        btn_row.addWidget(self.reset_btn)

        if show_provider:
            edit_btn = QPushButton("Edit model")
            edit_btn.setObjectName("GhostBtn")
            edit_btn.clicked.connect(lambda: self.edit_provider.emit(self.stage))
            btn_row.addWidget(edit_btn)

        btn_row.addStretch(1)
        outer.addLayout(btn_row)

        self.log_line = QLabel("")
        self.log_line.setObjectName("StageLog")
        self.log_line.setWordWrap(False)
        self.log_line.setVisible(False)
        outer.addWidget(self.log_line)

        # Container for "Open …" buttons surfacing this stage's outputs.
        # Populated by set_outputs(); hidden when there are no outputs.
        self.outputs_holder = QWidget()
        self.outputs_layout = QHBoxLayout(self.outputs_holder)
        self.outputs_layout.setContentsMargins(0, 6, 0, 0)
        self.outputs_layout.setSpacing(8)
        self.outputs_layout.addStretch(1)
        self.outputs_holder.setVisible(False)
        outer.addWidget(self.outputs_holder)

    # ----- public API -----

    def set_provider_line(self, text: str) -> None:
        if self.provider_label is not None:
            self.provider_label.setText(text)

    def set_status(self, s: StageStatus) -> None:
        self._status = s.status
        pill_text, pill_kind = _pill_for(s)
        self.pill.setText(pill_text)
        self.pill.setProperty("status", pill_kind)
        self._reapply_pill_style()

        # Detail / progress.
        if s.status == Status.STALE and s.stale_reason:
            self.detail.setText(f"⚠  Stale: {s.stale_reason}")
        elif s.detail:
            self.detail.setText(s.detail)
        else:
            self.detail.setText("")

        if s.total > 0 and s.status in (Status.PARTIAL, Status.COMPLETE,
                                        Status.RUNNING, Status.NEEDS_REVIEW,
                                        Status.STALE):
            self.progress.setVisible(True)
            self.progress.setMaximum(max(s.total, 1))
            self.progress.setValue(s.done)
        else:
            self.progress.setVisible(False)

        # Button visibility/labels.
        running = s.status == Status.RUNNING
        self.cancel_btn.setVisible(running)
        self.run_btn.setVisible(not running)
        self.run_btn.setText(_run_label(self.stage, s.status))
        self.run_btn.setEnabled(self._enabled_by_upstream and not running)
        self.reset_btn.setEnabled(
            s.status not in (Status.NOT_STARTED, Status.RUNNING))
        if self.stage == "segment":
            # Only allow review when there's something to review.
            self.review_btn.setEnabled(
                s.status in (Status.NEEDS_REVIEW, Status.COMPLETE, Status.STALE))

    def set_enabled_by_upstream(self, enabled: bool, hint: str = "") -> None:
        self._enabled_by_upstream = enabled
        running = self._status == Status.RUNNING
        self.run_btn.setEnabled(enabled and not running)
        if not enabled and hint:
            if not self.detail.text() or self.detail.text().startswith("⚠"):
                self.detail.setText(hint)

    def show_log_line(self, line: str) -> None:
        self.log_line.setText(line)
        self.log_line.setVisible(True)

    def set_outputs(self, files: list[Path]) -> None:
        """Show one 'Open' button per existing output file.

        Buttons launch the file with the OS's default app (Preview for PDF,
        browser for HTML, TextEdit for Markdown). Files that don't exist are
        silently skipped.
        """
        # Clear prior buttons (keep the trailing stretch).
        while self.outputs_layout.count() > 1:
            item = self.outputs_layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        existing = [f for f in files if f and Path(f).exists()]
        if not existing:
            self.outputs_holder.setVisible(False)
            return
        for path in existing:
            btn = _OutputButton(Path(path))
            self.outputs_layout.insertWidget(self.outputs_layout.count() - 1, btn)
        self.outputs_holder.setVisible(True)

    def _reapply_pill_style(self) -> None:
        st = self.pill.style()
        st.unpolish(self.pill); st.polish(self.pill)


def _pill_for(s: StageStatus) -> tuple[str, str]:
    """Return (label, kind). `kind` is the QSS property selector value."""
    if s.status == Status.NOT_STARTED:
        return ("○ Not started", "not_started")
    if s.status == Status.PARTIAL:
        return (f"◐ Partial · {s.done}/{s.total}", "partial")
    if s.status == Status.NEEDS_REVIEW:
        return (f"⊙ Needs review · {s.done}", "needs_review")
    if s.status == Status.COMPLETE:
        if s.total:
            return (f"● Complete · {s.done}/{s.total}", "complete")
        return ("● Complete", "complete")
    if s.status == Status.STALE:
        return ("⚠ Stale", "stale")
    if s.status == Status.RUNNING:
        return ("▶ Running…", "running")
    if s.status == Status.FAILED:
        return ("✕ Failed", "failed")
    return (s.status.value, "not_started")


_EXT_LABELS = {
    ".pdf": "Open PDF",
    ".html": "Open HTML",
    ".htm": "Open HTML",
    ".md": "Open Markdown",
}


class _OutputButton(QPushButton):
    """Button that opens an output file with the system default app."""

    def __init__(self, path: Path) -> None:
        ext = path.suffix.lower()
        base_label = _EXT_LABELS.get(ext, "Open")
        # Distinguish bilingual review HTML from the styled book HTML.
        if "_review" in path.stem:
            if ext == ".md":
                base_label = "Open review (Markdown)"
            else:
                base_label = "Open review (HTML)"
        elif ext == ".html":
            base_label = "Open book (HTML)"
        elif ext == ".pdf":
            base_label = "Open book (PDF)"
        super().__init__(base_label)
        self.setObjectName("OutputBtn" if ext == ".pdf" else "SecondaryBtn")
        self.setToolTip(str(path))
        self._path = path
        self.clicked.connect(self._open)

    def _open(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path)))


def _run_label(stage: str, status: Status) -> str:
    if status in (Status.NOT_STARTED,):
        return "Start"
    if status == Status.PARTIAL:
        return "Resume"
    if status in (Status.COMPLETE, Status.STALE):
        return "Re-run"
    if status == Status.NEEDS_REVIEW:
        return "Re-run"
    if status == Status.FAILED:
        return "Retry"
    return "Start"
