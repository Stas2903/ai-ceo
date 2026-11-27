"""Whole-system invariants over every generated mission (3 simulated days each, with
random behaviours, owner answer delays and platform restarts)."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from aiceo.adapters.console import ConsoleChannel
from aiceo.agents.runner import Workforce
from aiceo.clock import SimClock
from aiceo.config import DirectorConfig
from aiceo.director.actions import DirectorContext
from aiceo.director.agent import ScriptedDirector
from aiceo.guard.approvals import answer_card
from aiceo.models import ProofKind, TaskStatus, Verdict
from aiceo.scenarios import generate_missions, public_world, scripts_for
from aiceo.sim import Simulation
from aiceo.store import Store
from scripts.make_fixtures import render
from tests.conftest import START

MISSIONS = generate_missions()["missions"]
MAX_STEP_USD = 1.8


def simulate(mission):
    clock = SimClock(START)
    cfg = DirectorConfig(daily_cap_usd=mission["daily_cap_usd"])
    store, channel = Store(), ConsoleChannel(clock=clock)
    mid = mission["id"]
    store.create_mission(mid, mission["name"], now=clock.now())
    for t in mission["tasks"]:
        store.add_task(mid, t["title"], t["role"], ProofKind(t["done_kind"]), now=clock.now())
    wf = Workforce(store, public_world(), channel, cfg, clock, scripts_for(mission))
    sim = Simulation(DirectorContext(store, channel, cfg), clock, wf, ScriptedDirector())
    peak = {"spent_24h": 0.0}

    def watch(now):
        peak["spent_24h"] = max(
            peak["spent_24h"], store.spent_between(mid, now - timedelta(hours=24), now)
        )
        delay = mission["owner_answers_critical_after_hours"]
        if delay is None:
            return
        for card in store.decisions(mid, "card", "open"):
            if card.created_at + timedelta(hours=delay) <= now:
                answer_card(store, card.id, 0, now=now)

    sim.on_step = watch
    events = []
    if mission["crash_at_hour"] is not None:

        def crash():
            wf.crash()
            wf.recover(mid)

        events.append((START + timedelta(hours=mission["crash_at_hour"]), crash))
    sim.run_until(START + timedelta(days=3), events)
    return store, cfg, peak, sim


@pytest.fixture(scope="module", params=MISSIONS, ids=[m["id"] for m in MISSIONS])
def outcome(request):
    return request.param, *simulate(request.param)


def test_every_done_task_has_a_passing_proof_of_its_own_kind(outcome):
    mission, store, *_ = outcome
    for task in store.tasks(mission["id"], TaskStatus.DONE):
        proof = store.get_proof(task.proof_id)
        assert (proof.task_id, proof.kind, proof.verdict) == (task.id, task.done_kind, Verdict.PASS)


def test_no_critical_card_was_ever_answered_by_a_default(outcome):
    mission, store, *_ = outcome
    for card in store.decisions(mission["id"], "card"):
        if card.payload["critical"]:
            assert card.payload.get("answered_by") in (None, "owner")


def test_critical_cards_still_wait_when_the_owner_never_answers(outcome):
    mission, store, *_ = outcome
    if mission["owner_answers_critical_after_hours"] is None:
        assert all(
            c.status == "open"
            for c in store.decisions(mission["id"], "card")
            if c.payload["critical"]
        )


def test_spend_overshoots_the_cap_by_less_than_one_step_per_task(outcome):
    mission, _, cfg, peak, _ = outcome
    assert peak["spent_24h"] < cfg.daily_cap_usd + MAX_STEP_USD * len(mission["tasks"])


def test_retry_budget_holds_except_for_spend_cap_continuations(outcome):
    mission, store, cfg, *_ = outcome
    for task in store.tasks(mission["id"]):
        capped = sum(
            1
            for d in store.decisions(mission["id"], "retask")
            if d.task_id == task.id and "spend limit" in d.payload["feedback"]
        )
        assert task.spent_attempts <= cfg.max_attempts + capped


def test_every_failure_has_a_reason_code(outcome):
    mission, store, *_ = outcome
    assert all(t.not_done_code for t in store.tasks(mission["id"], TaskStatus.NOT_DONE))


def test_no_tick_failed(outcome):
    *_, sim = outcome
    assert [t.error for t in sim.ticks if t.error] == []


def test_committed_fixtures_match_the_generator():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "missions.json"
    assert path.read_text(encoding="utf-8") == render()
    assert json.loads(path.read_text(encoding="utf-8"))["synthetic"] is True


def test_the_generated_missions_exercise_every_path():
    """Guards the guards: if the generator stops producing a path, the invariants above
    would pass vacuously for it."""
    paths = set()
    for mission in MISSIONS:
        store, *_ = simulate(mission)
        mid = mission["id"]
        for t in store.tasks(mid):
            paths |= {"done"} if t.status is TaskStatus.DONE else set()
            paths |= {"stalled"} if t.not_done_code == "stalled" else set()
            paths |= {"interrupted"} if t.interruptions else set()
        paths |= {"retask"} if store.decisions(mid, "retask") else set()
        paths |= {"cap"} if store.decisions(mid, "cap_notice") else set()
        paths |= {"auto"} if store.decisions(mid, "auto_decision") else set()
        paths |= {
            f"critical_{c.status}" for c in store.decisions(mid, "card") if c.payload["critical"]
        }
    assert paths >= {
        "done",
        "stalled",
        "interrupted",
        "retask",
        "cap",
        "auto",
        "critical_answered",
        "critical_open",
    }
