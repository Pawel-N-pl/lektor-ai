"""Reading one line from a subprocess, with a timeout, on every platform this runs on.

Two backends hold a model in a subprocess launched from its own venv and speak to it over a
JSON-per-line pipe. Both waited on that pipe with `select.select`, which is correct on POSIX and
simply does not work on Windows: there `select` accepts sockets only, and handing it a pipe raises
`OSError` before a single line is read. That one call was the whole reason the pipeline could not
run natively on Windows - everything else in it is ffmpeg, HTTP and pure Python.

A reader thread with a queue behaves the same everywhere. The thread does a blocking `readline`,
which is what the OS is good at, and the caller takes from the queue with a timeout, which is what
the caller actually wants. The thread is a daemon and ends when the pipe closes, so a worker that
dies takes its reader with it rather than leaving one behind per render.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time


class LineReader:
    """One background reader per subprocess. Start it once, then `read` with a deadline."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._q: queue.Queue[bytes] = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self._q.put(line)
        except (ValueError, OSError):
            # The pipe was closed under us while we were blocked on it. Nothing to report: the
            # caller finds out from poll(), which is the authority on whether the worker is alive.
            pass
        finally:
            self._q.put(b"")

    def read(self, timeout_s: float) -> bytes | None:
        """The next line, b"" when the pipe closed, or None when the worker died or ran out of time.

        Polls in short slices rather than blocking for the whole timeout, so a worker that exits
        without answering is noticed in about a second instead of after the full budget - which on
        a model load is ten minutes of looking like a hang.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.proc.poll() is not None:
                    # Give the pump a moment to drain anything already written before the exit.
                    try:
                        return self._q.get(timeout=0.2)
                    except queue.Empty:
                        return None
