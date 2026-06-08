"""Main workflow window. Welcome screen + per-book workflow board."""

from __future__ import annotations

import json
import sys as _sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QToolBar,
    QVBoxLayout, QWidget,
)

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from config import Config, load_config  # noqa: E402
import segment as segment_mod  # noqa: E402

from . import keys, models, usage as usage_mod  # noqa: E402
from . import workflow_state as ws  # noqa: E402
from .stage_widget import StageCard  # noqa: E402
from .pipeline_runner import PipelineRunner  # noqa: E402
from .metadata_editor import (  # noqa: E402
    MetadataEditor, load_yaml, write_yaml, initial_yaml_skeleton,
    stale_stages_after_edit,
)
from .segment_review import SegmentReviewDialog  # noqa: E402
from .settings_dialog import SettingsDialog  # noqa: E402


PROVIDER_LABEL_FMT = "{provider_label} · {model}"


class MainWindow(QMainWindow):
    """Top-level window. Shows welcome or workflow depending on state."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Book Translator")
        self.resize(1020, 760)
        self.setMinimumSize(820, 580)

        self.cfg: Optional[Config] = None
        self.book_yaml_path: Optional[Path] = None
        self.usage_log: Optional[usage_mod.UsageLog] = None
        self.workers = 4
        self._runner: Optional[PipelineRunner] = None
        self._running_stage: Optional[str] = None

        self._build_toolbar()
        self._build_central()

        # Show welcome first; user opens an existing book or creates a new one.
        self.show_welcome()

    # ------------------------------------------------------------------ #
    # Layout                                                             #
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        tb.setObjectName("MainToolbar")
        self.addToolBar(tb)

        self.new_action = QAction("New book", self)
        self.new_action.triggered.connect(self.start_new_book)
        tb.addAction(self.new_action)

        self.open_action = QAction("Open…", self)
        self.open_action.triggered.connect(self.open_existing_book)
        tb.addAction(self.open_action)

        self.edit_meta_action = QAction("Edit metadata", self)
        self.edit_meta_action.triggered.connect(self.edit_metadata)
        self.edit_meta_action.setEnabled(False)
        tb.addAction(self.edit_meta_action)

        self.open_folder_action = QAction("Open output folder", self)
        self.open_folder_action.triggered.connect(self._open_output_folder)
        self.open_folder_action.setEnabled(False)
        tb.addAction(self.open_folder_action)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.settings_action = QAction("Settings", self)
        self.settings_action.triggered.connect(self.open_settings)
        tb.addAction(self.settings_action)

    def _build_central(self) -> None:
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._welcome = self._build_welcome()
        self._workflow = self._build_workflow()
        self.stack.addWidget(self._welcome)
        self.stack.addWidget(self._workflow)

    def _build_welcome(self) -> QWidget:
        w = QWidget(); w.setObjectName("WelcomePage")
        v = QVBoxLayout(w)
        v.setContentsMargins(40, 40, 40, 40); v.setSpacing(24)
        v.addStretch(1)

        title = QLabel("Book Translator")
        title.setObjectName("WelcomeTitle")
        f = title.font(); f.setPointSize(28); f.setWeight(QFont.DemiBold)
        title.setFont(f); title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)

        sub = QLabel("Translate a book end-to-end — PDF in, bilingual PDF out.")
        sub.setObjectName("WelcomeSubtitle")
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)

        row = QHBoxLayout(); row.setSpacing(24); row.setAlignment(Qt.AlignCenter)
        v.addLayout(row)

        new_card = _welcome_card(
            "Start a new book",
            "Pick a PDF or text file and fill in the metadata.",
            "Choose file…", self.start_new_book)
        open_card = _welcome_card(
            "Open existing book",
            "Resume work on a book you've already started.",
            "Open folder…", self.open_existing_book)
        row.addWidget(new_card)
        row.addWidget(open_card)

        v.addStretch(2)
        return w

    def _build_workflow(self) -> QWidget:
        w = QWidget(); w.setObjectName("WorkflowPage")
        v = QVBoxLayout(w)
        v.setContentsMargins(28, 18, 28, 18); v.setSpacing(12)

        # Header.
        self.header_title = QLabel("")
        self.header_title.setObjectName("BookTitle")
        f = self.header_title.font(); f.setPointSize(22); f.setWeight(QFont.DemiBold)
        self.header_title.setFont(f)
        v.addWidget(self.header_title)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(10)
        self.header_sub = QLabel("")
        self.header_sub.setObjectName("BookSubtitle")
        self.header_sub.setWordWrap(True)
        sub_row.addWidget(self.header_sub, 1)
        self.open_folder_btn = QPushButton("Open output folder")
        self.open_folder_btn.setObjectName("GhostBtn")
        self.open_folder_btn.setToolTip(
            "Reveal this book's output directory in Finder/Explorer.")
        self.open_folder_btn.clicked.connect(self._open_output_folder)
        sub_row.addWidget(self.open_folder_btn)
        v.addLayout(sub_row)

        self.header_usage = QLabel("")
        self.header_usage.setObjectName("UsageLine")
        v.addWidget(self.header_usage)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        v.addWidget(sep)

        # Cards scroll area.
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        cards_holder = QWidget(); cards_v = QVBoxLayout(cards_holder)
        cards_v.setContentsMargins(0, 4, 0, 4); cards_v.setSpacing(12)

        self.cards: dict[str, StageCard] = {}
        for i, stage in enumerate(ws.STAGES):
            card = StageCard(stage, ordinal=i,
                             show_provider=stage in ("transcribe", "translate"))
            card.run_clicked.connect(self._on_run)
            card.reset_clicked.connect(self._on_reset)
            card.review_clicked.connect(self._on_review)
            card.edit_provider.connect(self._on_edit_provider)
            card.cancel_clicked.connect(self._on_cancel)
            self.cards[stage] = card
            cards_v.addWidget(card)
        cards_v.addStretch(1)
        scroll.setWidget(cards_holder)
        v.addWidget(scroll, 1)

        return w

    # ------------------------------------------------------------------ #
    # Welcome actions                                                    #
    # ------------------------------------------------------------------ #

    def show_welcome(self) -> None:
        self.stack.setCurrentWidget(self._welcome)
        self.edit_meta_action.setEnabled(False)
        self.open_folder_action.setEnabled(False)

    def start_new_book(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose input file", "",
            "Documents (*.pdf *.txt);;All files (*)")
        if not path:
            return
        input_path = Path(path)
        skeleton = initial_yaml_skeleton(input_path)
        # Pick where to save book.yaml — default to ./<slug>/book.yaml next
        # to the project root.
        project_root = Path(__file__).resolve().parent.parent.parent
        slug = skeleton["assemble"]["name"]
        proposed = project_root / "books" / f"{slug}.yaml"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save book.yaml as", str(proposed),
            "YAML (*.yaml *.yml)")
        if not save_path:
            return
        save_path = Path(save_path)
        write_yaml(save_path, skeleton)
        # Open the editor pre-filled so the user can tweak before running.
        self._open_metadata_editor(save_path, prior_skeleton=True)

    def open_existing_book(self) -> None:
        # Two paths: pick a book.yaml directly, or pick an output dir that
        # contains one.
        path, _ = QFileDialog.getOpenFileName(
            self, "Open book.yaml", "",
            "YAML (*.yaml *.yml);;All files (*)")
        if not path:
            return
        try:
            self.load_book(Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Open failed", str(exc))

    def _open_metadata_editor(self, book_yaml_path: Path,
                              prior_skeleton: bool = False) -> None:
        data = load_yaml(book_yaml_path)
        dlg = MetadataEditor(data, book_yaml_path, parent=self)
        dlg.saved.connect(self._on_metadata_saved)
        result = dlg.exec()
        if result == MetadataEditor.Accepted:
            try:
                self.load_book(book_yaml_path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Load failed", str(exc))

    def edit_metadata(self) -> None:
        if not self.book_yaml_path:
            return
        self._open_metadata_editor(self.book_yaml_path)

    def _on_metadata_saved(self, path: Path, prior: dict) -> None:
        # Stale-mark stages that depend on what changed.
        try:
            data = load_yaml(path)
        except Exception:  # noqa: BLE001
            return
        stale = stale_stages_after_edit(prior or {}, data)
        if stale and self.cfg is not None:
            ws.mark_stale(self.cfg, stale, "metadata changed")

    # ------------------------------------------------------------------ #
    # Book loading                                                       #
    # ------------------------------------------------------------------ #

    def load_book(self, book_yaml_path: Path) -> None:
        cfg = load_config(book_yaml_path)
        self.cfg = cfg
        self.book_yaml_path = book_yaml_path
        self.usage_log = usage_mod.UsageLog(cfg.output_dir / "usage.jsonl")
        self.stack.setCurrentWidget(self._workflow)
        self.edit_meta_action.setEnabled(True)
        self.open_folder_action.setEnabled(True)
        self._refresh_header()
        self._refresh_state()

    def _refresh_header(self) -> None:
        if not self.cfg:
            return
        c = self.cfg
        self.header_title.setText(
            f"{c.book.title}  <span style='color:#888;font-weight:normal;'>"
            f"({c.languages.source} → {c.languages.target})</span>")
        try:
            inp_rel = c.input.pdf
        except Exception:  # noqa: BLE001
            inp_rel = "?"
        self.header_sub.setText(
            f"In: {inp_rel}  ·  pages {c.input.first_page}–{c.input.last_page}  ·  "
            f"Output: {c.output_dir}")
        self._refresh_usage_label()

    def _refresh_usage_label(self) -> None:
        if not self.usage_log:
            self.header_usage.setText("")
            return
        t = self.usage_log.summary.total
        ft = usage_mod.format_tokens
        breakdown = " · ".join(
            f"{stage}: {ft(tot.input_tokens)}/{ft(tot.output_tokens)}"
            for stage, tot in self.usage_log.summary.by_stage.items()
        )
        self.header_usage.setText(
            f"Token usage: <b>{ft(t.input_tokens)}</b> in / "
            f"<b>{ft(t.output_tokens)}</b> out  "
            f"({t.calls} API call(s)){' · ' + breakdown if breakdown else ''}")

    def _refresh_state(self) -> None:
        if not self.cfg:
            return
        state = ws.compute(self.cfg)
        # Apply running override for the currently active stage.
        if self._running_stage and self._running_stage in state.stages:
            current = state.stages[self._running_stage]
            state.stages[self._running_stage] = ws.StageStatus(
                stage=self._running_stage, status=ws.Status.RUNNING,
                done=current.done, total=current.total,
                detail=current.detail)
        for stage, card in self.cards.items():
            s = state[stage]
            card.set_status(s)
            # Enable downstream cards only if upstream is complete-ish.
            card.set_enabled_by_upstream(*self._upstream_ok(stage, state))
            # Provider line for transcribe/translate.
            if stage == "transcribe":
                p = self.cfg.providers.transcribe
                card.set_provider_line(
                    f"Provider: {models.PROVIDER_LABELS.get(p.provider, p.provider)} · {p.model}")
            elif stage == "translate":
                p = self.cfg.providers.translate
                card.set_provider_line(
                    f"Provider: {models.PROVIDER_LABELS.get(p.provider, p.provider)} · {p.model}")
            elif stage == "assemble":
                card.set_outputs(self._assemble_outputs())

    def _open_output_folder(self) -> None:
        if not self.cfg:
            return
        d = self.cfg.output_dir
        d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _maybe_warn_chrome_missing(self) -> None:
        """If the user asked for book-pdf but Chrome wasn't found, surface
        a clear note in the assemble card's hint line and the status bar
        instead of letting the user wonder why no PDF appeared.
        """
        if not self.cfg:
            return
        wanted_pdf = "book-pdf" in self.cfg.assemble.formats
        if not wanted_pdf:
            return
        pdf_path = self.cfg.output_dir / f"{self.cfg.assemble.name}.pdf"
        chrome = Path(self.cfg.assemble.chrome_path)
        if not pdf_path.exists() and not chrome.exists():
            self.statusBar().showMessage(
                "Assembled HTML only — install Google Chrome to also produce a PDF.",
                10000)
            card = self.cards.get("assemble")
            if card is not None:
                card.show_log_line(
                    "⚠  Chrome not found — PDF skipped. Install Google Chrome "
                    "and click Re-run to produce a PDF as well.")

    def _assemble_outputs(self) -> list[Path]:
        """Return the assemble stage's output files in display order.

        PDF first (headline artifact), then styled book HTML, then the
        bilingual review files. Non-existent files are filtered by the card.
        """
        if not self.cfg:
            return []
        out = self.cfg.output_dir
        name = self.cfg.assemble.name
        # Priority order regardless of which formats were configured — the
        # card filters to whatever actually exists on disk.
        return [
            out / f"{name}.pdf",
            out / f"{name}.html",
            out / f"{name}_review.html",
            out / f"{name}_review.md",
        ]

    def _upstream_ok(self, stage: str, state: ws.WorkflowState) -> tuple[bool, str]:
        """Return (enabled, hint)."""
        order = ws.STAGES
        idx = order.index(stage)
        if idx == 0:
            return True, ""
        # Special-case: transcribe needs at least some rendered pages.
        if stage == "transcribe":
            r = state["render"]
            if r.status in (ws.Status.COMPLETE, ws.Status.PARTIAL,
                            ws.Status.STALE):
                return True, ""
            return False, "render PDF pages first"
        if stage == "segment":
            t = state["transcribe"]
            if t.status in (ws.Status.COMPLETE, ws.Status.PARTIAL,
                            ws.Status.STALE):
                return True, ""
            return False, "transcribe pages first"
        if stage == "translate":
            s = state["segment"]
            if s.status in (ws.Status.COMPLETE, ws.Status.NEEDS_REVIEW,
                            ws.Status.STALE):
                return True, ""
            return False, "segment first"
        if stage == "assemble":
            t = state["translate"]
            if t.status in (ws.Status.COMPLETE, ws.Status.PARTIAL,
                            ws.Status.STALE):
                return True, ""
            return False, "translate sections first"
        return True, ""

    # ------------------------------------------------------------------ #
    # Card actions                                                       #
    # ------------------------------------------------------------------ #

    def _on_run(self, stage: str) -> None:
        if not self.cfg:
            return
        if self._running_stage is not None:
            QMessageBox.information(self, "Busy",
                                    f"{self._running_stage!r} is already running.")
            return
        if not self._require_keys_for(stage):
            return
        # For translate, clear stale flag because we're regenerating.
        force = False
        self._running_stage = stage
        self._refresh_state()
        runner = PipelineRunner(
            self.cfg, stage,
            workers=self.workers, force=force, usage_log=self.usage_log)
        runner.progress.connect(self._on_progress)
        runner.usage.connect(self._on_usage)
        runner.log.connect(self._on_log)
        runner.finished.connect(self._on_finished)
        self._runner = runner
        runner.start()

    def _on_cancel(self, stage: str) -> None:
        if self._runner:
            self._runner.cancel()

    def _on_reset(self, stage: str) -> None:
        if not self.cfg:
            return
        ans = QMessageBox.question(
            self, "Reset stage",
            f"Delete all artifacts for the '{stage}' stage? "
            f"Downstream stages will turn stale.")
        if ans != QMessageBox.Yes:
            return
        n = ws.reset_stage(self.cfg, stage)
        self._refresh_state()
        self._refresh_usage_label()
        self.statusBar().showMessage(f"Reset {stage}: removed {n} file(s)", 4000)

    def _on_review(self, stage: str) -> None:
        if not self.cfg:
            return
        # Load existing stories.json if present; otherwise compute fresh.
        if self.cfg.stories_json.exists():
            try:
                stories = json.loads(self.cfg.stories_json.read_text())
            except Exception:  # noqa: BLE001
                QMessageBox.warning(self, "Stories unreadable",
                                    "stories.json could not be parsed.")
                return
        else:
            if not self._require_keys_for("segment"):
                return
            try:
                stories = segment_mod.run_no_write(self.cfg)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "Segment failed", str(exc))
                return
        dlg = SegmentReviewDialog(self.cfg, stories, parent=self)
        if dlg.exec() == SegmentReviewDialog.Accepted:
            self.statusBar().showMessage(
                f"Saved {len(dlg.result_stories)} reviewed section(s).", 4000)
        self._refresh_state()

    def _on_edit_provider(self, stage: str) -> None:
        # Shortcut: open the metadata editor on the Providers tab.
        if not self.book_yaml_path:
            return
        data = load_yaml(self.book_yaml_path)
        dlg = MetadataEditor(data, self.book_yaml_path, parent=self)
        # Find "Providers" tab.
        for i in range(dlg.tabs.count()):
            if dlg.tabs.tabText(i) == "Providers":
                dlg.tabs.setCurrentIndex(i)
                break
        dlg.saved.connect(self._on_metadata_saved)
        if dlg.exec() == MetadataEditor.Accepted:
            self.load_book(self.book_yaml_path)

    # ------------------------------------------------------------------ #
    # Runner signals                                                     #
    # ------------------------------------------------------------------ #

    def _on_progress(self, stage: str, done: int, total: int, line: str) -> None:
        card = self.cards.get(stage)
        if not card:
            return
        # Update the progress bar + ETA without touching the global pill state.
        card.update_progress(done, total)
        if line:
            card.show_log_line(line)

    def _on_usage(self, stage: str, rec: dict) -> None:
        self._refresh_usage_label()

    def _on_log(self, line: str) -> None:
        if self._running_stage:
            card = self.cards.get(self._running_stage)
            if card:
                card.show_log_line(line)

    def _on_finished(self, stage: str, ok: bool, error: str) -> None:
        self._running_stage = None
        if ok and stage == "segment":
            # Open the review dialog with the freshly-computed stories.
            stories = getattr(self._runner, "segment_result", None) or []
            if stories:
                dlg = SegmentReviewDialog(self.cfg, stories, parent=self)
                if dlg.exec() == SegmentReviewDialog.Accepted:
                    self.statusBar().showMessage(
                        f"Saved {len(dlg.result_stories)} reviewed section(s).",
                        4000)
        elif ok and stage == "assemble":
            self._maybe_warn_chrome_missing()
        elif not ok:
            QMessageBox.critical(self, f"{stage} failed",
                                 error or "Unknown error")
        # CRITICAL: do NOT drop `self._runner = None` here. The runner's
        # QThread may still be tearing down (assemble in particular runs a
        # Chrome subprocess that can keep the worker thread alive for several
        # seconds after our `finished` signal fires). Dropping the Python
        # reference now causes the QThread destructor to run while the thread
        # is still alive → abort. Wait for the thread's own `finished` signal
        # before releasing the reference.
        runner = self._runner
        if runner is not None and runner.thread_done is not None:
            runner.thread_done.connect(
                lambda r=runner: self._discard_runner(r))
        # Defer state refresh so the QThread fully tears down first.
        QTimer.singleShot(50, self._refresh_state)
        QTimer.singleShot(50, self._refresh_usage_label)

    def _discard_runner(self, runner) -> None:
        """Called when the runner's worker thread has truly exited."""
        if self._runner is runner:
            self._runner = None

    # ------------------------------------------------------------------ #
    # Keys                                                               #
    # ------------------------------------------------------------------ #

    def _require_keys_for(self, stage: str) -> bool:
        if not self.cfg:
            return False
        needed: list[str] = []
        if stage in ("transcribe",):
            needed.append(self.cfg.providers.transcribe.provider)
        if stage in ("translate", "segment"):
            # segment uses the translate provider for pruning false titles
            needed.append(self.cfg.providers.translate.provider)
        missing = [p for p in set(needed) if not keys.get(p)]
        if not missing:
            return True
        labels = ", ".join(keys.LABELS[p] for p in missing)
        ans = QMessageBox.question(
            self, "API key required",
            f"This stage uses {labels}, but no key is configured. "
            f"Open Settings to add one?")
        if ans == QMessageBox.Yes:
            self.open_settings()
        return False

    def open_settings(self) -> None:
        dlg = SettingsDialog(workers=self.workers, parent=self)
        if dlg.exec() == SettingsDialog.Accepted:
            self.workers = dlg.workers

    # ------------------------------------------------------------------ #
    # Misc                                                               #
    # ------------------------------------------------------------------ #

    def maybe_import_env(self) -> None:
        """Offer to import .env keys into the keychain on first launch."""
        if any(keys.status().values()):
            return  # already have at least one key — skip prompt
        env = Path(__file__).resolve().parent.parent.parent / ".env"
        if not env.exists():
            return
        ans = QMessageBox.question(
            self, "Import keys from .env?",
            "A .env file with API keys was found. Import them into the "
            "system keychain so they're stored securely?")
        if ans == QMessageBox.Yes:
            imported = keys.import_from_env_file(env)
            if imported:
                self.statusBar().showMessage(
                    f"Imported keys: {', '.join(imported)}", 5000)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _welcome_card(title: str, subtitle: str, btn_text: str, on_click) -> QFrame:
    f = QFrame()
    f.setObjectName("WelcomeCard")
    v = QVBoxLayout(f); v.setContentsMargins(24, 22, 24, 22); v.setSpacing(8)
    f.setFixedWidth(280); f.setFixedHeight(180)
    t = QLabel(title); t.setObjectName("WelcomeCardTitle")
    fnt = t.font(); fnt.setPointSize(16); fnt.setWeight(QFont.DemiBold)
    t.setFont(fnt)
    v.addWidget(t)
    s = QLabel(subtitle); s.setObjectName("WelcomeCardSub"); s.setWordWrap(True)
    v.addWidget(s)
    v.addStretch(1)
    b = QPushButton(btn_text); b.setObjectName("PrimaryBtn")
    b.clicked.connect(on_click)
    v.addWidget(b)
    return f
