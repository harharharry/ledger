"""Kill switch: a file on disk. Present = all bot activity paused.

File-based on purpose — Harry can engage it with `touch KILL_SWITCH` even if
the Python environment is broken. Checked before any action, every run
(CLAUDE.md non-negotiable 5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def is_engaged(path: str | Path) -> bool:
    return Path(path).exists()


def engage(path: str | Path, reason: str = "") -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    Path(path).write_text(f"engaged at {stamp}\n{reason}\n")


def disengage(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
