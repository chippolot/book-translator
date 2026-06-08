"""Bootstrap a book.yaml from a source PDF.

Renders a small sample of pages (front-matter + middle of the book) and
asks a vision model to detect the title, author, language, script type,
typical body-page range, and any era/orthography quirks. Writes an
annotated book.yaml the user should review before running the pipeline.

  python src/init_book.py --pdf path/to/book.pdf [--out book.yaml]
                          [--target-lang English] [--provider google]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import pypdfium2 as pdfium
from dotenv import load_dotenv

from providers import _loads  # tolerant JSON parser


# --------------------------------------------------------------------------- #
# Retry helper                                                                #
# --------------------------------------------------------------------------- #

# Transient HTTP / SDK error markers worth retrying on. Anything else (auth
# failures, 4xx client errors, hard validation errors) fails fast — they
# won't get better by waiting.
_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "ServerError", "Overloaded",
    "429", "RateLimit", "rate_limit",
    "Timeout", "Timed out", "ConnectionError", "ConnectionResetError",
    "RemoteProtocolError", "RemoteDisconnected",
)


def _is_transient(exc: BaseException) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return any(m in s for m in _TRANSIENT_MARKERS)


def _retry_call(fn: Callable, *, label: str,
                max_attempts: int = 4,
                base_delay: float = 2.0,
                progress_cb: 'ProgressCb' = None):
    """Run `fn()` with exponential backoff on transient errors.

    Permanent errors (auth, validation, etc.) raise immediately so we
    don't waste a minute backing off something that'll never recover.
    """
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - SDK error types vary
            if attempt == max_attempts or not _is_transient(exc):
                raise
            msg = (f"  {label}: attempt {attempt} failed "
                   f"({type(exc).__name__}: {exc}); "
                   f"retrying in {delay:.0f}s")
            print(msg, file=sys.stderr)
            if progress_cb:
                progress_cb("call",
                            f"{label} attempt {attempt} failed; "
                            f"retrying in {delay:.0f}s…")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")

# Progress callback used by the GUI runner. Signature:
#   (event_kind: "render"|"fetch"|"call"|"done", message: str) -> None
# `kind` lets the UI choose a status pill / spinner; `message` is human-
# readable. CLI sets it to None and gets the existing print()s.
ProgressCb = Optional[Callable[[str, str], None]]

# Hard cap on get_page tool calls per init_book run, to bound cost in case
# the model gets confused and keeps fetching.
MAX_FETCHES = 25

AGENT_PROMPT_TEMPLATE = """You are inspecting a {total}-page PDF book in order to
fill in a config file for an automated translation pipeline.

You have ONE tool: `get_page(page)` which returns the given 1-indexed PDF page
as an image. Call it as many times as you need to figure out:

  - the book's title, author, era/byline
  - the language of the body text
  - the script (Fraktur, Antiqua, Cyrillic, handwritten, ...) and any
    orthography quirks the transcriber must preserve
  - whether there is multilingual apparatus (e.g. French footnotes under Greek
    verse) that should be preserved verbatim
  - the FIRST PDF page that contains real body text (skip cover, blank
    endpapers, library cards, title page, contents, foreword)
  - the LAST PDF page of body text (skip back-matter index, colophon, blank
    endpapers, library labels)

Tips:
  - Old library scans often have 3-6 pages of blank/yellow endpapers, library
    bookplates, or doodles before the real title page. Walk forward from page
    1 until you see real content.
  - The body text is usually bracketed by a title/dramatis-personae page on the
    front side and a "FIN" / colophon / publisher imprint on the back side.
  - You can request multiple pages per turn (parallel tool calls) to be quick.
  - Be efficient. Typically 6-12 fetches is plenty. Hard cap: {max_fetches}.

File hints from the PDF container (treat as tie-breakers only; the images are
authoritative):
{hints}

When you have enough information, STOP calling get_page and respond with ONLY
a JSON object (no prose, no markdown fence) of this shape. Use null where you
genuinely cannot tell rather than inventing values.

{{
  "title":              <string|null, the title in its original language>,
  "author":             <string|null>,
  "byline":             <string|null, e.g. "writing as X · 1900", or null>,
  "subtitle_translated":<string|null, a {target_lang} subtitle for the title page, or null>,
  "source_language":    <string, e.g. "German">,
  "book_context":       <string, 1-3 sentences: era, genre, register, anything notable>,
  "transcription_notes":<string, script type + orthography quirks + apparatus-language notes; "" if modern clean type with no apparatus>,
  "segmentation_notes": <string, 1-3 sentences describing WHAT COUNTS AS A SECTION TITLE in this book — chapter names? story titles? act/scene markers like "PROLOGUE", "EXODE"? — AND any inline structural markers that must NOT be treated as titles, e.g. "speaker labels (ANTIGONE., ISMHNH.) are inline, not section titles". Empty string "" only for simple prose with no special structure.>,
  "translation_style":  <string, 1-2 sentences on the appropriate translation register>,
  "first_body_page":    <integer, the PDF page number where body text begins>,
  "last_body_page":     <integer, the PDF page number where body text ends>
}}
"""

GET_PAGE_PARAMETERS = {
    "type": "object",
    "properties": {
        "page": {
            "type": "integer",
            "description": "1-indexed PDF page number to fetch.",
        },
    },
    "required": ["page"],
}
GET_PAGE_DESCRIPTION = (
    "Fetch one PDF page as a PNG image. Use to inspect any page (cover, title "
    "page, body page, colophon, ...). Pass the 1-indexed page number."
)


class PageFetcher:
    """Lazy renderer with a per-run fetch cap and on-disk cache."""

    def __init__(self, pdf: Path, total: int, dpi: int, tmp: Path,
                 max_fetches: int = MAX_FETCHES,
                 progress_cb: ProgressCb = None):
        self.pdf = pdf
        self.total = total
        self.dpi = dpi
        self.tmp = tmp
        self.cache: dict[int, Path] = {}
        self.remaining = max_fetches
        self.fetched: list[int] = []
        self._progress_cb = progress_cb

    def get(self, page: int) -> tuple[Path | None, str]:
        if not isinstance(page, int):
            return None, f"page must be an integer (got {page!r})"
        if page < 1 or page > self.total:
            return None, f"page {page} out of range (1..{self.total})"
        if page in self.cache:
            return self.cache[page], "ok"
        if self.remaining <= 0:
            return None, "fetch budget exhausted; produce the final JSON now"
        self.remaining -= 1
        if self._progress_cb:
            self._progress_cb(
                "fetch",
                f"reading page {page} of {self.total} "
                f"({len(self.fetched) + 1} fetched, "
                f"{self.remaining} budget left)")
        img = _render_page(self.pdf, page, self.dpi, self.tmp)
        if not img:
            return None, f"failed to render page {page}"
        self.cache[page] = img
        self.fetched.append(page)
        return img, "ok"


def _pdf_metadata(pdf: Path) -> tuple[int, dict[str, str]]:
    """Return (page count, metadata dict). Pure pypdfium2 — no subprocess
    so the bundled .app works without Poppler installed.
    """
    doc = pdfium.PdfDocument(pdf)
    n_pages = len(doc)
    md: dict[str, str] = {}
    # pypdfium2's metadata API: get_metadata_dict() returns the standard
    # PDF info-dict keys (Title, Author, Subject, etc.) where present.
    try:
        raw = doc.get_metadata_dict()
        if isinstance(raw, dict):
            for k, v in raw.items():
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    md[str(k)] = s
    except Exception:  # noqa: BLE001 - older versions / odd PDFs
        pass
    return n_pages, md


def _format_hints(pdf: Path, page_count: int, info: dict[str, str]) -> str:
    interesting = ("Title", "Author", "Subject", "Keywords", "Creator",
                   "Producer", "CreationDate", "ModDate")
    lines = [f"- filename: {pdf.name}",
             f"- PDF Pages: {page_count}"]
    for k in interesting:
        v = info.get(k)
        if v:
            lines.append(f"- PDF {k}: {v}")
    return "\n".join(lines)


def _render_page(pdf: Path, page: int, dpi: int, tmp: Path) -> Path | None:
    """Render `page` (1-indexed) to a PNG in `tmp` via pypdfium2."""
    out = tmp / f"sample_{page:04d}.png"
    try:
        doc = pdfium.PdfDocument(pdf)
        if page < 1 or page > len(doc):
            return None
        bitmap = doc[page - 1].render(scale=dpi / 72.0, grayscale=True)
        bitmap.to_pil().save(out, "PNG")
        return out
    except Exception:  # noqa: BLE001
        return None


def _b64(p: Path) -> str:
    return base64.standard_b64encode(p.read_bytes()).decode()


# Safety cap on assistant turns (each turn = one model call, possibly with
# multiple parallel get_page tool uses). Higher than MAX_FETCHES because the
# model may use multiple fetches per turn.
MAX_TURNS = 30


def _detect_anthropic(fetcher: PageFetcher, prompt: str, model: str) -> dict:
    import anthropic
    client = anthropic.Anthropic()

    tools = [{
        "name": "get_page",
        "description": GET_PAGE_DESCRIPTION,
        "input_schema": GET_PAGE_PARAMETERS,
    }]
    messages: list = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    last_text = ""
    for turn in range(MAX_TURNS):
        resp = _retry_call(
            lambda: client.messages.create(
                model=model, max_tokens=4096, tools=tools, messages=messages,
            ),
            label=f"anthropic turn {turn + 1}",
            progress_cb=fetcher._progress_cb,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        last_text = "".join(b.text for b in resp.content if b.type == "text")
        if not tool_uses:
            break

        tool_results: list = []
        for block in tool_uses:
            page = block.input.get("page") if isinstance(block.input, dict) else None
            img, status = fetcher.get(page)
            if img is None:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": status, "is_error": True,
                })
            else:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": [
                        {"type": "text", "text": f"PDF page {page} of {fetcher.total}:"},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png",
                            "data": _b64(img)}},
                    ],
                })
        messages.append({"role": "user", "content": tool_results})

    if not last_text:
        raise RuntimeError("model produced no final JSON text")
    return _loads(last_text)


def _detect_google(fetcher: PageFetcher, prompt: str, model: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    tool = types.Tool(function_declarations=[{
        "name": "get_page",
        "description": GET_PAGE_DESCRIPTION,
        "parameters": GET_PAGE_PARAMETERS,
    }])
    cfg = types.GenerateContentConfig(tools=[tool])

    contents: list = [types.Content(role="user", parts=[types.Part(text=prompt)])]

    last_text = ""
    for turn in range(MAX_TURNS):
        resp = _retry_call(
            lambda: client.models.generate_content(
                model=model, contents=contents, config=cfg,
            ),
            label=f"google turn {turn + 1}",
            progress_cb=fetcher._progress_cb,
        )
        cand = resp.candidates[0]
        contents.append(cand.content)

        calls = [p for p in (cand.content.parts or []) if p.function_call]
        text_parts = [p.text for p in (cand.content.parts or [])
                      if getattr(p, "text", None)]
        if text_parts:
            last_text = "".join(text_parts)
        if not calls:
            break

        response_parts: list = []
        for part in calls:
            fc = part.function_call
            page = (fc.args or {}).get("page") if hasattr(fc, "args") else None
            img, status = fetcher.get(page)
            response_parts.append(types.Part.from_function_response(
                name=fc.name, response={"status": status, "page": page},
            ))
            if img is not None:
                response_parts.append(types.Part(
                    text=f"PDF page {page} of {fetcher.total}:"))
                response_parts.append(types.Part.from_bytes(
                    data=img.read_bytes(), mime_type="image/png"))
        contents.append(types.Content(role="user", parts=response_parts))

    if not last_text:
        raise RuntimeError("model produced no final JSON text")
    return _loads(last_text)


def _detect_openai(fetcher: PageFetcher, prompt: str, model: str) -> dict:
    from openai import OpenAI
    client = OpenAI()
    tools = [{
        "type": "function",
        "function": {
            "name": "get_page",
            "description": GET_PAGE_DESCRIPTION,
            "parameters": GET_PAGE_PARAMETERS,
        },
    }]
    messages: list = [{"role": "user", "content": prompt}]

    last_text = ""
    for turn in range(MAX_TURNS):
        resp = _retry_call(
            lambda: client.chat.completions.create(
                model=model, messages=messages, tools=tools,
            ),
            label=f"openai turn {turn + 1}",
            progress_cb=fetcher._progress_cb,
        )
        msg = resp.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
        })
        if msg.content:
            last_text = msg.content
        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            page = args.get("page")
            img, status = fetcher.get(page)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": status if img is None else f"PDF page {page}; see next user message",
            })
            if img is not None:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text",
                         "text": f"PDF page {page} of {fetcher.total}:"},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{_b64(img)}"}},
                    ],
                })

    if not last_text:
        raise RuntimeError("model produced no final JSON text")
    return _loads(last_text)


_DETECT = {"google": (_detect_google, "gemini-2.5-flash"),
           "anthropic": (_detect_anthropic, "claude-sonnet-4-6"),
           "openai": (_detect_openai, "gpt-5")}


TXT_PROMPT_TEMPLATE = """You are inspecting samples of a plain-text book file in order to
fill in a config file for an automated translation pipeline.

Below are excerpts from the file: the opening, a middle slice, and the end.
Use them to figure out:

  - the book's title, author, era/byline
  - the language of the body text
  - any orthography quirks worth preserving (older spellings, archaic forms,
    multilingual apparatus, etc.)
  - WHAT COUNTS AS A SECTION TITLE in this book — chapter names? story titles?
    act/scene markers? short standalone headers like "Vorwort." or "Einleitung"?
  - inline structural markers that must NOT be treated as titles (e.g.
    speaker labels in plays)

Respond with ONLY a JSON object (no prose, no markdown fence) of this shape.
Use null where you genuinely cannot tell rather than inventing values.

{{
  "title":              <string|null, the title in its original language>,
  "author":             <string|null>,
  "byline":             <string|null, e.g. "writing as X · 1900", or null>,
  "subtitle_translated":<string|null, a {target_lang} subtitle for the title page, or null>,
  "source_language":    <string, e.g. "German">,
  "book_context":       <string, 1-3 sentences: era, genre, register, anything notable>,
  "transcription_notes":<string, orthography quirks or apparatus-language notes; "" if modern clean text>,
  "segmentation_notes": <string, 1-3 sentences describing what counts as a section title in this book, plus any inline markers that must NOT be treated as titles. Empty string "" only for simple prose with no special structure.>,
  "translation_style":  <string, 1-2 sentences on the appropriate translation register>
}}

File hints:
{hints}

OPENING:
{opening}

MIDDLE:
{middle}

END:
{end}
"""


def _detect_txt_anthropic(prompt: str, model: str,
                          progress_cb: ProgressCb = None) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    resp = _retry_call(
        lambda: client.messages.create(
            model=model, max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        ),
        label="anthropic detect", progress_cb=progress_cb,
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _loads(text)


def _detect_txt_google(prompt: str, model: str,
                       progress_cb: ProgressCb = None) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = _retry_call(
        lambda: client.models.generate_content(
            model=model, contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"),
        ),
        label="google detect", progress_cb=progress_cb,
    )
    return _loads(resp.text)


def _detect_txt_openai(prompt: str, model: str,
                       progress_cb: ProgressCb = None) -> dict:
    from openai import OpenAI
    client = OpenAI()
    resp = _retry_call(
        lambda: client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        ),
        label="openai detect", progress_cb=progress_cb,
    )
    return _loads(resp.choices[0].message.content)


_DETECT_TXT = {"google": _detect_txt_google,
               "anthropic": _detect_txt_anthropic,
               "openai": _detect_txt_openai}


def _yesno(s: str | None) -> str:
    return (s or "").strip()


def _slug(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", title or "book").strip("_")
    return s or "book"


def _render_yaml(pdf: Path, detected: dict, target_lang: str) -> str:
    title = _yesno(detected.get("title")) or "UNKNOWN TITLE"
    author = _yesno(detected.get("author"))
    byline = _yesno(detected.get("byline"))
    subtitle = _yesno(detected.get("subtitle_translated"))
    source_lang = _yesno(detected.get("source_language")) or "UNKNOWN"
    book_context = _yesno(detected.get("book_context"))
    transcription_notes = _yesno(detected.get("transcription_notes"))
    segmentation_notes = _yesno(detected.get("segmentation_notes"))
    translation_style = (_yesno(detected.get("translation_style"))
                         or "Aim for natural, readable, literary modern "
                            f"{target_lang} that preserves the tone and imagery "
                            "of the original.")
    first = detected.get("first_body_page") or 1
    last = detected.get("last_body_page") or "REPLACE_ME"
    name = _slug(title) + f"_{target_lang}"

    # German-source defaults to flagging German characters; otherwise empty.
    signals = '["ß", "ä", "ö", "ü"]' if source_lang.lower() == "german" else "[]"

    def block(text: str, indent: str = "    ") -> str:
        if not text:
            return f'{indent}""\n'
        lines = text.strip().splitlines()
        return "|\n" + "\n".join(f"{indent}{l}" for l in lines) + "\n"

    return f"""# Generated by init_book.py from {pdf.name}.
# REVIEW these fields before running the pipeline. The page range and
# transcription_notes especially benefit from a human check.

book:
  title: {json.dumps(title)}
  author: {json.dumps(author)}
  byline: {json.dumps(byline)}
  subtitle_translated: {json.dumps(subtitle)}
  # Optional. Paste in any HTML you want on the "About this book" page.
  about_html: ""
  # Optional. Path to a cover image (relative to this file).
  # cover: "cover.png"
  credit: "Draft {target_lang} translation"

input:
  pdf: {json.dumps(str(pdf))}
  # IMPORTANT: review these. They're a best-effort guess of where body text begins/ends.
  first_page: {first}
  last_page: {last}
  dpi: 200

languages:
  source: {json.dumps(source_lang)}
  target: {json.dumps(target_lang)}

prompts:
  book_context: {block(book_context)}
  transcription_notes: {block(transcription_notes)}
  segmentation_notes: {block(segmentation_notes)}
  translation_style: {block(translation_style)}

providers:
  transcribe:
    provider: "google"
  translate:
    provider: "anthropic"

output:
  dir: "out/{name}"
  pages_dir: "pages/{name}"

assemble:
  name: {json.dumps(name)}
  formats:
    - side-by-side-html
    - book-html
    - book-pdf
  chrome_path: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

validate:
  source_charset_signals: {signals}
  length_ratio_min: 0.4
  length_ratio_max: 2.5
  transcript_short_fraction: 0.25
"""


def _init_book_txt(src: Path, out: Path, target_lang: str,
                   provider: str, model: str | None,
                   progress_cb: ProgressCb = None) -> dict:
    import transcribe as transcribe_mod  # local: avoid heavy import on PDF path
    chunks = transcribe_mod.chunk_text_file(src)
    total = len(chunks)
    if total == 0:
        raise RuntimeError(f"{src} is empty or contains no paragraphs")
    print(f"Text file has {total} chunk(s) (~{transcribe_mod.TXT_CHUNK_CHARS} chars each)")

    raw = src.read_text(encoding="utf-8")
    n = len(raw)
    opening = raw[:6000]
    mid_start = max(0, n // 2 - 1500)
    middle = raw[mid_start:mid_start + 3000]
    end = raw[-3000:] if n > 3000 else ""
    hints = f"- filename: {src.name}\n- total characters: {n}\n- chunk count: {total}"

    detect_fn = _DETECT_TXT[provider]
    model = model or _DETECT[provider][1]
    print(f"Detecting metadata with {provider}/{model}...")
    if progress_cb:
        progress_cb("call", f"asking {provider}/{model} to identify the book…")
    prompt = TXT_PROMPT_TEMPLATE.format(
        target_lang=target_lang, hints=hints,
        opening=opening, middle=middle, end=end,
    )
    detected = detect_fn(prompt, model, progress_cb=progress_cb)
    # Chunk indices play the role of "pages" for the txt branch.
    detected["first_body_page"] = 1
    detected["last_body_page"] = total

    print("\nDetected:")
    print(json.dumps(detected, ensure_ascii=False, indent=2))

    out.write_text(_render_yaml(src, detected, target_lang))
    print(f"\nWrote {out}")
    if progress_cb:
        progress_cb("done", f"wrote {out.name}")
    print("\nReview especially:")
    print("  - prompts.segmentation_notes")
    print("  - book.about_html (left blank)")
    print(f"\nThen run: python src/run.py --config {out}")
    return detected


def init_book(pdf: Path, out: Path, target_lang: str,
              provider: str, model: str | None,
              progress_cb: ProgressCb = None) -> dict:
    """Detect book metadata and write `out` as YAML. Returns the detected
    fields dict (so the GUI can preview them without re-reading the file).
    Raises on missing input, overwrite collision, or detection failure.
    """
    if not pdf.exists():
        raise FileNotFoundError(f"file not found: {pdf}")
    if out.exists():
        raise FileExistsError(
            f"refusing to overwrite existing {out} (delete or rename it first)")
    out.parent.mkdir(parents=True, exist_ok=True)

    if pdf.suffix.lower() == ".txt":
        return _init_book_txt(pdf, out, target_lang, provider, model,
                              progress_cb)
    if pdf.suffix.lower() != ".pdf":
        raise ValueError(
            f"unsupported input extension: {pdf.suffix} (want .pdf or .txt)")

    if progress_cb:
        progress_cb("call", "reading PDF metadata…")
    total, pdf_info = _pdf_metadata(pdf)
    if total <= 0:
        raise RuntimeError("could not determine PDF page count")
    print(f"PDF has {total} page(s)")

    detect_fn, default_model = _DETECT[provider]
    model = model or default_model
    msg = f"asking {provider}/{model} to identify the book…"
    print(f"Detecting metadata with {provider}/{model}...")
    if progress_cb:
        progress_cb("call", msg)

    with tempfile.TemporaryDirectory() as tmp:
        fetcher = PageFetcher(pdf, total, dpi=200, tmp=Path(tmp),
                              progress_cb=progress_cb)
        prompt = AGENT_PROMPT_TEMPLATE.format(
            total=total, max_fetches=MAX_FETCHES, target_lang=target_lang,
            hints=_format_hints(pdf, total, pdf_info),
        )
        detected = detect_fn(fetcher, prompt, model)
        print(f"Pages fetched by agent: {fetcher.fetched}")

    print("\nDetected:")
    print(json.dumps(detected, ensure_ascii=False, indent=2))

    out.write_text(_render_yaml(pdf, detected, target_lang))
    print(f"\nWrote {out}")
    if progress_cb:
        progress_cb("done", f"wrote {out.name}")
    print("\nReview especially:")
    print("  - input.first_page / input.last_page (skip front/back matter)")
    print("  - book.about_html (left blank)")
    print("  - prompts.transcription_notes")
    print(f"\nThen run: python src/run.py --config {out}")
    return detected


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("book.yaml"))
    ap.add_argument("--target-lang", default="English")
    ap.add_argument("--provider", choices=tuple(_DETECT), default="google")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    try:
        init_book(args.pdf.expanduser().resolve(), args.out.resolve(),
                  args.target_lang, args.provider, args.model)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
