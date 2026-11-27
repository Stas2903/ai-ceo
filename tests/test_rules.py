from datetime import timedelta

import pytest

from aiceo.director import rules
from aiceo.director.actions import apply_all
from aiceo.guard.approvals import CardSpec, open_card
from aiceo.models import ProofKind, TaskStatus
from tests.conftest import M, owner_lines, snap, started


def kinds(actions):
    return [a.kind for a in actions if a.kind != "call_agent"]


def evaluate(store, clock, cfg):
    return rules.evaluate(snap(store, clock, cfg), cfg, clock.now())


# --- stuck work ---


@pytest.mark.parametrize(("minutes", "stuck"), [(29, False), (30, True)])
def test_stuck_threshold_after_last_step(store, clock, cfg, minutes, stuck):
    tid = started(store, clock)
    clock.advance(minutes=minutes)
    acts = [a for a in evaluate(store, clock, cfg) if a.kind == "stop_task"]
    assert bool(acts) is stuck
    if stuck:
        assert (acts[0].task_id, acts[0].key, acts[0].code) == (tid, f"stop:{tid}:1", "stalled")


@pytest.mark.parametrize(("minutes", "stuck"), [(44, False), (45, True)])
def test_no_first_step_threshold(store, clock, cfg, minutes, stuck):
    started(store, clock, steps=0)
    clock.advance(minutes=minutes)
    assert ("stop_task" in kinds(evaluate(store, clock, cfg))) is stuck


def test_stuck_without_live_run_is_closed_directly(store, clock, cfg):
    tid = started(store, clock, live=False)
    clock.advance(minutes=31)
    acts = [a for a in evaluate(store, clock, cfg) if a.task_id == tid]
    assert [a.kind for a in acts] == ["mark_not_done"]


def test_waiting_for_the_owner_is_never_stuck(store, clock, cfg):
    tid = started(store, clock)
    store.set_waiting(tid, True)
    clock.advance(days=3)
    assert not [a for a in evaluate(store, clock, cfg) if a.task_id == tid]


# --- restarts and the retry budget ---


def _stalled(store, clock, tid):
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="stalled", reason="no step")


def test_stalled_task_with_budget_left_is_restarted(store, clock, cfg):
    tid = started(store, clock)
    _stalled(store, clock, tid)
    acts = [a for a in evaluate(store, clock, cfg) if a.task_id == tid]
    assert [(a.kind, a.key) for a in acts] == [("restart_task", f"restart:{tid}:1")]


def test_restart_lost_to_the_platform_does_not_spend_the_budget(store, clock, cfg):
    tid = started(store, clock)
    store.requeue(tid, interrupted=True)  # attempt 1 lost to a restart
    store.claim(tid, now=clock.now())  # attempt 2 = first counted attempt
    _stalled(store, clock, tid)
    acts = [a.kind for a in evaluate(store, clock, cfg) if a.task_id == tid]
    assert acts == ["restart_task"]


def test_spent_budget_tells_the_owner_once(store, clock, cfg, ctx):
    tid = started(store, clock)
    _stalled(store, clock, tid)
    store.requeue(tid)
    store.claim(tid, now=clock.now())
    _stalled(store, clock, tid)  # second counted attempt stalled too
    s = snap(store, clock, cfg)
    acts = [a for a in rules.evaluate(s, cfg, clock.now()) if a.task_id == tid]
    assert [a.kind for a in acts] == ["post_owner"]
    apply_all(ctx, s, acts, clock.now())
    assert owner_lines(ctx.channel)[-1].endswith(
        "Not progressing: «Task». Stopped it, draft saved. What should I simplify?"
    )
    assert not [a for a in evaluate(store, clock, cfg) if a.task_id == tid]


def test_gate_failures_are_left_to_the_agent_not_restarted_by_rules(store, clock, cfg):
    tid = started(store, clock)
    store.finish(tid, TaskStatus.NOT_DONE, now=clock.now(), code="placeholders")
    assert not [a for a in evaluate(store, clock, cfg) if a.task_id == tid]


def test_actions_are_idempotent_across_ticks(store, clock, cfg, ctx):
    tid = started(store, clock)
    clock.advance(minutes=31)
    s = snap(store, clock, cfg)
    planned = rules.evaluate(s, cfg, clock.now())
    first = apply_all(ctx, s, planned, clock.now())
    assert {"action": "stop_task", "key": f"stop:{tid}:1", "outcome": "applied"} in first
    # the same stale snapshot applied again (overlapping or retried tick): nothing happens
    again = apply_all(ctx, s, planned, clock.now())
    assert all(r["outcome"].startswith("skipped") for r in again if r["action"] == "stop_task")
    assert not [a for a in evaluate(store, clock, cfg) if a.kind == "stop_task"]
    assert store.get_task(tid).stop_requested


# --- cards: defaults only for reversible questions ---


def _card(store, channel, clock, *, critical):
    spec = (
        CardSpec("Pay $300?", ("Pay", "Skip"), True, None, None)
        if critical
        else CardSpec("Light or dark?", ("Light", "Dark"), False, 0, 4.0)
    )
    return open_card(store, channel, mission_id=M, spec=spec, now=clock.now())


def test_reversible_card_gets_its_default_after_the_timeout(store, clock, cfg, channel):
    cid = _card(store, channel, clock, critical=False)
    clock.advance(hours=3, minutes=59)
    assert "apply_default" not in kinds(evaluate(store, clock, cfg))
    clock.advance(minutes=1)
    acts = [a for a in evaluate(store, clock, cfg) if a.kind == "apply_default"]
    assert [(a.card_id, a.key) for a in acts] == [(cid, f"default:{cid}")]


def test_critical_card_never_gets_a_default(store, clock, cfg, channel):
    _card(store, channel, clock, critical=True)
    clock.advance(days=30)
    assert "apply_default" not in kinds(evaluate(store, clock, cfg))


# --- spend cap ---


def test_cap_holds_work_and_notifies_once_per_episode(store, clock, cfg, ctx):
    tid = started(store, clock)
    _stalled(store, clock, tid)
    store.add_spend(M, 10.0, now=clock.now())
    s = snap(store, clock, cfg)
    planned = rules.evaluate(s, cfg, clock.now())
    assert kinds(planned)[:2] == ["hold_new_work", "cap_notice"]
    assert "restart_task" not in kinds(planned) and "call_agent" not in [a.kind for a in planned]
    apply_all(ctx, s, planned, clock.now())
    clock.advance(hours=15)  # still within 24 h, past midnight: no second notice
    assert "cap_notice" not in kinds(evaluate(store, clock, cfg))
    clock.advance(hours=10)  # the spend left the window
    s = snap(store, clock, cfg)
    planned = rules.evaluate(s, cfg, clock.now())
    assert "cap_cleared" in kinds(planned) and "restart_task" in kinds(planned)
    apply_all(ctx, s, planned, clock.now())
    lines = owner_lines(ctx.channel)
    assert sum(line.endswith("Back under the spend limit - resuming work.") for line in lines) == 1
    assert sum("Daily spend limit reached" in line for line in lines) == 1
    assert "cap_cleared" not in kinds(evaluate(store, clock, cfg))


def test_mission_cap_counts_only_spend_after_it_was_set(clock, cfg):
    from aiceo.store import Store

    s = Store()
    s.create_mission("m2", "Capped", now=clock.now(), spend_cap_usd=5.0)
    s.add_spend("m2", 4.0, now=clock.now() - timedelta(minutes=1))  # before the cap
    s.add_spend("m2", 4.9, now=clock.now())
    no_daily = type(cfg)(daily_cap_usd=0)
    assert rules.cap_hit(snap(s, clock, no_daily, "m2")) is None
    s.add_spend("m2", 0.1, now=clock.now())
    assert rules.cap_hit(snap(s, clock, no_daily, "m2")) == "mission_spend_cap"


# --- summary and agent wake-ups ---


def test_summary_once_per_day_from_the_summary_hour(store, clock, cfg, ctx):
    clock.advance(minutes=59)  # 08:59
    assert "daily_summary" not in kinds(evaluate(store, clock, cfg))
    clock.advance(minutes=1)
    s = snap(store, clock, cfg)
    planned = rules.evaluate(s, cfg, clock.now())
    assert "daily_summary" in kinds(planned)
    apply_all(ctx, s, planned, clock.now())
    clock.advance(hours=5)
    assert "daily_summary" not in kinds(evaluate(store, clock, cfg))


def test_agent_called_on_change_but_not_on_time_passing(store, clock, cfg, ctx):
    from aiceo.director.agent import ScriptedDirector
    from aiceo.director.tick import tick_mission

    store.add_task(M, "T", "r", ProofKind.FILE_OK, now=clock.now())
    assert tick_mission(ctx, ScriptedDirector(), M, clock.now()).agent_called
    clock.advance(minutes=20)
    assert not tick_mission(ctx, ScriptedDirector(), M, clock.now()).agent_called
    store.add_task(M, "T2", "r", ProofKind.FILE_OK, now=clock.now())
    clock.advance(minutes=20)
    record = tick_mission(ctx, ScriptedDirector(), M, clock.now())
    assert (record.agent_called, record.agent_reason) == (True, "changed")


@pytest.mark.parametrize(("streak", "hours"), [(0, 2), (1, 4), (3, 16), (4, 24), (9, 24)])
def test_idle_back_off_doubles_up_to_the_ceiling(store, clock, cfg, streak, hours):
    s = snap(store, clock, cfg)
    s = type(s)(**{**s.__dict__, "idle_streak": streak})
    assert rules.idle_interval(s, cfg) == timedelta(hours=hours)
