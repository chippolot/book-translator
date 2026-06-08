"""End-to-end pipeline driver.

  python src/run.py --config book.yaml
  python src/run.py --config book.yaml --stage transcribe
  python src/run.py --config book.yaml --skip-validate

Runs each stage in order. Each individual stage is resumable on its own
(already-done items skip), so re-running the driver is cheap. After
transcribe and translate, runs the validator and auto-retries any
flagged items ONCE with --force; remaining issues are printed and saved
to out/validation_report.json.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import render
import segment
import transcribe
import translate_stories
import assemble
import validate as validator
from config import Config, load_config

ALL_STAGES = ("render", "transcribe", "segment", "translate", "assemble")


def _retry_transcripts(cfg: Config, issues: list[validator.Issue]) -> int:
    pages = set()
    for i in issues:
        if i.stage != "transcript":
            continue
        m = re.match(r"page_(\d{4})\.json$", i.item)
        if m:
            page = int(m.group(1))
            # Delete the bad output so the retry rewrites it cleanly.
            out = cfg.transcript_dir / i.item
            if out.exists() and i.reason in ("malformed_json", "missing_segments_key"):
                out.unlink()
            pages.add(page)
    if not pages:
        return 0
    print(f"\nAuto-retrying {len(pages)} transcript page(s) with --force...\n")
    transcribe.run(cfg, pages=sorted(pages), force=True)
    return len(pages)


def _retry_translations(cfg: Config, issues: list[validator.Issue]) -> int:
    indices = set()
    for i in issues:
        if i.stage != "translation":
            continue
        m = re.match(r"(\d{3})_", i.item)
        if m:
            indices.add(int(m.group(1)))
            out = cfg.stories_dir / i.item
            if out.exists() and i.reason in (
                    "malformed_json", "empty_translation"):
                out.unlink()
    if not indices:
        return 0
    print(f"\nAuto-retrying {len(indices)} translation(s) with --force...\n")
    translate_stories.run(cfg, indices=sorted(indices), force=True)
    return len(indices)


def _validate_with_retry(cfg: Config, stage: str) -> list[validator.Issue]:
    """Run validator, auto-retry once, validate again, return remaining issues."""
    print(f"\n--- Validating {stage} output ---")
    issues = validator.validate(cfg)
    scoped = [i for i in issues
              if (stage == "transcribe" and i.stage == "transcript")
              or (stage == "translate" and i.stage == "translation")]
    print(validator.summarize(scoped))
    if not scoped:
        return []

    if stage == "transcribe":
        _retry_transcripts(cfg, scoped)
    else:
        _retry_translations(cfg, scoped)

    print(f"\n--- Re-validating {stage} after retry ---")
    issues = validator.validate(cfg)
    scoped = [i for i in issues
              if (stage == "transcribe" and i.stage == "transcript")
              or (stage == "translate" and i.stage == "translation")]
    print(validator.summarize(scoped))
    return scoped


def run_pipeline(cfg: Config, stages: list[str], *,
                 workers: int = 4, force: bool = False,
                 skip_validate: bool = False) -> int:
    """Run requested stages. Returns count of remaining issues after all retries."""
    remaining: list[validator.Issue] = []

    for stage in stages:
        print(f"\n========== Stage: {stage} ==========")
        if stage == "render":
            if cfg.input_kind == "txt":
                print("render: skipped (.txt input — no rasterization needed)")
            else:
                render.run(cfg)
        elif stage == "transcribe":
            transcribe.run(cfg, workers=workers, force=force)
            if not skip_validate:
                remaining += _validate_with_retry(cfg, "transcribe")
        elif stage == "segment":
            stories = segment.run(cfg)
            print(f"{len(stories)} section(s) -> {cfg.stories_json}")
        elif stage == "translate":
            translate_stories.run(cfg, workers=workers, force=force)
            if not skip_validate:
                remaining += _validate_with_retry(cfg, "translate")
        elif stage == "assemble":
            assemble.run(cfg)
        else:
            sys.exit(f"unknown stage: {stage}")

    if not skip_validate:
        report = validator.write_report(cfg, remaining)
        print(f"\n=== Pipeline complete ===")
        print(f"Remaining issues: {len(remaining)}")
        if remaining:
            print(validator.summarize(remaining))
            print(f"\nDetails written to {report}")
    return len(remaining)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True,
                    help="path to book.yaml")
    ap.add_argument("--stage", default="all",
                    choices=("all",) + ALL_STAGES + ("validate",),
                    help="run this single stage instead of all")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true",
                    help="re-do work even if outputs already exist")
    ap.add_argument("--skip-validate", action="store_true",
                    help="skip the post-stage validation + auto-retry")
    args = ap.parse_args()

    cfg = load_config(args.config)
    print(f"Loaded config: {cfg.config_path}")
    print(f"  Book: {cfg.book.title} by {cfg.book.author or '?'}")
    print(f"  Languages: {cfg.languages.source} -> {cfg.languages.target}")
    print(f"  Pages: {cfg.input.first_page}-{cfg.input.last_page}")
    print(f"  Providers: transcribe={cfg.providers.transcribe.provider}/"
          f"{cfg.providers.transcribe.model}, "
          f"translate={cfg.providers.translate.provider}/"
          f"{cfg.providers.translate.model}")

    if args.stage == "validate":
        issues = validator.validate(cfg)
        report = validator.write_report(cfg, issues)
        print(validator.summarize(issues))
        print(f"\nReport: {report}")
        sys.exit(0 if not issues else 1)

    stages = list(ALL_STAGES) if args.stage == "all" else [args.stage]
    remaining = run_pipeline(cfg, stages, workers=args.workers,
                             force=args.force, skip_validate=args.skip_validate)
    sys.exit(0 if remaining == 0 else 2)


if __name__ == "__main__":
    main()
