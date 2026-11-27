"""Apply the rules' actions. Each applier first claims the action's key in the decision
journal; if the key is taken the action was already applied and is skipped. Appliers also
re-read the task: time passes between snapshot and action, and the task may have moved."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiceo.adapters.console import OwnerChannel
from aiceo.config import DirectorConfig
from aiceo.director.rules import Action
from aiceo.director.snapshot import Snapshot
from aiceo.director.summary import daily_summary_text
from aiceo.guard import approvals
from aiceo.models import ProofKind, TaskStatus
from aiceo.store import Store


@dataclass(frozen=True)
class DirectorContext:
    store: Store
    channel: OwnerChannel
    cfg: DirectorConfig


def _claim(ctx: DirectorContext, s: Snapshot, a: Action, now: datetime, **payload) -> bool:
    return (
        ctx.store.add_decision(
            s.mission_id,
            a.kind,
            a.key,
            now=now,
            task_id=a.task_id,
            payload={"code": a.code, "reason": a.reason, "attempt": a.attempt, **payload},
        )
        is not None
    )


def _apply(ctx: DirectorContext, s: Snapshot, a: Action, now: datetime) -> str:
    store, ch = ctx.store, ctx.channel
    if a.kind == "hold_new_work":
        return "held"  # a state, not an event: nothing to claim
    if not a.key:
        raise ValueError(f"{a.kind} without an idempotency key")

    if a.kind in ("stop_task", "mark_not_done", "restart_task"):
        # claim and effect in one transaction: a crash between them cannot leave an action
        # marked as applied that never happened
        expected = TaskStatus.NOT_DONE if a.kind == "restart_task" else TaskStatus.IN_PROGRESS
        with store.transaction():
            task = store.get_task(a.task_id)
            if task.status is not expected or task.attempts != a.attempt:
                return "skipped: task moved"
            if not _claim(ctx, s, a, now):
                return "skipped: already applied"
            if a.kind == "stop_task":
                store.request_stop(a.task_id, a.code)
            elif a.kind == "mark_not_done":
                store.finish(a.task_id, TaskStatus.NOT_DONE, now=now, code=a.code, reason=a.reason)
            else:
                store.requeue(
                    a.task_id,
                    feedback=(
                        f"The previous attempt stalled ({task.not_done_reason}). Continue from the "
                        "draft and hand in what you have."
                    ),
                )
        budget = f"attempt {task.spent_attempts} of {ctx.cfg.max_attempts}"
        if a.kind == "stop_task":
            ch.log(s.mission_id, f"stop «{task.title}» ({budget}): {a.reason}")
        elif a.kind == "mark_not_done":
            ch.log(s.mission_id, f"«{task.title}» closed not_done ({a.code}): no live run")
        else:
            ch.log(
                s.mission_id,
                f"restart «{task.title}» from its draft "
                f"(attempt {task.spent_attempts + 1} of {ctx.cfg.max_attempts})",
            )
        return "applied"

    if a.kind == "post_owner":
        if not _claim(ctx, s, a, now, text=a.text):
            return "skipped: already applied"
        ch.send(s.mission_id, a.text)
        return "applied"

    if a.kind == "cap_notice":
        if (
            ctx.store.add_decision(
                s.mission_id, a.kind, a.key, now=now, status="open", payload={"cap": a.code}
            )
            is None
        ):
            return "skipped: already applied"
        ch.send(s.mission_id, a.text)
        return "applied"

    if a.kind == "cap_cleared":
        if not _claim(ctx, s, a, now):
            return "skipped: already applied"
        store.update_decision(s.open_cap_notice, status="closed", now=now, expect_status="open")
        ch.send(s.mission_id, a.text)
        return "applied"

    if a.kind == "apply_default":
        if not _claim(ctx, s, a, now):
            return "skipped: already applied"
        ok = approvals.apply_default(store, a.card_id, now=now)
        if ok:
            card = store.decision(a.card_id)
            choice = card.payload["options"][card.payload["default_index"]]
            ch.send(
                s.mission_id,
                f"No answer on «{card.payload['question']}» - going with "
                f"«{choice}». Reply to change it.",
            )
        return "applied" if ok else "refused"

    if a.kind == "launch_request":
        req = store.decision(a.request_id)
        if req.status != "queued" or not _claim(ctx, s, a, now):
            return "skipped: already applied"
        task_id = _launch(store, s.mission_id, req.payload, now)
        store.update_decision(
            req.id,
            status="launched",
            now=now,
            expect_status="queued",
            patch={"launched_task_id": task_id},
        )
        ch.send(s.mission_id, f"Preparation is done - starting «{req.payload['title']}».")
        return "applied"

    if a.kind == "request_blocked":
        req = store.decision(a.request_id)
        if req.status != "queued" or not _claim(ctx, s, a, now):
            return "skipped: already applied"
        store.update_decision(req.id, status="asked", now=now, expect_status="queued")
        ch.send(s.mission_id, a.text)
        return "applied"

    if a.kind == "daily_summary":
        if not _claim(ctx, s, a, now):
            return "skipped: already applied"
        ch.send(s.mission_id, daily_summary_text(store, s, now))
        return "applied"

    raise ValueError(f"no applier for {a.kind}")


def _launch(store: Store, mission_id: str, payload: dict, now: datetime) -> int:
    existing = store.find_task(mission_id, payload["title"])
    if existing is not None:
        return existing.id
    return store.add_task(
        mission_id, payload["title"], payload["role"], ProofKind(payload["done_kind"]), now=now
    )


def apply_all(
    ctx: DirectorContext, s: Snapshot, actions: list[Action], now: datetime
) -> list[dict]:
    """Apply in order; one failing action is recorded and does not stop the others."""
    records = []
    for a in actions:
        if a.kind == "call_agent":
            continue
        try:
            outcome = _apply(ctx, s, a, now)
        except Exception as exc:  # recorded on the tick, never swallowed
            outcome = f"error: {type(exc).__name__}: {exc}"
        records.append({"action": a.kind, "key": a.key, "outcome": outcome})
    return records
