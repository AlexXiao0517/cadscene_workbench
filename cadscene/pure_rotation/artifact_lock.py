from __future__ import annotations

import os
from pathlib import Path
from threading import RLock


_LOCKS_GUARD = RLock()
_RUN_LOCKS: dict[str, RLock] = {}


def pure_rotation_run_lock(run_directory: Path) -> RLock:
    key = os.path.normcase(str(run_directory.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _RUN_LOCKS.setdefault(key, RLock())
