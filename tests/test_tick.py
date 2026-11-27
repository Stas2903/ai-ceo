from aiceo.director import tick as tick_mod
from aiceo.director.agent import DirectorTools, ScriptedDirector
from aiceo.director.locks import InProcessLock
from aiceo.director.tick import tick_mission, tick_once
from aiceo.models import ProofKind, TaskStatus
from tests.conftest import M, snap, started


class CountingAgent(ScriptedDirector):
    def __init__(self):
        self.calls = 0

    def run(self, snapshot, tools, reason):
        self.calls += 1
        return super().run(snapshot, tools, reason)


def test_a_locked_mission_is_skipped(ctx, clock):
    lock = InProcessLock()
    assert lock.try_acquire(M)
    [record] = tick_once(ctx, CountingAgent(), lock, clock.now())
    assert record.skipped_locked and record.actions == []
    lock.release(M)
    [record] = tick_once(ctx, CountingAgent(), lock, clock.now())
    assert not record.skipped_locked


def test_one_failing_mission_does_not_stop_the_others(ctx, store, clock, monkeypatch):
    store.create_mission("m2", "Other", now=clock.now())
    real = tick_mod.build_snapshot

    def flaky(store_, mission_id, now, cfg):
        if mission_id == M:
            raise RuntimeError("corrupt row")
        return real(store_, mission_id, now, cfg)

    monkeypatch.setattr(tick_mod, "build_snapshot", flaky)
    records = {
        r.mission_id: r for r in tick_once(ctx, CountingAgent(), InProcessLock(), clock.now())
    }
    assert records[M].error == "RuntimeError: corrupt row"
    assert records["m2"].error is None and records["m2"].agent_called


def test_the_agent_is_not_woken_by_its_own_retask(ctx, store, clock):
    tid = started(store, clock)
    store.finish(
        tid,
        TaskStatus.NOT_DONE,
        now=clock.now(),
        code="placeholders",
        reason="the file still has placeholders",
    )
    agent = CountingAgent()
    first = tick_mission(ctx, agent, M, clock.now())
    assert first.agent_called and store.get_task(tid).status is TaskStatus.QUEUED
    clock.advance(minutes=20)
    assert not tick_mission(ctx, agent, M, clock.now()).agent_called
    assert agent.calls == 1


def test_agent_tools_enforce_the_limits(ctx, store, clock, cfg):
    tid = started(store, clock)
    for _ in range(cfg.max_attempts - 1):
        store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="placeholders")
        store.requeue(tid)
        store.claim(tid, now=clock.now())
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="placeholders")
    tools = DirectorTools(ctx, snap(store, clock, cfg), clock.now())
    assert tools.retask(tid, "again").startswith("refused: «Task» used its 2 attempts")

    stalled = started(store, clock, "Stalled")
    store.finish(stalled, TaskStatus.NOT_DONE, now=clock.now(), code="stalled")
    assert "rules restart" in tools.retask(stalled, "again")

    store.add_spend(M, 50.0, now=clock.now())
    held = DirectorTools(ctx, snap(store, clock, cfg), clock.now())
    assert held.add_task("New", "r", ProofKind.FILE_OK).startswith("refused: new work is on hold")
    assert store.find_task(M, "New") is None


def test_spend_cap_stops_can_be_retasked_beyond_the_budget(ctx, store, clock, cfg):
    tid = started(store, clock)
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="placeholders")
    store.requeue(tid)
    store.claim(tid, now=clock.now())
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="spend_cap")
    tools = DirectorTools(ctx, snap(store, clock, cfg), clock.now())
    assert tools.retask(tid, "continue") == "retasked «Task»"
    assert tools.retask(tid, "continue").startswith("refused")


def test_no_agent_call_while_work_is_held(ctx, store, clock):
    store.add_task(M, "T", "r", ProofKind.FILE_OK, now=clock.now())
    store.add_spend(M, 10.0, now=clock.now())
    agent = CountingAgent()
    record = tick_mission(ctx, agent, M, clock.now())
    assert not record.agent_called and agent.calls == 0


def test_retask_is_refused_while_work_is_held(ctx, store, clock, cfg):
    tid = started(store, clock)
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="spend_cap")
    store.add_spend(M, 50.0, now=clock.now())
    tools = DirectorTools(ctx, snap(store, clock, cfg), clock.now())
    assert tools.retask(tid, "continue").startswith("refused: new work is on hold")
    assert store.get_task(tid).status is TaskStatus.NOT_DONE
