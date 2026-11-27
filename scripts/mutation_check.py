"""Break each critical guard on purpose and prove a focused test notices.

    python scripts/mutation_check.py

Every mutant runs in a throwaway copy of the repo (nothing here is modified). The golden
demo transcript is excluded, because it would fail on any change and hide whether a
*focused* test catches the mutant. A surviving mutant means a guard nobody tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = "tests/test_evals_and_demo.py::test_demo_matches_the_golden_transcript"

MUTANTS = [
    (
        "done needs a passing proof (Python)",
        "src/aiceo/store.py",
        "if proof.task_id != task_id or proof.verdict is not Verdict.PASS:",
        "if False:",
    ),
    (
        "done needs a proof (database CHECK)",
        "src/aiceo/store.py",
        "CHECK (status != 'done' OR proof_id IS NOT NULL),",
        "",
    ),
    (
        "decision keys are claimed once",
        "src/aiceo/store.py",
        "return int(cur.lastrowid) if cur.rowcount == 1 else None",
        "return int(cur.lastrowid or 0)",
    ),
    (
        "platform restarts do not spend attempts",
        "src/aiceo/models.py",
        "return max(self.attempts - self.interruptions, 0)",
        "return self.attempts",
    ),
    (
        "a critical card cannot carry a default",
        "src/aiceo/guard/approvals.py",
        "if self.critical and (self.default_index is not None or self.timeout_hours is not None):",
        "if False:",
    ),
    (
        "apply_default refuses critical cards",
        "src/aiceo/guard/approvals.py",
        'if card.kind != CARD or card.payload.get("critical") or not default_due(card, now):',
        "if card.kind != CARD or not default_due(card, now):",
    ),
    (
        "rules never default a critical card",
        "src/aiceo/director/rules.py",
        "if c.critical or c.default_index is None or c.timeout_hours is None:",
        "if c.default_index is None or c.timeout_hours is None:",
    ),
    (
        "a classifier error fails safe to A",
        "src/aiceo/guard/criticality.py",
        'return Classification("A", ("classifier_error",), ())',
        'return Classification("B", ("classifier_error",), ())',
    ),
    (
        "the spend cap stops running work",
        "src/aiceo/agents/runner.py",
        "cap = run.stop_check()",
        "cap = None",
    ),
    (
        "the spend cap holds restarts",
        "src/aiceo/director/rules.py",
        'if not hold and not _taken(s, key):\n                out.append(Action("restart_task"',
        'if not _taken(s, key):\n                out.append(Action("restart_task"',
    ),
    (
        "the gate keeps only claims of the assigned kind",
        "src/aiceo/gate/gate.py",
        "matching = [c for c in claims if c.kind is task.done_kind]",
        "matching = list(claims)",
    ),
    (
        "agent tools refuse while work is held",
        "src/aiceo/director/agent.py",
        "        if hold_new_work(self.snapshot):\n"
        '            return self._done("refused: new work is on hold (spend cap)")\n'
        "        task =",
        "        task =",
    ),
    (
        "waiting for the owner is not stuck",
        "src/aiceo/director/rules.py",
        "if t.status is not TaskStatus.IN_PROGRESS:\n        return None  # waiting",
        "if t.status not in (TaskStatus.IN_PROGRESS, TaskStatus.WAITING_OWNER):\n"
        "        return None  # waiting",
    ),
]


def run_mutant(work: Path, rel: str, old: str, new: str) -> tuple[bool, str]:
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(
        ROOT,
        work,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".git"
        ),
    )
    target = work / rel
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"mutation anchor not found exactly once in {rel}")
    target.write_text(text.replace(old, new), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(work / "src"), str(work)])}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--deselect", GOLDEN],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    failed = next((ln[7:] for ln in r.stdout.splitlines() if ln.startswith("FAILED")), "")
    return r.returncode != 0, failed.split(" - ")[0]


def main() -> int:
    survived = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name, rel, old, new in MUTANTS:
            killed, by = run_mutant(Path(tmp) / "repo", rel, old, new)
            survived += not killed
            print(f"{'killed  ' if killed else 'SURVIVED'}  {name:48}  {by}")
    print(f"\n{len(MUTANTS) - survived}/{len(MUTANTS)} mutants killed")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
