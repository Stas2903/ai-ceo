"""The morning summary: four short blocks, each line backed by a fact in the store."""

from __future__ import annotations

from datetime import datetime, timedelta

from aiceo.director.snapshot import Snapshot
from aiceo.models import PROOF_HUMAN, TaskStatus
from aiceo.store import Store

MAX_LINES = 20


def daily_summary_text(store: Store, s: Snapshot, now: datetime) -> str:
    since = now - timedelta(hours=24)
    tasks = store.tasks(s.mission_id)
    done = [
        t for t in tasks if t.status is TaskStatus.DONE and t.finished_at and t.finished_at >= since
    ]
    stuck = [t for t in tasks if t.status is TaskStatus.NOT_DONE]
    lines = [f"Morning summary - {s.mission_name}"]
    lines.append(
        "Done: "
        + (", ".join(f"«{t.title}» ({PROOF_HUMAN[t.done_kind]})" for t in done) or "nothing new")
    )
    if stuck:
        lines.append(
            "Not done: "
            + ", ".join(f"«{t.title}» ({t.not_done_reason or t.not_done_code})" for t in stuck)
        )
    needs = [c for c in s.open_cards if c.critical]
    for c in needs:
        lines.append(f"Needs you (since {c.asked_at:%a %H:%M}): {c.question}")
    autos = [d for d in store.decisions(s.mission_id, "auto_decision") if d.status == "done"]
    for n, d in enumerate(autos, 1):
        choice = d.payload["options"][d.payload["choice"]]
        lines.append(
            f"Decided without you {n}: {d.payload['question']} -> «{choice}» "
            f"(reply 'undo {n}: <what you want>' to change)"
        )
    cap = f" of ${s.daily_cap:.2f}" if s.daily_cap else ""
    lines.append(f"Spent in the last 24 h: ${s.spent_24h:.2f}{cap}")
    if len(lines) > MAX_LINES:
        lines = [*lines[: MAX_LINES - 1], f"... and {len(lines) - MAX_LINES + 1} more"]
    return "\n".join(lines)
