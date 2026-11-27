"""Machine checks behind each proof kind.

In a real deployment the checkers call real systems (HTTP through an SSRF-safe fetcher,
the CI API, the mail log). Here they read a `World`: an in-memory stand-in that the mock
agents write to (publish a page, write a file, push a change), so the same verdict logic
runs offline and deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aiceo.models import ProofKind, Verdict


@dataclass
class World:
    urls: dict[str, int] = field(default_factory=dict)  # url -> HTTP status
    files: dict[str, str] = field(default_factory=dict)  # path -> content
    ci: dict[str, str] = field(default_factory=dict)  # change ref -> green|red|running


@dataclass(frozen=True)
class Claim:
    """What the agent says proves its work: "this page", "this file", "this change"."""

    kind: ProofKind
    ref: str


@dataclass(frozen=True)
class CheckResult:
    verdict: Verdict
    code: str = ""  # machine reason on fail/pending
    observed: str = ""


PLACEHOLDER = re.compile(
    r"\b(?:TODO|TBD|FIXME|lorem ipsum)\b|\[(?:insert|your|add|fill)[^\]]*\]|\{\{[^}]*\}\}|X{3,}",
    re.IGNORECASE,
)
MIN_SOURCES = 3
# a site that refuses robots is alive: these statuses do not make a source dead
_ALIVE = {401, 403, 405, 429}


def _alive(status: int | None) -> bool:
    return status is not None and (200 <= status < 400 or status in _ALIVE)


def check_url(world: World, ref: str) -> CheckResult:
    status = world.urls.get(ref)
    if status is None:
        return CheckResult(Verdict.FAIL, "site_unreachable", "no answer")
    if 200 <= status < 300:
        return CheckResult(Verdict.PASS, observed=f"HTTP {status}")
    return CheckResult(Verdict.FAIL, "site_down", f"HTTP {status}")


def check_file(world: World, ref: str) -> CheckResult:
    content = world.files.get(ref)
    if content is None:
        return CheckResult(Verdict.FAIL, "file_missing")
    if not content.strip():
        return CheckResult(Verdict.FAIL, "empty_file")
    found = PLACEHOLDER.search(content)
    if found:
        return CheckResult(Verdict.FAIL, "placeholders", found.group(0))
    return CheckResult(Verdict.PASS, observed=f"{len(content)} chars, no placeholders")


def check_ci(world: World, ref: str) -> CheckResult:
    state = world.ci.get(ref)
    if state == "green":
        return CheckResult(Verdict.PASS, observed="pipeline green")
    if state == "running":
        return CheckResult(Verdict.PENDING, "ci_running")
    if state == "red":
        return CheckResult(Verdict.FAIL, "ci_red", "pipeline red")
    # no pipeline for this exact change: never fall back to "the latest pipeline of main",
    # which would turn someone else's green run into this task's proof
    return CheckResult(Verdict.FAIL, "no_pipeline")


def check_sources(world: World, ref: str) -> CheckResult:
    urls = [u.strip() for u in ref.split(",") if u.strip()]
    if len(urls) < MIN_SOURCES:
        return CheckResult(Verdict.FAIL, "too_few_sources", f"{len(urls)} sources")
    alive = sum(_alive(world.urls.get(u)) for u in urls)
    if alive * 2 <= len(urls):
        return CheckResult(Verdict.FAIL, "dead_links", f"{alive}/{len(urls)} open")
    return CheckResult(Verdict.PASS, observed=f"{alive}/{len(urls)} open")


CHECKERS = {
    ProofKind.URL_OK: check_url,
    ProofKind.FILE_OK: check_file,
    ProofKind.CI_GREEN: check_ci,
    ProofKind.SOURCES_OK: check_sources,
}


def run_check(world: World, claim: Claim) -> CheckResult:
    checker = CHECKERS.get(claim.kind)
    if checker is None:
        raise ValueError(f"{claim.kind} is not machine-checkable")
    try:
        return checker(world, claim.ref)
    except Exception:  # a broken checker is a failed check, never a pass
        return CheckResult(Verdict.FAIL, "check_error")
