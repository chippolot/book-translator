"""Detect failed transcriptions and translations after a pipeline run.

Pure-Python heuristics — no API calls. Reads existing artifacts and emits
Issue records pointing at items to re-run.

Transcripts (out/transcript/page_XXXX.json)
  - file missing, JSON-malformed, or missing `segments`
  - empty `segments` when the page image is plausibly non-blank
  - total text length below transcript_short_fraction × per-book median

Translations (out/stories/NNN_*.json)
  - missing/empty `translated`
  - length ratio translated/source outside [length_ratio_min, length_ratio_max]
  - source-charset signals appear too often in `translated`
    (suggests the model echoed source text or refused)
  - source title present but `translated_title` empty
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path

from config import Config, load_config

PAGE_PNG_MIN_BYTES = 5_000  # smaller than this -> probably blank/cover
SIGNAL_PER_1K_THRESHOLD = 4  # source-charset chars / 1000 in translation


@dataclass
class Issue:
    stage: str        # "transcript" | "translation"
    item: str         # filename or identifier
    reason: str
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _segment_chars(data: dict) -> int:
    return sum(len(s.get("text") or "") for s in (data.get("segments") or []))


def validate_transcripts(cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    pages = list(range(cfg.input.first_page, cfg.input.last_page + 1))

    char_counts: dict[int, int] = {}
    for page in pages:
        out = cfg.transcript_dir / f"page_{page:04d}.json"
        img = cfg.pages_dir / f"page_{page:04d}.png"
        if not out.exists():
            if img.exists() and img.stat().st_size > PAGE_PNG_MIN_BYTES:
                issues.append(Issue("transcript", out.name,
                                    "missing", f"page {page} has no transcript"))
            continue
        data = _load_json(out)
        if data is None:
            issues.append(Issue("transcript", out.name, "malformed_json"))
            continue
        if "segments" not in data:
            issues.append(Issue("transcript", out.name, "missing_segments_key"))
            continue
        chars = _segment_chars(data)
        char_counts[page] = chars
        if chars == 0 and img.exists() and img.stat().st_size > PAGE_PNG_MIN_BYTES:
            issues.append(Issue("transcript", out.name, "empty_segments",
                                f"page {page}: image is {img.stat().st_size} bytes "
                                f"but no text transcribed"))

    # Median-relative short-transcript check (skip edge cases of tiny corpora).
    non_zero = [c for c in char_counts.values() if c > 0]
    if len(non_zero) >= 10:
        median = statistics.median(non_zero)
        cutoff = median * cfg.validate.transcript_short_fraction
        for page, chars in char_counts.items():
            if 0 < chars < cutoff:
                # First/last few pages can legitimately be short (chapter ends).
                # Don't flag those.
                if page in (cfg.input.first_page, cfg.input.last_page):
                    continue
                out = cfg.transcript_dir / f"page_{page:04d}.json"
                issues.append(Issue("transcript", out.name, "short_transcript",
                                    f"page {page}: {chars} chars (median {median:.0f}, "
                                    f"cutoff {cutoff:.0f})"))
    return issues


def _signal_density(text: str, signals: tuple[str, ...]) -> float:
    if not signals or not text:
        return 0.0
    hits = sum(text.count(c) for c in signals)
    return hits * 1000 / max(1, len(text))


def validate_translations(cfg: Config) -> list[Issue]:
    issues: list[Issue] = []
    if not cfg.stories_dir.exists():
        return issues

    for f in sorted(cfg.stories_dir.glob("*.json")):
        data = _load_json(f)
        if data is None:
            issues.append(Issue("translation", f.name, "malformed_json"))
            continue
        # Normalize legacy keys for back-compat with old artifacts.
        source = data.get("source") or data.get("german") or ""
        translated = data.get("translated") or data.get("english") or ""
        title = (data.get("title") or "").strip()
        translated_title = (data.get("translated_title")
                            or data.get("english_title") or "").strip()

        if not translated.strip():
            issues.append(Issue("translation", f.name, "empty_translation"))
            continue
        if title and not translated_title:
            issues.append(Issue("translation", f.name, "empty_translated_title",
                                f"source title: {title!r}"))

        if source:
            ratio = len(translated) / len(source)
            if (ratio < cfg.validate.length_ratio_min
                    or ratio > cfg.validate.length_ratio_max):
                issues.append(Issue(
                    "translation", f.name, "length_ratio_out_of_band",
                    f"translated/source = {ratio:.2f} "
                    f"(band {cfg.validate.length_ratio_min}–{cfg.validate.length_ratio_max})"))

        signals = cfg.validate.source_charset_signals
        if signals:
            density = _signal_density(translated, signals)
            if density >= SIGNAL_PER_1K_THRESHOLD:
                issues.append(Issue(
                    "translation", f.name, "source_language_residue",
                    f"{density:.1f} source-signal chars per 1000 in translation "
                    f"(threshold {SIGNAL_PER_1K_THRESHOLD})"))
    return issues


def validate(cfg: Config) -> list[Issue]:
    return validate_transcripts(cfg) + validate_translations(cfg)


def write_report(cfg: Config, issues: list[Issue]) -> Path:
    path = cfg.output_dir / "validation_report.json"
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"count": len(issues), "issues": [i.to_dict() for i in issues]},
        ensure_ascii=False, indent=2))
    return path


def summarize(issues: list[Issue]) -> str:
    if not issues:
        return "No issues found."
    by_reason: dict[str, int] = {}
    for i in issues:
        by_reason[i.reason] = by_reason.get(i.reason, 0) + 1
    lines = [f"{len(issues)} issue(s) found:"]
    for reason, count in sorted(by_reason.items(),
                                key=lambda kv: -kv[1]):
        lines.append(f"  - {reason}: {count}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    issues = validate(cfg)
    report = write_report(cfg, issues)
    print(summarize(issues))
    print(f"\nReport: {report}")


if __name__ == "__main__":
    main()
