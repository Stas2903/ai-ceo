"""Every tunable of the director in one place: behaviour changes are config, not code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DirectorConfig:
    tick_minutes: int = 20
    # in progress, had steps, but no new step for this long -> stuck
    stuck_minutes: int = 30
    # in progress and not a single step for this long -> stuck before it started
    no_first_step_minutes: int = 45
    # retry budget per task; platform interruptions do not count against it
    max_attempts: int = 2
    # a task interrupted this many times is closed instead of requeued forever
    max_interruptions: int = 3
    # rolling 24 h spend cap per mission (0 disables it)
    daily_cap_usd: float = 10.0
    # the director's LLM agent runs only on a snapshot change, at most this often
    agent_min_interval_minutes: int = 20
    # with nothing changing, the agent still looks in, with exponential back-off
    idle_base_hours: float = 2.0
    idle_max_hours: float = 24.0
    # hour (UTC in this showcase) after which the morning summary is posted
    summary_hour: int = 9
    # reversible questions the owner may answer get a default after this long;
    # critical questions never get one
    soft_question_timeout_hours: float = 4.0
    # a proof stuck in "pending" for this long closes the task as not done
    verify_timeout_hours: float = 2.0
    # running work re-reads the spend total at most this often
    spend_check_interval_s: float = 15.0
