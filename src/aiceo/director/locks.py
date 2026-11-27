"""One tick per mission at a time.

With PostgreSQL this is a session-level advisory lock per mission
(`pg_try_advisory_lock(hashtext('director:' || mission_id))`) on a dedicated autocommit
connection, so a second API process skips the mission instead of double-acting. The
showcase runs in one process, so the same contract is met with in-process locks.
"""

from __future__ import annotations

import threading
from typing import Protocol


class MissionLock(Protocol):
    def try_acquire(self, mission_id: str) -> bool: ...

    def release(self, mission_id: str) -> None: ...


class InProcessLock:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: set[str] = set()

    def try_acquire(self, mission_id: str) -> bool:
        with self._guard:
            if mission_id in self._held:
                return False
            self._held.add(mission_id)
            return True

    def release(self, mission_id: str) -> None:
        with self._guard:
            self._held.discard(mission_id)
