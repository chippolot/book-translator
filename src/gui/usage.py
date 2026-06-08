"""Token-usage logging.

Each API call appends one JSON record to `<output_dir>/usage.jsonl`. We
intentionally do NOT estimate dollar cost — model prices drift and we'd
ship stale numbers. The GUI shows raw token totals and a per-stage
breakdown.

Schema (one per line):
  {"ts": 1717800000, "stage": "transcribe",
   "provider": "anthropic", "model": "claude-sonnet-4-6",
   "input_tokens": 1234, "output_tokens": 567, "cache_read_tokens": 0}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable, Iterable


@dataclass
class Totals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    calls: int = 0

    def add(self, rec: dict) -> None:
        self.input_tokens += int(rec.get("input_tokens", 0) or 0)
        self.output_tokens += int(rec.get("output_tokens", 0) or 0)
        self.cache_read_tokens += int(rec.get("cache_read_tokens", 0) or 0)
        self.calls += 1


@dataclass
class UsageSummary:
    total: Totals = field(default_factory=Totals)
    by_stage: dict[str, Totals] = field(default_factory=dict)
    by_provider: dict[str, Totals] = field(default_factory=dict)

    def add(self, rec: dict) -> None:
        self.total.add(rec)
        self.by_stage.setdefault(rec.get("stage", "?"), Totals()).add(rec)
        self.by_provider.setdefault(rec.get("provider", "?"), Totals()).add(rec)


class UsageLog:
    """Thread-safe append-only writer + in-memory aggregator."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self._listeners: list[Callable[[dict], None]] = []
        self.summary = UsageSummary()
        if path.exists():
            for rec in _read_records(path):
                self.summary.add(rec)

    def make_callback(self, stage: str) -> Callable[[dict], None]:
        """Return a usage_cb to pass into providers.* for this stage.

        The returned callable is safe to invoke from worker threads.
        """
        def _cb(rec: dict) -> None:
            rec = dict(rec)
            rec["stage"] = stage
            rec["ts"] = int(time.time())
            self._append(rec)
        return _cb

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked (on the writer thread) for each new
        record — used by the GUI to update the top-bar live."""
        self._listeners.append(fn)

    def _append(self, rec: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.summary.add(rec)
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:  # noqa: BLE001 - never let UI break the run
                pass


def _read_records(path: Path) -> Iterable[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def format_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}K"
    return f"{n/1_000_000:.2f}M"
