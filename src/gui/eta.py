"""Shared ETA estimator.

Used by every stage card that has a meaningful done/total: feed it
`record(done, total)` on each progress update and read `remaining_seconds()`
to display "Xm Ys remaining".

Approach: rate from a rolling window of recent samples. Window-based is
better than since-start for parallel API work, which speeds up after warm-
up (cache hits, fewer rate-limit retries) — the ETA shouldn't be dragged
down by the cold first item forever.

Two safeguards make the display calm:
- Need a minimum of 2 completed items before estimating (single-sample
  rates are nonsense).
- Min sample interval — a stage that emits 50 progress updates per second
  isn't actually making N×50 items of progress; we cap the sample rate so
  the rolling window reflects real elapsed time.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ETACalculator:
    # Keep ~30 samples or 60 seconds, whichever is more recent.
    window_size: int = 30
    window_seconds: float = 60.0
    # Don't record more than one sample every 0.2s. Spammy callers
    # otherwise dominate the window with near-duplicate samples.
    min_sample_interval: float = 0.2

    _samples: deque = field(default_factory=deque)
    _start_time: Optional[float] = None
    _start_done: int = 0
    _last_recorded_time: float = 0.0

    def reset(self) -> None:
        self._samples.clear()
        self._start_time = None
        self._start_done = 0
        self._last_recorded_time = 0.0

    def record(self, done: int, total: int) -> None:
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            self._start_done = done
        if now - self._last_recorded_time < self.min_sample_interval \
                and self._samples and done == self._samples[-1][1]:
            return
        self._last_recorded_time = now
        self._samples.append((now, done, total))
        # Trim by count.
        while len(self._samples) > self.window_size:
            self._samples.popleft()
        # Trim by age.
        cutoff = now - self.window_seconds
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    # -------------------------------------------------------------------- #

    def remaining_seconds(self) -> Optional[float]:
        """Return estimated seconds remaining, or None if not enough data."""
        if len(self._samples) < 2:
            return None
        first_t, first_done, _ = self._samples[0]
        last_t, last_done, last_total = self._samples[-1]
        elapsed = last_t - first_t
        delta = last_done - first_done
        # Need at least one item completed within the window AND a sensible
        # elapsed time (a parallel pool may emit completions in bursts).
        if elapsed <= 0.001 or delta < 1:
            # Fall back to since-start rate when the window itself is too
            # tight or hasn't accumulated multiple items yet.
            if self._start_time is None or last_t == self._start_time:
                return None
            elapsed = last_t - self._start_time
            delta = last_done - self._start_done
            if delta < 1 or elapsed <= 0.001:
                return None
        rate = delta / elapsed  # items per second
        remaining_items = max(0, last_total - last_done)
        if rate <= 0 or remaining_items == 0:
            return 0.0 if remaining_items == 0 else None
        return remaining_items / rate


def format_remaining(seconds: Optional[float]) -> str:
    """Render seconds as a compact human-readable duration. Empty string
    when None or negative.

    Examples: 4 -> "4s", 84 -> "1m 24s", 3725 -> "1h 2m"."""
    if seconds is None or seconds < 0:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h}h {m}m"
