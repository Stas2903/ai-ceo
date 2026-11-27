"""Spend caps that also stop work already running.

A cap that only blocks *new* tasks is not a cap: agents that are already running keep
spending. So the cap is checked in two places:

* the director's rules hold new work (no restarts, no new tasks, no agent calls) and tell
  the owner once per day;
* every running task gets a `stop_check` that the runner calls between agent steps. When
  the cap is reached, the run stops at the next step boundary, its draft is saved and the
  task closes as `not_done: spend_cap`. Nested helpers inherit the same check.

The check reads the database at most every `spend_check_interval_s` seconds and, once it
has fired, stays fired without further reads.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from aiceo.clock import Clock
from aiceo.config import DirectorConfig
from aiceo.store import Store

DAILY_CAP = "daily_spend_cap"
MISSION_CAP = "mission_spend_cap"
SPEND_CAP_CODE = "spend_cap"

CAP_REASON = {
    DAILY_CAP: "daily spend limit reached",
    MISSION_CAP: "project spend limit reached",
}
# the daily cap frees itself as the 24 h window moves; the mission cap does not, so its
# text must not promise "tomorrow"
CAP_OWNER_TEXT = {
    DAILY_CAP: "Daily spend limit reached - new work resumes when the 24-hour window frees up.",
    MISSION_CAP: "Project spend limit reached - no new work until you raise the limit.",
}


def spent_24h(store: Store, mission_id: str, now: datetime) -> float:
    return store.spent_between(mission_id, now - timedelta(hours=24), now)


def mission_spent(store: Store, mission_id: str, now: datetime) -> tuple[float, float | None]:
    """Spend since the mission cap was set, and the cap (None when there is no cap)."""
    row = store.mission(mission_id)
    cap = row["spend_cap_usd"]
    if not cap:
        return 0.0, None
    since = datetime.fromisoformat(row["cap_set_at"])
    return store.spent_between(mission_id, since, now), float(cap)


def cap_reason(store: Store, mission_id: str, now: datetime, cfg: DirectorConfig) -> str | None:
    """Which cap is exhausted, if any. Both exhausted -> the daily one (its text is the one
    that is true soonest)."""
    if cfg.daily_cap_usd > 0 and spent_24h(store, mission_id, now) >= cfg.daily_cap_usd:
        return DAILY_CAP
    spent, cap = mission_spent(store, mission_id, now)
    if cap is not None and spent >= cap:
        return MISSION_CAP
    return None


def make_stop_check(
    store: Store,
    mission_id: str,
    task_id: int,
    cfg: DirectorConfig,
    clock: Clock,
) -> Callable[[], str | None]:
    """Returns a function for the runner: None to continue, or the cap that stops the run."""
    state: dict = {"last": None, "hit": None}

    def check() -> str | None:
        if state["hit"]:
            return state["hit"]
        now = clock.now()
        last = state["last"]
        if last is not None and (now - last).total_seconds() < cfg.spend_check_interval_s:
            return None
        state["last"] = now
        try:
            reason = cap_reason(store, mission_id, now, cfg)
        except Exception:  # a flaky read must not kill healthy work
            return None
        if reason:
            state["hit"] = reason
        return reason

    return check
