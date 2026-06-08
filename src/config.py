"""Per-book configuration loaded from a YAML file.

Every stage in the pipeline reads this dataclass instead of hard-coded values,
so the same scripts can translate any book by editing book.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_TRANSCRIBE_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google": "gemini-2.5-flash",
    "openai": "gpt-5",
}
DEFAULT_TRANSLATE_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google": "gemini-2.5-flash",
    "openai": "gpt-5",
}

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@dataclass(frozen=True)
class BookMeta:
    title: str
    author: str
    byline: str = ""              # e.g. "writing as Lynkeus · 1900"
    subtitle_translated: str = ""  # e.g. "Fantasies of a Realist"
    about_html: str = ""          # HTML for the info page (optional)
    cover: Optional[Path] = None
    credit: str = ""              # e.g. "Prepared 2026"


@dataclass(frozen=True)
class Input:
    # Source file: `.pdf` (rendered + OCR'd) or `.txt` (chunked directly).
    pdf: Path
    first_page: int
    last_page: int
    dpi: int = 200


@dataclass(frozen=True)
class Languages:
    source: str       # e.g. "German"
    target: str = "English"


@dataclass(frozen=True)
class PromptNotes:
    book_context: str = ""         # spliced into the system prompt
    transcription_notes: str = ""  # extra rules for the transcribe stage
    segmentation_notes: str = ""   # what counts as a section title vs. inline
    translation_style: str = ""    # extra rules for the translate stage


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    model: str


@dataclass(frozen=True)
class Providers:
    transcribe: ProviderSpec
    translate: ProviderSpec


@dataclass(frozen=True)
class ValidateOpts:
    # Characters that should NOT appear (often) in the target translation.
    # e.g. ["ß", "ä", "ö", "ü"] for German source.
    source_charset_signals: tuple[str, ...] = ()
    length_ratio_min: float = 0.4
    length_ratio_max: float = 2.5
    transcript_short_fraction: float = 0.25  # below 25% of page-median = suspicious


@dataclass(frozen=True)
class AssembleOpts:
    chrome_path: str = DEFAULT_CHROME
    name: str = "book"
    formats: tuple[str, ...] = ("side-by-side-html", "book-html", "book-pdf")


@dataclass(frozen=True)
class Config:
    book: BookMeta
    input: Input
    languages: Languages
    prompts: PromptNotes
    providers: Providers
    output_dir: Path
    pages_dir: Path
    validate: ValidateOpts
    assemble: AssembleOpts
    project_root: Path
    config_path: Path

    @property
    def input_kind(self) -> str:
        suffix = self.input.pdf.suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix == ".txt":
            return "txt"
        raise ValueError(
            f"input.pdf must end in .pdf or .txt; got {self.input.pdf}")

    @property
    def transcript_dir(self) -> Path:
        return self.output_dir / "transcript"

    @property
    def stories_dir(self) -> Path:
        return self.output_dir / "stories"

    @property
    def stories_json(self) -> Path:
        return self.output_dir / "stories.json"


def _expand(value: str, base: Path) -> Path:
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else (base / p).resolve()


def _require(d: dict, key: str, ctx: str) -> object:
    if key not in d or d[key] in (None, ""):
        raise ValueError(f"book.yaml: missing required field '{ctx}.{key}'")
    return d[key]


def load_config(path: Path | str) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    base = path.parent
    raw = yaml.safe_load(path.read_text()) or {}

    book = raw.get("book", {}) or {}
    inp = raw.get("input", {}) or {}
    langs = raw.get("languages", {}) or {}
    prompts = raw.get("prompts", {}) or {}
    providers_raw = raw.get("providers", {}) or {}
    output = raw.get("output", {}) or {}
    validate = raw.get("validate", {}) or {}
    assemble = raw.get("assemble", {}) or {}

    cover = book.get("cover")
    cover_path = _expand(cover, base) if cover else None

    t = providers_raw.get("transcribe", {}) or {}
    t_provider = t.get("provider", "google")
    if t_provider not in DEFAULT_TRANSCRIBE_MODELS:
        raise ValueError(f"providers.transcribe.provider must be one of "
                         f"{sorted(DEFAULT_TRANSCRIBE_MODELS)}; got {t_provider!r}")
    t_model = t.get("model") or DEFAULT_TRANSCRIBE_MODELS[t_provider]

    tr = providers_raw.get("translate", {}) or {}
    tr_provider = tr.get("provider", "anthropic")
    if tr_provider not in DEFAULT_TRANSLATE_MODELS:
        raise ValueError(f"providers.translate.provider must be one of "
                         f"{sorted(DEFAULT_TRANSLATE_MODELS)}; got {tr_provider!r}")
    tr_model = tr.get("model") or DEFAULT_TRANSLATE_MODELS[tr_provider]

    # Book-scoped output paths default to out/<name> and pages/<name> so
    # multiple book.yaml configs in one project don't trample each other's
    # rendered pages, transcripts, and stories.
    assemble_name = assemble.get("name") or _slug_for(book.get("title", "book"))

    return Config(
        book=BookMeta(
            title=str(_require(book, "title", "book")),
            author=book.get("author", ""),
            byline=book.get("byline", ""),
            subtitle_translated=book.get("subtitle_translated", ""),
            about_html=book.get("about_html", ""),
            cover=cover_path,
            credit=book.get("credit", ""),
        ),
        input=Input(
            pdf=_expand(_require(inp, "pdf", "input"), base),
            first_page=int(_require(inp, "first_page", "input")),
            last_page=int(_require(inp, "last_page", "input")),
            dpi=int(inp.get("dpi", 200)),
        ),
        languages=Languages(
            source=str(_require(langs, "source", "languages")),
            target=langs.get("target", "English"),
        ),
        prompts=PromptNotes(
            book_context=(prompts.get("book_context") or "").strip(),
            transcription_notes=(prompts.get("transcription_notes") or "").strip(),
            segmentation_notes=(prompts.get("segmentation_notes") or "").strip(),
            translation_style=(prompts.get("translation_style") or "").strip(),
        ),
        providers=Providers(
            transcribe=ProviderSpec(t_provider, t_model),
            translate=ProviderSpec(tr_provider, tr_model),
        ),
        output_dir=_expand(output.get("dir") or f"out/{assemble_name}", base),
        pages_dir=_expand(output.get("pages_dir") or f"pages/{assemble_name}", base),
        validate=ValidateOpts(
            source_charset_signals=tuple(validate.get("source_charset_signals") or ()),
            length_ratio_min=float(validate.get("length_ratio_min", 0.4)),
            length_ratio_max=float(validate.get("length_ratio_max", 2.5)),
            transcript_short_fraction=float(
                validate.get("transcript_short_fraction", 0.25)),
        ),
        assemble=AssembleOpts(
            chrome_path=assemble.get("chrome_path", DEFAULT_CHROME),
            name=assemble_name,
            formats=tuple(assemble.get("formats") or
                          ("side-by-side-html", "book-html", "book-pdf")),
        ),
        # Repo root — where .env lives next to src/. The config file itself
        # may live anywhere; `base` (used for _expand) tracks that separately.
        project_root=Path(__file__).resolve().parent.parent,
        config_path=path,
    )


def _slug_for(title: str) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_")
    return s or "book"
