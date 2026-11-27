"""A discrete-time driver: every `step_minutes` the workforce advances, every
`tick_minutes` the director ticks, and scheduled events (owner messages, a platform
restart) fire at their time."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiceo.agents.runner import Workforce
from aiceo.clock import SimClock
from aiceo.director.actions import DirectorContext
from aiceo.director.agent import DirectorAgent
from aiceo.director.locks import InProcessLock
from aiceo.director.tick import TickRecord, tick_once


@dataclass
class Simulation:
    ctx: DirectorContext
    clock: SimClock
    workforce: Workforce
    agent: DirectorAgent
    step_minutes: int = 10
    lock: InProcessLock = field(default_factory=InProcessLock)
    ticks: list[TickRecord] = field(default_factory=list)
    on_step: Callable[[datetime], None] | None = None

    def run_until(
        self, end: datetime, events: list[tuple[datetime, Callable[[], None]]] = ()
    ) -> None:
        pending = sorted(events, key=lambda e: e[0])
        next_tick = self.clock.now()
        while self.clock.now() < end:
            now = self.clock.now()
            while pending and pending[0][0] <= now:
                pending.pop(0)[1]()
            for mission_id in self.ctx.store.mission_ids():
                self.workforce.start_queued(mission_id)
                self.workforce.step(mission_id)
            if now >= next_tick:
                self.ticks.extend(tick_once(self.ctx, self.agent, self.lock, now))
                next_tick = now + timedelta(minutes=self.ctx.cfg.tick_minutes)
            if self.on_step is not None:
                self.on_step(now)
            self.clock.advance(minutes=self.step_minutes)
