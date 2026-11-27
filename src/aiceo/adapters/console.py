"""The owner's chat. A real deployment uses a messenger; here it is a console (or a list in tests).

Everything the system says to the owner goes through `OwnerChannel`, so a test can assert
on the exact transcript and the demo can print it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Protocol, TextIO

from aiceo.clock import Clock


class OwnerChannel(Protocol):
    def send(self, mission_id: str, text: str) -> None: ...

    def send_card(
        self,
        mission_id: str,
        card_id: int,
        question: str,
        options: tuple[str, ...],
        *,
        critical: bool,
    ) -> None: ...

    def log(self, mission_id: str, text: str) -> None:
        """Internal trace (director decisions); never shown to the owner in a real chat."""
        ...


@dataclass
class ConsoleChannel:
    clock: Clock
    out: TextIO | None = None
    show_log: bool = True
    transcript: list[str] = field(default_factory=list)

    def _emit(self, line: str) -> None:
        stamp = self.clock.now().strftime("%a %H:%M")
        line = f"[{stamp}] {line}"
        self.transcript.append(line)
        if self.out is not None:
            print(line, file=self.out)

    def send(self, mission_id: str, text: str) -> None:
        first, *rest = text.splitlines() or [""]
        self._emit(f"to owner: {first}")
        for extra in rest:
            self._emit(f"           {extra}")

    def send_card(
        self,
        mission_id: str,
        card_id: int,
        question: str,
        options: tuple[str, ...],
        *,
        critical: bool,
    ) -> None:
        kind = "needs your decision" if critical else "question"
        buttons = " | ".join(f"[{i + 1}] {o}" for i, o in enumerate(options)) or "[reply]"
        tail = "no default, waits for you" if critical else "default applies if you don't answer"
        self._emit(f"to owner: card #{card_id} ({kind}): {question}  {buttons}  ({tail})")

    def log(self, mission_id: str, text: str) -> None:
        if self.show_log:
            self._emit(f"  director: {text}")
        else:
            self.transcript.append(f"  director: {text}")

    def owner_says(self, text: str) -> None:
        self._emit(f"owner: {text}")


def console(clock: Clock, *, show_log: bool = True) -> ConsoleChannel:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return ConsoleChannel(clock=clock, out=sys.stdout, show_log=show_log)
