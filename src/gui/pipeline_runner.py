"""Run pipeline stages on a background QThread with progress + cancel.

The GUI's main thread stays responsive while a stage runs. Signals:
  - progress(stage, done, total, line) — per-item progress.
  - usage(stage, provider, model, in_tok, out_tok) — per-API-call usage.
  - log(line) — captured stdout from the pipeline.
  - finished(stage, ok, error) — terminal.
"""

from __future__ import annotations

import contextlib
import io
import sys as _sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in _sys.path:
    _sys.path.insert(0, str(_HERE.parent))

from config import Config  # noqa: E402
import render  # noqa: E402
import transcribe  # noqa: E402
import segment  # noqa: E402
import translate_stories  # noqa: E402
import assemble  # noqa: E402

from . import keys, usage as usage_mod


class _LineCapture(io.TextIOBase):
    """A write-only file object that emits each line via a Qt signal."""

    def __init__(self, sink) -> None:
        super().__init__()
        self._buf = ""
        self._sink = sink

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._sink(self._buf)
            self._buf = ""


class PipelineRunner(QObject):
    progress = Signal(str, int, int, str)  # stage, done, total, line
    usage = Signal(str, dict)               # stage, record
    log = Signal(str)
    finished = Signal(str, bool, str)       # stage, ok, error

    def __init__(self, cfg: Config, stage: str, *,
                 workers: int = 4,
                 force: bool = False,
                 indices: Optional[list[int]] = None,
                 usage_log: Optional[usage_mod.UsageLog] = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.stage = stage
        self.workers = workers
        self.force = force
        self.indices = indices
        self.usage_log = usage_log
        self._thread: Optional[QThread] = None
        self._cancel = threading.Event()
        self._restore_env: dict[str, str] = {}

    # ----- public API -----

    def start(self) -> None:
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run)
        # When _run emits finished, ask the worker's event loop to quit.
        self.finished.connect(self._thread.quit)
        # Once the thread is truly stopped, schedule both QObjects for
        # deletion via Qt so Python's GC doesn't free them while the
        # thread is still spinning down.
        self._thread.finished.connect(self.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @property
    def thread_done(self):
        """Convenience accessor for the worker QThread's finished signal."""
        return self._thread.finished if self._thread is not None else None

    def cancel(self) -> None:
        self._cancel.set()

    # ----- worker body -----

    def _run(self) -> None:  # pragma: no cover - exercised via the GUI
        ok = False
        error = ""
        # Push keychain creds into env right before invoking SDKs.
        self._restore_env = keys.apply_to_env()
        sink = _LineCapture(self.log.emit)

        def progress_cb(done: int, total: int, line: str) -> None:
            self.progress.emit(self.stage, done, total, line)

        usage_cb = None
        if self.usage_log is not None:
            base = self.usage_log.make_callback(self.stage)

            def usage_cb(rec: dict) -> None:
                base(rec)
                self.usage.emit(self.stage, rec)

        try:
            with contextlib.redirect_stdout(sink), \
                    contextlib.redirect_stderr(sink):
                self._dispatch(progress_cb, usage_cb)
            sink.flush()
            ok = True
        except BaseException as exc:  # noqa: BLE001 - incl SystemExit from sub-stages
            sink.flush()
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        finally:
            # Restore prior env (don't leak keys to other in-process callers).
            for k, v in self._restore_env.items():
                if v:
                    import os
                    os.environ[k] = v
                else:
                    import os
                    os.environ.pop(k, None)
            self.finished.emit(self.stage, ok, error)

    def _dispatch(self, progress_cb, usage_cb) -> None:
        cfg = self.cfg
        if self.stage == "render":
            # render is single-threaded and short; no progress hook in the
            # existing impl. Estimate from page count.
            if cfg.input_kind == "txt":
                progress_cb(1, 1, "text input — no rasterization needed")
                return
            total = cfg.input.last_page - cfg.input.first_page + 1
            progress_cb(0, total, "rendering...")
            render.run(cfg)
            progress_cb(total, total, "done")
            return
        if self.stage == "transcribe":
            transcribe.run(cfg, workers=self.workers, force=self.force,
                           progress_cb=progress_cb, usage_cb=usage_cb,
                           cancel=self._cancel)
            return
        if self.stage == "segment":
            # Compute (with LLM prune) but do NOT write — the GUI shows
            # the review dialog and writes when the user clicks Apply.
            stories = segment.run_no_write(cfg)
            # Stash on the runner so the caller can grab them.
            self.segment_result = stories  # type: ignore[attr-defined]
            progress_cb(len(stories), len(stories), f"{len(stories)} section(s)")
            return
        if self.stage == "translate":
            translate_stories.run(cfg, indices=self.indices,
                                  workers=self.workers, force=self.force,
                                  progress_cb=progress_cb, usage_cb=usage_cb,
                                  cancel=self._cancel)
            return
        if self.stage == "assemble":
            written = assemble.run(cfg)
            progress_cb(len(written), len(written), f"{len(written)} file(s)")
            return
        raise ValueError(f"unknown stage: {self.stage}")
