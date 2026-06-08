"""Manual segment-title review dialog.

After segment.run_no_write() produces the candidate stories, this dialog
lets the user:
  - uncheck false-positive titles (merges into prior section);
  - edit a title for typo/OCR fixes;
  - preview the resulting section text.

On Apply, we write stories.json with the (merged + renamed) result and
record the user's choices in .gui_state.json so re-running segment doesn't
silently undo their work.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
import json as _json

from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from config import Config  # noqa: E402
import segment as segment_mod  # noqa: E402

from . import workflow_state as ws  # noqa: E402


class SegmentReviewDialog(QDialog):
    """Modal dialog. After accept(), the caller can read `result_stories`."""

    def __init__(self, cfg: Config, stories: list[dict],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._original = [dict(s) for s in stories]
        self._titles: list[str] = [s.get("title") or "" for s in stories]
        self._checked: list[bool] = [True] * len(stories)
        self.result_stories: list[dict] = []

        self.setWindowTitle("Review section titles")
        self.setModal(True)
        self.resize(960, 640)
        self._build()
        self._populate()
        self._refresh_summary()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.summary = QLabel("")
        self.summary.setObjectName("ReviewSummary")
        layout.addWidget(self.summary)

        explanation = QLabel(
            "Uncheck a row to merge its content back into the previous "
            "section. Double-click a title to edit it. The right pane shows "
            "the source text for the selected section.")
        explanation.setObjectName("FormHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: table.
        left = QWidget(); lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(4)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "#", "Title", "Pages"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(
            lambda r, c, pr, pc: self._show_preview(r))
        lv.addWidget(self.table, 1)
        splitter.addWidget(left)

        # Right: source preview.
        right = QWidget(); rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0); rv.setSpacing(4)
        self.preview_title = QLabel("(select a section)")
        self.preview_title.setObjectName("PreviewTitle")
        f = self.preview_title.font(); f.setBold(True); f.setPointSize(14)
        self.preview_title.setFont(f)
        rv.addWidget(self.preview_title)
        self.preview_meta = QLabel("")
        self.preview_meta.setObjectName("PreviewMeta")
        rv.addWidget(self.preview_meta)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        ff = QFont("Iowan Old Style"); ff.setStyleHint(QFont.Serif)
        ff.setPointSize(13)
        self.preview.setFont(ff)
        rv.addWidget(self.preview, 1)
        splitter.addWidget(right)
        splitter.setSizes([420, 540])

        btns = QHBoxLayout()
        check_all = QPushButton("Check all")
        check_all.setObjectName("GhostBtn")
        check_all.clicked.connect(self._check_all)
        uncheck_all = QPushButton("Uncheck all")
        uncheck_all.setObjectName("GhostBtn")
        uncheck_all.clicked.connect(self._uncheck_all)
        btns.addWidget(check_all)
        btns.addWidget(uncheck_all)
        btns.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        apply_btn = bb.addButton("Apply & save", QDialogButtonBox.AcceptRole)
        apply_btn.setObjectName("PrimaryBtn")
        bb.accepted.connect(self._on_apply)
        bb.rejected.connect(self.reject)
        btns.addWidget(bb)
        layout.addLayout(btns)

    def _populate(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._original))
        for i, s in enumerate(self._original):
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
            check.setFlags(check.flags() & ~Qt.ItemIsEditable)
            check.setCheckState(Qt.Checked if self._checked[i] else Qt.Unchecked)
            self.table.setItem(i, 0, check)

            idx_item = QTableWidgetItem(str(s.get("index", i + 1)))
            idx_item.setFlags(idx_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(i, 1, idx_item)

            title_text = self._titles[i] or "(continuation)"
            title_item = QTableWidgetItem(title_text)
            if not self._titles[i]:
                f = title_item.font(); f.setItalic(True); title_item.setFont(f)
                # Continuation rows shouldn't be unchecked (already merged).
                check.setFlags(check.flags() & ~Qt.ItemIsUserCheckable
                               & ~Qt.ItemIsEnabled)
            self.table.setItem(i, 2, title_item)

            sp = s.get("start_page"); ep = s.get("end_page")
            pages = f"p{sp}" if sp == ep else f"p{sp}–{ep}"
            page_item = QTableWidgetItem(pages)
            page_item.setFlags(page_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(i, 3, page_item)
        self.table.blockSignals(False)
        if self.table.rowCount() > 0:
            self.table.setCurrentCell(0, 2)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        row = item.row()
        col = item.column()
        if col == 0:
            self._checked[row] = item.checkState() == Qt.Checked
            self._refresh_summary()
        elif col == 2:
            new = item.text().strip()
            self._titles[row] = new
            if not new:
                f = item.font(); f.setItalic(True); item.setFont(f)
                item.setText("(continuation)")
            else:
                f = item.font(); f.setItalic(False); item.setFont(f)
            self._refresh_summary()

    def _show_preview(self, row: int) -> None:
        if row < 0 or row >= len(self._original):
            self.preview.setPlainText("")
            self.preview_title.setText("")
            self.preview_meta.setText("")
            return
        s = self._original[row]
        title = self._titles[row] or "(continuation)"
        self.preview_title.setText(title)
        sp = s.get("start_page"); ep = s.get("end_page")
        chars = len(s.get("source") or "")
        pages = f"p{sp}" if sp == ep else f"p{sp}–{ep}"
        self.preview_meta.setText(f"{pages} · {chars} source characters")
        self.preview.setPlainText(s.get("source") or "")

    def _check_all(self) -> None:
        self.table.blockSignals(True)
        for i in range(self.table.rowCount()):
            if self._titles[i]:  # only titled rows are toggleable
                self.table.item(i, 0).setCheckState(Qt.Checked)
                self._checked[i] = True
        self.table.blockSignals(False)
        self._refresh_summary()

    def _uncheck_all(self) -> None:
        self.table.blockSignals(True)
        for i in range(self.table.rowCount()):
            if self._titles[i]:
                self.table.item(i, 0).setCheckState(Qt.Unchecked)
                self._checked[i] = False
        self.table.blockSignals(False)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        kept = sum(1 for i in range(len(self._original))
                   if self._checked[i] and self._titles[i])
        merged = sum(1 for i in range(len(self._original))
                     if not self._checked[i] and self._titles[i])
        total_chars = sum(len(s.get("source") or "") for s in self._original)
        self.summary.setText(
            f"<b>{kept}</b> section(s) kept · {merged} merged into previous · "
            f"~{total_chars:,} source characters total")

    def _on_apply(self) -> None:
        # 1. Apply title edits (in place on a copy of the originals).
        edited = []
        for i, s in enumerate(self._original):
            cp = dict(s)
            new_title = self._titles[i].strip() or None
            cp["title"] = new_title
            edited.append(cp)

        # 2. Compute reject set (rows the user unchecked).
        rejects = {i for i in range(len(edited)) if not self._checked[i]}
        merged = segment_mod.merge_into_previous(edited, rejects)
        self.result_stories = merged

        # 3. Invalidate any existing translations whose source/title no
        #    longer matches the new sections. Without this, translate would
        #    skip them (file already exists) and the assembled book would
        #    use stale translations of the pre-edit sections.
        stale_files = _stale_translations(self.cfg, merged)
        if stale_files:
            ans = QMessageBox.question(
                self, "Re-translate edited sections?",
                f"{len(stale_files)} existing translation(s) no longer match "
                f"the edited sections. Delete them so they will be re-translated "
                f"on the next run? Cancel to keep the old translations as-is.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes)
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                for f in stale_files:
                    try:
                        f.unlink()
                    except OSError:
                        pass

        # 4. Write stories.json and record review state.
        segment_mod.write_stories(self.cfg, merged)
        ws.mark_reviewed(self.cfg)
        ws.clear_stale(self.cfg, "segment")
        self.accept()


def _stale_translations(cfg, new_stories: list[dict]) -> list:
    """Return existing per-section JSON files whose source or title no
    longer matches the new section at that index. Files for indices that
    don't exist in `new_stories` (e.g. after merges reduced the count) are
    also returned as stale."""
    stories_dir = cfg.stories_dir
    if not stories_dir.exists():
        return []
    new_by_index = {s.get("index"): s for s in new_stories}
    stale = []
    for f in stories_dir.glob("*.json"):
        try:
            idx = int(f.name[:3])
        except ValueError:
            continue
        new = new_by_index.get(idx)
        if new is None:
            # Index no longer exists (section was merged away).
            stale.append(f)
            continue
        try:
            existing = _json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            stale.append(f)
            continue
        if existing.get("source") != new.get("source") \
                or existing.get("title") != new.get("title"):
            stale.append(f)
    return stale
