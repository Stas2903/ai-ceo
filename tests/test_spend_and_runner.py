from aiceo.agents.mock import Ask, Finish, Scripts, Stall, Work, WriteFile
from aiceo.agents.runner import Workforce
from aiceo.budget import spend
from aiceo.budget.spend import CAP_OWNER_TEXT, make_stop_check
from aiceo.gate.proofs import Claim, World
from aiceo.guard.approvals import answer_card
from aiceo.models import ProofKind, TaskStatus
from tests.conftest import M, owner_lines

FILE = ProofKind.FILE_OK
DONE = (WriteFile("r.md", "complete"), Finish("ok", (Claim(FILE, "r.md"),)))


def workforce(store, channel, cfg, clock, scripts, world=None):
    return Workforce(store, world or World(), channel, cfg, clock, scripts)


def run(wf, clock, steps):
    for _ in range(steps):
        wf.start_queued(M)
        wf.step(M)
        clock.advance(minutes=10)


# --- stop check ---


def test_stop_check_fires_when_the_cap_is_reached_and_stays_fired(store, cfg, clock):
    check = make_stop_check(store, M, 1, cfg, clock)
    assert check() is None
    store.add_spend(M, 10.0, now=clock.now())
    clock.advance(seconds=15)
    assert check() == spend.DAILY_CAP
    clock.advance(days=2)  # the window has freed up, but a stopped run stays stopped
    assert check() == spend.DAILY_CAP


def test_stop_check_reads_the_database_at_most_every_interval(store, cfg, clock):
    check = make_stop_check(store, M, 1, cfg, clock)
    assert check() is None
    store.add_spend(M, 10.0, now=clock.now())
    clock.advance(seconds=14)
    assert check() is None  # throttled
    clock.advance(seconds=1)
    assert check() == spend.DAILY_CAP


def test_stop_check_survives_a_failing_read(store, cfg, clock, monkeypatch):
    check = make_stop_check(store, M, 1, cfg, clock)
    monkeypatch.setattr(spend, "cap_reason", lambda *a: (_ for _ in ()).throw(OSError("db")))
    assert check() is None


def test_mission_cap_text_promises_nothing_about_tomorrow():
    assert "24-hour" not in CAP_OWNER_TEXT[spend.MISSION_CAP]
    assert "raise the limit" in CAP_OWNER_TEXT[spend.MISSION_CAP]


# --- runner ---


def test_cap_stops_running_work_at_a_step_boundary_and_keeps_the_draft(store, channel, cfg, clock):
    burn = tuple(Work(f"pass {i}", usd=3.0) for i in range(10))
    tid = store.add_task(M, "Burn", "copywriter", FILE, now=clock.now())
    wf = workforce(store, channel, cfg, clock, Scripts().add("Burn", burn + DONE))
    run(wf, clock, 6)
    task = store.get_task(tid)
    assert (task.status, task.not_done_code) == (TaskStatus.NOT_DONE, "spend_cap")
    assert task.steps == 4 and "pass 3" in task.draft and "pass 4" not in task.draft
    assert store.spent_between(M, clock.now().replace(year=2000), clock.now()) == 12.0
    assert owner_lines(channel)[-1].endswith(
        "Stopped «Burn»: daily spend limit reached. Draft saved."
    )


def test_nothing_new_starts_while_the_cap_is_reached(store, channel, cfg, clock):
    store.add_spend(M, 10.0, now=clock.now())
    tid = store.add_task(M, "Later", "r", FILE, now=clock.now())
    wf = workforce(store, channel, cfg, clock, Scripts().add("Later", DONE))
    assert wf.start_queued(M) == []
    assert store.get_task(tid).status is TaskStatus.QUEUED


def test_director_stop_is_honoured_at_the_next_step(store, channel, cfg, clock):
    tid = store.add_task(M, "Hang", "r", FILE, now=clock.now())
    wf = workforce(store, channel, cfg, clock, Scripts().add("Hang", (Work("start"), Stall())))
    run(wf, clock, 2)
    store.request_stop(tid, "stalled")
    run(wf, clock, 1)
    task = store.get_task(tid)
    assert (task.status, task.not_done_code, task.draft) == (
        TaskStatus.NOT_DONE,
        "stalled",
        "start\n",
    )
    assert wf.live_task_ids() == set()


def test_waiting_for_a_critical_answer_then_resuming(store, channel, cfg, clock):
    tid = store.add_task(M, "Booth", "r", FILE, now=clock.now())
    script = (Ask("Pay $300 for the booth?", ("Pay", "Skip")), *DONE)
    wf = workforce(store, channel, cfg, clock, Scripts().add("Booth", script))
    run(wf, clock, 50)  # eight simulated hours
    assert store.get_task(tid).status is TaskStatus.WAITING_OWNER
    card = store.decisions(M, "card")[0]
    answer_card(store, card.id, 0, now=clock.now())
    run(wf, clock, 4)
    task = store.get_task(tid)
    assert task.status is TaskStatus.DONE and "Owner answered: Pay" in task.draft


def test_platform_restart_requeues_without_spending_an_attempt(store, channel, cfg, clock):
    tid = store.add_task(M, "Job", "r", FILE, now=clock.now())
    wf = workforce(store, channel, cfg, clock, Scripts().add("Job", (Work("a"), Stall())))
    run(wf, clock, 2)
    wf.crash()
    assert wf.recover(M) == [tid]
    run(wf, clock, 1)
    task = store.get_task(tid)
    assert (task.attempts, task.interruptions, task.spent_attempts) == (2, 1, 1)


def test_too_many_restarts_close_the_task(store, channel, cfg, clock):
    tid = store.add_task(M, "Job", "r", FILE, now=clock.now())
    wf = workforce(store, channel, cfg, clock, Scripts().add("Job", (Work("a"), Stall())))
    for _ in range(cfg.max_interruptions + 1):
        run(wf, clock, 1)
        wf.crash()
        wf.recover(M)
    task = store.get_task(tid)
    assert (task.status, task.not_done_code, task.interruptions) == (
        TaskStatus.NOT_DONE,
        "interrupted",
        cfg.max_interruptions,
    )


def test_finish_goes_through_the_gate(store, channel, cfg, clock):
    tid = store.add_task(M, "Doc", "r", FILE, now=clock.now())
    bad = (WriteFile("d.md", "TODO"), Finish("done!", (Claim(FILE, "d.md"),)))
    wf = workforce(store, channel, cfg, clock, Scripts().add("Doc", bad))
    run(wf, clock, 3)
    assert store.get_task(tid).not_done_code == "placeholders"
