"""Synthetic missions: fictional small businesses, their tasks and how each agent behaves.

Everything here is invented. `generate_missions(seed)` is deterministic; the committed
`fixtures/missions.json` is its output (see `scripts/make_fixtures.py`) and a test checks
the two stay identical.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from aiceo.agents.mock import Ask, Finish, Publish, Scripts, Stall, Step, Work, WriteFile
from aiceo.gate.proofs import Claim, World
from aiceo.models import ProofKind

BUSINESSES = (
    ("Larkfield Ceramics", "hand-thrown stoneware"),
    ("Harbor Bike Repair", "bicycle servicing"),
    ("Juniper Tea House", "loose-leaf tea"),
    ("Quill and Ink Stationery", "notebooks and pens"),
    ("Maple Lane Bakery", "sourdough bread"),
    ("Cobalt Yoga Studio", "yoga classes"),
    ("Fernwood Plant Nursery", "house plants"),
    ("Brightside Tutoring", "maths tutoring"),
    ("Saltmarsh Candle Co", "soy candles"),
    ("Ridgeway Leatherworks", "leather goods"),
)

QUESTIONS_B = (
    (
        "Which headline should the landing page use?",
        ("Handmade in small batches", "Made by hand, made to last"),
    ),
    (
        "Should the price list be grouped by collection or sorted by price?",
        ("By collection", "By price"),
    ),
    ("Light or dark theme for the product pages?", ("Light", "Dark")),
    ("Should the research table include fairs outside the region?", ("Only the region", "All")),
)
QUESTIONS_A = (
    ("Pay $300 for a booth at the spring fair?", ("Pay $300", "Skip the fair")),
    ("Send the launch email to 25 wholesale leads?", ("Send", "Not yet")),
    ("Delete the old product listings from the shop?", ("Delete them", "Keep them")),
    ("Give the freelance designer admin access to the website?", ("Give access", "No")),
)

BEHAVIOURS = (
    "proof_ok",
    "placeholder_then_fixed",
    "claims_without_proof",
    "stall_then_ok",
    "stall_always",
    "overspend_then_ok",
    "asks_reversible",
    "asks_critical",
)

PUBLIC_SOURCES = {
    "https://www.example.org/events/fairs": 200,
    "https://www.example.org/makers/directory": 200,
    "https://www.example.net/markets/calendar": 403,  # alive, refuses robots
    "https://www.example.net/old/listing": 404,
    "https://www.example.com/craft/news": 200,
}
GOOD_SOURCES = ",".join([*list(PUBLIC_SOURCES)[:3], "https://www.example.com/craft/news"])
PUBLIC_SOURCES_URL = "https://www.example.com/craft/news"
DEAD_SOURCES = "https://www.example.net/old/listing,https://www.example.org/gone,https://x.example"


@dataclass(frozen=True)
class Profile:
    name: str
    product: str

    @property
    def slug(self) -> str:
        return "-".join(re.sub(r"\band\b", " ", self.name.lower()).split())

    @property
    def site(self) -> str:
        return f"https://{self.slug}.example.com"


def complete_doc(title: str, profile: Profile) -> str:
    return (
        f"# {title}\n\nPrepared for {profile.name} ({profile.product}).\n\n"
        "| Item | Detail |\n|---|---|\n| Scope | Complete |\n| Status | Ready for review |\n"
    )


def _file_path(title: str) -> str:
    return "files/" + "-".join(title.lower().split())[:48] + ".md"


def compile_behaviour(
    title: str, kind: ProofKind, behaviour: str, profile: Profile, *, question: int = 0
) -> list[tuple[Step, ...]]:
    """Scripts (one per attempt) that make the agent behave as described."""
    path = _file_path(title)
    if kind is ProofKind.URL_OK:
        ok = (
            Work("build the page"),
            Publish(profile.site),
            Finish("page is live", (Claim(kind, profile.site),)),
        )
    elif kind is ProofKind.SOURCES_OK:
        ok = (Work("collect sources"), Finish("research done", (Claim(kind, GOOD_SOURCES),)))
    else:
        ok = (
            Work("prepare the document"),
            WriteFile(path, complete_doc(title, profile)),
            Finish("document ready", (Claim(ProofKind.FILE_OK, path),)),
        )
    if behaviour == "proof_ok":
        return [ok]
    if behaviour == "placeholder_then_fixed":
        bad = (
            Work("first draft"),
            WriteFile(path, f"# {title}\n\nPrices: TBD\n"),
            Finish("done", (Claim(ProofKind.FILE_OK, path),)),
        )
        return [bad, ok]
    if behaviour == "claims_without_proof":
        # says "done" and offers some other evidence instead of the result the task asked for
        other = ProofKind.URL_OK if kind is not ProofKind.URL_OK else ProofKind.FILE_OK
        lazy = (
            Work("looked around"),
            Finish(f"see {PUBLIC_SOURCES_URL}", (Claim(other, PUBLIC_SOURCES_URL),)),
        )
        return [lazy, ok]
    if behaviour == "stall_then_ok":
        return [(Work("start"), Stall()), ok]
    if behaviour == "stall_always":
        return [(Work("start"), Stall())]
    if behaviour == "overspend_then_ok":
        burn = tuple(Work(f"long generation pass {i}", usd=1.8) for i in range(1, 13))
        return [burn + ok, ok]
    if behaviour == "asks_reversible":
        q, opts = QUESTIONS_B[question % len(QUESTIONS_B)]
        return [(Work("outline"), Ask(q, opts, recommended=0), *ok)]
    if behaviour == "asks_critical":
        q, opts = QUESTIONS_A[question % len(QUESTIONS_A)]
        return [(Work("prepare"), Ask(q, opts, recommended=0), *ok)]
    raise ValueError(f"unknown behaviour {behaviour}")


_TASK_TEMPLATES = (
    ("Publish the landing page", "web_developer", ProofKind.URL_OK),
    ("Draft the wholesale price list", "analyst", ProofKind.FILE_OK),
    ("Research local craft fairs", "researcher", ProofKind.SOURCES_OK),
    ("Write product descriptions for the shop", "copywriter", ProofKind.FILE_OK),
    ("Prepare the spring promotion plan", "marketer", ProofKind.FILE_OK),
    ("Build a list of wholesale leads", "researcher", ProofKind.FILE_OK),
    ("Write the FAQ page copy", "copywriter", ProofKind.FILE_OK),
    ("Compare delivery partners", "analyst", ProofKind.FILE_OK),
)


def generate_missions(seed: int = 1, n: int = 10) -> dict:
    """Deterministic synthetic missions: a business, 3-6 tasks, one behaviour per task."""
    rng = random.Random(seed)
    missions = []
    for i in range(n):
        name, product = BUSINESSES[i % len(BUSINESSES)]
        tasks = []
        for title, role, kind in rng.sample(_TASK_TEMPLATES, rng.randint(3, 6)):
            behaviour = rng.choice(BEHAVIOURS)
            if kind is ProofKind.URL_OK and behaviour == "placeholder_then_fixed":
                behaviour = "proof_ok"
            tasks.append(
                {
                    "title": title,
                    "role": role,
                    "done_kind": kind.value,
                    "behaviour": behaviour,
                    "question": rng.randrange(4),
                }
            )
        missions.append(
            {
                "id": f"m{i + 1:02d}",
                "name": name,
                "product": product,
                "daily_cap_usd": rng.choice([8.0, 10.0, 15.0]),
                "owner_answers_critical_after_hours": rng.choice([None, 2, 20, 50]),
                "crash_at_hour": rng.choice([None, 1, 3, 30]),
                "tasks": tasks,
            }
        )
    return {
        "synthetic": True,
        "note": "Fictional businesses generated by scripts/make_fixtures.py. Not real data.",
        "seed": seed,
        "missions": missions,
    }


def scripts_for(mission: dict) -> Scripts:
    profile = Profile(mission["name"], mission["product"])
    scripts = Scripts()
    for t in mission["tasks"]:
        scripts.add(
            t["title"],
            *compile_behaviour(
                t["title"],
                ProofKind(t["done_kind"]),
                t["behaviour"],
                profile,
                question=t.get("question", 0),
            ),
        )
    return scripts


def public_world() -> World:
    return World(urls=dict(PUBLIC_SOURCES))
