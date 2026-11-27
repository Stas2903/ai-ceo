"""A frozen picture of one mission, built without any model call.

The rules run on this snapshot and nothing else, so they are pure and table-testable. The
snapshot's `hash()` covers what the director's LLM agent would reason about (task states,
open and answered cards, owner requests), but not clock-driven numbers such as "idle for
37 minutes", so the agent is woken by change, not by time passing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from aiceo.budget.spend import mission_spent, spent_24h
from aiceo.config import DirectorConfig
from aiceo.models import TaskStatus
from aiceo.store import Store


@dataclass(frozen=True)
class TaskView:
    id: int
    title: str
    status: TaskStatus
    attempts: int
    interruptions: int
    spent_attempts: int
    not_done_code: str | None
    has_live_run: bool
    steps: int
    idle_minutes: float | None  # since the last step (None: no step yet)
    running_minutes: float | None  # since the attempt started
    has_draft: bool


@dataclass(frozen=True)
class CardView:
    id: int
    question: str
    critical: bool
    status: str
    task_id: int | None
    asked_at: datetime
    default_index: int | None
    timeout_hours: float | None


@dataclass(frozen=True)
class RequestView:
    id: int
    title: str
    status: str  # queued (waits for a prerequisite) | asked (objection sent) | ...
    prerequisite_task_id: int | None
    prerequisite_status: TaskStatus | None


@dataclass(frozen=True)
class OverrideView:
    id: int
    question: str
    owner_text: str


@dataclass(frozen=True)
class Snapshot:
    mission_id: str
    mission_name: str
    now: datetime
    tasks: tuple[TaskView, ...]
    open_cards: tuple[CardView, ...]
    answered_cards: tuple[CardView, ...]  # answered since the agent last ran
    requests: tuple[RequestView, ...]
    overrides: tuple[OverrideView, ...]  # "undo N" the agent has not handled yet
    spent_24h: float
    daily_cap: float
    mission_spent: float
    mission_cap: float | None
    decision_keys: frozenset[str]
    last_agent_at: datetime | None
    last_agent_hash: str | None
    idle_streak: int  # consecutive agent calls on an unchanged mission
    open_cap_notice: int | None = None  # the owner was told work is held; not yet cleared

    def task(self, task_id: int) -> TaskView:
        return next(t for t in self.tasks if t.id == task_id)

    def hash(self) -> str:
        material = {
            "tasks": [(t.id, t.status, t.attempts, t.not_done_code) for t in self.tasks],
            "open_cards": sorted(c.id for c in self.open_cards),
            "answered_cards": sorted(c.id for c in self.answered_cards),
            "requests": sorted((r.id, r.status) for r in self.requests),
            "overrides": sorted(o.id for o in self.overrides),
            # whether work is held, not how much was spent: a cap that frees up is news
            "held": (self.daily_cap > 0 and self.spent_24h >= self.daily_cap)
            or (self.mission_cap is not None and self.mission_spent >= self.mission_cap),
        }
        raw = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["decision_keys"] = sorted(self.decision_keys)
        return data


def _minutes(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else (now - then).total_seconds() / 60


def build_snapshot(store: Store, mission_id: str, now: datetime, cfg: DirectorConfig) -> Snapshot:
    mission = store.mission(mission_id)
    live = {task_id for task_id, _ in store.open_runs()}
    tasks = tuple(
        TaskView(
            id=t.id,
            title=t.title,
            status=t.status,
            attempts=t.attempts,
            interruptions=t.interruptions,
            spent_attempts=t.spent_attempts,
            not_done_code=t.not_done_code,
            has_live_run=t.id in live,
            steps=t.steps,
            idle_minutes=_minutes(now, t.last_step_at),
            running_minutes=_minutes(now, t.started_at),
            has_draft=bool(t.draft.strip()),
        )
        for t in store.tasks(mission_id)
    )
    decisions = store.decisions(mission_id)
    agent_calls = [d for d in decisions if d.kind == "agent_call"]
    last_call = agent_calls[-1] if agent_calls else None
    last_at = last_call.created_at if last_call else None

    def card(d) -> CardView:
        p = d.payload
        return CardView(
            d.id,
            p["question"],
            bool(p["critical"]),
            d.status,
            d.task_id,
            datetime.fromisoformat(p["asked_at"]),
            p.get("default_index"),
            p.get("timeout_hours"),
        )

    cards = [d for d in decisions if d.kind == "card"]
    answered = tuple(
        card(d)
        for d in cards
        if d.status == "answered" and (last_at is None or (d.resolved_at or now) > last_at)
    )
    by_id = {t.id: t for t in tasks}
    requests = tuple(
        RequestView(
            d.id,
            d.payload["title"],
            d.status,
            d.payload.get("prerequisite_task_id"),
            by_id[d.payload["prerequisite_task_id"]].status
            if d.payload.get("prerequisite_task_id") in by_id
            else None,
        )
        for d in decisions
        if d.kind == "owner_request" and d.status in ("queued", "asked")
    )
    overrides = tuple(
        OverrideView(d.id, d.payload["question"], d.payload.get("owner_text", ""))
        for d in decisions
        if d.kind == "auto_decision"
        and d.status == "overridden"
        and not d.payload.get("override_handled")
    )
    m_spent, m_cap = mission_spent(store, mission_id, now)
    return Snapshot(
        mission_id=mission_id,
        mission_name=mission["name"],
        now=now,
        tasks=tasks,
        open_cards=tuple(card(d) for d in cards if d.status == "open"),
        answered_cards=answered,
        requests=requests,
        overrides=overrides,
        spent_24h=spent_24h(store, mission_id, now),
        daily_cap=cfg.daily_cap_usd,
        mission_spent=m_spent,
        mission_cap=m_cap,
        decision_keys=frozenset(d.key for d in decisions),
        last_agent_at=last_at,
        last_agent_hash=last_call.payload.get("hash") if last_call else None,
        idle_streak=int(last_call.payload.get("idle_streak", 0)) if last_call else 0,
        open_cap_notice=next(
            (d.id for d in reversed(decisions) if d.kind == "cap_notice" and d.status == "open"),
            None,
        ),
    )
