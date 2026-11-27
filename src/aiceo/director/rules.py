"""The director's rules: snapshot in, list of actions out. Pure functions, no I/O, no model.

Each action carries an idempotency `key`. Rules skip keys the snapshot already shows as
taken, and the applier claims the key in the database before acting, so the same action
can never run twice even if two ticks overlap or a tick is retried after a crash.

Rule table (in evaluation order):

    spend cap reached          -> hold new work, tell the owner once per hold episode
    cap no longer reached      -> close the episode, tell the owner work resumes
    in progress, no new step   -> stop the run (live) or close it not_done (no live run)
    stalled, budget left       -> restart from the draft
    stalled, budget spent      -> one line to the owner: "not progressing, what to simplify?"
    reversible card timed out  -> apply its default (critical cards: never)
    owner request unblocked    -> launch it / ask "do it?" if its prerequisite failed
    morning                    -> daily summary
    snapshot changed / idle    -> call the LLM agent (unless work is held)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from aiceo.budget.spend import CAP_OWNER_TEXT, DAILY_CAP, MISSION_CAP
from aiceo.config import DirectorConfig
from aiceo.director.snapshot import Snapshot, TaskView
from aiceo.models import TaskStatus

ActionKind = Literal[
    "hold_new_work",
    "post_owner",
    "cap_notice",
    "cap_cleared",
    "stop_task",
    "mark_not_done",
    "restart_task",
    "apply_default",
    "launch_request",
    "request_blocked",
    "daily_summary",
    "call_agent",
]

# not_done codes the rules restart by themselves. Gate verdicts ("placeholders",
# "site_down", ...) are not here: whether to retry, re-scope or ask is a judgement call
# for the agent. `spend_cap` is not here: the cap decides, not the retry loop.
RESTARTABLE = frozenset({"stalled"})
BACK_UNDER_CAP_TEXT = "Back under the spend limit - resuming work."
STALLED = "stalled"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    key: str = ""
    task_id: int | None = None
    attempt: int | None = None
    code: str = ""
    reason: str = ""
    text: str = ""
    card_id: int | None = None
    request_id: int | None = None


def cap_hit(s: Snapshot) -> str | None:
    if s.daily_cap > 0 and s.spent_24h >= s.daily_cap:
        return DAILY_CAP
    if s.mission_cap is not None and s.mission_spent >= s.mission_cap:
        return MISSION_CAP
    return None


def hold_new_work(s: Snapshot) -> bool:
    """The single definition of "no new work now". The agent's tools call it too, so an
    LLM cannot start work the rules would refuse."""
    return cap_hit(s) is not None


def _taken(s: Snapshot, key: str) -> bool:
    return key in s.decision_keys


def _is_stuck(t: TaskView, cfg: DirectorConfig) -> str | None:
    if t.status is not TaskStatus.IN_PROGRESS:
        return None  # waiting for the owner or verifying is not hanging
    if t.steps > 0 and t.idle_minutes is not None and t.idle_minutes >= cfg.stuck_minutes:
        return f"no new step for {int(t.idle_minutes)} min"
    if t.steps == 0 and (t.running_minutes or 0) >= cfg.no_first_step_minutes:
        return f"no first step after {int(t.running_minutes or 0)} min"
    return None


def stalled_out_text(title: str, has_draft: bool) -> str:
    draft = "draft saved" if has_draft else "no draft yet"
    return f"Not progressing: «{title}». Stopped it, {draft}. What should I simplify?"


def _task_rules(s: Snapshot, t: TaskView, cfg: DirectorConfig, hold: bool) -> list[Action]:
    out: list[Action] = []
    why = _is_stuck(t, cfg)
    if why:
        if t.has_live_run:
            key = f"stop:{t.id}:{t.attempts}"
            if not _taken(s, key):
                out.append(Action("stop_task", key, t.id, t.attempts, STALLED, why))
        else:
            # nobody would read a stop flag: close it directly
            key = f"not_done:{t.id}:{t.attempts}"
            if not _taken(s, key):
                out.append(Action("mark_not_done", key, t.id, t.attempts, STALLED, why))
    if t.status is TaskStatus.NOT_DONE and t.not_done_code in RESTARTABLE:
        if t.spent_attempts < cfg.max_attempts:
            key = f"restart:{t.id}:{t.attempts}"
            if not hold and not _taken(s, key):
                out.append(Action("restart_task", key, t.id, t.attempts, t.not_done_code))
        else:
            key = f"stalled_out:{t.id}:{t.attempts}"
            if not _taken(s, key):
                out.append(
                    Action(
                        "post_owner",
                        key,
                        t.id,
                        t.attempts,
                        text=stalled_out_text(t.title, t.has_draft),
                    )
                )
    return out


def _card_rules(s: Snapshot) -> list[Action]:
    out = []
    for c in s.open_cards:
        if c.critical or c.default_index is None or c.timeout_hours is None:
            continue  # critical cards wait for a human, however long that takes
        if (s.now - c.asked_at).total_seconds() >= c.timeout_hours * 3600:
            key = f"default:{c.id}"
            if not _taken(s, key):
                out.append(Action("apply_default", key, card_id=c.id))
    return out


def _request_rules(s: Snapshot, hold: bool) -> list[Action]:
    out = []
    for r in s.requests:
        if r.status != "queued" or r.prerequisite_status is None:
            continue
        if r.prerequisite_status is TaskStatus.DONE and not hold:
            key = f"launch:{r.id}"
            if not _taken(s, key):
                out.append(Action("launch_request", key, request_id=r.id))
        elif r.prerequisite_status in (TaskStatus.NOT_DONE, TaskStatus.CANCELLED):
            key = f"blocked:{r.id}"
            if not _taken(s, key):
                out.append(
                    Action(
                        "request_blocked",
                        key,
                        request_id=r.id,
                        text=f"Could not prepare what «{r.title}» needs. Reply 'do it' to start "
                        "it anyway, or 'cancel'.",
                    )
                )
    return out


def summary_due(s: Snapshot, cfg: DirectorConfig) -> str | None:
    day = s.now.date().isoformat()
    if s.now.hour >= cfg.summary_hour and not _taken(s, f"summary:{day}"):
        return day
    return None


def idle_interval(s: Snapshot, cfg: DirectorConfig) -> timedelta:
    hours = min(cfg.idle_base_hours * (2**s.idle_streak), cfg.idle_max_hours)
    return timedelta(hours=hours)


def _agent_rule(s: Snapshot, cfg: DirectorConfig, now: datetime) -> list[Action]:
    since = None if s.last_agent_at is None else now - s.last_agent_at
    if since is not None and since < timedelta(minutes=cfg.agent_min_interval_minutes):
        return []
    if s.hash() != s.last_agent_hash:
        return [Action("call_agent", reason="changed")]
    if since is not None and since >= idle_interval(s, cfg):
        return [Action("call_agent", reason="idle")]
    return []


def evaluate(s: Snapshot, cfg: DirectorConfig, now: datetime) -> list[Action]:
    actions: list[Action] = []
    cap = cap_hit(s)
    hold = cap is not None
    if cap:
        actions.append(Action("hold_new_work", reason=cap))
        if s.open_cap_notice is None:
            actions.append(
                Action(
                    "cap_notice", f"cap:{cap}:{now.isoformat()}", code=cap, text=CAP_OWNER_TEXT[cap]
                )
            )
    elif s.open_cap_notice is not None:
        actions.append(
            Action("cap_cleared", f"cap_cleared:{s.open_cap_notice}", text=BACK_UNDER_CAP_TEXT)
        )
    for t in s.tasks:
        actions.extend(_task_rules(s, t, cfg, hold))
    actions.extend(_card_rules(s))
    actions.extend(_request_rules(s, hold))
    day = summary_due(s, cfg)
    if day:
        actions.append(Action("daily_summary", f"summary:{day}"))
    if not hold:
        actions.extend(_agent_rule(s, cfg, now))
    return actions
