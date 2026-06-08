"""Compute per-stage status from artifacts on disk.

Pure read-only logic — given a Config, returns one StageStatus per stage
plus what's stale because of upstream changes the user explicitly made
(tracked in `.gui_state.json` in the output dir).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# Lazy import: pipeline modules are siblings, not subpackages.
import sys as _sys
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from config import Config  # noqa: E402

STAGES = ("render", "transcribe", "segment", "translate", "assemble")


class Status(str, Enum):
    NOT_STARTED = "not_started"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    COMPLETE = "complete"
    STALE = "stale"          # upstream changed; outputs may not reflect config
    FAILED = "failed"
    RUNNING = "running"


@dataclass
class StageStatus:
    stage: str
    status: Status
    done: int = 0
    total: int = 0
    detail: str = ""
    # When status==STALE, the reason the user changed (e.g. "page range changed").
    stale_reason: str = ""


@dataclass
class WorkflowState:
    stages: dict[str, StageStatus] = field(default_factory=dict)

    def __getitem__(self, k: str) -> StageStatus:
        return self.stages[k]

    def items(self):
        return self.stages.items()


# --------------------------------------------------------------------------- #
# Detection                                                                   #
# --------------------------------------------------------------------------- #

def _render_status(cfg: Config) -> StageStatus:
    if cfg.input_kind == "txt":
        # No rasterization for txt input — always "complete" (no-op).
        return StageStatus("render", Status.COMPLETE,
                           detail="text input — no rasterization")
    expected = list(range(cfg.input.first_page, cfg.input.last_page + 1))
    present = [p for p in expected
               if (cfg.pages_dir / f"page_{p:04d}.png").exists()]
    total, done = len(expected), len(present)
    if done == 0:
        return StageStatus("render", Status.NOT_STARTED, 0, total)
    if done < total:
        return StageStatus("render", Status.PARTIAL, done, total)
    return StageStatus("render", Status.COMPLETE, done, total)


def _transcribe_status(cfg: Config) -> StageStatus:
    if cfg.input_kind == "txt":
        # For txt, the upstream "page range" is the chunk range; the existing
        # files are page_NNNN.json under transcript_dir. We can't know the
        # total chunk count without re-chunking the file — defer to "any
        # exist => complete enough to segment".
        existing = list(cfg.transcript_dir.glob("page_*.json")) \
            if cfg.transcript_dir.exists() else []
        n = len(existing)
        if n == 0:
            return StageStatus("transcribe", Status.NOT_STARTED, 0, 0,
                               detail="text mode")
        return StageStatus("transcribe", Status.COMPLETE, n, n,
                           detail=f"{n} chunk(s) transcribed")

    expected = list(range(cfg.input.first_page, cfg.input.last_page + 1))
    # Only count pages we've actually rendered as expected for transcribe.
    rendered = [p for p in expected
                if (cfg.pages_dir / f"page_{p:04d}.png").exists()]
    if not rendered:
        return StageStatus("transcribe", Status.NOT_STARTED, 0, len(expected),
                           detail="render first")
    done = sum(1 for p in rendered
               if (cfg.transcript_dir / f"page_{p:04d}.json").exists())
    if done == 0:
        return StageStatus("transcribe", Status.NOT_STARTED, 0, len(rendered))
    if done < len(rendered):
        return StageStatus("transcribe", Status.PARTIAL, done, len(rendered))
    return StageStatus("transcribe", Status.COMPLETE, done, len(rendered))


def _segment_status(cfg: Config) -> StageStatus:
    if not cfg.stories_json.exists():
        # Did the user already review? If yes, we'd see the gui_state log.
        return StageStatus("segment", Status.NOT_STARTED)
    try:
        stories = json.loads(cfg.stories_json.read_text())
        n = len(stories)
    except Exception:  # noqa: BLE001
        return StageStatus("segment", Status.FAILED, detail="stories.json unreadable")
    gs = _load_gui_state(cfg)
    if not gs.get("segment_reviewed"):
        return StageStatus("segment", Status.NEEDS_REVIEW, n, n,
                           detail=f"{n} section(s) — please review titles")
    return StageStatus("segment", Status.COMPLETE, n, n,
                       detail=f"{n} section(s)")


def _translate_status(cfg: Config) -> StageStatus:
    if not cfg.stories_json.exists():
        return StageStatus("translate", Status.NOT_STARTED, detail="segment first")
    try:
        stories = json.loads(cfg.stories_json.read_text())
        total = len(stories)
    except Exception:  # noqa: BLE001
        return StageStatus("translate", Status.NOT_STARTED, detail="segment first")
    if total == 0:
        return StageStatus("translate", Status.NOT_STARTED, 0, 0)
    if not cfg.stories_dir.exists():
        return StageStatus("translate", Status.NOT_STARTED, 0, total)
    # Match by index — file names start with NNN_.
    existing_indices = set()
    for f in cfg.stories_dir.glob("*.json"):
        try:
            existing_indices.add(int(f.name[:3]))
        except ValueError:
            continue
    done = sum(1 for s in stories if s.get("index") in existing_indices)
    if done == 0:
        return StageStatus("translate", Status.NOT_STARTED, 0, total)
    if done < total:
        return StageStatus("translate", Status.PARTIAL, done, total)
    return StageStatus("translate", Status.COMPLETE, done, total)


def _assemble_status(cfg: Config) -> StageStatus:
    formats = cfg.assemble.formats
    if not formats:
        return StageStatus("assemble", Status.NOT_STARTED, detail="no formats configured")
    targets: list[Path] = []
    name = cfg.assemble.name
    out = cfg.output_dir
    if "side-by-side-html" in formats:
        targets.append(out / f"{name}_review.html")
        targets.append(out / f"{name}_review.md")
    if "book-html" in formats or "book-pdf" in formats:
        targets.append(out / f"{name}.html")
    if "book-pdf" in formats:
        targets.append(out / f"{name}.pdf")
    if not targets:
        return StageStatus("assemble", Status.NOT_STARTED)
    present = [t for t in targets if t.exists()]
    if not present:
        return StageStatus("assemble", Status.NOT_STARTED, 0, len(targets))
    if len(present) < len(targets):
        return StageStatus("assemble", Status.PARTIAL, len(present), len(targets))
    return StageStatus("assemble", Status.COMPLETE, len(present), len(targets))


def compute(cfg: Config) -> WorkflowState:
    state = WorkflowState()
    state.stages["render"] = _render_status(cfg)
    state.stages["transcribe"] = _transcribe_status(cfg)
    state.stages["segment"] = _segment_status(cfg)
    state.stages["translate"] = _translate_status(cfg)
    state.stages["assemble"] = _assemble_status(cfg)
    _apply_stale(cfg, state)
    return state


# --------------------------------------------------------------------------- #
# gui_state.json: small sidecar file in the output dir                        #
# --------------------------------------------------------------------------- #

def _gui_state_path(cfg: Config) -> Path:
    return cfg.output_dir / ".gui_state.json"


def _load_gui_state(cfg: Config) -> dict:
    p = _gui_state_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_gui_state(cfg: Config, data: dict) -> None:
    p = _gui_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def update_gui_state(cfg: Config, **kwargs) -> dict:
    data = _load_gui_state(cfg)
    data.update(kwargs)
    save_gui_state(cfg, data)
    return data


def mark_stale(cfg: Config, stages: list[str], reason: str) -> None:
    data = _load_gui_state(cfg)
    stale = dict(data.get("stale", {}))
    for s in stages:
        stale[s] = reason
    data["stale"] = stale
    save_gui_state(cfg, data)


def clear_stale(cfg: Config, stage: str) -> None:
    data = _load_gui_state(cfg)
    stale = dict(data.get("stale", {}))
    stale.pop(stage, None)
    data["stale"] = stale
    save_gui_state(cfg, data)


def mark_reviewed(cfg: Config) -> None:
    update_gui_state(cfg, segment_reviewed=True)


def clear_reviewed(cfg: Config) -> None:
    data = _load_gui_state(cfg)
    data.pop("segment_reviewed", None)
    save_gui_state(cfg, data)


def _apply_stale(cfg: Config, state: WorkflowState) -> None:
    data = _load_gui_state(cfg)
    stale = data.get("stale") or {}
    for stage, reason in stale.items():
        if stage not in state.stages:
            continue
        s = state.stages[stage]
        # Only mark stale if there are artifacts to be stale about.
        if s.status in (Status.COMPLETE, Status.PARTIAL):
            state.stages[stage] = StageStatus(
                stage=stage, status=Status.STALE,
                done=s.done, total=s.total,
                detail=s.detail, stale_reason=reason,
            )


# --------------------------------------------------------------------------- #
# Reset helpers (file deletion)                                               #
# --------------------------------------------------------------------------- #

def _delete_dir_contents(d: Path, pattern: str) -> int:
    if not d.exists():
        return 0
    n = 0
    for f in d.glob(pattern):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def reset_stage(cfg: Config, stage: str) -> int:
    """Delete this stage's artifacts. Returns count of files removed.

    Does NOT cascade: downstream cards will turn stale by themselves
    because their inputs are now missing.
    """
    if stage == "render":
        n = _delete_dir_contents(cfg.pages_dir, "page_*.png")
    elif stage == "transcribe":
        n = _delete_dir_contents(cfg.transcript_dir, "page_*.json")
    elif stage == "segment":
        n = 0
        if cfg.stories_json.exists():
            try:
                cfg.stories_json.unlink()
                n = 1
            except OSError:
                pass
        clear_reviewed(cfg)
    elif stage == "translate":
        n = _delete_dir_contents(cfg.stories_dir, "*.json")
    elif stage == "assemble":
        n = 0
        for ext in (".html", ".pdf"):
            f = cfg.output_dir / f"{cfg.assemble.name}{ext}"
            if f.exists():
                try:
                    f.unlink(); n += 1
                except OSError:
                    pass
            r = cfg.output_dir / f"{cfg.assemble.name}_review{ext}"
            if r.exists():
                try:
                    r.unlink(); n += 1
                except OSError:
                    pass
        md = cfg.output_dir / f"{cfg.assemble.name}_review.md"
        if md.exists():
            try:
                md.unlink(); n += 1
            except OSError:
                pass
    else:
        raise ValueError(f"unknown stage: {stage}")
    clear_stale(cfg, stage)
    return n
