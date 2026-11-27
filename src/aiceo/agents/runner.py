"""Runs role agents step by step, the way a real executor drives a ReAct loop.

Between two steps the runner asks one question: should this run stop? Either the director
asked for it (stuck work) or the spend cap fired. Stopping happens only at a step boundary,
the draft is kept, and the task closes `not_done` with a code the director understands.

`crash()` + `recover()` simulate a platform restart: live runs vanish, their rows stay
open, and recovery requeues those tasks as *interrupted*, which does not consume their
retry budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aiceo.adapters.console import OwnerChannel
from aiceo.agents.mock import Ask, Finish, Publish, Scripts, SetCI, Stall, Step, Work, WriteFile
from aiceo.budget.spend import CAP_REASON, SPEND_CAP_CODE, cap_reason, make_stop_check
from aiceo.clock import Clock
from aiceo.config import DirectorConfig
from aiceo.gate.gate import close_task, recheck
from aiceo.gate.proofs import World
from aiceo.guard import approvals
from aiceo.models import TaskStatus
from aiceo.store import Store

DIRECTOR_STOP_REASON = "no progress, stopped by the director"


@dataclass
class _Run:
    task_id: int
    mission_id: str
    attempt: int
    steps: tuple[Step, ...]
    stop_check: Callable[[], str | None]
    pos: int = 0
    waiting_card: int | None = None


class Workforce:
    def __init__(
        self,
        store: Store,
        world: World,
        channel: OwnerChannel,
        cfg: DirectorConfig,
        clock: Clock,
        scripts: Scripts,
        *,
        autonomy: bool = True,
    ) -> None:
        self.store, self.world, self.channel = store, world, channel
        self.cfg, self.clock, self.scripts, self.autonomy = cfg, clock, scripts, autonomy
        self._runs: dict[int, _Run] = {}

    # --- lifecycle ---

    def start_queued(self, mission_id: str) -> list[int]:
        """Claim queued tasks. Nothing new starts while a spend cap is reached."""
        now = self.clock.now()
        if cap_reason(self.store, mission_id, now, self.cfg):
            return []
        started = []
        for task in self.store.tasks(mission_id, TaskStatus.QUEUED):
            if not self.store.claim(task.id, now=now):
                continue
            task = self.store.get_task(task.id)
            self.store.open_run(task.id, task.attempts, now=now)
            self._runs[task.id] = _Run(
                task.id,
                mission_id,
                task.attempts,
                self.scripts.for_attempt(task.title, task.spent_attempts - 1),
                make_stop_check(self.store, mission_id, task.id, self.cfg, self.clock),
            )
            started.append(task.id)
        return started

    def crash(self) -> None:
        """The platform restarts: every in-memory run is lost mid-flight."""
        self._runs.clear()

    def recover(self, mission_id: str) -> list[int]:
        now = self.clock.now()
        recovered = []
        for task_id, attempt in self.store.open_runs():
            if task_id in self._runs:
                continue
            task = self.store.get_task(task_id)
            if task.mission_id != mission_id:
                continue
            self.store.close_run(task_id, attempt, now=now)
            if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.WAITING_OWNER):
                continue
            if task.interruptions + 1 > self.cfg.max_interruptions:
                self.store.finish(
                    task_id,
                    TaskStatus.NOT_DONE,
                    now=now,
                    code="interrupted",
                    reason="interrupted by platform restarts too often",
                )
                continue
            self.store.requeue(task_id, interrupted=True)
            self.channel.log(
                mission_id,
                f"«{task.title}» lost to a platform restart - "
                "requeued; this does not use up an attempt",
            )
            recovered.append(task_id)
        return recovered

    # --- one scheduling step for every live run of the mission ---

    def step(self, mission_id: str) -> None:
        now = self.clock.now()
        for task_id, outcome in recheck(self.store, self.world, mission_id, self.cfg, now=now):
            task = self.store.get_task(task_id)
            self.channel.send(mission_id, outcome.owner_line(task.title, task.done_kind))
        for run in [r for r in self._runs.values() if r.mission_id == mission_id]:
            self._advance(run)

    def _advance(self, run: _Run) -> None:
        now = self.clock.now()
        task = self.store.get_task(run.task_id)
        if task.status is TaskStatus.WAITING_OWNER:
            card = self.store.decision(run.waiting_card)
            if card.status != "answered":
                return  # waiting is not hanging and costs nothing
            choice = card.payload["options"][card.payload["answer_index"]]
            self.store.set_waiting(task.id, False)
            self.store.record_step(task.id, now=now, note=f"Owner answered: {choice}")
            run.waiting_card = None
            return
        if task.status is not TaskStatus.IN_PROGRESS or task.attempts != run.attempt:
            self._close(run)  # closed elsewhere (director, recovery)
            return
        if task.stop_requested:
            self._stop(run, task.stop_code or "stopped", DIRECTOR_STOP_REASON)
            return
        cap = run.stop_check()
        if cap:
            self._stop(run, SPEND_CAP_CODE, CAP_REASON[cap])
            return
        if run.pos >= len(run.steps):
            return  # the script ran out without finishing: behaves like a hang
        self._execute(run, run.steps[run.pos])

    def _execute(self, run: _Run, step: Step) -> None:
        now, store, mid = self.clock.now(), self.store, run.mission_id
        if isinstance(step, Stall):
            return
        run.pos += 1
        if isinstance(step, Work | WriteFile | Publish):
            store.add_spend(mid, step.usd, now=now, task_id=run.task_id)
        if isinstance(step, Work):
            store.record_step(run.task_id, now=now, note=step.note)
        elif isinstance(step, WriteFile):
            self.world.files[step.path] = step.content
            store.record_step(run.task_id, now=now, note=f"wrote {step.path}")
        elif isinstance(step, Publish):
            self.world.urls[step.url] = step.status
            store.record_step(run.task_id, now=now, note=f"published {step.url}")
        elif isinstance(step, SetCI):
            self.world.ci[step.ref] = step.state
            store.record_step(run.task_id, now=now, note=f"pushed {step.ref}")
        elif isinstance(step, Ask):
            store.record_step(run.task_id, now=now, note=f"asked: {step.question}")
            res = approvals.ask(
                store,
                self.channel,
                self.cfg,
                mission_id=mid,
                question=step.question,
                options=step.options,
                recommended=step.recommended,
                task_id=run.task_id,
                autonomy=self.autonomy,
                now=now,
            )
            if res.card_id is not None:
                store.set_waiting(run.task_id, True)
                run.waiting_card = res.card_id
            else:
                self.channel.log(
                    mid,
                    f"decided without the owner: {step.question} -> "
                    f"{step.options[res.choice_index] if step.options else '-'}",
                )
        elif isinstance(step, Finish):
            self._close(run)
            task = store.get_task(run.task_id)
            outcome = close_task(
                store,
                self.channel,
                self.world,
                run.task_id,
                step.claims,
                now=now,
                result=step.summary,
            )
            if outcome.status is not TaskStatus.VERIFYING or outcome.code != "awaiting_owner":
                self.channel.send(mid, outcome.owner_line(task.title, task.done_kind))

    def _stop(self, run: _Run, code: str, reason: str) -> None:
        now = self.clock.now()
        task = self.store.get_task(run.task_id)
        self._close(run)
        self.store.finish(run.task_id, TaskStatus.NOT_DONE, now=now, code=code, reason=reason)
        if code == SPEND_CAP_CODE:
            draft = "Draft saved" if task.draft.strip() else "Nothing saved yet"
            self.channel.send(run.mission_id, f"Stopped «{task.title}»: {reason}. {draft}.")
        else:
            self.channel.log(
                run.mission_id, f"«{task.title}» stopped at a step boundary ({code}); draft kept"
            )

    def _close(self, run: _Run) -> None:
        self.store.close_run(run.task_id, run.attempt, now=self.clock.now())
        self._runs.pop(run.task_id, None)

    def live_task_ids(self) -> set[int]:
        return set(self._runs)
