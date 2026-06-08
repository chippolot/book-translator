"""Tabbed editor for book.yaml.

Used both as the new-book wizard and as the always-available "Edit
metadata" dialog. Round-trips through ruamel.yaml so user comments are
preserved across edits.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from ruamel.yaml import YAML

from config import PAGE_SIZE_PRESETS

from . import models
from . import workflow_state as ws


# Curated list of book-friendly fonts surfaced in the Assemble tab's
# Font family dropdown. Each is either a Google Font (auto-loaded via an
# `@import` in the generated stylesheet — see _GOOGLE_FONTS in assemble.py)
# or a face that ships with macOS. The dropdown is editable, so users can
# also type any font name their target Mac has installed.
ASSEMBLE_FONT_CHOICES: tuple[str, ...] = (
    "EB Garamond",
    "Iowan Old Style",
    "Hoefler Text",
    "Baskerville",
    "Georgia",
    "Palatino",
    "Cochin",
    "Charter",
    "Lora",
    "Crimson Pro",
    "Cormorant Garamond",
    "Source Serif Pro",
    "Libre Baskerville",
)


# --------------------------------------------------------------------------- #
# YAML round-trip                                                             #
# --------------------------------------------------------------------------- #

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml(path: Path) -> dict:
    return _yaml().load(path.read_text(encoding="utf-8")) or {}


def dump_yaml(data: dict) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_yaml(data)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Editor dialog                                                                #
# --------------------------------------------------------------------------- #

SLUG_RE = re.compile(r"^[A-Za-z0-9_]+$")


class MetadataEditor(QDialog):
    """Dialog. Exposes `data` (the dict) and `book_yaml_path` (where to save).

    Emits `saved` with (path, prior_data) after successful save so callers
    can compute stale stages.
    """

    saved = Signal(Path, dict)  # new path, prior data snapshot (pre-edit)

    def __init__(self, data: dict, book_yaml_path: Path,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit book metadata")
        self.setModal(True)
        self.resize(720, 640)
        self.data = data
        self.book_yaml_path = book_yaml_path
        self._prior = _deep_copy_yaml(data)
        self._build()
        self._load_into_fields()

    # ----- layout -----
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        self.path_label = QLabel(str(self.book_yaml_path))
        self.path_label.setObjectName("PathLabel")
        layout.addWidget(self.path_label)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self._build_book_tab()
        self._build_input_tab()
        self._build_languages_tab()
        self._build_prompts_tab()
        self._build_providers_tab()
        self._build_output_tab()
        self._build_assemble_tab()
        self._build_validate_tab()
        self._build_raw_tab()

        self.stale_banner = QLabel("")
        self.stale_banner.setObjectName("StaleBanner")
        self.stale_banner.setWordWrap(True)
        self.stale_banner.setVisible(False)
        layout.addWidget(self.stale_banner)

        btns = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_book_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Antigone")
        self.author_edit = QLineEdit()
        self.byline_edit = QLineEdit()
        self.byline_edit.setPlaceholderText("trans. Leconte de Lisle · 1877")
        self.subtitle_edit = QLineEdit()
        self.subtitle_edit.setPlaceholderText("(an English translation)")
        self.credit_edit = QLineEdit()
        self.about_edit = QPlainTextEdit()
        self.about_edit.setPlaceholderText(
            "<p>A short HTML description of the book.</p>")
        self.about_edit.setFixedHeight(120)

        self.cover_edit = QLineEdit()
        cover_row = QHBoxLayout()
        cover_row.setContentsMargins(0, 0, 0, 0)
        cover_row.addWidget(self.cover_edit, 1)
        cover_btn = QPushButton("Browse…")
        cover_btn.clicked.connect(self._pick_cover)
        cover_row.addWidget(cover_btn)
        cover_holder = QWidget(); cover_holder.setLayout(cover_row)

        f.addRow("Title*", self.title_edit)
        f.addRow("Author", self.author_edit)
        f.addRow("Byline", self.byline_edit)
        f.addRow("Subtitle (target)", self.subtitle_edit)
        f.addRow("Cover image", cover_holder)
        f.addRow("Credit", self.credit_edit)
        f.addRow("About (HTML)", self.about_edit)

        self.tabs.addTab(w, "Book")

    def _build_input_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.input_path_edit = QLineEdit()
        pick_row = QHBoxLayout(); pick_row.setContentsMargins(0, 0, 0, 0)
        pick_row.addWidget(self.input_path_edit, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_input)
        pick_row.addWidget(b)
        holder = QWidget(); holder.setLayout(pick_row)

        self.first_page = QSpinBox(); self.first_page.setRange(1, 99999)
        self.last_page = QSpinBox(); self.last_page.setRange(1, 99999)
        self.dpi = QSpinBox(); self.dpi.setRange(72, 600); self.dpi.setSingleStep(25)
        self.dpi.setValue(200)

        self.input_info = QLabel("")
        self.input_info.setObjectName("FormHint")

        f.addRow("Input file (PDF/TXT)*", holder)
        f.addRow("", self.input_info)
        f.addRow("First page", self.first_page)
        f.addRow("Last page", self.last_page)
        f.addRow("DPI", self.dpi)

        self.tabs.addTab(w, "Input")

    def _build_languages_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.source_lang = QComboBox(); self.source_lang.setEditable(True)
        for L in ("German", "French", "Italian", "Spanish", "Portuguese",
                  "Russian", "Latin", "Ancient Greek", "Japanese", "Chinese"):
            self.source_lang.addItem(L)
        self.target_lang = QComboBox(); self.target_lang.setEditable(True)
        for L in ("English", "German", "French", "Spanish", "Italian"):
            self.target_lang.addItem(L)
        f.addRow("Source language*", self.source_lang)
        f.addRow("Target language", self.target_lang)
        self.tabs.addTab(w, "Languages")

    def _build_prompts_tab(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8); v.setSpacing(6)

        def text_block(label: str, hint: str) -> QPlainTextEdit:
            v.addWidget(_section_label(label))
            h = QLabel(hint); h.setObjectName("FormHint"); h.setWordWrap(True)
            v.addWidget(h)
            ed = QPlainTextEdit(); ed.setFixedHeight(80)
            ff = QFont("Menlo"); ff.setStyleHint(QFont.TypeWriter)
            ed.setFont(ff)
            v.addWidget(ed)
            return ed

        self.book_context = text_block(
            "Book context",
            "1–3 sentences about the book. Era, genre, register.")
        self.transcription_notes = text_block(
            "Transcription notes",
            "Script type, orthography quirks, OCR hazards (e.g. Fraktur, long-s).")
        self.segmentation_notes = text_block(
            "Segmentation notes",
            "What counts as a section title in THIS book — and what doesn't "
            "(speaker labels, stanza markers, scene casts). This is what "
            "reduces false-positive titles.")
        self.translation_style = text_block(
            "Translation style",
            "Stylistic register, tone, verse handling.")

        v.addStretch(1)
        self.tabs.addTab(w, "Prompts")

    def _build_providers_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.t_provider = QComboBox()
        for p in models.PROVIDERS:
            self.t_provider.addItem(models.PROVIDER_LABELS[p], p)
        self.t_model = QComboBox(); self.t_model.setEditable(True)
        self.t_provider.currentIndexChanged.connect(
            lambda _: self._refill_models("transcribe"))

        self.tr_provider = QComboBox()
        for p in models.PROVIDERS:
            self.tr_provider.addItem(models.PROVIDER_LABELS[p], p)
        self.tr_model = QComboBox(); self.tr_model.setEditable(True)
        self.tr_provider.currentIndexChanged.connect(
            lambda _: self._refill_models("translate"))

        f.addRow(_section_label("Transcribe (vision)"), QLabel(""))
        f.addRow("Provider", self.t_provider)
        f.addRow("Model", self.t_model)
        f.addRow(_section_label("Translate (text)"), QLabel(""))
        f.addRow("Provider", self.tr_provider)
        f.addRow("Model", self.tr_model)
        self.tabs.addTab(w, "Providers")

    def _build_output_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.output_dir = QLineEdit()
        self.pages_dir = QLineEdit()
        f.addRow("Output directory", self.output_dir)
        f.addRow("Pages directory", self.pages_dir)
        hint = QLabel(
            "Default to <code>out/&lt;name&gt;</code> and "
            "<code>pages/&lt;name&gt;</code>. Use absolute paths or paths "
            "relative to <code>book.yaml</code>.")
        hint.setObjectName("FormHint"); hint.setWordWrap(True)
        f.addRow("", hint)
        self.tabs.addTab(w, "Output")

    def _build_assemble_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.assemble_name = QLineEdit()
        self.assemble_name.setPlaceholderText("Antigone_English")
        self.fmt_sbs = QCheckBox("Side-by-side bilingual HTML")
        self.fmt_book_html = QCheckBox("Styled book HTML")
        self.fmt_book_pdf = QCheckBox("Book PDF (via Chrome)")
        self.chrome_path = QLineEdit()
        chrome_row = QHBoxLayout(); chrome_row.setContentsMargins(0, 0, 0, 0)
        chrome_row.addWidget(self.chrome_path, 1)
        b = QPushButton("Browse…"); b.clicked.connect(self._pick_chrome)
        chrome_row.addWidget(b)
        chrome_holder = QWidget(); chrome_holder.setLayout(chrome_row)

        f.addRow("Filename stem", self.assemble_name)
        f.addRow("Formats", self.fmt_sbs)
        f.addRow("", self.fmt_book_html)
        f.addRow("", self.fmt_book_pdf)
        f.addRow("Chrome path", chrome_holder)

        # Visual divider between operational settings and aesthetic ones.
        f.addRow(_hline(), QLabel(""))
        style_label = _section_label("Book styling")
        f.addRow(style_label, QLabel(""))

        # Font family — editable combo with each item rendered in its own
        # face so it looks like a real font picker.
        self.style_font_family = QComboBox()
        self.style_font_family.setEditable(True)
        for fam in ASSEMBLE_FONT_CHOICES:
            self.style_font_family.addItem(fam)
            idx = self.style_font_family.count() - 1
            preview_font = QFont(fam)
            preview_font.setPointSize(13)
            self.style_font_family.setItemData(idx, preview_font, Qt.FontRole)
        family_hint = QLabel(
            "Used for the body text and headings. macOS ships several of "
            "these (Iowan Old Style, Hoefler Text, Baskerville, Georgia, "
            "Palatino, Cochin, Charter); the rest are loaded automatically "
            "from Google Fonts when the PDF is rendered.")
        family_hint.setObjectName("FormHint"); family_hint.setWordWrap(True)

        # Body font size — 0.5pt resolution, 9–14pt is a sensible book range.
        self.style_body_size = QDoubleSpinBox()
        self.style_body_size.setRange(8.0, 16.0)
        self.style_body_size.setSingleStep(0.5)
        self.style_body_size.setDecimals(1)
        self.style_body_size.setSuffix(" pt")

        # Page size — preset labels mapped to CSS values; editable for custom.
        self.style_page_size = QComboBox()
        self.style_page_size.setEditable(True)
        for label in PAGE_SIZE_PRESETS:
            self.style_page_size.addItem(label, PAGE_SIZE_PRESETS[label])
        page_hint = QLabel(
            "The physical page size of the PDF. \"6 × 9 in\" is the "
            "standard trade paperback. You can also type a raw CSS size "
            "like <code>5.25in 8in</code>.")
        page_hint.setObjectName("FormHint"); page_hint.setWordWrap(True)

        # Custom CSS escape hatch for power users.
        self.style_custom_css = QPlainTextEdit()
        self.style_custom_css.setFixedHeight(100)
        # Lock the size policy to Fixed so the surrounding QVBoxLayout
        # (see _stack) correctly accounts for both the textedit and its
        # hint label below; the default Expanding policy makes the
        # holder report a too-small total height, causing the hint to
        # render inside the textedit's bounds.
        self.style_custom_css.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        ff = QFont("Menlo"); ff.setStyleHint(QFont.TypeWriter)
        self.style_custom_css.setFont(ff)
        self.style_custom_css.setPlaceholderText(
            "/* Optional. Appended after the built-in stylesheet. */\n"
            "/* body { line-height: 1.6; } */\n"
            "/* .story h2 { font-size: 17pt; } */")
        custom_hint = QLabel(
            "Optional. Any CSS you put here is appended after the built-in "
            "stylesheet, so you can override anything (line-height, "
            "margins, heading sizes, colors).")
        custom_hint.setObjectName("FormHint"); custom_hint.setWordWrap(True)

        f.addRow("Font family", _stack(self.style_font_family, family_hint))
        f.addRow("Body text size", self.style_body_size)
        f.addRow("Page size", _stack(self.style_page_size, page_hint))
        f.addRow("Custom CSS", _stack(self.style_custom_css, custom_hint))

        self.tabs.addTab(w, "Assemble")

    def _build_validate_tab(self) -> None:
        w = QWidget()
        f = QFormLayout(w)
        self.charset_signals = QLineEdit()
        self.charset_signals.setPlaceholderText("ß ä ö ü")
        hint = QLabel(
            "Space-separated characters that should NOT appear in the target "
            "translation. Used to flag stories that may contain untranslated "
            "source text.")
        hint.setObjectName("FormHint"); hint.setWordWrap(True)
        self.length_min = QLineEdit(); self.length_min.setPlaceholderText("0.4")
        self.length_max = QLineEdit(); self.length_max.setPlaceholderText("2.5")
        self.short_frac = QLineEdit(); self.short_frac.setPlaceholderText("0.25")
        f.addRow("Source charset signals", self.charset_signals)
        f.addRow("", hint)
        f.addRow("Length ratio min", self.length_min)
        f.addRow("Length ratio max", self.length_max)
        f.addRow("Transcript short fraction", self.short_frac)
        self.tabs.addTab(w, "Validate")

    def _build_raw_tab(self) -> None:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        self.raw_edit = QPlainTextEdit()
        ff = QFont("Menlo"); ff.setStyleHint(QFont.TypeWriter)
        self.raw_edit.setFont(ff)
        v.addWidget(self.raw_edit, 1)
        self.raw_error = QLabel("")
        self.raw_error.setObjectName("FormError")
        self.raw_error.setWordWrap(True)
        v.addWidget(self.raw_error)
        self.tabs.addTab(w, "Raw YAML")

    # ----- field load/save -----

    def _load_into_fields(self) -> None:
        d = self.data
        book = d.setdefault("book", {})
        inp = d.setdefault("input", {})
        langs = d.setdefault("languages", {})
        prompts = d.setdefault("prompts", {})
        providers_ = d.setdefault("providers", {})
        output = d.setdefault("output", {})
        assemble_ = d.setdefault("assemble", {})
        validate_ = d.setdefault("validate", {})

        self.title_edit.setText(_s(book.get("title")))
        self.author_edit.setText(_s(book.get("author")))
        self.byline_edit.setText(_s(book.get("byline")))
        self.subtitle_edit.setText(_s(book.get("subtitle_translated")))
        self.credit_edit.setText(_s(book.get("credit")))
        self.about_edit.setPlainText(_s(book.get("about_html")))
        self.cover_edit.setText(_s(book.get("cover")))

        self.input_path_edit.setText(_s(inp.get("pdf")))
        self.first_page.setValue(int(inp.get("first_page") or 1))
        self.last_page.setValue(int(inp.get("last_page") or 1))
        self.dpi.setValue(int(inp.get("dpi") or 200))
        self._refresh_input_info()

        self.source_lang.setCurrentText(_s(langs.get("source")) or "German")
        self.target_lang.setCurrentText(_s(langs.get("target")) or "English")

        self.book_context.setPlainText(_s(prompts.get("book_context")))
        self.transcription_notes.setPlainText(_s(prompts.get("transcription_notes")))
        self.segmentation_notes.setPlainText(_s(prompts.get("segmentation_notes")))
        self.translation_style.setPlainText(_s(prompts.get("translation_style")))

        tp = (providers_.get("transcribe") or {}).get("provider") or "google"
        trp = (providers_.get("translate") or {}).get("provider") or "anthropic"
        self.t_provider.setCurrentIndex(max(0, models.PROVIDERS.index(tp))) \
            if tp in models.PROVIDERS else self.t_provider.setCurrentIndex(0)
        self.tr_provider.setCurrentIndex(max(0, models.PROVIDERS.index(trp))) \
            if trp in models.PROVIDERS else self.tr_provider.setCurrentIndex(0)
        self._refill_models("transcribe", initial=(providers_.get("transcribe") or {}).get("model"))
        self._refill_models("translate", initial=(providers_.get("translate") or {}).get("model"))

        self.output_dir.setText(_s(output.get("dir")) or "out")
        self.pages_dir.setText(_s(output.get("pages_dir")) or "pages")

        self.assemble_name.setText(_s(assemble_.get("name")))
        fmts = assemble_.get("formats") or ("side-by-side-html", "book-html", "book-pdf")
        self.fmt_sbs.setChecked("side-by-side-html" in fmts)
        self.fmt_book_html.setChecked("book-html" in fmts)
        self.fmt_book_pdf.setChecked("book-pdf" in fmts)
        self.chrome_path.setText(_s(assemble_.get("chrome_path")) or
                                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

        style = (assemble_.get("style") or {}) if isinstance(assemble_, dict) else {}
        self.style_font_family.setCurrentText(_s(style.get("font_family")) or "EB Garamond")
        try:
            self.style_body_size.setValue(float(style.get("body_font_size") or 12.0))
        except (TypeError, ValueError):
            self.style_body_size.setValue(12.0)
        page = _s(style.get("page_size")) or "6in 9in"
        # If the stored value matches a preset's CSS value, show its label.
        label_for_page = next(
            (lbl for lbl, val in PAGE_SIZE_PRESETS.items() if val == page),
            None)
        self.style_page_size.setCurrentText(label_for_page or page)
        self.style_custom_css.setPlainText(_s(style.get("custom_css")))

        sigs = validate_.get("source_charset_signals") or []
        if isinstance(sigs, (list, tuple)):
            sigs = " ".join(sigs)
        self.charset_signals.setText(_s(sigs))
        self.length_min.setText(str(validate_.get("length_ratio_min") or 0.4))
        self.length_max.setText(str(validate_.get("length_ratio_max") or 2.5))
        self.short_frac.setText(str(validate_.get("transcript_short_fraction") or 0.25))

        # Raw view
        self._sync_raw_from_data()

    def _refill_models(self, stage: str, initial: Optional[str] = None) -> None:
        if stage == "transcribe":
            prov = self.t_provider.currentData() or "google"
            box = self.t_model
            choices = models.TRANSCRIBE_MODELS.get(prov, [])
        else:
            prov = self.tr_provider.currentData() or "anthropic"
            box = self.tr_model
            choices = models.TRANSLATE_MODELS.get(prov, [])
        existing = box.currentText()
        box.blockSignals(True)
        box.clear()
        for m in choices:
            box.addItem(m)
        keep = initial or existing or models.default_model(stage, prov)
        if keep:
            i = box.findText(keep)
            if i >= 0:
                box.setCurrentIndex(i)
            else:
                box.setEditText(keep)
        box.blockSignals(False)

    def _collect_into_data(self) -> dict:
        """Pull form values into self.data (in place, preserving comments)."""
        d = self.data
        book = d.setdefault("book", {})
        inp = d.setdefault("input", {})
        langs = d.setdefault("languages", {})
        prompts = d.setdefault("prompts", {})
        providers_ = d.setdefault("providers", {})
        output = d.setdefault("output", {})
        assemble_ = d.setdefault("assemble", {})
        validate_ = d.setdefault("validate", {})

        _put(book, "title", self.title_edit.text().strip(), required=True)
        _put(book, "author", self.author_edit.text().strip())
        _put(book, "byline", self.byline_edit.text().strip())
        _put(book, "subtitle_translated", self.subtitle_edit.text().strip())
        _put(book, "credit", self.credit_edit.text().strip())
        _put(book, "about_html", self.about_edit.toPlainText())
        _put(book, "cover", self.cover_edit.text().strip())

        _put(inp, "pdf", self.input_path_edit.text().strip(), required=True)
        inp["first_page"] = int(self.first_page.value())
        inp["last_page"] = int(self.last_page.value())
        inp["dpi"] = int(self.dpi.value())

        langs["source"] = self.source_lang.currentText().strip() or "German"
        langs["target"] = self.target_lang.currentText().strip() or "English"

        _put(prompts, "book_context", self.book_context.toPlainText())
        _put(prompts, "transcription_notes", self.transcription_notes.toPlainText())
        _put(prompts, "segmentation_notes", self.segmentation_notes.toPlainText())
        _put(prompts, "translation_style", self.translation_style.toPlainText())

        t_prov = self.t_provider.currentData() or "google"
        tr_prov = self.tr_provider.currentData() or "anthropic"
        providers_["transcribe"] = providers_.get("transcribe") or {}
        providers_["transcribe"]["provider"] = t_prov
        _put(providers_["transcribe"], "model", self.t_model.currentText().strip())
        providers_["translate"] = providers_.get("translate") or {}
        providers_["translate"]["provider"] = tr_prov
        _put(providers_["translate"], "model", self.tr_model.currentText().strip())

        _put(output, "dir", self.output_dir.text().strip())
        _put(output, "pages_dir", self.pages_dir.text().strip())

        name = self.assemble_name.text().strip()
        if name and not SLUG_RE.match(name):
            raise ValueError(
                "assemble.name must contain only letters, digits, and underscores")
        if name:
            assemble_["name"] = name
        fmts = []
        if self.fmt_sbs.isChecked(): fmts.append("side-by-side-html")
        if self.fmt_book_html.isChecked(): fmts.append("book-html")
        if self.fmt_book_pdf.isChecked(): fmts.append("book-pdf")
        if fmts:
            assemble_["formats"] = fmts
        _put(assemble_, "chrome_path", self.chrome_path.text().strip())

        # Resolve the page-size dropdown back to its CSS value (the combo
        # stores labels, but yaml should hold the CSS).
        page_text = self.style_page_size.currentText().strip()
        page_val = self.style_page_size.currentData()
        if page_val is None:
            # User typed something custom; pass through verbatim. If it
            # matches a known label, swap to the CSS value.
            page_val = PAGE_SIZE_PRESETS.get(page_text, page_text)
        # Only persist `style:` when at least one field deviates from the
        # defaults. Otherwise opening + saving an untouched form would add
        # an inert `style:` block, polluting older yamls and falsely
        # triggering stale-stage warnings.
        font_family = self.style_font_family.currentText().strip()
        body_size = float(self.style_body_size.value())
        page_size = str(page_val).strip()
        custom_css = self.style_custom_css.toPlainText().rstrip()
        deviates = (
            font_family not in ("", "EB Garamond")
            or abs(body_size - 12.0) > 1e-6
            or page_size not in ("", "6in 9in")
            or custom_css != ""
        )
        if deviates:
            style_block = assemble_.get("style") or {}
            if not isinstance(style_block, dict):
                style_block = {}
            _put(style_block, "font_family", font_family)
            style_block["body_font_size"] = body_size
            _put(style_block, "page_size", page_size)
            _put(style_block, "custom_css", custom_css)
            assemble_["style"] = style_block
        else:
            assemble_.pop("style", None)

        sigs = [s for s in re.split(r"\s+", self.charset_signals.text().strip()) if s]
        if sigs:
            validate_["source_charset_signals"] = sigs
        validate_["length_ratio_min"] = _float_or(self.length_min.text(), 0.4)
        validate_["length_ratio_max"] = _float_or(self.length_max.text(), 2.5)
        validate_["transcript_short_fraction"] = _float_or(self.short_frac.text(), 0.25)

        return d

    def _sync_raw_from_data(self) -> None:
        text = dump_yaml(self.data)
        self.raw_edit.blockSignals(True)
        self.raw_edit.setPlainText(text)
        self.raw_edit.blockSignals(False)
        self.raw_error.setText("")

    def _sync_data_from_raw(self) -> bool:
        """Returns True on success."""
        text = self.raw_edit.toPlainText()
        try:
            data = _yaml().load(text)
        except Exception as exc:  # noqa: BLE001
            self.raw_error.setText(f"YAML parse error: {exc}")
            return False
        if not isinstance(data, dict):
            self.raw_error.setText("Top-level YAML must be a mapping.")
            return False
        self.data = data
        self.raw_error.setText("")
        self._load_into_fields()
        return True

    def _on_tab_changed(self, idx: int) -> None:
        # Switching to Raw: collect from form. Leaving Raw: parse it back.
        raw_idx = self.tabs.indexOf(self.tabs.findChild(QPlainTextEdit, "") or self.raw_edit.parent())
        # Simpler: just check the label
        if self.tabs.tabText(idx) == "Raw YAML":
            try:
                self._collect_into_data()
            except ValueError:
                # Allow viewing raw even if form validation fails.
                pass
            self._sync_raw_from_data()
        else:
            # Coming FROM Raw — re-parse.
            if hasattr(self, "_prev_tab_was_raw") and self._prev_tab_was_raw:
                if not self._sync_data_from_raw():
                    # Stay on Raw if invalid.
                    self.tabs.setCurrentIndex(self.tabs.count() - 1)
        self._prev_tab_was_raw = self.tabs.tabText(idx) == "Raw YAML"
        self._refresh_stale_banner()

    def _refresh_input_info(self) -> None:
        p = Path(self.input_path_edit.text().strip()).expanduser()
        if not p.exists():
            self.input_info.setText("(file not found)")
            return
        kind = "PDF" if p.suffix.lower() == ".pdf" else "TXT" if p.suffix.lower() == ".txt" else p.suffix
        try:
            size = p.stat().st_size
            self.input_info.setText(f"{kind} · {_human_bytes(size)}")
        except OSError:
            self.input_info.setText(kind)

    def _refresh_stale_banner(self) -> None:
        # Best-effort preview: compare current form values to self._prior.
        try:
            current = _deep_copy_yaml(self.data)
            self._collect_into_data()
            stale = _stale_stages_from_diff(self._prior, self.data)
        finally:
            # Restore — collect mutates self.data, which is fine, but for
            # the banner preview we want it computed against the latest.
            pass
        if not stale:
            self.stale_banner.setVisible(False)
            return
        nice = ", ".join(sorted(set(stale)))
        self.stale_banner.setText(
            f"⚠  Saving will mark these stages stale: {nice}. "
            f"Their on-disk artifacts will be kept but flagged as out-of-date.")
        self.stale_banner.setVisible(True)

    # ----- pickers -----
    def _pick_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose input file", "",
            "Documents (*.pdf *.txt);;All files (*)")
        if path:
            self.input_path_edit.setText(path)
            self._refresh_input_info()

    def _pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", "",
            "Images (*.png *.jpg *.jpeg);;All files (*)")
        if path:
            self.cover_edit.setText(path)

    def _pick_chrome(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Chrome binary", "",
            "All files (*)")
        if path:
            self.chrome_path.setText(path)

    # ----- save -----
    def _on_save(self) -> None:
        # If Raw is the active tab, parse it first.
        if self.tabs.tabText(self.tabs.currentIndex()) == "Raw YAML":
            if not self._sync_data_from_raw():
                QMessageBox.warning(self, "YAML error",
                                    self.raw_error.text() or "Invalid YAML")
                return
        try:
            self._collect_into_data()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid field", str(exc))
            return
        if not self.data.get("book", {}).get("title"):
            QMessageBox.warning(self, "Missing field", "Book title is required.")
            self.tabs.setCurrentIndex(0)
            self.title_edit.setFocus()
            return
        if not self.data.get("input", {}).get("pdf"):
            QMessageBox.warning(self, "Missing field", "Input file is required.")
            self.tabs.setCurrentIndex(1)
            self.input_path_edit.setFocus()
            return
        try:
            write_yaml(self.book_yaml_path, self.data)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.saved.emit(self.book_yaml_path, self._prior)
        self.accept()


# --------------------------------------------------------------------------- #
# Stale-stage detection                                                       #
# --------------------------------------------------------------------------- #

def stale_stages_after_edit(prior: dict, current: dict) -> list[str]:
    return list(_stale_stages_from_diff(prior, current))


def _stale_stages_from_diff(prior: dict, current: dict) -> set[str]:
    stale: set[str] = set()

    def _diff(path: tuple[str, ...]) -> bool:
        a, b = prior, current
        for k in path:
            a = (a or {}).get(k) if isinstance(a, dict) else None
            b = (b or {}).get(k) if isinstance(b, dict) else None
        return a != b

    if _diff(("input", "pdf")) or _diff(("input", "first_page")) \
            or _diff(("input", "last_page")) or _diff(("input", "dpi")):
        stale.update({"render", "transcribe", "segment", "translate", "assemble"})
    if _diff(("languages", "source")) or _diff(("languages", "target")):
        stale.update({"transcribe", "segment", "translate", "assemble"})
    if _diff(("prompts", "book_context")) or _diff(("prompts", "transcription_notes")):
        stale.add("transcribe")
    if _diff(("prompts", "segmentation_notes")):
        stale.add("segment")
    if _diff(("prompts", "translation_style")):
        stale.add("translate")
    if _diff(("providers", "transcribe")):
        stale.add("transcribe")
    if _diff(("providers", "translate")):
        stale.update({"translate", "segment"})  # segment uses translate provider for prune
    for k in ("title", "author", "byline", "subtitle_translated",
              "about_html", "cover", "credit"):
        if _diff(("book", k)):
            stale.add("assemble"); break
    if _diff(("assemble", "formats")) or _diff(("assemble", "name")) \
            or _diff(("assemble", "chrome_path")) \
            or _diff(("assemble", "style")):
        stale.add("assemble")
    return stale


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _section_label(text: str) -> QLabel:
    lbl = QLabel(text); lbl.setObjectName("SectionLabel")
    f = lbl.font(); f.setBold(True); lbl.setFont(f)
    return lbl


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def _stack(*widgets: QWidget) -> QWidget:
    """Stack widgets vertically in their own container with controlled
    spacing. Used in QFormLayout rows where a control needs a help
    label directly underneath it — QFormLayout doesn't enlarge a row to
    fit a holder widget's internal layout, so we compute and pin the
    holder's minimum height ourselves.
    """
    holder = QWidget()
    v = QVBoxLayout(holder)
    v.setContentsMargins(0, 2, 0, 4)
    spacing = 8
    v.setSpacing(spacing)
    total_h = v.contentsMargins().top() + v.contentsMargins().bottom()
    for i, w in enumerate(widgets):
        v.addWidget(w)
        if isinstance(w, QLabel) and w.wordWrap():
            # Word-wrapped labels report a misleading single-line width
            # for sizeHint(). Budget 54px so a 3-line wrap (the Font
            # family hint is borderline 2/3 depending on dialog width)
            # isn't clipped; shorter 1-2 line hints get a small gap
            # below them, which is acceptable.
            hint = 54
        else:
            # Use sizeHint, but cap at any fixed max the caller set
            # (e.g. QPlainTextEdit.setFixedHeight(100) leaves sizeHint
            # at ~192 for an "ideal" textarea — we want the constrained
            # value).
            sh = w.sizeHint().height()
            mx = w.maximumHeight()
            hint = min(sh, mx) if mx > 0 and mx < 16_777_215 else sh
        total_h += hint
        if i < len(widgets) - 1:
            total_h += spacing
    # Pin the holder to its computed height. Without this, QFormLayout
    # gives spare vertical space to expanding holders, opening a big
    # gap between a control and its help text.
    holder.setFixedHeight(total_h)
    return holder


def _s(v) -> str:
    if v is None:
        return ""
    return str(v)


def _put(d: dict, key: str, value: str, required: bool = False) -> None:
    """Write `value` into `d[key]` if non-empty, otherwise remove it
    (unless required, in which case let the empty value through so a
    later validator can catch it)."""
    if value or required:
        d[key] = value
    else:
        d.pop(key, None)


def _float_or(text: str, default: float) -> float:
    try:
        return float(text)
    except ValueError:
        return default


def _deep_copy_yaml(d):
    # ruamel objects aren't always copy.deepcopy-able cleanly; serialize.
    return _yaml().load(dump_yaml(d)) or {}


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def initial_yaml_skeleton(input_path: Path) -> dict:
    """Sensible defaults for a brand-new book, given a chosen input file."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", input_path.stem).strip("_") or "book"
    return {
        "book": {
            "title": input_path.stem.replace("_", " "),
            "author": "",
        },
        "input": {
            "pdf": str(input_path),
            "first_page": 1,
            "last_page": 1,
            "dpi": 200,
        },
        "languages": {"source": "German", "target": "English"},
        "prompts": {
            "book_context": "",
            "transcription_notes": "",
            "segmentation_notes": "",
            "translation_style": "",
        },
        "providers": {
            "transcribe": {"provider": "google"},
            "translate": {"provider": "anthropic"},
        },
        "output": {"dir": f"out/{slug}", "pages_dir": f"pages/{slug}"},
        "assemble": {
            "name": slug,
            "formats": ["side-by-side-html", "book-html", "book-pdf"],
            "chrome_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "style": {
                "font_family": "EB Garamond",
                "body_font_size": 12.0,
                "page_size": "6in 9in",
                "custom_css": "",
            },
        },
        "validate": {
            "source_charset_signals": [],
            "length_ratio_min": 0.4,
            "length_ratio_max": 2.5,
            "transcript_short_fraction": 0.25,
        },
    }
