"""Render a range of PDF pages to PNG images via pypdfium2.

We use pypdfium2 (a wrapper around Google's PDFium) instead of the
Poppler CLI (`pdftoppm`) so the .app bundle has no external binary
dependencies. pypdfium2 ships universal2 wheels for macOS.
"""

import argparse
import sys
from pathlib import Path

import pypdfium2 as pdfium

from config import Config, load_config


def render(pdf: Path, first: int, last: int, dpi: int,
           pages_dir: Path) -> list[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(pdf)
    n_pages = len(doc)
    # PDFium renders at 72 dpi by default; `scale` is the multiplier.
    scale = dpi / 72.0

    written: list[Path] = []
    for page_num in range(first, last + 1):
        out = pages_dir / f"page_{page_num:04d}.png"
        if out.exists():
            written.append(out)
            continue
        idx = page_num - 1  # PDF page numbers are 1-based; pypdfium2 is 0-based.
        if idx < 0 or idx >= n_pages:
            print(f"WARNING: page {page_num} is out of range (1..{n_pages})",
                  file=sys.stderr)
            continue
        page = doc[idx]
        bitmap = page.render(scale=scale, grayscale=True)
        bitmap.to_pil().save(out, "PNG")
        written.append(out)
        print(f"rendered {out.name}")
    return written


def run(cfg: Config, first: int | None = None, last: int | None = None,
        dpi: int | None = None) -> list[Path]:
    pdf = cfg.input.pdf
    if not pdf.exists():
        raise SystemExit(f"PDF not found: {pdf}")
    return render(
        pdf,
        first or cfg.input.first_page,
        last or cfg.input.last_page,
        dpi or cfg.input.dpi,
        cfg.pages_dir,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    ap.add_argument("--first", type=int, default=None,
                    help="override input.first_page from config")
    ap.add_argument("--last", type=int, default=None,
                    help="override input.last_page from config")
    ap.add_argument("--dpi", type=int, default=None,
                    help="override input.dpi from config")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pages = run(cfg, first=args.first, last=args.last, dpi=args.dpi)
    print(f"\n{len(pages)} page(s) ready in {cfg.pages_dir}")


if __name__ == "__main__":
    main()
