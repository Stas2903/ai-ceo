"""End-to-end demo: three simulated days of a fictional pottery studio.

    python -m aiceo.demo            # full transcript with the director's internal log
    python -m aiceo.demo --quiet    # only what the owner sees

What happens (all offline, deterministic):

* the owner sends four requests; triage answers do_now / do_after / object;
* a landing page is accepted with proof; a price list with placeholders is rejected and
  fixed on the second attempt;
* a research task hangs, is stopped and restarted, survives a platform restart without
  losing an attempt, hangs again and is escalated to the owner;
* a reversible question is decided automatically and later undone by the owner;
* two critical questions (money, emailing leads) wait for the owner with no default and no
  timeout - one is answered two days later, the other is still waiting at the end;
* an agent that burns money is stopped mid-run by the daily cap, held, and resumed once
  the 24-hour window frees up.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from aiceo.adapters.console import ConsoleChannel, console
from aiceo.agents.mock import Scripts
from aiceo.agents.runner import Workforce
from aiceo.clock import SimClock
from aiceo.config import DirectorConfig
from aiceo.director.actions import DirectorContext
from aiceo.director.agent import ScriptedDirector
from aiceo.guard.approvals import answer_card
from aiceo.models import ProofKind
from aiceo.scenarios import Profile, compile_behaviour, public_world
from aiceo.sim import Simulation
from aiceo.store import Store
from aiceo.triage.assess import TriageContext
from aiceo.triage.inbox import Inbox
from aiceo.triage.llm import ScriptedLLM

START = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)  # a Monday
MISSION = "larkfield"
PROFILE = Profile("Larkfield Ceramics", "hand-thrown stoneware")

# what a model would answer for each request; the real-model path is the triage eval
TRIAGE_REPLIES = [
    (
        "price list",
        {
            "verdict": "do_now",
            "title": "Draft the wholesale price list",
            "role": "analyst",
            "done_kind": "file_ok",
        },
    ),
    (
        "wholesale leads",
        {
            "verdict": "do_after",
            "title": "Email wholesale leads about the new glaze collection",
            "role": "marketer",
            "done_kind": "file_ok",
            "prerequisite": {
                "title": "Build a list of wholesale leads",
                "role": "researcher",
                "done_kind": "file_ok",
            },
            "reason": "there are no contacts yet, so the list comes first",
        },
    ),
    (
        "first wholesale order",
        {
            "verdict": "object",
            "title": "Win the first wholesale order today",
            "role": "marketer",
            "done_kind": "human_confirmed",
            "reason": "there are no contacts yet and wholesale buyers rarely order on "
            "first contact",
            "alternative": "build the lead list and send a sample offer this week",
        },
    ),
    (
        "product descriptions",
        {
            "verdict": "do_now",
            "title": "Write product descriptions for the shop",
            "role": "copywriter",
            "done_kind": "file_ok",
        },
    ),
]


def showcase_scripts() -> Scripts:
    s = Scripts()
    for title, kind, behaviour, q in (
        ("Publish the landing page", ProofKind.URL_OK, "asks_reversible", 0),
        ("Draft the wholesale price list", ProofKind.FILE_OK, "placeholder_then_fixed", 0),
        ("Build a list of wholesale leads", ProofKind.FILE_OK, "proof_ok", 0),
        ("Research local craft fairs", ProofKind.SOURCES_OK, "stall_always", 0),
        ("Book a booth at the spring fair", ProofKind.FILE_OK, "asks_critical", 0),
        (
            "Email wholesale leads about the new glaze collection",
            ProofKind.FILE_OK,
            "asks_critical",
            1,
        ),
        ("Write product descriptions for the shop", ProofKind.FILE_OK, "overspend_then_ok", 0),
    ):
        s.add(title, *compile_behaviour(title, kind, behaviour, PROFILE, question=q))
    correction = "Apply the owner's correction: Which headline should the landing page use?"
    s.add(correction, *compile_behaviour(correction, ProofKind.FILE_OK, "proof_ok", PROFILE))
    return s


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return START.replace(day=START.day + day, hour=hour, minute=minute)


def build(channel_factory=None, *, show_log: bool = True):
    clock = SimClock(START)
    channel: ConsoleChannel = (channel_factory or console)(clock, show_log=show_log)
    cfg = DirectorConfig()
    store = Store()
    store.create_mission(MISSION, PROFILE.name, now=clock.now())
    for title, role, kind in (
        ("Publish the landing page", "web_developer", ProofKind.URL_OK),
        ("Research local craft fairs", "researcher", ProofKind.SOURCES_OK),
        ("Book a booth at the spring fair", "marketer", ProofKind.FILE_OK),
    ):
        store.add_task(MISSION, title, role, kind, now=clock.now())
    ctx = DirectorContext(store, channel, cfg)
    workforce = Workforce(store, public_world(), channel, cfg, clock, showcase_scripts())
    sim = Simulation(ctx, clock, workforce, ScriptedDirector())
    inbox = Inbox(store, channel, ScriptedLLM(TRIAGE_REPLIES))
    return sim, inbox


def events(sim: Simulation, inbox: Inbox, channel: ConsoleChannel):
    store, ctx0 = sim.ctx.store, TriageContext(contacts=0)

    def owner(text: str):
        def fire():
            channel.owner_says(text)
            inbox.handle(MISSION, text, now=sim.clock.now(), context=ctx0)

        return fire

    def crash():
        channel.log(MISSION, "*** platform restart (deploy) ***")
        sim.workforce.crash()
        sim.workforce.recover(MISSION)

    def answer(question_start: str, option: int):
        def fire():
            card = next(
                d
                for d in store.decisions(MISSION, "card")
                if d.payload["question"].startswith(question_start)
            )
            channel.owner_says(f"(taps «{card.payload['options'][option]}» on card #{card.id})")
            answer_card(store, card.id, option, now=sim.clock.now())

        return fire

    return [
        (at(0, 8, 10), owner("Draft a wholesale price list for our stockists")),
        (at(0, 8, 20), owner("Email our wholesale leads about the new glaze collection")),
        (at(0, 8, 30), owner("Get our first wholesale order today")),
        (at(0, 9, 20), crash),
        (at(0, 14, 0), owner("Write product descriptions for all 40 items in the shop")),
        (at(1, 9, 30), owner("undo 1: use 'Made by hand, made to last' as the headline")),
        (at(2, 10, 0), answer("Pay $300", 0)),
    ]


def run(*, show_log: bool = True, channel_factory=None) -> ConsoleChannel:
    sim, inbox = build(channel_factory, show_log=show_log)
    channel = sim.ctx.channel
    sim.run_until(at(2, 12, 0), events(sim, inbox, channel))
    return channel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true", help="hide the director's internal log")
    args = parser.parse_args()
    run(show_log=not args.quiet)


if __name__ == "__main__":
    main()
