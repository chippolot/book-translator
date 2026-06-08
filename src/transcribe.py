"""Stage 1: transcribe a range of rendered pages into segmented source text.

Reads pages/page_XXXX.png, writes out/transcript/page_XXXX.json:
  {"page": N, "provider": ..., "model": ..., "segments": [{"title", "text"}]}
Resumable: already-done pages are skipped unless --force.
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

from config import Config, load_config
from providers import transcribe, transcribe_text, UsageCb

# Progress callback receives (done, total, message). Default: None (silent).
ProgressCb = Optional[Callable[[int, int, str], None]]

MAX_RETRIES = 5

# Substrings in `f"{type(exc).__name__}: {exc}"` that flag a retryable
# transient failure. Anything else (auth errors, validation) raises
# immediately so we don't waste a minute backing off something that
# won't recover.
_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "ServerError", "Overloaded",
    "429", "RateLimit", "rate_limit",
    "Timeout", "Timed out", "ConnectionError", "ConnectionResetError",
    "RemoteProtocolError", "RemoteDisconnected",
)


def _is_transient_error(exc: BaseException) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(m in s for m in _TRANSIENT_MARKERS)


# Target chunk size for .txt inputs. Big enough to keep API calls down,
# small enough that any one chunk's transcription fits in an 8k-token reply.
TXT_CHUNK_CHARS = 12000


def _with_retry(image_path: Path, cfg: Config, provider: str, model: str,
                usage_cb: UsageCb = None) -> dict:
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return transcribe(image_path, cfg, provider=provider, model=model,
                              usage_cb=usage_cb)
        except Exception as exc:  # noqa: BLE001 - SDK error types vary
            # Fast-fail on permanent errors (auth, validation) so a bad
            # API key doesn't burn 62 seconds of exponential backoff.
            if attempt == MAX_RETRIES or not _is_transient_error(exc):
                raise
            print(f"  {image_path.name}: attempt {attempt} failed "
                  f"({type(exc).__name__}: {exc}); retrying in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def _with_retry_text(label: str, text: str, cfg: Config,
                     provider: str, model: str,
                     usage_cb: UsageCb = None) -> dict:
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return transcribe_text(text, cfg, provider=provider, model=model,
                                   usage_cb=usage_cb)
        except Exception as exc:  # noqa: BLE001
            if attempt == MAX_RETRIES or not _is_transient_error(exc):
                raise
            print(f"  {label}: attempt {attempt} failed "
                  f"({type(exc).__name__}: {exc}); retrying in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def chunk_text_file(path: Path, target_chars: int = TXT_CHUNK_CHARS) -> list[str]:
    """Split text on paragraph boundaries into ~target_chars chunks.

    A paragraph is a run of lines separated from its neighbours by one or
    more blank lines. We never split within a paragraph: an oversize
    paragraph becomes its own oversize chunk.
    """
    raw = path.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the joining "\n\n"
        if buf and size + para_len > target_chars:
            chunks.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += para_len
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _process_chunk(idx: int, chunk_text: str, cfg: Config,
                   provider: str, model: str, force: bool,
                   out_dir: Path, usage_cb: UsageCb = None) -> str:
    out = out_dir / f"page_{idx:04d}.json"
    if out.exists() and not force:
        return f"chunk {idx}: skip (already done)"
    label = f"chunk {idx}"
    result = _with_retry_text(label, chunk_text, cfg, provider, model,
                              usage_cb=usage_cb)
    result = {"page": idx, "provider": provider, "model": model, **result}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    segs = result["segments"]
    titles = [s["title"] for s in segs if s["title"]]
    note = f", new sections: {titles}" if titles else ""
    return f"chunk {idx}: {len(segs)} segment(s){note}"


def _process(page: int, cfg: Config, provider: str, model: str,
             force: bool, out_dir: Path, pages_dir: Path,
             usage_cb: UsageCb = None) -> str:
    img = pages_dir / f"page_{page:04d}.png"
    out = out_dir / f"page_{page:04d}.json"
    if not img.exists():
        return f"page {page}: SKIP (no rendered image)"
    if out.exists() and not force:
        return f"page {page}: skip (already done)"
    result = _with_retry(img, cfg, provider, model, usage_cb=usage_cb)
    result = {"page": page, "provider": provider, "model": model, **result}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    segs = result["segments"]
    titles = [s["title"] for s in segs if s["title"]]
    note = f", new sections: {titles}" if titles else ""
    return f"page {page}: {len(segs)} segment(s){note}"


def run(cfg: Config, pages: list[int] | None = None,
        workers: int = 4, force: bool = False,
        provider: str | None = None, model: str | None = None,
        progress_cb: ProgressCb = None,
        usage_cb: UsageCb = None,
        cancel: Optional[threading.Event] = None) -> None:
    load_dotenv(cfg.project_root / ".env")
    provider = provider or cfg.providers.transcribe.provider
    model = model or cfg.providers.transcribe.model
    cfg.transcript_dir.mkdir(parents=True, exist_ok=True)

    if cfg.input_kind == "txt":
        _run_txt(cfg, pages, workers, force, provider, model,
                 progress_cb, usage_cb, cancel)
        return

    if pages is None:
        pages = list(range(cfg.input.first_page, cfg.input.last_page + 1))

    print(f"Transcribing {len(pages)} page(s) "
          f"({pages[0]}-{pages[-1]}) with {provider}/{model} "
          f"({workers} workers)\n")
    total = len(pages)
    if progress_cb:
        progress_cb(0, total, "starting")

    failures = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, p, cfg, provider, model, force,
                               cfg.transcript_dir, cfg.pages_dir,
                               usage_cb): p
                   for p in pages}
        for fut in as_completed(futures):
            page = futures[fut]
            done += 1
            try:
                line = fut.result()
                print(line)
                if progress_cb:
                    progress_cb(done, total, line)
            except Exception as exc:  # noqa: BLE001 - SDK error types vary
                failures += 1
                msg = (f"page {page}: FAILED after retries "
                       f"({type(exc).__name__}: {exc})")
                print(msg, file=sys.stderr)
                if progress_cb:
                    progress_cb(done, total, msg)
            if cancel is not None and cancel.is_set():
                # Cancel any pending futures; in-flight ones finish.
                for f in futures:
                    f.cancel()
                print("\nCancelled — in-flight requests may still complete.")
                break

    suffix = f" ({failures} page(s) failed; validator will retry)" if failures else ""
    print(f"\nDone.{suffix}")


def _run_txt(cfg: Config, pages: list[int] | None, workers: int,
             force: bool, provider: str, model: str,
             progress_cb: ProgressCb = None,
             usage_cb: UsageCb = None,
             cancel: Optional[threading.Event] = None) -> None:
    all_chunks = chunk_text_file(cfg.input.pdf)
    total_chunks = len(all_chunks)
    if pages is None:
        first = max(1, cfg.input.first_page)
        last = min(total_chunks, cfg.input.last_page)
        pages = list(range(first, last + 1))

    print(f"Transcribing {len(pages)} chunk(s) of "
          f"{total_chunks} ({pages[0]}-{pages[-1]}) with {provider}/{model} "
          f"(text mode, {workers} workers)\n")
    total = len(pages)
    if progress_cb:
        progress_cb(0, total, "starting")

    failures = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for p in pages:
            if p < 1 or p > total_chunks:
                print(f"chunk {p}: SKIP (out of range 1..{total_chunks})")
                continue
            chunk = all_chunks[p - 1]
            futures[pool.submit(_process_chunk, p, chunk, cfg, provider,
                                model, force, cfg.transcript_dir,
                                usage_cb)] = p
        for fut in as_completed(futures):
            p = futures[fut]
            done += 1
            try:
                line = fut.result()
                print(line)
                if progress_cb:
                    progress_cb(done, total, line)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                msg = (f"chunk {p}: FAILED after retries "
                       f"({type(exc).__name__}: {exc})")
                print(msg, file=sys.stderr)
                if progress_cb:
                    progress_cb(done, total, msg)
            if cancel is not None and cancel.is_set():
                for f in futures:
                    f.cancel()
                print("\nCancelled — in-flight requests may still complete.")
                break

    suffix = f" ({failures} chunk(s) failed; validator will retry)" if failures else ""
    print(f"\nDone.{suffix}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    ap.add_argument("--provider", default=None,
                    help="override providers.transcribe.provider from config")
    ap.add_argument("--model", default=None,
                    help="override providers.transcribe.model from config")
    ap.add_argument("--first", type=int, default=None)
    ap.add_argument("--last", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    first = args.first or cfg.input.first_page
    last = args.last or cfg.input.last_page
    pages = list(range(first, last + 1))
    run(cfg, pages=pages, workers=args.workers, force=args.force,
        provider=args.provider, model=args.model)


if __name__ == "__main__":
    main()
