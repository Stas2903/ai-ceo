"""Domain types shared by the director, the evidence gate, the guard and the runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TaskStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    WAITING_OWNER = "waiting_owner"  # blocked on an owner answer: waiting is not hanging
    VERIFYING = "verifying"  # result handed in, proof still pending
    DONE = "done"  # reachable only with a passing proof
    NOT_DONE = "not_done"  # honest failure with a machine-readable reason code
    CANCELLED = "cancelled"


OPEN_STATUSES = frozenset(
    {TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_OWNER, TaskStatus.VERIFYING}
)


class ProofKind(StrEnum):
    URL_OK = "url_ok"  # the page answers 2xx
    FILE_OK = "file_ok"  # the file exists, is non-empty and has no placeholders
    CI_GREEN = "ci_green"  # the pipeline of this exact change is green
    SOURCES_OK = "sources_ok"  # enough sources, most of them reachable
    HUMAN_CONFIRMED = "human_confirmed"  # the owner pressed "confirm"


PROOF_HUMAN: dict[ProofKind, str] = {
    ProofKind.URL_OK: "the page opens",
    ProofKind.FILE_OK: "a complete file with the result",
    ProofKind.CI_GREEN: "a change with green checks",
    ProofKind.SOURCES_OK: "sources that open",
    ProofKind.HUMAN_CONFIRMED: "your confirmation",
}


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"


@dataclass(frozen=True)
class Task:
    id: int
    mission_id: str
    title: str
    role: str
    done_kind: ProofKind
    status: TaskStatus
    attempts: int  # every start counts, so (task, attempt) is a unique idempotency key
    interruptions: int  # starts lost to a platform restart, not to the agent
    created_at: datetime
    started_at: datetime | None
    last_step_at: datetime | None
    steps: int  # steps in the current attempt
    stop_requested: bool
    stop_code: str | None
    not_done_code: str | None
    not_done_reason: str | None
    draft: str  # work notes kept across attempts; a restart continues from here
    feedback: str  # what the next attempt is told (gate verdict, director note)
    proof_id: int | None
    result: str
    finished_at: datetime | None

    @property
    def spent_attempts(self) -> int:
        """Attempts that count against the retry budget: restarts of the platform do not."""
        return max(self.attempts - self.interruptions, 0)


@dataclass(frozen=True)
class Proof:
    id: int
    task_id: int
    kind: ProofKind
    ref: str
    verdict: Verdict
    reason: str
    observed: str
    checks: int
    pending_since: datetime | None
    checked_at: datetime


@dataclass(frozen=True)
class Decision:
    """One row of the director's journal. `key` is unique per mission: writing the same
    decision twice is a no-op, which is what makes every action idempotent."""

    id: int
    mission_id: str
    kind: str
    key: str
    task_id: int | None
    status: str
    payload: dict
    created_at: datetime
    resolved_at: datetime | None
