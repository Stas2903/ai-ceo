import sqlite3

import pytest

from aiceo.models import ProofKind, TaskStatus, Verdict
from aiceo.store import ProofRequired
from tests.conftest import M, started


def test_done_without_proof_is_refused_and_status_unchanged(store, clock):
    tid = started(store, clock)
    with pytest.raises(ProofRequired):
        store.finish(tid, TaskStatus.DONE, now=clock.now())
    assert store.get_task(tid).status is TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("verdict", [Verdict.FAIL, Verdict.PENDING])
def test_done_with_a_proof_that_does_not_pass_is_refused(store, clock, verdict):
    tid = started(store, clock)
    pid = store.upsert_proof(tid, ProofKind.FILE_OK, "a.md", verdict, now=clock.now())
    with pytest.raises(ProofRequired):
        store.finish(tid, TaskStatus.DONE, now=clock.now(), proof_id=pid)
    assert store.get_task(tid).status is TaskStatus.IN_PROGRESS


def test_done_with_another_tasks_passing_proof_is_refused(store, clock):
    a, b = started(store, clock, "A"), started(store, clock, "B")
    pid = store.upsert_proof(a, ProofKind.FILE_OK, "a.md", Verdict.PASS, now=clock.now())
    with pytest.raises(ProofRequired):
        store.finish(b, TaskStatus.DONE, now=clock.now(), proof_id=pid)


def test_done_with_own_passing_proof_is_accepted(store, clock):
    tid = started(store, clock)
    pid = store.upsert_proof(tid, ProofKind.FILE_OK, "a.md", Verdict.PASS, now=clock.now())
    store.finish(tid, TaskStatus.DONE, now=clock.now(), proof_id=pid)
    task = store.get_task(tid)
    assert (task.status, task.proof_id) == (TaskStatus.DONE, pid)


def test_database_itself_rejects_done_without_proof(store, clock):
    """Second line of defence: even raw SQL that bypasses finish() cannot do it."""
    tid = started(store, clock)
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (tid,))


def test_decision_key_is_claimed_once(store, clock):
    first = store.add_decision(M, "stop_task", "stop:1:1", now=clock.now())
    second = store.add_decision(M, "stop_task", "stop:1:1", now=clock.now())
    assert first is not None and second is None
    assert len(store.decisions(M, "stop_task")) == 1


def test_claim_is_atomic(store, clock):
    tid = store.add_task(M, "T", "r", ProofKind.FILE_OK, now=clock.now())
    assert store.claim(tid, now=clock.now()) is True
    assert store.claim(tid, now=clock.now()) is False
    assert store.get_task(tid).attempts == 1


def test_interrupted_requeue_does_not_consume_the_retry_budget(store, clock):
    tid = started(store, clock)
    assert store.requeue(tid, interrupted=True)
    store.claim(tid, now=clock.now())
    task = store.get_task(tid)
    assert (task.attempts, task.interruptions, task.spent_attempts) == (2, 1, 1)


def test_ordinary_requeue_does_consume_it(store, clock):
    tid = started(store, clock)
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="stalled")
    store.requeue(tid)
    store.claim(tid, now=clock.now())
    assert store.get_task(tid).spent_attempts == 2


def test_pending_since_is_kept_while_pending_and_reset_after_a_pass(store, clock):
    tid = started(store, clock)
    t0 = clock.now()
    pid = store.upsert_proof(tid, ProofKind.CI_GREEN, "c1", Verdict.PENDING, now=t0)
    clock.advance(minutes=30)
    store.upsert_proof(tid, ProofKind.CI_GREEN, "c1", Verdict.PENDING, now=clock.now())
    assert store.get_proof(pid).pending_since == t0
    assert store.get_proof(pid).checks == 2
    store.upsert_proof(tid, ProofKind.CI_GREEN, "c1", Verdict.PASS, now=clock.now())
    clock.advance(minutes=30)
    store.upsert_proof(tid, ProofKind.CI_GREEN, "c1", Verdict.PENDING, now=clock.now())
    assert store.get_proof(pid).pending_since == clock.now()


def test_update_decision_compare_and_set(store, clock):
    did = store.add_decision(
        M, "card", "k", now=clock.now(), status="open", payload={"question": "q"}
    )
    assert store.update_decision(did, status="answered", expect_status="open")
    assert not store.update_decision(did, status="answered", expect_status="open")


def test_naive_timestamps_are_rejected(store):
    from datetime import datetime

    with pytest.raises(ValueError):
        store.add_task(M, "T", "r", ProofKind.FILE_OK, now=datetime(2026, 1, 1))
