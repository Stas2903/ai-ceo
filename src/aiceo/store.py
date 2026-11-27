"""SQLite persistence (standard library only).

Two invariants live in the database itself, not only in Python:

* `decisions.UNIQUE(mission_id, key)` - every director action is claimed by inserting its
  decision row first; a second tick that plans the same action finds the key taken and
  does nothing. Idempotency survives retries, crashes and duplicate ticks.
* `tasks.CHECK(status != 'done' OR proof_id IS NOT NULL)` - no code path can mark a task
  done without a proof. `finish()` additionally checks that the proof passed and belongs
  to the task.

The same design maps onto PostgreSQL; SQLite keeps the showcase install-free.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from aiceo.models import Decision, Proof, ProofKind, Task, TaskStatus, Verdict

_STATUSES = ", ".join(f"'{s.value}'" for s in TaskStatus)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    spend_cap_usd REAL,
    cap_set_at TEXT
);
CREATE TABLE IF NOT EXISTS proofs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'pending')),
    reason TEXT NOT NULL DEFAULT '',
    observed TEXT NOT NULL DEFAULT '',
    checks INTEGER NOT NULL DEFAULT 1,
    pending_since TEXT,
    checked_at TEXT NOT NULL,
    UNIQUE (task_id, kind, ref)
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    done_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ({_STATUSES})),
    attempts INTEGER NOT NULL DEFAULT 0,
    interruptions INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    last_step_at TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    stop_code TEXT,
    not_done_code TEXT,
    not_done_reason TEXT,
    draft TEXT NOT NULL DEFAULT '',
    feedback TEXT NOT NULL DEFAULT '',
    proof_id INTEGER REFERENCES proofs(id),
    result TEXT NOT NULL DEFAULT '',
    finished_at TEXT,
    CHECK (status != 'done' OR proof_id IS NOT NULL),
    UNIQUE (mission_id, title)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    task_id INTEGER,
    status TEXT NOT NULL DEFAULT 'done',
    payload TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (mission_id, key)
);
CREATE TABLE IF NOT EXISTS spend (
    id INTEGER PRIMARY KEY,
    mission_id TEXT NOT NULL,
    task_id INTEGER,
    usd REAL NOT NULL CHECK (usd >= 0),
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    task_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (task_id, attempt)
);
"""


class ProofRequired(RuntimeError):
    """A task was about to become `done` without a passing proof of its own."""


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """`BEGIN IMMEDIATE` takes the write lock up front: read-check-write sequences
        inside cannot interleave with another writer."""
        if self._db.in_transaction:
            yield self._db
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # --- missions ---

    def create_mission(
        self, mission_id: str, name: str, *, now: datetime, spend_cap_usd: float | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO missions (id, name, spend_cap_usd, cap_set_at) VALUES (?, ?, ?, ?)",
            (mission_id, name, spend_cap_usd, _ts(now) if spend_cap_usd else None),
        )

    def mission(self, mission_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise KeyError(mission_id)
        return row

    def mission_ids(self) -> list[str]:
        return [r["id"] for r in self._db.execute("SELECT id FROM missions ORDER BY id")]

    # --- tasks ---

    def add_task(
        self,
        mission_id: str,
        title: str,
        role: str,
        done_kind: ProofKind,
        *,
        now: datetime,
        feedback: str = "",
    ) -> int:
        cur = self._db.execute(
            "INSERT INTO tasks (mission_id, title, role, done_kind, created_at, feedback) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mission_id, title, role, ProofKind(done_kind).value, _ts(now), feedback),
        )
        return int(cur.lastrowid)

    def find_task(self, mission_id: str, title: str) -> Task | None:
        row = self._db.execute(
            "SELECT * FROM tasks WHERE mission_id = ? AND title = ?", (mission_id, title)
        ).fetchone()
        return self._task(row) if row else None

    def get_task(self, task_id: int) -> Task:
        row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def tasks(self, mission_id: str, *statuses: TaskStatus) -> list[Task]:
        sql, args = "SELECT * FROM tasks WHERE mission_id = ?", [mission_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            args += [s.value for s in statuses]
        return [self._task(r) for r in self._db.execute(sql + " ORDER BY id", args)]

    def claim(self, task_id: int, *, now: datetime) -> bool:
        """Start the next attempt atomically. False if someone else already started it."""
        cur = self._db.execute(
            "UPDATE tasks SET status = 'in_progress', attempts = attempts + 1, started_at = ?, "
            "last_step_at = NULL, steps = 0, stop_requested = 0, stop_code = NULL, "
            "not_done_code = NULL, not_done_reason = NULL "
            "WHERE id = ? AND status = 'queued'",
            (_ts(now), task_id),
        )
        return cur.rowcount == 1

    def record_step(self, task_id: int, *, now: datetime, note: str = "") -> None:
        draft_add = f"{note}\n" if note else ""
        self._db.execute(
            "UPDATE tasks SET steps = steps + 1, last_step_at = ?, draft = draft || ? "
            "WHERE id = ? AND status = 'in_progress'",
            (_ts(now), draft_add, task_id),
        )

    def request_stop(self, task_id: int, code: str) -> bool:
        cur = self._db.execute(
            "UPDATE tasks SET stop_requested = 1, stop_code = ? "
            "WHERE id = ? AND status IN ('in_progress', 'waiting_owner')",
            (code, task_id),
        )
        return cur.rowcount == 1

    def set_waiting(self, task_id: int, waiting: bool) -> bool:
        src, dst = ("in_progress", "waiting_owner") if waiting else ("waiting_owner", "in_progress")
        cur = self._db.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status = ?", (dst, task_id, src)
        )
        return cur.rowcount == 1

    def finish(
        self,
        task_id: int,
        status: TaskStatus,
        *,
        now: datetime,
        proof_id: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        result: str = "",
    ) -> None:
        status = TaskStatus(status)
        if status not in (
            TaskStatus.DONE,
            TaskStatus.NOT_DONE,
            TaskStatus.VERIFYING,
            TaskStatus.CANCELLED,
        ):
            raise ValueError(f"finish() cannot set {status}")
        with self.transaction():
            if status is TaskStatus.DONE:
                if proof_id is None:
                    raise ProofRequired(f"task {task_id}: done needs a proof")
                proof = self.get_proof(proof_id)
                if proof.task_id != task_id or proof.verdict is not Verdict.PASS:
                    raise ProofRequired(f"task {task_id}: proof {proof_id} does not pass")
            self._db.execute(
                "UPDATE tasks SET status = ?, proof_id = ?, not_done_code = ?, "
                "not_done_reason = ?, result = CASE WHEN ? = '' THEN result ELSE ? END, "
                "finished_at = ? WHERE id = ?",
                (status.value, proof_id, code, reason, result, result, _ts(now), task_id),
            )

    def requeue(
        self, task_id: int, *, feedback: str | None = None, interrupted: bool = False
    ) -> bool:
        """Back to the queue for another attempt. `interrupted` marks a start lost to the
        platform: it is counted separately and does not consume the retry budget."""
        cur = self._db.execute(
            "UPDATE tasks SET status = 'queued', stop_requested = 0, stop_code = NULL, "
            "proof_id = NULL, finished_at = NULL, "
            "interruptions = interruptions + ?, feedback = COALESCE(?, feedback) "
            "WHERE id = ? AND status IN ('not_done', 'in_progress', 'waiting_owner')",
            (1 if interrupted else 0, feedback, task_id),
        )
        return cur.rowcount == 1

    # --- proofs ---

    def upsert_proof(
        self,
        task_id: int,
        kind: ProofKind,
        ref: str,
        verdict: Verdict,
        *,
        now: datetime,
        reason: str = "",
        observed: str = "",
    ) -> int:
        """One row per (task, kind, ref): a re-check updates it instead of adding rows.
        `pending_since` is set when the row turns pending and kept while it stays pending,
        so a verification timeout is measured from this attempt, not an older one."""
        verdict = Verdict(verdict)
        with self.transaction():
            row = self._db.execute(
                "SELECT id, verdict, pending_since FROM proofs "
                "WHERE task_id = ? AND kind = ? AND ref = ?",
                (task_id, ProofKind(kind).value, ref),
            ).fetchone()
            pending_since = None
            if verdict is Verdict.PENDING:
                was_pending = row is not None and row["verdict"] == Verdict.PENDING.value
                pending_since = row["pending_since"] if was_pending else _ts(now)
            if row is None:
                cur = self._db.execute(
                    "INSERT INTO proofs (task_id, kind, ref, verdict, reason, observed, "
                    "pending_since, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        kind.value,
                        ref,
                        verdict.value,
                        reason,
                        observed,
                        pending_since,
                        _ts(now),
                    ),
                )
                return int(cur.lastrowid)
            self._db.execute(
                "UPDATE proofs SET verdict = ?, reason = ?, observed = ?, checks = checks + 1, "
                "pending_since = ?, checked_at = ? WHERE id = ?",
                (verdict.value, reason, observed, pending_since, _ts(now), row["id"]),
            )
            return int(row["id"])

    def get_proof(self, proof_id: int) -> Proof:
        row = self._db.execute("SELECT * FROM proofs WHERE id = ?", (proof_id,)).fetchone()
        if row is None:
            raise KeyError(proof_id)
        return Proof(
            id=row["id"],
            task_id=row["task_id"],
            kind=ProofKind(row["kind"]),
            ref=row["ref"],
            verdict=Verdict(row["verdict"]),
            reason=row["reason"],
            observed=row["observed"],
            checks=row["checks"],
            pending_since=_dt(row["pending_since"]),
            checked_at=_dt(row["checked_at"]),
        )

    def proofs_for(self, task_id: int) -> list[Proof]:
        ids = self._db.execute("SELECT id FROM proofs WHERE task_id = ? ORDER BY id", (task_id,))
        return [self.get_proof(r["id"]) for r in ids]

    # --- decisions (the director's journal) ---

    def add_decision(
        self,
        mission_id: str,
        kind: str,
        key: str,
        *,
        now: datetime,
        task_id: int | None = None,
        status: str = "done",
        payload: dict | None = None,
    ) -> int | None:
        """Insert or do nothing. Returns the new id, or None if `key` was already taken."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO decisions (mission_id, kind, key, task_id, status, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mission_id, kind, key, task_id, status, json.dumps(payload or {}), _ts(now)),
        )
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def decision(self, decision_id: int) -> Decision:
        row = self._db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(decision_id)
        return self._decision(row)

    def decision_by_key(self, mission_id: str, key: str) -> Decision | None:
        row = self._db.execute(
            "SELECT * FROM decisions WHERE mission_id = ? AND key = ?", (mission_id, key)
        ).fetchone()
        return self._decision(row) if row else None

    def decisions(
        self, mission_id: str, kind: str | None = None, status: str | None = None
    ) -> list[Decision]:
        sql, args = "SELECT * FROM decisions WHERE mission_id = ?", [mission_id]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        return [self._decision(r) for r in self._db.execute(sql + " ORDER BY id", args)]

    def update_decision(
        self,
        decision_id: int,
        *,
        status: str | None = None,
        patch: dict | None = None,
        now: datetime | None = None,
        expect_status: str | None = None,
    ) -> bool:
        """Compare-and-set on status: with `expect_status`, a concurrent change wins and
        this call returns False."""
        with self.transaction():
            current = self.decision(decision_id)
            if expect_status is not None and current.status != expect_status:
                return False
            payload = {**current.payload, **(patch or {})}
            self._db.execute(
                "UPDATE decisions SET status = ?, payload = ?, resolved_at = COALESCE(?, "
                "resolved_at) WHERE id = ?",
                (status or current.status, json.dumps(payload), _ts(now), decision_id),
            )
            return True

    # --- spend ---

    def add_spend(
        self, mission_id: str, usd: float, *, now: datetime, task_id: int | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO spend (mission_id, task_id, usd, at) VALUES (?, ?, ?, ?)",
            (mission_id, task_id, usd, _ts(now)),
        )

    def spent_between(self, mission_id: str, start: datetime, end: datetime) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(usd), 0) AS s FROM spend WHERE mission_id = ? AND at >= ? "
            "AND at <= ?",
            (mission_id, _ts(start), _ts(end)),
        ).fetchone()
        return round(float(row["s"]), 6)

    # --- runs (which attempts have a live worker) ---

    def open_run(self, task_id: int, attempt: int, *, now: datetime) -> None:
        self._db.execute(
            "INSERT INTO runs (task_id, attempt, opened_at) VALUES (?, ?, ?)",
            (task_id, attempt, _ts(now)),
        )

    def close_run(self, task_id: int, attempt: int, *, now: datetime) -> None:
        self._db.execute(
            "UPDATE runs SET closed_at = ? WHERE task_id = ? AND attempt = ? AND closed_at IS NULL",
            (_ts(now), task_id, attempt),
        )

    def open_runs(self) -> list[tuple[int, int]]:
        rows = self._db.execute("SELECT task_id, attempt FROM runs WHERE closed_at IS NULL")
        return [(r["task_id"], r["attempt"]) for r in rows]

    # --- row mapping ---

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            mission_id=row["mission_id"],
            title=row["title"],
            role=row["role"],
            done_kind=ProofKind(row["done_kind"]),
            status=TaskStatus(row["status"]),
            attempts=row["attempts"],
            interruptions=row["interruptions"],
            created_at=_dt(row["created_at"]),
            started_at=_dt(row["started_at"]),
            last_step_at=_dt(row["last_step_at"]),
            steps=row["steps"],
            stop_requested=bool(row["stop_requested"]),
            stop_code=row["stop_code"],
            not_done_code=row["not_done_code"],
            not_done_reason=row["not_done_reason"],
            draft=row["draft"],
            feedback=row["feedback"],
            proof_id=row["proof_id"],
            result=row["result"],
            finished_at=_dt(row["finished_at"]),
        )

    @staticmethod
    def _decision(row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"],
            mission_id=row["mission_id"],
            kind=row["kind"],
            key=row["key"],
            task_id=row["task_id"],
            status=row["status"],
            payload=json.loads(row["payload"]),
            created_at=_dt(row["created_at"]),
            resolved_at=_dt(row["resolved_at"]),
        )
