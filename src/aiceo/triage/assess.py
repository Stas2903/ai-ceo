"""Triage of an owner request: do it now, do it after a prerequisite, or advise against it.

One model call produces a structured verdict. Everything around that call is deterministic:

* The verdict is validated (pydantic) including cross-field rules; an invalid answer is
  sent back to the model with the validation error, up to `max_attempts` times.
* The reply to the owner is a template filled with short, sanitised fields. Free-form model
  prose never reaches the owner.
* If the model fails entirely, triage falls back to starting the request with an owner
  confirmation as its proof. Triage advises; it is not a safety guard. Money, external
  contact and irreversible actions are guarded later by the critical-action guard, whatever
  triage said.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aiceo.models import PROOF_HUMAN, ProofKind
from aiceo.triage.llm import LLM, LLMError

SYSTEM_PROMPT = files("aiceo.triage").joinpath("prompt.md").read_text(encoding="utf-8")
_FIELD_MAX = 160


class Prerequisite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=80)
    role: str = Field(min_length=2, max_length=40)
    done_kind: ProofKind


class AssessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["do_now", "do_after", "object"]
    title: str = Field(min_length=3, max_length=80)
    role: str = Field(default="generalist", min_length=2, max_length=40)
    done_kind: ProofKind | None = None
    prerequisite: Prerequisite | None = None
    reason: str = Field(default="", max_length=300)
    alternative: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _consistent(self) -> AssessResult:
        if self.verdict in ("do_now", "do_after") and self.done_kind is None:
            raise ValueError(f"{self.verdict} needs a done_kind")
        if self.verdict == "do_after" and self.prerequisite is None:
            raise ValueError("do_after needs a prerequisite")
        if self.verdict == "do_after" and self.prerequisite.title.lower() == self.title.lower():
            raise ValueError("the prerequisite cannot be the request itself")
        if self.verdict in ("object", "do_after") and not self.reason.strip():
            raise ValueError(f"{self.verdict} needs a reason")
        return self


@dataclass(frozen=True)
class TriageContext:
    contacts: int = 0
    notes: tuple[str, ...] = ()
    open_tasks: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f"Contacts in the mission's base: {self.contacts}"]
        if self.open_tasks:
            lines.append("Open tasks: " + "; ".join(self.open_tasks))
        lines += list(self.notes)
        return "\n".join(lines)


@dataclass
class Assessment:
    result: AssessResult | None
    attempts: int
    errors: list[str] = field(default_factory=list)


def user_message(request: str, ctx: TriageContext) -> str:
    # the owner's words are data, fenced so they cannot pose as instructions
    return f"<context>\n{ctx.render()}\n</context>\n<owner_request>\n{request}\n</owner_request>"


def parse_result(text: str) -> AssessResult:
    """The first JSON object in the text (tolerates code fences and stray prose around it)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return AssessResult.model_validate(obj)


def assess(request: str, ctx: TriageContext, llm: LLM, *, max_attempts: int = 3) -> Assessment:
    base = user_message(request, ctx)
    message, errors = base, []
    for attempt in range(1, max_attempts + 1):
        try:
            reply = llm.complete(SYSTEM_PROMPT, message)
        except LLMError as exc:
            errors.append(f"model: {exc}")
            return Assessment(None, attempt, errors)
        try:
            return Assessment(parse_result(reply), attempt, errors)
        except (ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                detail = "; ".join(
                    f"{'.'.join(map(str, e['loc'])) or 'answer'}: {e['msg']}" for e in exc.errors()
                )[:300]
            else:
                detail = str(exc)[:300]
            errors.append(detail)
            message = (
                f"{base}\n<previous_answer_invalid>{detail}</previous_answer_invalid>\n"
                "Answer again with one valid JSON object only."
            )
    return Assessment(None, max_attempts, errors)


def clean_field(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")
    text = " ".join(text.split()).strip(" .")
    return text if len(text) <= _FIELD_MAX else text[: _FIELD_MAX - 1].rstrip() + "…"


def render_reply(result: AssessResult) -> str:
    title = clean_field(result.title)
    if result.verdict == "do_now":
        return f"On it: «{title}». Done when: {PROOF_HUMAN[result.done_kind]}."
    if result.verdict == "do_after":
        pre = clean_field(result.prerequisite.title)
        return (
            f"First «{pre}», then «{title}»: {clean_field(result.reason)}. "
            f"Done when: {PROOF_HUMAN[result.done_kind]}."
        )
    alt = f" Instead: {clean_field(result.alternative)}." if result.alternative.strip() else ""
    return (
        f"I would not start «{title}» yet: {clean_field(result.reason)}.{alt} "
        "Reply 'do it' to start it anyway."
    )


FALLBACK_REPLY = "On it: «{title}». I'll ask you to confirm the result when it is ready."
