"""Asking the owner, or deciding without them.

Three outcomes for a question an agent (or the director) wants answered:

* class A -> an owner card with **no default and no timeout**. Nothing happens until a
  human answers. The agent is told not to act and to carry on with other work.
* class B, autonomy on -> an `auto_decision`: the recommended option is taken immediately
  and listed in the morning summary under "Decided without you", where "undo N" reverts it.
* class B, autonomy off -> a soft card: the owner may answer; if they do not, the
  recommended option applies after `soft_question_timeout_hours`.

The no-timeout rule is enforced twice: `CardSpec` refuses to build a critical card with a
default, and `apply_default` re-reads the card and refuses again. Either check alone keeps
the rule; both together survive a bug in one of them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from aiceo.adapters.console import OwnerChannel
from aiceo.config import DirectorConfig
from aiceo.guard.criticality import classify
from aiceo.models import Decision
from aiceo.store import Store

CARD = "card"
AUTO = "auto_decision"

NO_ACT_TEXT = (
    "Do not do this yet: the owner decides. Continue with work that does not depend on the "
    "answer and write 'Needs owner decision: <question>' in your result."
)


@dataclass(frozen=True)
class CardSpec:
    question: str
    options: tuple[str, ...]
    critical: bool
    default_index: int | None
    timeout_hours: float | None

    def __post_init__(self) -> None:
        if self.critical and (self.default_index is not None or self.timeout_hours is not None):
            raise ValueError("a critical card cannot have a default or a timeout")
        if self.default_index is not None and not 0 <= self.default_index < len(self.options):
            raise ValueError("default option out of range")
        if (self.default_index is None) != (self.timeout_hours is None):
            raise ValueError("a default needs a timeout and vice versa")


@dataclass(frozen=True)
class AskResult:
    mode: Literal["card", "soft_card", "auto"]
    card_id: int | None
    choice_index: int | None
    text_for_agent: str
    categories: tuple[str, ...] = ()


def _fingerprint(text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def open_card(
    store: Store,
    channel: OwnerChannel,
    *,
    mission_id: str,
    spec: CardSpec,
    now: datetime,
    task_id: int | None = None,
    extra: dict | None = None,
) -> int:
    """Post a card once. Asking the same question again for the same task returns the
    existing card instead of a second one."""
    key = f"card:{task_id}:{_fingerprint(spec.question)}"
    payload = {
        "question": spec.question,
        "options": list(spec.options),
        "critical": spec.critical,
        "default_index": spec.default_index,
        "timeout_hours": spec.timeout_hours,
        "asked_at": now.isoformat(),
        **(extra or {}),
    }
    card_id = store.add_decision(
        mission_id, CARD, key, now=now, task_id=task_id, status="open", payload=payload
    )
    if card_id is None:
        existing = store.decision_by_key(mission_id, key)
        assert existing is not None
        return existing.id
    channel.send_card(mission_id, card_id, spec.question, spec.options, critical=spec.critical)
    return card_id


def ask(
    store: Store,
    channel: OwnerChannel,
    cfg: DirectorConfig,
    *,
    mission_id: str,
    question: str,
    options: tuple[str, ...],
    recommended: int = 0,
    task_id: int | None = None,
    autonomy: bool = True,
    now: datetime,
) -> AskResult:
    verdict = classify(question, options)
    if verdict.critical:
        spec = CardSpec(question, tuple(options), True, None, None)
        card_id = open_card(
            store,
            channel,
            mission_id=mission_id,
            spec=spec,
            now=now,
            task_id=task_id,
            extra={"categories": list(verdict.categories)},
        )
        return AskResult("card", card_id, None, NO_ACT_TEXT, verdict.categories)
    if not options:
        return AskResult(
            "auto",
            None,
            None,
            "Decide yourself and write in your result what you chose and why.",
        )
    index = recommended if 0 <= recommended < len(options) else 0
    if autonomy:
        key = f"auto:{task_id}:{_fingerprint(question)}"
        store.add_decision(
            mission_id,
            AUTO,
            key,
            now=now,
            task_id=task_id,
            payload={"question": question, "options": list(options), "choice": index},
        )
        return AskResult(
            "auto",
            None,
            index,
            f"Decide yourself: option {index + 1} ({options[index]}). Note why in your result.",
        )
    spec = CardSpec(question, tuple(options), False, index, cfg.soft_question_timeout_hours)
    card_id = open_card(store, channel, mission_id=mission_id, spec=spec, now=now, task_id=task_id)
    return AskResult("soft_card", card_id, None, "Wait for the answer; a default applies later.")


def answer_card(
    store: Store, card_id: int, option_index: int, *, now: datetime, by: str = "owner"
) -> bool:
    card = store.decision(card_id)
    if card.kind != CARD or not 0 <= option_index < len(card.payload["options"]):
        return False
    return store.update_decision(
        card_id,
        status="answered",
        now=now,
        expect_status="open",
        patch={"answer_index": option_index, "answered_by": by},
    )


def default_due(card: Decision, now: datetime) -> bool:
    p = card.payload
    if card.status != "open" or p.get("critical") or p.get("default_index") is None:
        return False
    asked = datetime.fromisoformat(p["asked_at"])
    return (now - asked).total_seconds() >= float(p["timeout_hours"]) * 3600


def apply_default(store: Store, card_id: int, *, now: datetime) -> bool:
    """Close a reversible card with its default. Critical cards are refused here as well,
    whatever the caller decided."""
    card = store.decision(card_id)
    if card.kind != CARD or card.payload.get("critical") or not default_due(card, now):
        return False
    return answer_card(store, card_id, int(card.payload["default_index"]), now=now, by="default")


def undo_auto_decision(
    store: Store, mission_id: str, number: int, owner_text: str, *, now: datetime
) -> Decision | None:
    """ "undo N" from the morning summary: N counts the still-active auto decisions."""
    active = [d for d in store.decisions(mission_id, AUTO) if d.status == "done"]
    if not 1 <= number <= len(active):
        return None
    target = active[number - 1]
    store.update_decision(
        target.id,
        status="overridden",
        now=now,
        expect_status="done",
        patch={"owner_text": owner_text},
    )
    return store.decision(target.id)
