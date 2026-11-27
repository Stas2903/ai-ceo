"""The evidence gate: the only way a task becomes `done`.

An agent saying "done" is a claim, not a result. `close_task` takes the agent's claims,
keeps only the ones of the kind the task was assigned (a link in a summary cannot stand in
for the file the task was supposed to produce), checks the first of them and maps the
verdict:

    pass    -> done (with the proof id; the store refuses `done` without one)
    fail    -> not_done with a reason code the director and the owner can act on
    pending -> verifying; `recheck` polls until pass, fail or timeout

`human_confirmed` opens an owner card (critical: no default, no timeout) and waits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiceo.adapters.console import OwnerChannel
from aiceo.config import DirectorConfig
from aiceo.gate.proofs import Claim, World, run_check
from aiceo.guard.approvals import CardSpec, open_card
from aiceo.models import PROOF_HUMAN, ProofKind, TaskStatus, Verdict
from aiceo.store import Store

REASON_HUMAN = {
    "no_proof_claimed": "no proof of the result was handed in",
    "site_down": "the page does not open",
    "site_unreachable": "the site does not answer",
    "file_missing": "the result file is missing",
    "empty_file": "the result file is empty",
    "placeholders": "the file still has placeholders",
    "ci_red": "the checks of the change are red",
    "no_pipeline": "no checks ran for this change",
    "too_few_sources": "too few sources",
    "dead_links": "most sources do not open",
    "human_rejected": "you did not accept the result",
    "verify_timeout": "the check did not finish in time",
    "check_error": "the check itself failed",
}
CONFIRM_OPTIONS = ("Confirm", "Not accepted")


@dataclass(frozen=True)
class GateOutcome:
    status: TaskStatus
    code: str = ""
    proof_id: int | None = None
    observed: str = ""

    def owner_line(self, title: str, kind: ProofKind) -> str:
        if self.status is TaskStatus.DONE:
            return f"Done: «{title}». Checked: {PROOF_HUMAN[kind]} ({self.observed})."
        if self.status is TaskStatus.VERIFYING:
            return f"Handed in «{title}», checking: {PROOF_HUMAN[kind]}."
        reason = REASON_HUMAN.get(self.code, self.code)
        return f"Not done: «{title}» - {reason}. The work so far is saved."


def close_task(
    store: Store,
    channel: OwnerChannel,
    world: World,
    task_id: int,
    claims: list[Claim] | tuple[Claim, ...],
    *,
    now: datetime,
    result: str = "",
) -> GateOutcome:
    task = store.get_task(task_id)
    matching = [c for c in claims if c.kind is task.done_kind]
    if not matching:
        reason = f"expected proof: {PROOF_HUMAN[task.done_kind]}"
        store.finish(
            task_id,
            TaskStatus.NOT_DONE,
            now=now,
            code="no_proof_claimed",
            reason=reason,
            result=result,
        )
        return GateOutcome(TaskStatus.NOT_DONE, "no_proof_claimed")
    claim = matching[0]

    if claim.kind is ProofKind.HUMAN_CONFIRMED:
        card_id = open_card(
            store,
            channel,
            mission_id=task.mission_id,
            now=now,
            task_id=task_id,
            spec=CardSpec(
                f"Please confirm the result: «{task.title}»", CONFIRM_OPTIONS, True, None, None
            ),
            extra={"proof_task_id": task_id},
        )
        pid = store.upsert_proof(task_id, claim.kind, f"card:{card_id}", Verdict.PENDING, now=now)
        store.finish(task_id, TaskStatus.VERIFYING, now=now, result=result)
        return GateOutcome(TaskStatus.VERIFYING, "awaiting_owner", pid)

    checked = run_check(world, claim)
    pid = store.upsert_proof(
        task_id,
        claim.kind,
        claim.ref,
        checked.verdict,
        now=now,
        reason=checked.code,
        observed=checked.observed,
    )
    return _settle(
        store, task_id, pid, checked.verdict, checked.code, checked.observed, now=now, result=result
    )


def _settle(
    store: Store,
    task_id: int,
    proof_id: int,
    verdict: Verdict,
    code: str,
    observed: str,
    *,
    now: datetime,
    result: str = "",
) -> GateOutcome:
    if verdict is Verdict.PASS:
        store.finish(task_id, TaskStatus.DONE, now=now, proof_id=proof_id, result=result)
        return GateOutcome(TaskStatus.DONE, proof_id=proof_id, observed=observed)
    if verdict is Verdict.PENDING:
        store.finish(task_id, TaskStatus.VERIFYING, now=now, result=result)
        return GateOutcome(TaskStatus.VERIFYING, code, proof_id)
    store.finish(
        task_id,
        TaskStatus.NOT_DONE,
        now=now,
        code=code,
        reason=REASON_HUMAN.get(code, code),
        result=result,
    )
    return GateOutcome(TaskStatus.NOT_DONE, code, proof_id, observed)


def recheck(
    store: Store,
    world: World,
    mission_id: str,
    cfg: DirectorConfig,
    *,
    now: datetime,
) -> list[tuple[int, GateOutcome]]:
    """Advance every `verifying` task of the mission; returns the tasks that settled."""
    settled: list[tuple[int, GateOutcome]] = []
    for task in store.tasks(mission_id, TaskStatus.VERIFYING):
        proofs = [p for p in store.proofs_for(task.id) if p.verdict is Verdict.PENDING]
        if not proofs:
            continue
        proof = proofs[-1]
        if proof.kind is ProofKind.HUMAN_CONFIRMED:
            card = store.decision(int(proof.ref.split(":", 1)[1]))
            if card.status != "answered":
                continue  # a human confirmation never times out
            ok = card.payload.get("answer_index") == 0
            verdict = Verdict.PASS if ok else Verdict.FAIL
            code = "" if ok else "human_rejected"
            pid = store.upsert_proof(
                task.id,
                proof.kind,
                proof.ref,
                verdict,
                now=now,
                reason=code,
                observed="owner confirmed" if ok else "owner rejected",
            )
            settled.append(
                (task.id, _settle(store, task.id, pid, verdict, code, "owner confirmed", now=now))
            )
            continue
        checked = run_check(world, Claim(proof.kind, proof.ref))
        if checked.verdict is Verdict.PENDING:
            waited = (now - (proof.pending_since or proof.checked_at)).total_seconds() / 3600
            if waited < cfg.verify_timeout_hours:
                store.upsert_proof(
                    task.id, proof.kind, proof.ref, Verdict.PENDING, now=now, reason=checked.code
                )
                continue
            checked = type(checked)(Verdict.FAIL, "verify_timeout", checked.observed)
        pid = store.upsert_proof(
            task.id,
            proof.kind,
            proof.ref,
            checked.verdict,
            now=now,
            reason=checked.code,
            observed=checked.observed,
        )
        settled.append(
            (
                task.id,
                _settle(
                    store, task.id, pid, checked.verdict, checked.code, checked.observed, now=now
                ),
            )
        )
    return settled
