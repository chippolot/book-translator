"""Stage 3: translate each whole section into the target language.

Reads out/stories.json, writes out/stories/NNN_<slug>.json. Resumable: sections
already translated are skipped unless --force. Very long sections are
translated in paragraph chunks to stay within output limits.
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
from providers import translate_text, translate_title, UsageCb

ProgressCb = Optional[Callable[[int, int, str], None]]

MAX_RETRIES = 5
CHUNK_CHARS = 9000  # split sections longer than this by paragraph

# Substrings in `f"{type(exc).__name__}: {exc}"` that flag a retryable
# transient failure. Anything else (auth errors, validation) raises
# immediately so we don't burn 62 seconds of exponential backoff on
# a permanently-broken API key.
_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "ServerError", "Overloaded",
    "429", "RateLimit", "rate_limit",
    "Timeout", "Timed out", "ConnectionError", "ConnectionResetError",
    "RemoteProtocolError", "RemoteDisconnected",
)


def _is_transient_error(exc: BaseException) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(m in s for m in _TRANSIENT_MARKERS)


def _slug(title: str | None, index: int) -> str:
    base = title or "untitled"
    s = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return f"{index:03d}_{s[:50] or 'untitled'}"


def _chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > CHUNK_CHARS:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _with_retry(call, label: str):
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            # Fast-fail on permanent errors (auth, validation) instead of
            # burning ~62s of exponential backoff before giving up.
            if attempt == MAX_RETRIES or not _is_transient_error(exc):
                raise
            print(f"  {label} attempt {attempt} failed "
                  f"({type(exc).__name__}: {exc}); retrying in {delay:.0f}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2


def _translate_body(source: str, cfg: Config, provider: str, model: str,
                    title: str | None, usage_cb: UsageCb = None) -> str:
    parts = []
    for chunk in _chunks(source):
        parts.append(_with_retry(
            lambda c=chunk: translate_text(c, cfg, title=title,
                                           provider=provider, model=model,
                                           usage_cb=usage_cb),
            label="translate body"))
    return "\n\n".join(parts)


def _process(story: dict, cfg: Config, provider: str, model: str,
             force: bool, out_dir: Path, usage_cb: UsageCb = None) -> str:
    out = out_dir / f"{_slug(story['title'], story['index'])}.json"
    label = story["title"] or "[untitled]"
    # If a previous run produced a file at THIS slug, reuse it.
    if out.exists() and not force:
        return f"section {story['index']}: skip (already done)"
    # Otherwise scrub any stale file at a different slug for the same index
    # (e.g. the user edited the title in the review step). Without this,
    # assemble.load_stories would pick up BOTH the old and new file and
    # duplicate the section in the final book.
    for stale in out_dir.glob(f"{story['index']:03d}_*.json"):
        if stale.name != out.name:
            try:
                stale.unlink()
            except OSError:
                pass
    translated_title = _with_retry(
        lambda: translate_title(story["title"] or "", cfg,
                                provider=provider, model=model,
                                usage_cb=usage_cb),
        label="translate title")
    translated = _translate_body(story["source"], cfg, provider, model,
                                 story["title"], usage_cb=usage_cb)
    out.write_text(json.dumps({
        "index": story["index"],
        "title": story["title"],
        "translated_title": translated_title,
        "start_page": story["start_page"],
        "end_page": story["end_page"],
        "source": story["source"],
        "translated": translated,
        "provider": provider,
        "model": model,
    }, ensure_ascii=False, indent=2))
    return (f"section {story['index']}: done ({len(translated)} target chars)  "
            f"{label} -> {translated_title or '[no title]'}")


def run(cfg: Config, indices: list[int] | None = None,
        workers: int = 4, force: bool = False,
        provider: str | None = None, model: str | None = None,
        progress_cb: ProgressCb = None,
        usage_cb: UsageCb = None,
        cancel: Optional[threading.Event] = None) -> None:
    load_dotenv(cfg.project_root / ".env")
    provider = provider or cfg.providers.translate.provider
    model = model or cfg.providers.translate.model
    cfg.stories_dir.mkdir(parents=True, exist_ok=True)

    all_stories = json.loads(cfg.stories_json.read_text())
    if indices is None:
        stories = all_stories
    else:
        sel = set(indices)
        stories = [s for s in all_stories if s["index"] in sel]

    print(f"Translating {len(stories)} section(s) with {provider}/{model} "
          f"({workers} workers) -> {cfg.stories_dir}\n")
    total = len(stories)
    if progress_cb:
        progress_cb(0, total, "starting")
    failures = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, s, cfg, provider, model, force,
                               cfg.stories_dir, usage_cb): s for s in stories}
        for fut in as_completed(futures):
            story = futures[fut]
            done += 1
            try:
                line = fut.result()
                print(line)
                if progress_cb:
                    progress_cb(done, total, line)
            except Exception as exc:  # noqa: BLE001 - SDK error types vary
                failures += 1
                label = f"{story['index']:03d} {story['title'] or '[untitled]'}"
                msg = (f"{label}: FAILED after retries "
                       f"({type(exc).__name__}: {exc})")
                print(msg, file=sys.stderr)
                if progress_cb:
                    progress_cb(done, total, msg)
            if cancel is not None and cancel.is_set():
                for f in futures:
                    f.cancel()
                print("\nCancelled — in-flight requests may still complete.")
                break

    suffix = (f" ({failures} story(ies) failed; validator will retry)"
              if failures else "")
    print(f"\nDone.{suffix}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", type=int, nargs="*", default=None,
                    help="translate only these section indices (1-based)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    run(cfg, indices=args.only, workers=args.workers, force=args.force,
        provider=args.provider, model=args.model)


if __name__ == "__main__":
    main()
