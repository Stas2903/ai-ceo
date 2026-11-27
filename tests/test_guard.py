import pytest

from aiceo.director import rules
from aiceo.guard import approvals, criticality
from aiceo.guard.approvals import CardSpec, answer_card, apply_default, ask, undo_auto_decision
from aiceo.guard.criticality import classify
from tests.conftest import M, snap

# --- classifier: anchoring cases (the full labelled set lives in evals/) ---


@pytest.mark.parametrize(
    "text",
    [
        "Pay $300 for the booth?",
        "Delete the customer database?",
        "Email the stockists about the new range?",
        "Give the designer admin access to the shop?",
        "Upload the customer list to an external AI tool?",
    ],
)
def test_critical(text):
    assert classify(text).cls == "A"


@pytest.mark.parametrize(
    "text",
    [
        "Delete the typo in the draft?",
        "What are the key findings?",
        "Draft an email to stockists for review?",
        "Should the page explain how customers can pay?",
        "Can I access the analytics dashboard?",
    ],
)
def test_routine(text):
    assert classify(text).cls == "B"


def test_options_are_part_of_the_question():
    assert classify("Clean up old copies?").cls == "B"
    assert classify("Clean up old copies?", ["Delete all backups", "Keep"]).cls == "A"


def test_model_label_never_changes_the_class():
    assert classify("Pay $300?", model_says_critical=False).cls == "A"
    assert classify("Light or dark theme?", model_says_critical=True).cls == "B"


def test_classifier_error_fails_safe_to_the_owner(monkeypatch):
    def boom(*args):
        raise RuntimeError("bad pattern")

    monkeypatch.setattr(criticality, "_classify", boom)
    verdict = classify("Light or dark theme?")
    assert (verdict.cls, verdict.categories) == ("A", ("classifier_error",))


# --- cards ---


@pytest.mark.parametrize(("default", "timeout"), [(0, 4.0), (0, None), (None, 4.0)])
def test_a_critical_card_cannot_be_built_with_a_default_or_timeout(default, timeout):
    with pytest.raises(ValueError):
        CardSpec("Pay?", ("Yes", "No"), True, default, timeout)


def test_critical_question_opens_one_card_and_tells_the_agent_not_to_act(
    store, channel, cfg, clock
):
    kwargs = dict(
        mission_id=M,
        question="Pay $300 for the booth?",
        options=("Pay", "Skip"),
        task_id=7,
        now=clock.now(),
    )
    first = ask(store, channel, cfg, **kwargs)
    second = ask(store, channel, cfg, **kwargs)
    assert first.mode == "card" and first.card_id == second.card_id
    assert first.text_for_agent == approvals.NO_ACT_TEXT
    assert sum("card #" in line for line in channel.transcript) == 1
    card = store.decision(first.card_id)
    assert (
        card.payload["critical"],
        card.payload["default_index"],
        card.payload["timeout_hours"],
    ) == (True, None, None)


def test_routine_question_is_decided_and_can_be_undone(store, channel, cfg, clock):
    res = ask(
        store,
        channel,
        cfg,
        mission_id=M,
        question="Which headline?",
        options=("Small batches", "Made to last"),
        recommended=1,
        now=clock.now(),
    )
    assert (res.mode, res.choice_index, res.card_id) == ("auto", 1, None)
    undone = undo_auto_decision(store, M, 1, "use the first one", now=clock.now())
    assert undone.status == "overridden"
    assert [o.owner_text for o in snap(store, clock, cfg).overrides] == ["use the first one"]
    assert undo_auto_decision(store, M, 1, "again", now=clock.now()) is None


def test_routine_question_without_autonomy_gets_a_soft_card(store, channel, cfg, clock):
    res = ask(
        store,
        channel,
        cfg,
        mission_id=M,
        question="Which headline?",
        options=("A", "B"),
        autonomy=False,
        now=clock.now(),
    )
    assert res.mode == "soft_card"
    clock.advance(hours=4)
    assert apply_default(store, res.card_id, now=clock.now())
    assert store.decision(res.card_id).payload["answered_by"] == "default"


def test_apply_default_refuses_a_critical_card_even_if_the_payload_is_corrupt(store, cfg, clock):
    """Defence in depth: a buggy writer stored a default on a critical card. Neither the
    rules nor apply_default may use it."""
    cid = store.add_decision(
        M,
        "card",
        "corrupt",
        now=clock.now(),
        status="open",
        payload={
            "question": "Pay $300?",
            "options": ["Pay", "Skip"],
            "critical": True,
            "default_index": 0,
            "timeout_hours": 1.0,
            "asked_at": clock.now().isoformat(),
        },
    )
    clock.advance(days=5)
    assert not [
        a
        for a in rules.evaluate(snap(store, clock, cfg), cfg, clock.now())
        if a.kind == "apply_default"
    ]
    assert apply_default(store, cid, now=clock.now()) is False
    assert store.decision(cid).status == "open"


def test_answer_card_rejects_bad_options_and_second_answers(store, channel, cfg, clock):
    res = ask(
        store,
        channel,
        cfg,
        mission_id=M,
        question="Pay $300?",
        options=("Pay", "Skip"),
        now=clock.now(),
    )
    assert not answer_card(store, res.card_id, 5, now=clock.now())
    assert answer_card(store, res.card_id, 1, now=clock.now())
    assert not answer_card(store, res.card_id, 0, now=clock.now())
    assert store.decision(res.card_id).payload["answer_index"] == 1


def test_default_due_is_false_for_critical_cards_whatever_the_payload(store, clock):
    from aiceo.guard.approvals import default_due

    cid = store.add_decision(
        M,
        "card",
        "corrupt2",
        now=clock.now(),
        status="open",
        payload={
            "question": "Pay?",
            "options": ["Pay", "Skip"],
            "critical": True,
            "default_index": 0,
            "timeout_hours": 1.0,
            "asked_at": clock.now().isoformat(),
        },
    )
    clock.advance(days=5)
    assert default_due(store.decision(cid), clock.now()) is False


def test_apply_default_checks_criticality_itself(store, clock, monkeypatch):
    """Even if the timing helper wrongly said "due", apply_default must still refuse."""
    cid = store.add_decision(
        M,
        "card",
        "corrupt3",
        now=clock.now(),
        status="open",
        payload={
            "question": "Pay?",
            "options": ["Pay", "Skip"],
            "critical": True,
            "default_index": 0,
            "timeout_hours": 1.0,
            "asked_at": clock.now().isoformat(),
        },
    )
    monkeypatch.setattr(approvals, "default_due", lambda card, now: True)
    assert apply_default(store, cid, now=clock.now()) is False
    assert store.decision(cid).status == "open"
