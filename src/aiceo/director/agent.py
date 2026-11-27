"""The director's judgement layer.

In a real deployment this is a tool-using LLM agent. It runs only when the rules ask for it (the
snapshot changed, or the mission has been idle long enough), and it can act only through
`DirectorTools`. The tools enforce the limits themselves (held work, retry budget, task
state), so a model that ignores its prompt still cannot exceed them: the agent proposes,
the tools decide.

This showcase ships a deterministic `ScriptedDirector` with the same tool surface, so the
demo and the tests are reproducible without a model or a key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from aiceo.config import DirectorConfig
from aiceo.director.actions import DirectorContext
from aiceo.director.rules import RESTARTABLE, hold_new_work
from aiceo.director.snapshot import Snapshot
from aiceo.models import ProofKind, TaskStatus

# gate verdicts the agent may retry with feedback (judgement, so not a rule)
RETRYABLE_GATE_CODES = frozenset(
    {
        "placeholders",
        "empty_file",
        "file_missing",
        "no_proof_claimed",
        "site_down",
        "ci_red",
        "too_few_sources",
        "dead_links",
    }
)


@dataclass
class DirectorTools:
    ctx: DirectorContext
    snapshot: Snapshot
    now: datetime
    calls: list[str] = field(default_factory=list)

    def retask(self, task_id: int, feedback: str) -> str:
        """Send a not-done task back to work with feedback. Refuses when work is held,
        when the retry budget is spent, or when the task is not in a retryable state."""
        if hold_new_work(self.snapshot):
            return self._done("refused: new work is on hold (spend cap)")
        task = self.ctx.store.get_task(task_id)
        if task.status is not TaskStatus.NOT_DONE:
            return self._done(f"refused: «{task.title}» is {task.status}")
        if task.not_done_code in RESTARTABLE:
            return self._done("refused: the rules restart stalled tasks themselves")
        cfg: DirectorConfig = self.ctx.cfg
        if task.not_done_code != "spend_cap" and task.spent_attempts >= cfg.max_attempts:
            return self._done(f"refused: «{task.title}» used its {cfg.max_attempts} attempts")
        key = f"retask:{task_id}:{task.attempts}"
        if (
            self.ctx.store.add_decision(
                self.snapshot.mission_id,
                "retask",
                key,
                now=self.now,
                task_id=task_id,
                payload={"feedback": feedback},
            )
            is None
        ):
            return self._done("refused: already retasked for this attempt")
        self.ctx.store.requeue(task_id, feedback=feedback)
        self.ctx.channel.log(self.snapshot.mission_id, f"retask «{task.title}»: {feedback}")
        return self._done(f"retasked «{task.title}»")

    def add_task(self, title: str, role: str, done_kind: ProofKind, feedback: str = "") -> str:
        if hold_new_work(self.snapshot):
            return self._done("refused: new work is on hold (spend cap)")
        if self.ctx.store.find_task(self.snapshot.mission_id, title) is not None:
            return self._done(f"refused: «{title}» already exists")
        self.ctx.store.add_task(
            self.snapshot.mission_id, title, role, done_kind, now=self.now, feedback=feedback
        )
        self.ctx.channel.log(self.snapshot.mission_id, f"new task «{title}» -> {role}")
        return self._done(f"added «{title}»")

    def mark_override_handled(self, decision_id: int) -> None:
        self.ctx.store.update_decision(decision_id, patch={"override_handled": True})

    def _done(self, text: str) -> str:
        self.calls.append(text)
        return text


class DirectorAgent(Protocol):
    def run(self, snapshot: Snapshot, tools: DirectorTools, reason: str) -> str: ...


class ScriptedDirector:
    """Deterministic stand-in for the LLM agent: the decisions a sensible director makes
    on a changed snapshot, expressed through the same tools."""

    def run(self, snapshot: Snapshot, tools: DirectorTools, reason: str) -> str:
        store = tools.ctx.store
        for view in snapshot.tasks:
            if view.status is not TaskStatus.NOT_DONE:
                continue
            task = store.get_task(view.id)
            if task.not_done_code in RETRYABLE_GATE_CODES:
                tools.retask(
                    task.id,
                    f"The result was not accepted: {task.not_done_reason}. "
                    "Fix exactly that and hand it in again.",
                )
            elif task.not_done_code == "spend_cap":
                tools.retask(
                    task.id,
                    "Stopped by the spend limit. Continue from the draft and "
                    "finish with the smallest complete result.",
                )
        for o in snapshot.overrides:
            tools.add_task(
                f"Apply the owner's correction: {o.question}"[:80],
                "generalist",
                ProofKind.FILE_OK,
                feedback=f"The owner said: {o.owner_text}",
            )
            tools.mark_override_handled(o.id)
        return "; ".join(tools.calls) or f"nothing to do ({reason})"
