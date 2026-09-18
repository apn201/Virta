"""Atomic file writes that survive a briefly locked target.

Write to a temp file in the same folder, then os.replace() it over the target,
so a reader or a power cut never sees half a file.

Measured on the dev PC: the project lives in a Dropbox folder, and Dropbox
briefly locks a file it has just seen change - os.replace() then fails with
PermissionError [WinError 5] on roughly 1 write in 6. A few short retries clear
it. On the Pi (no sync client) the first attempt simply succeeds.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

RETRIES = 8
RETRY_SLEEP_S = 0.05


def atomic_write_text(path: str | Path, text: str) -> None:
    """Atomically replace `path` with `text`. Raises if it still fails after retries."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        for attempt in range(RETRIES):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(RETRY_SLEEP_S * (attempt + 1))
    finally:
        Path(tmp).unlink(missing_ok=True)
