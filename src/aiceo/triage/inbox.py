"""The owner's messages, routed before any model sees them.

Short control phrases are handled deterministically: "do it" confirms the latest
objection, "cancel" cancels (or asks which), "undo N: ..." reverts an automatic decision.
Only a genuinely new request goes to triage. A bare "do it" must never be mistaken for a
new task, and "do it, and also write a report" must never be mistaken for a bare "do it".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from aiceo.adapters.console import OwnerChannel
from aiceo.guard.approvals import undo_auto_decision
from aiceo.models import OPEN_STATUSES, ProofKind
from aiceo.store import Store
from aiceo.triage.assess import FALLBACK_REPLY, TriageContext, assess, clean_field, render_reply
from aiceo.triage.llm import LLM

_CONFIRM = re.compile(
    r"^(?:(?:ok|okay|yes|yeah|fine|please|just|then|go ahead and|still)\s+)*do it"
    r"(?:\s+(?:anyway|please|now|then))*$"
)
_CANCEL = re.compile(r"^(?:cancel|cancel it|never mind|nevermind|drop it|don't|do not do it)$")
_UNDO = re.compile(r"^undo\s+(\d+)\s*[:,-]?\s*(.*)$")


def normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^\w\s':,-]", " ", text)
    return " ".join(text.split()).strip(" ,:")


@dataclass
class Inbox:
    store: Store
    channel: OwnerChannel
    llm: LLM

    def handle(
        self,
        mission_id: str,
        text: str,
        *,
        now: datetime,
        context: TriageContext,
        reply_to: int | None = None,
    ) -> str:
        """Returns the route taken (for tests and logs)."""
        norm = normalise(text)
        undo = _UNDO.match(norm)
        if undo:
            return self._undo(mission_id, int(undo.group(1)), undo.group(2), now)
        bare = " ".join(re.sub(r"[^\w\s']", " ", norm).split())  # "OK, do it!" -> "ok do it"
        if _CONFIRM.match(bare):
            return self._confirm(mission_id, reply_to, now)
        if _CANCEL.match(bare):
            return self._cancel(mission_id, reply_to, now)
        return self._new_request(mission_id, text, context, now)

    # --- control phrases ---

    def _pending(self, mission_id: str, *statuses: str):
        return [
            d for d in self.store.decisions(mission_id, "owner_request") if d.status in statuses
        ]

    def _target(self, mission_id: str, reply_to: int | None, *statuses: str):
        pending = self._pending(mission_id, *statuses)
        if reply_to is not None:
            return next((d for d in pending if d.id == reply_to), None)
        return pending[-1] if pending else None

    def _confirm(self, mission_id: str, reply_to: int | None, now: datetime) -> str:
        req = self._target(mission_id, reply_to, "asked")
        if req is None:
            self.channel.send(
                mission_id, "There is nothing waiting for a 'do it'. Tell me what you need."
            )
            return "confirm:nothing"
        if not self.store.update_decision(
            req.id, status="launched", now=now, expect_status="asked"
        ):
            return "confirm:race"
        p = req.payload
        kind = ProofKind(p.get("done_kind") or ProofKind.HUMAN_CONFIRMED)
        if self.store.find_task(mission_id, p["title"]) is None:
            self.store.add_task(mission_id, p["title"], p["role"], kind, now=now)
        self.channel.send(mission_id, f"Starting «{p['title']}» as you asked.")
        return "confirm:launched"

    def _cancel(self, mission_id: str, reply_to: int | None, now: datetime) -> str:
        pending = self._pending(mission_id, "asked", "queued")
        if reply_to is None and len(pending) != 1:
            if not pending:
                self.channel.send(mission_id, "There is no pending request to cancel.")
                return "cancel:nothing"
            self.channel.send(mission_id, "Which one? Reply 'cancel' to the request you mean.")
            return "cancel:which"
        req = self._target(mission_id, reply_to, "asked", "queued")
        if req is None or not self.store.update_decision(
            req.id, status="cancelled", now=now, expect_status=req.status
        ):
            return "cancel:race"
        self.channel.send(mission_id, f"Cancelled «{req.payload['title']}».")
        return "cancel:done"

    def _undo(self, mission_id: str, number: int, owner_text: str, now: datetime) -> str:
        undone = undo_auto_decision(self.store, mission_id, number, owner_text, now=now)
        if undone is None:
            self.channel.send(mission_id, f"There is no automatic decision number {number}.")
            return "undo:unknown"
        self.channel.send(mission_id, f"Noted - I'll redo «{undone.payload['question']}» your way.")
        return "undo:done"

    # --- new requests ---

    def _new_request(
        self, mission_id: str, text: str, context: TriageContext, now: datetime
    ) -> str:
        open_titles = tuple(t.title for t in self.store.tasks(mission_id, *OPEN_STATUSES))
        ctx = TriageContext(context.contacts, context.notes, open_titles)
        assessment = assess(text, ctx, self.llm)
        r = assessment.result
        if r is None:
            title = clean_field(text)[:80]
            if self.store.find_task(mission_id, title) is None:
                self.store.add_task(
                    mission_id, title, "generalist", ProofKind.HUMAN_CONFIRMED, now=now
                )
            self.channel.log(
                mission_id,
                f"triage failed ({'; '.join(assessment.errors)}) - started without assessment",
            )
            self.channel.send(mission_id, FALLBACK_REPLY.format(title=title))
            return "fallback"
        key = "request:" + hashlib.sha1(f"{now.isoformat()}|{text}".encode()).hexdigest()[:12]
        payload = {
            "title": r.title,
            "role": r.role,
            "done_kind": r.done_kind.value if r.done_kind else None,
            "request_text": text,
            "verdict": r.verdict,
        }
        if r.verdict == "do_now":
            if self.store.find_task(mission_id, r.title) is None:
                self.store.add_task(mission_id, r.title, r.role, r.done_kind, now=now)
            self.store.add_decision(
                mission_id, "owner_request", key, now=now, status="launched", payload=payload
            )
        elif r.verdict == "do_after":
            pre = self.store.find_task(mission_id, r.prerequisite.title)
            pre_id = (
                pre.id
                if pre
                else self.store.add_task(
                    mission_id,
                    r.prerequisite.title,
                    r.prerequisite.role,
                    r.prerequisite.done_kind,
                    now=now,
                )
            )
            self.store.add_decision(
                mission_id,
                "owner_request",
                key,
                now=now,
                status="queued",
                payload={**payload, "prerequisite_task_id": pre_id},
            )
        else:
            self.store.add_decision(
                mission_id, "owner_request", key, now=now, status="asked", payload=payload
            )
        self.channel.send(mission_id, render_reply(r))
        return r.verdict
