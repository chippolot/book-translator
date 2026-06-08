"""Two-stage backends for the translation pipeline.

Stage 1 - transcribe(image, cfg): vision call returning SEGMENTED source text,
where a segment carries a `title` when a new section begins on that page
(null = the segment continues the previous section). Captures multiple
sections per page and sections that span pages.

Stage 2 - translate_text(text, cfg): text-only call translating a whole
section into the target language.

Each stage has anthropic / google / openai backends. All book-specific
knobs (language pair, book context, transcription notes, translation style)
come from the Config passed in.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from config import Config, DEFAULT_TRANSCRIBE_MODELS, DEFAULT_TRANSLATE_MODELS


# A small dict reported per API call: {provider, model, input_tokens,
# output_tokens, cache_read_tokens}. The GUI installs a callback that
# logs these to usage.jsonl; the CLI passes None and behavior is unchanged.
UsageCb = Optional[Callable[[dict], None]]


def _emit(cb: UsageCb, provider: str, model: str,
          input_tokens: int, output_tokens: int,
          cache_read_tokens: int = 0) -> None:
    if cb is None:
        return
    try:
        cb({
            "provider": provider,
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
        })
    except Exception:  # noqa: BLE001 - never let logging break the pipeline
        pass


# --------------------------------------------------------------------------- #
# Prompt construction                                                         #
# --------------------------------------------------------------------------- #

def system_prompt(cfg: Config) -> str:
    parts = [
        "You are an expert palaeographer and literary translator. "
        f"You are working with a book printed in {cfg.languages.source}."
    ]
    if cfg.book.title:
        bk = f"The book is titled '{cfg.book.title}'"
        if cfg.book.author:
            bk += f" by {cfg.book.author}"
        parts.append(bk + ".")
    if cfg.prompts.book_context:
        parts.append(cfg.prompts.book_context)
    return " ".join(parts).strip()


def transcribe_prompt(cfg: Config) -> str:
    transcription_block = ""
    if cfg.prompts.transcription_notes:
        transcription_block = (
            "\n\nAdditional transcription notes specific to this book:\n"
            + cfg.prompts.transcription_notes
        )
    segmentation_block = ""
    if cfg.prompts.segmentation_notes:
        segmentation_block = (
            "\n\nWhat counts as a section title in THIS book:\n"
            + cfg.prompts.segmentation_notes
        )
    return (
        f"This image is one page of the book. Transcribe its {cfg.languages.source} "
        f"body text and split it into SEGMENTS by section (story, chapter, "
        f"essay, act, scene, or similar).\n\n"
        "A new section is marked by a TITLE — typically a centred heading in "
        "larger or heavier type that names a whole division of the book "
        "(e.g. 'Chapter 1', 'PROLOGUE', 'ACT II', 'SCENE 3', a story or essay "
        "title). Whenever such a title appears, start a new segment whose "
        "`title` is that heading. Text that continues a section already running "
        "from the previous page (no title above it) goes in a leading segment "
        "with `title` set to null. A page may contain several segments (end of "
        "one section, then one or more new ones).\n\n"
        "What is NOT a section title (these belong inline in the segment's "
        "`text`, not in the `title` field):\n"
        "  - Speaker/dialogue labels in plays or dialogues (e.g. 'HAMLET.', "
        "'ANTIGONE.', 'SOCRATES:', 'NARRATOR:'). Even when set in capitals, "
        "these mark who is speaking, not a new section. Keep them inline so "
        "the reader can still see who said what.\n"
        "  - Stage directions, line numbers, verse numbers.\n"
        "  - Stanza markers inside verse (Strophe, Antistrophe, Épode, "
        "Strophe 1 / 2 / 3, etc.). These are sub-structural rhythm markers, "
        "not new sections — keep them inline in `text`.\n"
        "  - Scene cast lists at the start of a scene (e.g. '1. Créon. Le "
        "Chœur.', 'Scene 2: Hamlet, Horatio, Marcellus'). They introduce who "
        "is in the upcoming scene; they are inline, not section titles.\n"
        "  - RUNNING HEADERS at the very top of the page repeating the "
        "section name on every page within that section. Set `title` to the "
        "section name ONLY on the FIRST page where the heading actually "
        "appears in display type with the section's text starting fresh "
        "below it. On every subsequent page where the same name appears in "
        "smaller/regular type at the page top while the section continues, "
        "treat it as a running header and set `title` to null.\n"
        "  - Page numbers, publisher/typesetter footers.\n"
        "If you see the SAME 'heading' repeating every few lines on the page, "
        "it is a speaker label or a recurring marker — not a section title.\n\n"
        "Transcription rules for each segment's `text`:\n"
        f"  - Transcribe in the original {cfg.languages.source}. Do NOT translate.\n"
        "  - Join words split by a hyphen at a line break into one word.\n"
        "  - Separate paragraphs with a blank line; do NOT hard-wrap lines within "
        "a paragraph (except verse, where you keep the line breaks).\n"
        "  - Do NOT include the section title inside `text` (it is in the "
        "`title` field). Speaker labels and stage directions DO go in `text`.\n"
        "  - IGNORE page numbers, running headers, and the publisher/typesetter "
        "footer."
        f"{transcription_block}{segmentation_block}\n\n"
        "If the page is a part-divider, a table of contents, blank, or an image "
        "only, return an empty segment list.\n\n"
        'Return JSON: {"segments": [{"title": <string|null>, "text": <string>}]}'
    )


def transcribe_text_prompt(cfg: Config) -> str:
    transcription_block = ""
    if cfg.prompts.transcription_notes:
        transcription_block = (
            "\n\nAdditional notes specific to this book:\n"
            + cfg.prompts.transcription_notes
        )
    segmentation_block = ""
    if cfg.prompts.segmentation_notes:
        segmentation_block = (
            "\n\nWhat counts as a section title in THIS book:\n"
            + cfg.prompts.segmentation_notes
        )
    return (
        f"Below is a chunk of the book's {cfg.languages.source} body text "
        f"(clean machine-readable text, not OCR). Split it into SEGMENTS by "
        f"section (story, chapter, essay, act, scene, or similar).\n\n"
        "A new section is marked by a TITLE — a standalone heading line "
        "that names a whole division of the book (e.g. 'Chapter 1', "
        "'PROLOGUE', 'ACT II', 'SCENE 3', 'Vorwort.', a story or essay "
        "title). Whenever such a title appears, start a new segment whose "
        "`title` is that heading. Text that continues a section already "
        "running from the previous chunk (no title above it) goes in a "
        "leading segment with `title` set to null. A chunk may contain "
        "several segments.\n\n"
        "What is NOT a section title (these belong inline in the segment's "
        "`text`, not in the `title` field):\n"
        "  - Speaker/dialogue labels in plays or dialogues (e.g. 'HAMLET.', "
        "'ANTIGONE.', 'SOCRATES:').\n"
        "  - Stage directions, line numbers, verse numbers.\n"
        "  - Stanza markers inside verse (Strophe, Antistrophe, Épode, etc.).\n"
        "  - Scene cast lists at the start of a scene.\n\n"
        "Transcription rules for each segment's `text`:\n"
        f"  - Preserve the original {cfg.languages.source} text verbatim. Do "
        "NOT translate, modernize, or rewrite.\n"
        "  - Separate paragraphs with a single blank line; do NOT hard-wrap "
        "prose lines within a paragraph. Preserve verse line breaks.\n"
        "  - Do NOT include the section title inside `text` (it goes in the "
        "`title` field). Speaker labels and stage directions DO go in `text`.\n"
        f"{transcription_block}{segmentation_block}\n\n"
        'Return JSON: {"segments": [{"title": <string|null>, "text": <string>}]}'
        "\n\nCHUNK:\n"
    )


def title_prompt(cfg: Config, source_title: str) -> str:
    return (
        f"Translate the following {cfg.languages.source} title into faithful, "
        f"literary {cfg.languages.target}. Preserve proper names. Drop trailing "
        f"punctuation. Output ONLY the {cfg.languages.target} title on one line "
        f"— no quotes around it, no preamble, no notes.\n\n"
        f"TITLE: {source_title}"
    )


def translate_prompt(cfg: Config, source_text: str, source_title: str | None) -> str:
    title_clause = f', titled "{source_title}"' if source_title else ""
    style_block = ""
    if cfg.prompts.translation_style:
        style_block = (
            f"\nStyle notes for this book:\n{cfg.prompts.translation_style}\n"
        )
    return (
        f"Below is the complete {cfg.languages.source} text of one section"
        f"{title_clause}. Translate it into natural, readable, literary modern "
        f"{cfg.languages.target} that preserves the tone and imagery of the "
        f"original.\n"
        f"{style_block}\n"
        "Output rules — follow exactly:\n"
        f"  - Output ONLY the {cfg.languages.target} translation: no preamble, "
        f"no notes, no commentary, and do NOT repeat the source text.\n"
        "  - Do NOT add a title or heading. Begin directly with the first line "
        "of text.\n"
        "  - Plain text only: no Markdown (no '#', '*', '**', '>') and no HTML "
        "or HTML entities (no '&nbsp;', '<br>', etc.).\n"
        "  - Separate paragraphs with a single blank line. Translate verse as "
        "verse, keeping its line breaks; render prose as flowing paragraphs "
        "(do not hard-wrap prose lines).\n"
        "  - If the original has a centred section break, render it as a line "
        "containing exactly: * * *\n\n"
        f"SOURCE TEXT:\n{source_text}"
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _b64(image_path: Path) -> str:
    return base64.standard_b64encode(image_path.read_bytes()).decode()


def _loads(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _clean_segments(data: dict) -> dict:
    out = []
    for seg in data.get("segments", []) or []:
        title = (seg.get("title") or "").strip() or None
        text = (seg.get("text") or "").strip()
        if title or text:
            out.append({"title": title, "text": text})
    return {"segments": out}


# --------------------------------------------------------------------------- #
# Stage 1: transcription (vision)                                             #
# --------------------------------------------------------------------------- #

_TRANSCRIBE_TOOL = {
    "name": "record_page",
    "description": "Record the page's source-language text split into section segments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": ["string", "null"]},
                        "text": {"type": "string"},
                    },
                    "required": ["title", "text"],
                },
            }
        },
        "required": ["segments"],
    },
}


def _anthropic_transcribe(image_path: Path, model: str, system: str, prompt: str,
                          usage_cb: UsageCb = None) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        tools=[_TRANSCRIBE_TOOL],
        tool_choice={"type": "tool", "name": "record_page"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": _b64(image_path)}},
            {"type": "text", "text": prompt},
        ]}],
    )
    u = getattr(msg, "usage", None)
    _emit(usage_cb, "anthropic", model,
          getattr(u, "input_tokens", 0) if u else 0,
          getattr(u, "output_tokens", 0) if u else 0,
          getattr(u, "cache_read_input_tokens", 0) if u else 0)
    for block in msg.content:
        if block.type == "tool_use":
            return _clean_segments(block.input)
    raise ValueError("no tool_use block in Anthropic response")


def _google_transcribe(image_path: Path, model: str, system: str, prompt: str,
                       usage_cb: UsageCb = None) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    cfg_kwargs = dict(system_instruction=system,
                      response_mime_type="application/json")
    try:
        gcfg = types.GenerateContentConfig(
            media_resolution="MEDIA_RESOLUTION_HIGH", **cfg_kwargs)
    except TypeError:
        gcfg = types.GenerateContentConfig(**cfg_kwargs)
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=image_path.read_bytes(),
                                        mime_type="image/png"),
                  prompt],
        config=gcfg,
    )
    um = getattr(resp, "usage_metadata", None)
    _emit(usage_cb, "google", model,
          getattr(um, "prompt_token_count", 0) if um else 0,
          getattr(um, "candidates_token_count", 0) if um else 0,
          getattr(um, "cached_content_token_count", 0) if um else 0)
    return _clean_segments(_loads(resp.text))


def _openai_transcribe(image_path: Path, model: str, system: str, prompt: str,
                       usage_cb: UsageCb = None) -> dict:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{_b64(image_path)}"}},
            ]},
        ],
        response_format={"type": "json_object"},
    )
    u = getattr(resp, "usage", None)
    _emit(usage_cb, "openai", model,
          getattr(u, "prompt_tokens", 0) if u else 0,
          getattr(u, "completion_tokens", 0) if u else 0, 0)
    return _clean_segments(_loads(resp.choices[0].message.content))


# --------------------------------------------------------------------------- #
# Stage 2: translation (text-only)                                            #
# --------------------------------------------------------------------------- #

def _anthropic_translate(model: str, system: str, prompt: str,
                         usage_cb: UsageCb = None) -> str:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=16384,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    u = getattr(msg, "usage", None)
    _emit(usage_cb, "anthropic", model,
          getattr(u, "input_tokens", 0) if u else 0,
          getattr(u, "output_tokens", 0) if u else 0,
          getattr(u, "cache_read_input_tokens", 0) if u else 0)
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def _google_translate(model: str, system: str, prompt: str,
                      usage_cb: UsageCb = None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(system_instruction=system),
    )
    um = getattr(resp, "usage_metadata", None)
    _emit(usage_cb, "google", model,
          getattr(um, "prompt_token_count", 0) if um else 0,
          getattr(um, "candidates_token_count", 0) if um else 0,
          getattr(um, "cached_content_token_count", 0) if um else 0)
    return (resp.text or "").strip()


def _openai_translate(model: str, system: str, prompt: str,
                      usage_cb: UsageCb = None) -> str:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    u = getattr(resp, "usage", None)
    _emit(usage_cb, "openai", model,
          getattr(u, "prompt_tokens", 0) if u else 0,
          getattr(u, "completion_tokens", 0) if u else 0, 0)
    return (resp.choices[0].message.content or "").strip()


def _clean_title(text: str) -> str:
    text = (text or "").strip().splitlines()[0].strip() if text else ""
    while text and text[0] in '"“„«‹\'':
        text = text[1:].lstrip()
    while text and text[-1] in '"”“»›\'':
        text = text[:-1].rstrip()
    return text.rstrip(".!?:;")


def _anthropic_transcribe_text(text: str, model: str, system: str, prompt: str,
                               usage_cb: UsageCb = None) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        tools=[_TRANSCRIBE_TOOL],
        tool_choice={"type": "tool", "name": "record_page"},
        messages=[{"role": "user", "content": prompt + text}],
    )
    u = getattr(msg, "usage", None)
    _emit(usage_cb, "anthropic", model,
          getattr(u, "input_tokens", 0) if u else 0,
          getattr(u, "output_tokens", 0) if u else 0,
          getattr(u, "cache_read_input_tokens", 0) if u else 0)
    for block in msg.content:
        if block.type == "tool_use":
            return _clean_segments(block.input)
    raise ValueError("no tool_use block in Anthropic response")


def _google_transcribe_text(text: str, model: str, system: str, prompt: str,
                            usage_cb: UsageCb = None) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=model,
        contents=[prompt + text],
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json"),
    )
    um = getattr(resp, "usage_metadata", None)
    _emit(usage_cb, "google", model,
          getattr(um, "prompt_token_count", 0) if um else 0,
          getattr(um, "candidates_token_count", 0) if um else 0,
          getattr(um, "cached_content_token_count", 0) if um else 0)
    return _clean_segments(_loads(resp.text))


def _openai_transcribe_text(text: str, model: str, system: str, prompt: str,
                            usage_cb: UsageCb = None) -> dict:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt + text},
        ],
        response_format={"type": "json_object"},
    )
    u = getattr(resp, "usage", None)
    _emit(usage_cb, "openai", model,
          getattr(u, "prompt_tokens", 0) if u else 0,
          getattr(u, "completion_tokens", 0) if u else 0, 0)
    return _clean_segments(_loads(resp.choices[0].message.content))


_TRANSCRIBE = {"anthropic": _anthropic_transcribe,
               "google": _google_transcribe,
               "openai": _openai_transcribe}
_TRANSCRIBE_TEXT = {"anthropic": _anthropic_transcribe_text,
                    "google": _google_transcribe_text,
                    "openai": _openai_transcribe_text}
_TRANSLATE = {"anthropic": _anthropic_translate,
              "google": _google_translate,
              "openai": _openai_translate}


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #

def transcribe(image_path: Path, cfg: Config,
               provider: str | None = None,
               model: str | None = None,
               usage_cb: UsageCb = None) -> dict:
    provider = provider or cfg.providers.transcribe.provider
    model = model or cfg.providers.transcribe.model
    return _TRANSCRIBE[provider](
        image_path, model, system_prompt(cfg), transcribe_prompt(cfg),
        usage_cb=usage_cb,
    )


def transcribe_text(text: str, cfg: Config,
                    provider: str | None = None,
                    model: str | None = None,
                    usage_cb: UsageCb = None) -> dict:
    provider = provider or cfg.providers.transcribe.provider
    model = model or cfg.providers.transcribe.model
    return _TRANSCRIBE_TEXT[provider](
        text, model, system_prompt(cfg), transcribe_text_prompt(cfg),
        usage_cb=usage_cb,
    )


def prune_titles_prompt(cfg: Config, items: list[dict]) -> str:
    seg_block = ""
    if cfg.prompts.segmentation_notes:
        seg_block = (
            "\n\nWhat counts as a section title in this book:\n"
            f"{cfg.prompts.segmentation_notes}\n"
        )
    listing = "\n".join(
        f'  [{it["index"]}] title={json.dumps(it["title"], ensure_ascii=False)} '
        f'preview={json.dumps(it["preview"], ensure_ascii=False)}'
        for it in items
    )
    return (
        f"Below is the list of candidate section titles a segmenter pulled "
        f"from a {cfg.languages.source} book. Some are real section headings; "
        f"others are false positives — list-item prefixes, sub-roman numerals "
        f"inside a table, speaker labels, stage directions, or otherwise "
        f"mis-promoted inline markers. The real headings should form a "
        f"coherent, usually monotonically progressing series (e.g. I., II., "
        f"III., ... plus a few specials like 'Vorwort.').{seg_block}\n\n"
        "Each item shows the title and a short preview of the text that "
        "follows it. Use the preview to judge whether the candidate is "
        "really opening a new section or is just inline content.\n\n"
        "BE CONSERVATIVE: only reject a title when you are confident it is "
        "NOT a real section heading. When in doubt, KEEP it.\n\n"
        "Candidates:\n"
        f"{listing}\n\n"
        'Return JSON ONLY: {"reject": [<index>, <index>, ...]} listing the '
        "indices of titles to REJECT. Return an empty list if all are real."
    )


def prune_titles(items: list[dict], cfg: Config,
                 provider: str | None = None,
                 model: str | None = None,
                 usage_cb: UsageCb = None) -> list[int]:
    """Ask the configured translate-provider which candidate titles are false
    positives. `items` is a list of {index, title, preview}. Returns the
    indices to reject."""
    if not items:
        return []
    provider = provider or cfg.providers.translate.provider
    model = model or cfg.providers.translate.model
    raw = _TRANSLATE[provider](
        model, system_prompt(cfg), prune_titles_prompt(cfg, items),
        usage_cb=usage_cb,
    )
    try:
        data = _loads(raw)
    except Exception:
        return []
    rejects = data.get("reject", []) if isinstance(data, dict) else []
    return [int(i) for i in rejects if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]


def translate_text(text: str, cfg: Config, title: str | None = None,
                   provider: str | None = None,
                   model: str | None = None,
                   usage_cb: UsageCb = None) -> str:
    provider = provider or cfg.providers.translate.provider
    model = model or cfg.providers.translate.model
    return _TRANSLATE[provider](
        model, system_prompt(cfg), translate_prompt(cfg, text, title),
        usage_cb=usage_cb,
    )


def translate_title(source_title: str, cfg: Config,
                    provider: str | None = None,
                    model: str | None = None,
                    usage_cb: UsageCb = None) -> str:
    if not (source_title or "").strip():
        return ""
    provider = provider or cfg.providers.translate.provider
    model = model or cfg.providers.translate.model
    raw = _TRANSLATE[provider](
        model, system_prompt(cfg), title_prompt(cfg, source_title),
        usage_cb=usage_cb,
    )
    return _clean_title(raw)
