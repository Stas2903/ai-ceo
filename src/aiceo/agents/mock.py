"""Scripted role agents.

A real role agent is a ReAct loop over an LLM with tools. For the director, the gate and the
guards only its observable behaviour matters: it takes steps (or stops taking them), spends
money, writes results, asks questions and finally claims "done" with some evidence. A
script per (task title, attempt) reproduces exactly that, deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiceo.gate.proofs import Claim


@dataclass(frozen=True)
class Work:
    note: str
    usd: float = 0.05


@dataclass(frozen=True)
class WriteFile:
    path: str
    content: str
    usd: float = 0.05


@dataclass(frozen=True)
class Publish:
    url: str
    status: int = 200
    usd: float = 0.05


@dataclass(frozen=True)
class SetCI:
    ref: str
    state: str  # green | red | running


@dataclass(frozen=True)
class Ask:
    question: str
    options: tuple[str, ...] = ()
    recommended: int = 0


@dataclass(frozen=True)
class Finish:
    summary: str
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True)
class Stall:
    """The agent stops producing steps (hung tool call, loop without progress)."""


Step = Work | WriteFile | Publish | SetCI | Ask | Finish | Stall


@dataclass
class Scripts:
    """Scripts per task title; the list index is the attempt that counts against the
    budget (`spent_attempts`), so a start lost to a platform restart replays the same
    script. Past the end of the list the last script repeats."""

    by_title: dict[str, list[tuple[Step, ...]]] = field(default_factory=dict)
    default: tuple[Step, ...] = (Stall(),)

    def add(self, title: str, *attempts: tuple[Step, ...]) -> Scripts:
        self.by_title[title] = list(attempts)
        return self

    def for_attempt(self, title: str, attempt_index: int) -> tuple[Step, ...]:
        attempts = self.by_title.get(title)
        if not attempts:
            return self.default
        return attempts[min(attempt_index, len(attempts) - 1)]
