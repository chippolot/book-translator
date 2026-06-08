"""Stage 4: assemble per-section JSON into output.

Modes (driven by cfg.assemble.formats):
  - side-by-side-html  -> bilingual review HTML (source | target)
  - book-html          -> book-styled target-language HTML
  - book-pdf           -> book-html rendered to PDF via headless Chrome
The side-by-side mode also writes a parallel Markdown file.
"""

import argparse
import ctypes
import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from config import Config, load_config


class ChromeMissingError(RuntimeError):
    """Raised by html_to_pdf when the configured Chrome binary doesn't exist.

    `run()` catches this and falls back to producing only the HTML output,
    so users without Chrome still get a usable bilingual document.
    """

DIVIDER_LINE = re.compile(r"^[\s*•·.—–\-]+$")


# --------------------------------------------------------------------------- #
# Loading (with back-compat for the original Popper artifacts)                #
# --------------------------------------------------------------------------- #

def _normalize(s: dict) -> dict:
    """Map legacy keys (german/english/english_title) onto generic ones."""
    if "source" not in s and "german" in s:
        s["source"] = s["german"]
    if "translated" not in s and "english" in s:
        s["translated"] = s["english"]
    if "translated_title" not in s and "english_title" in s:
        s["translated_title"] = s["english_title"]
    return s


def load_stories(cfg: Config) -> list[dict]:
    stories = [_normalize(json.loads(f.read_text()))
               for f in sorted(cfg.stories_dir.glob("*.json"))]
    stories.sort(key=lambda s: s["index"])
    legacy = cfg.output_dir / "english_titles.json"
    if legacy.exists() and any(not s.get("translated_title") for s in stories):
        en = json.loads(legacy.read_text())
        for s in stories:
            if not s.get("translated_title") and en.get(str(s["index"])):
                s["translated_title"] = en[str(s["index"])]
    return stories


def _target_title(s: dict) -> str:
    return s.get("translated_title") or s.get("title") or "Untitled"


def _span(s: dict) -> str:
    a, b = s["start_page"], s["end_page"]
    return f"PDF page {a}" if a == b else f"PDF pages {a}–{b}"


# --------------------------------------------------------------------------- #
# Side-by-side (review) mode                                                  #
# --------------------------------------------------------------------------- #

def _sidebyside_head(cfg: Config) -> str:
    title_html = html.escape(cfg.book.title)
    author_html = html.escape(cfg.book.author)
    pair = f"{cfg.languages.source} / {cfg.languages.target}"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title_html} — {pair}</title>
<style>
 body {{ font-family: Georgia, serif; max-width: 1150px; margin: 2rem auto;
        line-height: 1.5; padding: 0 1rem; }}
 h1 {{ text-align: center; }}
 h2 {{ margin: 2.5rem 0 .2rem; }}
 .pages {{ color: #999; font-size: .8rem; margin-bottom: .6rem; }}
 .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;
         border-top: 1px solid #ccc; padding-top: .8rem; }}
 .src {{ color: #444; }} .tgt {{ color: #000; }}
 .src p, .tgt p {{ margin: 0 0 .8rem; white-space: pre-wrap; }}
 @media (max-width: 720px) {{ .cols {{ grid-template-columns: 1fr; }} }}
</style></head><body>
<h1>{title_html}<br><small>{author_html} — {pair}</small></h1>
"""


def _paras_sbs(text: str) -> str:
    parts = [html.escape(p.strip()) for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in parts)


def write_sidebyside_html(cfg: Config, stories: list[dict]) -> Path:
    out = cfg.output_dir / f"{cfg.assemble.name}_review.html"
    chunks = [_sidebyside_head(cfg)]
    for s in stories:
        title = html.escape(s["title"] or "[untitled]")
        chunks.append(
            f'<h2>{title}</h2><div class="pages">{_span(s)}</div>'
            f'<div class="cols"><div class="src">{_paras_sbs(s["source"])}</div>'
            f'<div class="tgt">{_paras_sbs(s["translated"])}</div></div>'
        )
    chunks.append("</body></html>")
    out.write_text("\n".join(chunks))
    return out


def write_sidebyside_md(cfg: Config, stories: list[dict]) -> Path:
    out = cfg.output_dir / f"{cfg.assemble.name}_review.md"
    pair = f"{cfg.languages.source} / {cfg.languages.target}"
    lines = [f"# {cfg.book.title} — {pair}\n"]
    for s in stories:
        lines.append(f"\n## {s['title'] or '[untitled]'}\n\n_{_span(s)}_\n")
        lines.append(f"**{cfg.languages.source}**\n\n" + s["source"].strip() + "\n")
        lines.append(f"\n**{cfg.languages.target}**\n\n"
                     + s["translated"].strip() + "\n")
    out.write_text("\n".join(lines))
    return out


# --------------------------------------------------------------------------- #
# Book mode                                                                   #
# --------------------------------------------------------------------------- #

BOOK_CSS = """
@import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400&display=swap');
@page { size: 6in 9in; margin: 0.75in 0.7in 0.85in 0.7in; }
@page :first { margin: 0; }
* { box-sizing: border-box; }
body { font-family: 'EB Garamond', 'Iowan Old Style', 'Hoefler Text',
       Baskerville, Georgia, serif;
       font-size: 12pt; line-height: 1.5; color: #111;
       text-align: justify; hyphens: auto; -webkit-hyphens: auto; margin: 0; }

.coverpage { margin: 0; padding: 0; width: 6in; height: 9in;
             background: #6c5039; page-break-after: always;
             display: flex; align-items: center; justify-content: center; }
.coverpage img { max-width: 100%; max-height: 100%; object-fit: contain;
                 display: block; }

.titlepage { text-align: center; padding-top: 28%; page-break-after: always;
             hyphens: none; }
.titlepage h1 { font-size: 30pt; font-weight: 600; font-variant: small-caps;
                letter-spacing: 0.06em; margin: 0 0 0.6em; line-height: 1.15; }
.titlepage .sub { font-size: 14pt; font-style: italic; margin-bottom: 4em;
                  color: #444; }
.titlepage .author { font-size: 13pt; margin-bottom: 8em; }
.titlepage .meta { font-size: 10.5pt; color: #666; font-style: italic; }

.infopage { padding-top: 1em; page-break-after: always; hyphens: none;
            text-align: left; }
.infopage h2 { font-variant: small-caps; font-size: 13pt; letter-spacing: 0.06em;
               margin: 1.6em 0 0.5em; font-weight: 600; }
.infopage h2:first-of-type { margin-top: 0; }
.infopage p { margin: 0 0 0.7em; text-indent: 0; font-size: 11pt; }
.infopage ul { margin: 0.2em 0 0.8em 1.4em; padding: 0; font-size: 11pt; }
.infopage li { margin: 0.15em 0; }
.infopage .credit { margin-top: 3em; font-size: 10pt; color: #555;
                    font-style: italic; text-align: center; }

.toc { page-break-after: always; hyphens: none; }
.toc h2 { font-size: 20pt; font-variant: small-caps; text-align: center;
          letter-spacing: 0.08em; margin: 0.5em 0 2em; font-weight: 600; }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc li { margin: 0 0 0.55em; page-break-inside: avoid; }
.toc a { display: flex; align-items: baseline; color: #111;
         text-decoration: none; }
.toc-title { flex: 0 1 auto; padding-right: 0.4em; }
.toc-dots { flex: 1 1 auto; border-bottom: 1px dotted #999;
            margin: 0 0.3em 0.3em; min-width: 1em; }
.toc-page { flex: 0 0 auto; font-variant-numeric: lining-nums;
            min-width: 1.5em; text-align: right; }

.story { page-break-before: always; }
.story h2 { text-align: center; font-size: 15pt; font-weight: 600;
            font-variant: small-caps; letter-spacing: 0.05em;
            margin: 1.5em 0 0.3em; hyphens: none; }
.story .original { text-align: center; font-size: 10.5pt; font-style: italic;
                   color: #666; margin: 0 0 2em; hyphens: none; }
.story p { margin: 0; text-indent: 1.4em; orphans: 2; widows: 2; }
.story p.first { text-indent: 0; }
.story p.verse { text-indent: 0; text-align: left; margin: 0 0 0.6em 2em;
                 hyphens: none; }
.divider { text-align: center; letter-spacing: 0.5em; margin: 1.2em 0;
           text-indent: 0; }
"""


def _is_verse(para: str) -> bool:
    lines = [l for l in para.split("\n") if l.strip()]
    if len(lines) < 2:
        return False
    return sum(len(l) < 70 for l in lines) >= max(2, int(len(lines) * 0.6))


def _book_paragraphs(text: str) -> str:
    out = []
    first_prose = True
    for raw in text.split("\n\n"):
        para = raw.strip()
        if not para:
            continue
        if DIVIDER_LINE.match(para):
            out.append('<p class="divider">* * *</p>')
            first_prose = True
            continue
        if _is_verse(para):
            lines = "<br>".join(html.escape(l.strip())
                                for l in para.split("\n") if l.strip())
            out.append(f'<p class="verse">{lines}</p>')
            first_prose = True
            continue
        cls = ' class="first"' if first_prose else ""
        first_prose = False
        para = re.sub(r"\s*\n\s*", " ", para)
        out.append(f"<p{cls}>{html.escape(para)}</p>")
    return "\n".join(out)


def _toc_html(stories: list[dict], pages: dict[int, int] | None) -> str:
    items = []
    for s in stories:
        title = html.escape(_target_title(s))
        pg = "" if pages is None else str(pages.get(s["index"], ""))
        items.append(
            f'<li><a href="#story-{s["index"]}">'
            f'<span class="toc-title">{title}</span>'
            f'<span class="toc-dots"></span>'
            f'<span class="toc-page">{pg}</span></a></li>'
        )
    return ('<div class="toc"><h2>Contents</h2><ol>'
            + "".join(items) + "</ol></div>")


def write_book_html(cfg: Config, stories: list[dict],
                    toc_pages: dict[int, int] | None = None) -> Path:
    out = cfg.output_dir / f"{cfg.assemble.name}.html"
    title = html.escape(cfg.book.title)
    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
        f"<title>{title}</title>",
        f"<style>{BOOK_CSS}</style></head><body>",
    ]
    if cfg.book.cover and cfg.book.cover.exists():
        # Copy cover next to the HTML so relative references work.
        dest = cfg.output_dir / cfg.book.cover.name
        if not dest.exists() or dest.resolve() != cfg.book.cover.resolve():
            try:
                dest.write_bytes(cfg.book.cover.read_bytes())
            except (OSError, ValueError):
                pass
        parts.append(f'<div class="coverpage"><img src="{cfg.book.cover.name}"></div>')

    byline = " · ".join(filter(None, [cfg.book.author, cfg.book.byline])) \
        if cfg.book.byline else cfg.book.author
    parts.extend([
        '<div class="titlepage">',
        f'<h1>{title}</h1>',
    ])
    if cfg.book.subtitle_translated:
        parts.append(f'<div class="sub">{html.escape(cfg.book.subtitle_translated)}</div>')
    if byline:
        parts.append(f'<div class="author">{html.escape(byline)}</div>')
    if cfg.book.credit:
        parts.append(f'<div class="meta">{html.escape(cfg.book.credit)}</div>')
    parts.append('</div>')

    if cfg.book.about_html.strip():
        parts.append(f'<div class="infopage"><h2>About this book</h2>'
                     f'{cfg.book.about_html}</div>')

    parts.append(_toc_html(stories, toc_pages))

    for s in stories:
        en = html.escape(_target_title(s))
        de = (s.get("title") or "").strip()
        original = (f'<div class="original">{html.escape(de)}</div>'
                    if de and de != _target_title(s) else "")
        parts.append(
            f'<div class="story" id="story-{s["index"]}">'
            f'<h2>{en}</h2>{original}{_book_paragraphs(s["translated"])}</div>'
        )
    parts.append("</body></html>")
    out.write_text("\n".join(parts))
    return out


def html_to_pdf(cfg: Config, html_path: Path, pdf_path: Path) -> None:
    chrome = cfg.assemble.chrome_path
    if not Path(chrome).exists():
        raise ChromeMissingError(
            f"Chrome not found at {chrome}. Install Google Chrome (or set "
            f"assemble.chrome_path in book.yaml) to enable PDF output.")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
           f"--print-to-pdf={pdf_path}", f"file://{html_path}"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


_STORY_DEST_RE = re.compile(r"^story-(\d+)$")


def read_story_pages(pdf_path: Path) -> dict[int, int]:
    """Map story index → 1-based PDF page number using PDFium's named
    destinations. Replaces the previous `pdfinfo -dests` subprocess so the
    assembled .app has no Poppler dependency.
    """
    doc = pdfium.PdfDocument(pdf_path)
    pages: dict[int, int] = {}
    count = pdfium_c.FPDF_CountNamedDests(doc.raw)
    for i in range(count):
        buf_len = ctypes.c_long(0)
        pdfium_c.FPDF_GetNamedDest(doc.raw, i, None, ctypes.byref(buf_len))
        if buf_len.value <= 0:
            continue
        name_buf = ctypes.create_string_buffer(buf_len.value)
        handle = pdfium_c.FPDF_GetNamedDest(
            doc.raw, i, name_buf, ctypes.byref(buf_len))
        if not handle:
            continue
        raw = ctypes.string_at(name_buf, buf_len.value)
        # PDFium returns UTF-16LE with a trailing null terminator.
        name = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
        m = _STORY_DEST_RE.match(name)
        if not m:
            continue
        page_idx = pdfium_c.FPDFDest_GetDestPageIndex(doc.raw, handle)
        if page_idx >= 0:
            pages[int(m.group(1))] = page_idx + 1
    return pages


def build_book_pdf(cfg: Config, stories: list[dict]) -> Path:
    pdf_path = cfg.output_dir / f"{cfg.assemble.name}.pdf"
    html_path = write_book_html(cfg, stories, toc_pages=None)
    html_to_pdf(cfg, html_path, pdf_path)
    toc_pages = read_story_pages(pdf_path)
    html_path = write_book_html(cfg, stories, toc_pages=toc_pages)
    html_to_pdf(cfg, html_path, pdf_path)
    return pdf_path


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def run(cfg: Config, formats: list[str] | None = None) -> list[Path]:
    stories = load_stories(cfg)
    if not stories:
        raise SystemExit(f"No stories found in {cfg.stories_dir}.")
    formats = list(formats if formats is not None else cfg.assemble.formats)

    written: list[Path] = []
    if "side-by-side-html" in formats:
        h = write_sidebyside_html(cfg, stories)
        m = write_sidebyside_md(cfg, stories)
        written.extend([h, m])
        print(f"side-by-side: {h}\n               {m}")
    if "book-pdf" in formats:
        try:
            p = build_book_pdf(cfg, stories)
            written.append(p)
            print(f"book PDF:     {p}")
        except ChromeMissingError as exc:
            # Chrome isn't installed. Fall back to writing just the book
            # HTML so the user still gets a usable output, and tag the
            # written list with a sentinel comment line they (or the GUI)
            # can surface.
            print(f"WARNING: {exc}", file=sys.stderr)
            print("Falling back to book-html only (no PDF will be produced).",
                  file=sys.stderr)
            p = write_book_html(cfg, stories)
            written.append(p)
            print(f"book HTML:    {p}")
    elif "book-html" in formats:
        p = write_book_html(cfg, stories)
        written.append(p)
        print(f"book HTML:    {p}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    ap.add_argument("--formats", nargs="*", default=None,
                    help="override assemble.formats from config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run(cfg, formats=args.formats)


if __name__ == "__main__":
    main()
