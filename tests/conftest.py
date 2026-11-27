from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aiceo.adapters.console import ConsoleChannel
from aiceo.clock import SimClock
from aiceo.config import DirectorConfig
from aiceo.director.actions import DirectorContext
from aiceo.director.snapshot import build_snapshot
from aiceo.models import ProofKind
from aiceo.store import Store

START = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
M = "m1"


@pytest.fixture
def clock() -> SimClock:
    return SimClock(START)


@pytest.fixture
def cfg() -> DirectorConfig:
    return DirectorConfig()


@pytest.fixture
def store(clock) -> Store:
    s = Store()
    s.create_mission(M, "Test Pottery", now=clock.now())
    return s


@pytest.fixture
def channel(clock) -> ConsoleChannel:
    return ConsoleChannel(clock=clock)


@pytest.fixture
def ctx(store, channel, cfg) -> DirectorContext:
    return DirectorContext(store, channel, cfg)


def snap(store, clock, cfg, mission: str = M):
    return build_snapshot(store, mission, clock.now(), cfg)


def started(store, clock, title="Task", kind=ProofKind.FILE_OK, *, live=True, steps=1):
    """A task in progress with `steps` steps recorded at the current time."""
    tid = store.add_task(M, title, "generalist", kind, now=clock.now())
    assert store.claim(tid, now=clock.now())
    if live:
        store.open_run(tid, store.get_task(tid).attempts, now=clock.now())
    for _ in range(steps):
        store.record_step(tid, now=clock.now(), note="worked")
    return tid


def owner_lines(channel) -> list[str]:
    return [line for line in channel.transcript if "to owner:" in line]
