"""Render a range of PDF pages to PNG images using pdftoppm (poppler)."""

import argparse
import subprocess
import sys
from pathlib import Path

from config import Config, load_config


def render(pdf: Path, first: int, last: int, dpi: int,
           pages_dir: Path) -> list[Path]:
    pages_dir.mkdir(parents=True, exist_ok=True)
    # pdftoppm writes <prefix>-<pagenum>.png. We render one page at a time so
    # we control the output name and can skip already-rendered pages cheaply.
    written: list[Path] = []
    for page in range(first, last + 1):
        out = pages_dir / f"page_{page:04d}.png"
        if out.exists():
            written.append(out)
            continue
        prefix = pages_dir / f"_tmp_{page:04d}"
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-gray",
             "-f", str(page), "-l", str(page), str(pdf), str(prefix)],
            check=True,
        )
        produced = sorted(pages_dir.glob(f"_tmp_{page:04d}*.png"))
        if not produced:
            print(f"WARNING: no image produced for page {page}", file=sys.stderr)
            continue
        produced[0].rename(out)
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
