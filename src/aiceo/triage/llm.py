"""The one model interface the showcase needs: system prompt + user message -> text.

* `ScriptedLLM` / `SequenceLLM` - offline, deterministic; used by the demo and the tests.
* `AnthropicLLM` - opt-in (`AI_CEO_LLM=anthropic` + `ANTHROPIC_API_KEY`), official SDK,
  used only to run the triage eval against a real model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class LLMSetupError(LLMError):
    """The real model cannot be used at all (for example, the SDK is not installed)."""


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


def _text(reply: str | dict) -> str:
    return reply if isinstance(reply, str) else json.dumps(reply)


def _owner_part(user: str) -> str:
    """The owner's words only, so context lines cannot trigger a scripted reply."""
    start, end = user.find("<owner_request>"), user.find("</owner_request>")
    return user[start + len("<owner_request>") : end] if 0 <= start < end else user


class ScriptedLLM:
    """Replies by the first needle found in the user message (case-insensitive)."""

    def __init__(
        self, replies: Iterable[tuple[str, str | dict]], default: str | dict | None = None
    ):
        self.replies = [(needle.lower(), reply) for needle, reply in replies]
        self.default = default
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        haystack = _owner_part(user).lower()
        for needle, reply in self.replies:
            if needle in haystack:
                return _text(reply)
        if self.default is None:
            raise LLMError("no scripted reply for this request")
        return _text(self.default)


class SequenceLLM:
    """Returns the given replies in order; an exception instance is raised instead."""

    def __init__(self, replies: Iterable[str | dict | Exception]):
        self.replies = list(replies)
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        if not self.replies:
            raise LLMError("sequence exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _text(reply)


class AnthropicLLM:
    """Claude through the official SDK.

    Classification is short and routine, so effort is `low`. Server-side refusal fallback
    is on (`fallbacks="default"`): a request a safety classifier declines is re-run on a
    fallback model inside the same call; a refusal of the whole chain raises `LLMRefusal`
    and triage falls back to its no-model path.

    Every failure of the call itself (outage, rate limit, bad or missing key) is raised as
    `LLMError`, the one error type triage handles, so a model outage never blocks the owner."""

    def __init__(
        self,
        model: str | None = None,
        *,
        client: Any = None,
        effort: str = "low",
        max_tokens: int = 2048,
    ) -> None:
        if client is None:
            try:
                import anthropic  # optional dependency
            except ImportError:
                raise LLMSetupError(
                    'the anthropic package is not installed: pip install -e ".[anthropic]"'
                ) from None
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.environ.get("AI_CEO_MODEL", DEFAULT_MODEL)
        self.effort = effort
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except Exception as exc:  # any SDK failure means "no answer from the model"
            raise LLMError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        if response.stop_reason == "refusal":
            raise LLMRefusal("the request was declined")
        if response.stop_reason == "max_tokens":
            raise LLMError("the reply was cut off at max_tokens")
        return "".join(b.text for b in response.content if b.type == "text")


def llm_from_env() -> LLM | None:
    """The real model only when explicitly asked for; otherwise None (use a fake)."""
    if os.environ.get("AI_CEO_LLM", "fake").lower() != "anthropic":
        return None
    return AnthropicLLM()
