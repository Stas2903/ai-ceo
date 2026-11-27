# Design decisions

Short records of the choices behind this code: what was decided, why, and what it costs.
Four of them start from something that went wrong in an earlier version of the design;
each records the failure and what replaced it.

## D1. Keep the LLM out of the control loop

**Decision.** Each director tick builds a snapshot of the mission with plain database reads,
runs pure rules over it, and applies their actions. The LLM agent runs only when the rules
ask for it: the snapshot hash changed, or the mission has been idle long enough (exponential
back-off, 2 h up to 24 h). The hash covers states, not clock-driven numbers, so time passing
does not wake the model. The agent's "seen" hash is taken after it runs, so its own actions
do not wake it again.

**Why.** Stuck work, restarts, retries, defaults and caps are mechanical. Doing them
deterministically makes them testable as tables and keeps model calls for judgement.

**Cost.** The rule table grows with every case it handles. Each rule is a pure function
with its own tests, which keeps that manageable.

## D2. "Done" is a database invariant, not an instruction

**Decision.** A task becomes `done` only through the evidence gate, which needs a passing
proof of the kind the task was assigned. The store refuses `done` without a proof that
passed and belongs to the task, and the schema has `CHECK (status != 'done' OR proof_id IS NOT NULL)`.
Only claims of the assigned kind count: a link in a summary cannot stand in for the file
the task was supposed to produce.

**Why.** An agent saying "done" is a claim. Prompting agents to be honest does not survive
contact with long tasks; a constraint does.

**Credit.** The idea of gating "done" on a passing proof comes from a former colleague's
design; this repo has a minimal version so the demo runs end to end.

## D3. Idempotency through a decision journal

**Decision.** Every director action has a key such as `stop:<task>:<attempt>`. The applier
inserts the decision row first (`UNIQUE(mission_id, key)`), and only then acts; claim and
effect share a transaction where both are database writes.

**Why.** Ticks overlap, crash and get retried. With keys per (task, attempt), "stop this
attempt" happens once, however many ticks see the same stuck task.

## D4. Critical questions: no default and no timeout, ever

**What went wrong.** Early on, questions to the owner had a reply timeout with a default
option, which is fine for "light or dark theme". Testing showed that the same
mechanism would also apply a destructive option, such as "delete the old copies", whenever
nobody answered in time, for example overnight.

**Decision.** Questions are classified as A (only the owner decides: money, contacting
people outside the team, irreversible changes to external or production systems, access,
customer data leaving) or B (the AI decides and reports). A class-A card has no default and
no timeout. It waits for a human for as long as it takes, and the agent is told not to act
and to carry on with other work. The rule is enforced in two independent places
(`CardSpec` and `apply_default`); the mutation check proves each one is tested.

**Cost.** Work that genuinely needs the owner can wait for days. The morning summary lists
every open critical card with its age, so the wait is visible instead of silent.

## D5. Platform restarts do not spend retries

**What went wrong.** Deploys restart the platform. A restart killed running tasks, and each
restart counted as a failed attempt. Tasks ran out of retries without doing anything wrong.

**Decision.** Each task counts `attempts` (every start, which keeps (task, attempt) unique)
and `interruptions` (starts lost to the platform) separately. The retry budget is
`attempts - interruptions`. Interruptions have their own ceiling, so a task that keeps
crashing the platform still stops.

## D6. A classification lexicon, anchored to objects

**Decision.** The A/B class comes from a deterministic lexicon, not from the model. A
model's own "this is critical / not critical" label is ignored, in both directions. Any
error inside the classifier yields A.

**What went wrong.** The first lexicon used broad stems ("price", "send", "delete"). It
classified ordinary internal questions as critical, and since critical means no default
and no timeout, routine work waited indefinitely for the owner.

**Fix.** Stems are anchored to an object ("delete the customer database" yes, "delete the
typo in the draft" no), and a labelled regression set runs in CI. Writing the synthetic set
for this repo found 5 critical questions the lexicon missed and 3 false alarms; all were
fixed by better anchoring rather than per-item exceptions.

**Limit.** A paraphrase the lexicon does not know is classified B. In a real deployment the
guard should be one layer among several (outbound messages and server commands get their
own approval gates). Those layers are not part of this showcase.

## D7. Spend caps stop running work

**What went wrong.** The daily spend cap blocked new tasks, but agents that were already
running could keep spending until they finished.

**Decision.** Every run gets a `stop_check` that the runner calls between agent steps. When
a cap is reached, the run stops at the next step boundary, keeps its draft, and closes as
`not_done: spend_cap`. The check reads the database at most every 15 seconds and stays
fired once it has fired. The rules hold new work (no restarts, no new tasks, no agent call)
and tell the owner once per hold episode, then again when work resumes.

**Cost.** Stopping at step boundaries means the cap can be overshot by up to one step per
running task. The invariant tests assert exactly that bound.

## D8. Triage advises; guards guard

**Decision.** Owner requests get one model call that returns `do_now`, `do_after` (with a
prerequisite the team can produce) or `object` (with a reason and an alternative). The
verdict is validated, including cross-field rules, and invalid answers go back to the model
with the error, up to three times. The reply to the owner is a template filled with short,
sanitised fields. If the model fails entirely, the request starts anyway with "owner
confirms the result" as its proof.

**Why.** Triage that blocks on a model outage turns a helper into a single point of
failure. Money, external contact and irreversible actions are guarded later by D4,
whatever triage said.

## D9. Simulated time and scripted agents

**Decision.** The demo and the tests use an injectable clock and scripted role agents that
reproduce the observable behaviour of real ones: steps, spend, results, questions, hangs and
evidence claims.

**Why.** Three days of operation run in about a second and give the same transcript every
time, so a golden-file test can pin the whole story and anyone can run it without a key.
