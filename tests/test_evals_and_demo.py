import json
import sys
from pathlib import Path

import pytest

from aiceo import demo
from aiceo.adapters.console import ConsoleChannel
from aiceo.triage.llm import LLMError
from evals import run_evals

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "demo_transcript.txt"


def test_criticality_eval_has_no_false_b_and_no_false_a():
    r = run_evals.eval_criticality()
    assert r["n"] >= 80
    assert (r["false_b"], r["false_a"], r["misses"]) == (0, 0, [])


def test_gate_eval_has_no_false_done():
    r = run_evals.eval_gate()
    assert (r["correct"], r["false_done"]) == (r["n"], 0)


def test_triage_contract_eval():
    r = run_evals.eval_triage_contract()
    assert all(row[-1] for row in r["rows"]), r["rows"]
    assert all(row[-1] for row in r["routes"]), r["routes"]
    assert r["single_line_reply"]


class FlakyModel:
    """Answers every request with a valid do_now, except on the listed call numbers."""

    def __init__(self, fail_on):
        self.fail_on, self.calls = fail_on, 0

    def complete(self, system, user):
        self.calls += 1
        if self.fail_on == "all" or self.calls in self.fail_on:
            raise LLMError("AuthenticationError: invalid x-api-key")
        return json.dumps(
            {"verdict": "do_now", "title": "Do it", "role": "analyst", "done_kind": "file_ok"}
        )


def test_live_eval_stops_at_once_when_the_model_cannot_be_reached(monkeypatch):
    model = FlakyModel("all")
    monkeypatch.setattr(run_evals, "llm_from_env", lambda: model)
    with pytest.raises(SystemExit, match="model call failed: AuthenticationError: invalid"):
        run_evals.eval_triage_live()
    assert model.calls == 1  # not 40 silent fallbacks


def test_live_eval_reports_model_errors_apart_from_wrong_answers(monkeypatch):
    monkeypatch.setattr(run_evals, "llm_from_env", lambda: FlakyModel({3}))
    third = run_evals._load("triage_set.yaml")[2]["id"]
    r = run_evals.eval_triage_live()
    assert r["n"] == 40 and r["model_errors"] == [third]
    assert third not in r["misses"]
    assert run_evals.live_line(r).endswith(f"; model errors: 1 ({third})")


def test_live_eval_without_the_sdk_exits_with_the_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setenv("AI_CEO_LLM", "anthropic")
    with pytest.raises(SystemExit, match=r'pip install -e "\.\[anthropic\]"'):
        run_evals.eval_triage_live()


def test_committed_results_are_reproducible():
    assert (ROOT / "evals" / "RESULTS.md").read_text(encoding="utf-8") == run_evals.offline_report()


def _demo_transcript() -> list[str]:
    channel = demo.run(
        channel_factory=lambda clock, show_log: ConsoleChannel(clock=clock, show_log=show_log)
    )
    return channel.transcript


def test_demo_matches_the_golden_transcript():
    assert _demo_transcript() == GOLDEN.read_text(encoding="utf-8").splitlines()


def test_demo_story_beats():
    text = "\n".join(_demo_transcript())
    for beat in [
        "First «Build a list of wholesale leads», then «Email wholesale leads",
        "I would not start «Win the first wholesale order today» yet",
        "Not done: «Draft the wholesale price list» - the file still has placeholders.",
        "Done: «Draft the wholesale price list». Checked:",
        "lost to a platform restart - requeued; this does not use up an attempt",
        "Not progressing: «Research local craft fairs».",
        "Stopped «Write product descriptions for the shop»: daily spend limit reached.",
        "Back under the spend limit - resuming work.",
        "Noted - I'll redo «Which headline should the landing page use?» your way.",
        "Done: «Book a booth at the spring fair».",
    ]:
        assert beat in text, beat
    # the email card asked on day 1 is still open at the end: no default, no timeout
    assert text.count("Needs you (since Mon 09:00): Send the launch email") == 3
