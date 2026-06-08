"""Stage 2: group page segments into whole sections (stories/chapters).

Walks out/transcript/page_*.json in page order. A segment whose `title` is set
opens a new section; a segment with title=null continues the current section.
Writes out/stories.json = [{index, title, start_page, end_page, source}].
The key is named `source` (rather than e.g. `german`) because the pipeline is
language-agnostic; downstream stages key off it.
"""

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv

from config import Config, load_config
from providers import prune_titles

TITLE_PREVIEW_CHARS = 200

# A page break is a real paragraph break only if the preceding page ended on
# sentence-final punctuation; otherwise the sentence continues onto the next page.
_SENTENCE_END = re.compile(r'[.!?:;»"”—)\]]\s*$')

# Ligatures and the like that NFKD does NOT decompose.
_LIGATURES = {"Œ": "OE", "œ": "oe", "Æ": "AE", "æ": "ae", "ß": "ss"}


def _norm_title(t: str | None) -> str:
    """Fold accents/case/punctuation/ligatures so OCR variants of the same
    title match. "DEUXIÈME ÉPISODE" and "DEUXIEME EPISODE" coalesce, as do
    "CHŒUR" and "CHOEUR"."""
    if not t:
        return ""
    for src, dst in _LIGATURES.items():
        t = t.replace(src, dst)
    s = unicodedata.normalize("NFKD", t)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _titles_match(a: str | None, b: str | None) -> bool:
    """True if `a` and `b` normalize-equal, OR are close enough that one is
    plausibly an OCR misread of the other ("PROLOGUS" of "PROLOGUE")."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return na == nb
    if na == nb:
        return True
    # Require both titles to be reasonably long before fuzzy-matching, so
    # we don't accidentally fold "ACT I" into "ACT II".
    if min(len(na), len(nb)) < 6:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= 0.85


def _join_pages(parts: list[str]) -> str:
    out = ""
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if not out:
            out = p
        elif _SENTENCE_END.search(out):
            out += "\n\n" + p
        else:
            out += " " + p
    return out.strip()


def build_stories(transcript_dir: Path) -> list[dict]:
    stories: list[dict] = []
    current: dict | None = None

    for f in sorted(transcript_dir.glob("page_*.json")):
        page_data = json.loads(f.read_text())
        page = page_data["page"]
        for seg in page_data.get("segments", []):
            title = seg.get("title")
            text = (seg.get("text") or "").strip()
            same_title = (current is not None
                          and _titles_match(title, current["title"]))
            if title and not same_title:
                current = {"index": len(stories) + 1, "title": title,
                           "start_page": page, "end_page": page,
                           "_parts": [text] if text else []}
                stories.append(current)
            else:
                if current is None:  # transcript started mid-section
                    current = {"index": len(stories) + 1, "title": None,
                               "start_page": page, "end_page": page, "_parts": []}
                    stories.append(current)
                if text:
                    current["_parts"].append(text)
                current["end_page"] = page

    for s in stories:
        s["source"] = _join_pages(s.pop("_parts"))
    return [s for s in stories if s["source"]]


def prune_false_titles(stories: list[dict], cfg: Config) -> list[dict]:
    """Ask the LLM which candidate titles look like false positives, then
    merge each rejected story's text into the previous story.

    Skipped when `prompts.segmentation_notes` is empty (no pattern to judge
    against) or when there are fewer than two titled stories."""
    if not cfg.prompts.segmentation_notes:
        return stories
    titled = [(i, s) for i, s in enumerate(stories) if s.get("title")]
    if len(titled) < 2:
        return stories

    items = [{"index": i, "title": s["title"],
              "preview": s["source"][:TITLE_PREVIEW_CHARS]}
             for i, s in titled]
    rejects = set(prune_titles(items, cfg))
    if not rejects:
        return stories

    print(f"Pruning {len(rejects)} false-positive title(s): "
          + ", ".join(stories[i]["title"] for i in sorted(rejects)
                      if 0 <= i < len(stories)))

    merged: list[dict] = []
    for i, s in enumerate(stories):
        if i in rejects and merged:
            prev = merged[-1]
            extra = (s["title"] + "\n\n" + s["source"]).strip() if s["title"] else s["source"]
            prev["source"] = (prev["source"] + "\n\n" + extra).strip()
            prev["end_page"] = s["end_page"]
        else:
            merged.append(s)
    for n, s in enumerate(merged, start=1):
        s["index"] = n
    return merged


def run_no_write(cfg: Config) -> list[dict]:
    """Compute stitched + LLM-pruned stories without writing stories.json.

    The GUI uses this to show the manual review dialog before committing
    the file. The CLI path goes through `run()` and writes immediately.
    """
    load_dotenv(cfg.project_root / ".env")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    stories = build_stories(cfg.transcript_dir)
    return prune_false_titles(stories, cfg)


def write_stories(cfg: Config, stories: list[dict]) -> Path:
    """Write `stories` to stories.json after re-indexing 1..N."""
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    for n, s in enumerate(stories, start=1):
        s["index"] = n
    cfg.stories_json.write_text(json.dumps(stories, ensure_ascii=False, indent=2))
    return cfg.stories_json


def merge_into_previous(stories: list[dict], reject_indices: set[int]) -> list[dict]:
    """Merge stories whose position is in `reject_indices` (0-based) into
    the previous accepted story. Same semantics as prune_false_titles's
    merge step, exposed so the GUI review dialog can reuse it."""
    merged: list[dict] = []
    for i, s in enumerate(stories):
        if i in reject_indices and merged:
            prev = merged[-1]
            extra = ((s.get("title") or "") + "\n\n" + s.get("source", "")).strip() \
                if s.get("title") else s.get("source", "")
            prev["source"] = (prev.get("source", "") + "\n\n" + extra).strip()
            prev["end_page"] = s.get("end_page", prev.get("end_page"))
        else:
            merged.append(dict(s))
    for n, s in enumerate(merged, start=1):
        s["index"] = n
    return merged


def run(cfg: Config) -> list[dict]:
    stories = run_no_write(cfg)
    write_stories(cfg, stories)
    return stories


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    stories = run(cfg)

    print(f"{len(stories)} section(s) -> {cfg.stories_json}\n")
    for s in stories:
        span = (f"p{s['start_page']}" if s["start_page"] == s["end_page"]
                else f"p{s['start_page']}-{s['end_page']}")
        title = s["title"] or "[untitled / continuation]"
        print(f"  {s['index']:>3}. {span:<10} {len(s['source']):>6} chars  {title}")


if __name__ == "__main__":
    main()
