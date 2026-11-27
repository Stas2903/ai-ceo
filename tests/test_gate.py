import pytest

from aiceo.gate import gate as gate_mod
from aiceo.gate.gate import close_task, recheck
from aiceo.gate.proofs import Claim, World
from aiceo.guard.approvals import answer_card
from aiceo.models import ProofKind, TaskStatus, Verdict
from tests.conftest import M, started

FILE, URL, CI, SRC, HUMAN = (
    ProofKind.FILE_OK,
    ProofKind.URL_OK,
    ProofKind.CI_GREEN,
    ProofKind.SOURCES_OK,
    ProofKind.HUMAN_CONFIRMED,
)


def close(store, channel, clock, world, kind, claims):
    tid = started(store, clock, kind=kind)
    return tid, close_task(store, channel, world, tid, claims, now=clock.now())


def test_passing_url_closes_done_with_its_proof(store, channel, clock):
    world = World(urls={"https://shop.example.com": 200})
    tid, out = close(store, channel, clock, world, URL, [Claim(URL, "https://shop.example.com")])
    task = store.get_task(tid)
    assert (out.status, task.status, task.proof_id) == (
        TaskStatus.DONE,
        TaskStatus.DONE,
        out.proof_id,
    )
    assert store.get_proof(out.proof_id).verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("# Prices\nMugs: TBD\n", "placeholders"),
        ("Dear [insert name],", "placeholders"),
        ("Total: {{amount}}", "placeholders"),
        ("   \n", "empty_file"),
        (None, "file_missing"),
    ],
)
def test_bad_files_close_not_done_with_the_reason(store, channel, clock, content, code):
    world = World(files={} if content is None else {"p.md": content})
    tid, out = close(store, channel, clock, world, FILE, [Claim(FILE, "p.md")])
    task = store.get_task(tid)
    assert (out.status, task.not_done_code) == (TaskStatus.NOT_DONE, code)


def test_a_link_cannot_stand_in_for_the_file_the_task_asked_for(store, channel, clock):
    world = World(urls={"https://example.com/news": 200})
    tid, out = close(store, channel, clock, world, FILE, [Claim(URL, "https://example.com/news")])
    assert (out.status, out.code) == (TaskStatus.NOT_DONE, "no_proof_claimed")
    assert store.proofs_for(tid) == []


def test_the_first_claim_of_the_expected_kind_decides(store, channel, clock):
    world = World(files={"bad.md": "TODO", "good.md": "complete"})
    _, out = close(
        store, channel, clock, world, FILE, [Claim(FILE, "bad.md"), Claim(FILE, "good.md")]
    )
    assert out.code == "placeholders"


def test_ci_pending_then_green(store, channel, clock, cfg):
    world = World(ci={"c1": "running"})
    tid, out = close(store, channel, clock, world, CI, [Claim(CI, "c1")])
    assert out.status is TaskStatus.VERIFYING
    clock.advance(minutes=30)
    assert recheck(store, world, M, cfg, now=clock.now()) == []
    world.ci["c1"] = "green"
    [(settled_id, outcome)] = recheck(store, world, M, cfg, now=clock.now())
    assert (settled_id, outcome.status) == (tid, TaskStatus.DONE)


def test_ci_pending_too_long_times_out_from_its_own_pending_mark(store, channel, clock, cfg):
    world = World(ci={"c1": "running"})
    tid, _ = close(store, channel, clock, world, CI, [Claim(CI, "c1")])
    clock.advance(hours=1, minutes=59)
    assert recheck(store, world, M, cfg, now=clock.now()) == []
    clock.advance(minutes=1)
    [(_, outcome)] = recheck(store, world, M, cfg, now=clock.now())
    assert (outcome.status, store.get_task(tid).not_done_code) == (
        TaskStatus.NOT_DONE,
        "verify_timeout",
    )


def test_no_pipeline_for_this_change_is_a_failure_not_someone_elses_green(store, channel, clock):
    world = World(ci={"main": "green"})
    _, out = close(store, channel, clock, world, CI, [Claim(CI, "c-other")])
    assert out.code == "no_pipeline"


@pytest.mark.parametrize(
    ("urls", "code"),
    [
        ({"https://a.example": 200, "https://b.example": 200}, "too_few_sources"),
        (
            {"https://a.example": 404, "https://b.example": 500, "https://c.example": 200},
            "dead_links",
        ),
        ({"https://a.example": 403, "https://b.example": 429, "https://c.example": 404}, ""),
    ],
)
def test_sources(store, channel, clock, urls, code):
    world = World(urls=urls)
    _, out = close(store, channel, clock, world, SRC, [Claim(SRC, ",".join(urls))])
    assert out.code == code
    assert out.status is (TaskStatus.DONE if not code else TaskStatus.NOT_DONE)


def test_human_confirmation_waits_without_timeout_then_accepts(store, channel, clock, cfg):
    tid, out = close(store, channel, clock, World(), HUMAN, [Claim(HUMAN, "owner")])
    assert out.status is TaskStatus.VERIFYING
    card = store.decisions(M, "card")[-1]
    assert card.payload["critical"] is True and card.payload["default_index"] is None
    clock.advance(days=10)
    assert recheck(store, World(), M, cfg, now=clock.now()) == []
    answer_card(store, card.id, 0, now=clock.now())
    [(_, settled)] = recheck(store, World(), M, cfg, now=clock.now())
    assert settled.status is TaskStatus.DONE and store.get_task(tid).proof_id is not None


def test_human_rejection_closes_not_done(store, channel, clock, cfg):
    tid, _ = close(store, channel, clock, World(), HUMAN, [Claim(HUMAN, "owner")])
    answer_card(store, store.decisions(M, "card")[-1].id, 1, now=clock.now())
    recheck(store, World(), M, cfg, now=clock.now())
    assert store.get_task(tid).not_done_code == "human_rejected"


def test_a_crashing_checker_is_a_failed_check(store, channel, clock, monkeypatch):
    def boom(world, ref):
        raise RuntimeError("network down")

    monkeypatch.setitem(gate_mod.run_check.__globals__["CHECKERS"], URL, boom)
    _, out = close(store, channel, clock, World(), URL, [Claim(URL, "https://x.example")])
    assert (out.status, out.code) == (TaskStatus.NOT_DONE, "check_error")


def test_owner_lines_follow_the_gate_status(store, channel, clock):
    world = World(files={"p.md": "TBD"})
    _, out = close(store, channel, clock, world, FILE, [Claim(FILE, "p.md")])
    assert out.owner_line("Price list", FILE) == (
        "Not done: «Price list» - the file still has placeholders. The work so far is saved."
    )
