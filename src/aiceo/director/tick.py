"""One director tick: lock -> snapshot -> rules -> actions -> (maybe) agent -> unlock.

The LLM agent is the expensive part, so it runs only when the rules say so, never while
work is held by a spend cap, and its "seen" state is the snapshot hash taken *after* it
ran: its own retasks do not wake it again on the next tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from aiceo.director import rules
from aiceo.director.actions import DirectorContext, apply_all
from aiceo.director.agent import DirectorAgent, DirectorTools
from aiceo.director.locks import MissionLock
from aiceo.director.snapshot import build_snapshot

logger = logging.getLogger(__name__)


@dataclass
class TickRecord:
    mission_id: str
    actions: list[dict] = field(default_factory=list)
    agent_called: bool = False
    agent_reason: str = ""
    agent_summary: str = ""
    skipped_locked: bool = False
    error: str | None = None


def tick_mission(
    ctx: DirectorContext, agent: DirectorAgent, mission_id: str, now: datetime
) -> TickRecord:
    record = TickRecord(mission_id)
    try:
        snap = build_snapshot(ctx.store, mission_id, now, ctx.cfg)
        planned = rules.evaluate(snap, ctx.cfg, now)
        record.actions = apply_all(ctx, snap, planned, now)
        call = next((a for a in planned if a.kind == "call_agent"), None)
        if call is not None:
            if any(r["outcome"] == "applied" for r in record.actions):
                snap = build_snapshot(ctx.store, mission_id, now, ctx.cfg)  # agent sees fresh state
            tools = DirectorTools(ctx, snap, now)
            record.agent_summary = agent.run(snap, tools, call.reason)
            record.agent_called, record.agent_reason = True, call.reason
            idle = call.reason == "idle" and not tools.calls
            streak = snap.idle_streak + 1 if idle else 0
            call_id = ctx.store.add_decision(
                mission_id,
                "agent_call",
                f"agent:{now.isoformat()}",
                now=now,
                payload={
                    "reason": call.reason,
                    "idle_streak": streak,
                    "summary": record.agent_summary,
                },
            )
            after = build_snapshot(ctx.store, mission_id, now, ctx.cfg)
            if call_id is not None:
                ctx.store.update_decision(call_id, patch={"hash": after.hash()})
    except Exception as exc:  # one mission's failure is recorded, not raised
        logger.warning("tick of mission %s failed", mission_id, exc_info=True)
        record.error = f"{type(exc).__name__}: {exc}"
    return record


def tick_once(
    ctx: DirectorContext,
    agent: DirectorAgent,
    lock: MissionLock,
    now: datetime,
    mission_ids: list[str] | None = None,
) -> list[TickRecord]:
    records = []
    for mission_id in mission_ids if mission_ids is not None else ctx.store.mission_ids():
        if not lock.try_acquire(mission_id):
            records.append(TickRecord(mission_id, skipped_locked=True))
            continue
        try:
            records.append(tick_mission(ctx, agent, mission_id, now))
        finally:
            lock.release(mission_id)
    return records
